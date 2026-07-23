bl_info = {
    "name": "Collection(s) to GLB",
    "author": "Daniel Marcin from 3D Content Team (Prompted in Claude AI)",
    "version": (1, 5, 0),
    "blender": (5, 2, 1),
    "location": "View3D > N-Panel > GLB Export",
    "description": "Export collections as GLB with automatic scaling and transforms",
    "category": "Import-Export",
}

import bpy
import bmesh
import os
import math
import re
import subprocess
import zipfile
import tempfile
import shutil
from bpy.props import StringProperty, BoolProperty, IntProperty, FloatProperty, EnumProperty, CollectionProperty, PointerProperty
from bpy.types import Panel, Operator, PropertyGroup, UIList
from mathutils import Vector


GLB_ALPHA_MODE_ITEMS = [
    ('BLEND', "Blend",
     "Smooth semi-transparency and soft edges. Can show sorting artifacts where "
     "transparent surfaces overlap. Use for glass, fades, soft gradients"),
    ('MASK', "Mask",
     "Hard cut-out edges: every pixel fully visible or fully invisible (threshold). "
     "No sorting issues, faster to render. Best for hair/fur cards, foliage, fences"),
]


def material_has_alpha(mat):
    """Same alpha test the bake uses: Alpha input linked, or value below 1.0."""
    if not mat or not mat.use_nodes or not mat.node_tree:
        return False
    principled = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if not principled:
        return False
    alpha_input = principled.inputs.get('Alpha')
    if alpha_input is None:
        return False
    return alpha_input.is_linked or alpha_input.default_value < 1.0


def collection_has_alpha(coll):
    for obj in coll.all_objects:
        if obj.type == 'MESH':
            for slot in obj.material_slots:
                if material_has_alpha(slot.material):
                    return True
    return False


def resolve_alpha_mode(props, collection_name):
    """Per-collection override if listed, otherwise the global default."""
    if collection_name:
        for item in props.alpha_collections:
            if item.collection_ref and item.collection_ref.name == collection_name:
                return item.alpha_mode, item.alpha_threshold, item.double_sided
    return props.alpha_mode, props.alpha_threshold, False


def apply_alpha_mode(mat, mode, threshold, double_sided):
    if mode == 'MASK':
        mat.blend_method = 'CLIP'
        mat.alpha_threshold = threshold
        mat.use_backface_culling = not double_sided
        if hasattr(mat, 'surface_render_method'):
            mat.surface_render_method = 'DITHERED'
        # Blender 4.2+ glTF exporter ignores blend_method and detects MASK from
        # the nodes: insert  alpha > threshold  before the Alpha input.
        if mat.use_nodes and mat.node_tree:
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            principled = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
            if principled:
                alpha_in = principled.inputs.get('Alpha')
                if alpha_in and alpha_in.is_linked:
                    src = alpha_in.links[0].from_socket
                    clip = nodes.new('ShaderNodeMath')
                    clip.operation = 'GREATER_THAN'
                    clip.label = "Alpha Clip"
                    clip.location = (principled.location.x - 200, principled.location.y - 300)
                    clip.inputs[1].default_value = threshold
                    links.remove(alpha_in.links[0])
                    links.new(src, clip.inputs[0])
                    links.new(clip.outputs[0], alpha_in)
    else:
        mat.blend_method = 'BLEND'
        mat.use_backface_culling = not double_sided
        if hasattr(mat, 'surface_render_method'):
            mat.surface_render_method = 'BLENDED'

_UNWRAP_RUNNING = False
_UNWRAP_CANCEL_REQUESTED = False


def build_mof_command(props, exe, in_path, out_path):
    """Single source of truth for MOF parameters (used by export and Unwrap button)."""
    cmd = [exe, in_path, out_path]
    params = [
        ("-RESOLUTION", str(props.bake_resolution)),
        ("-SEPARATE", "TRUE" if props.mof_separate_hard_edges else "FALSE"),
        ("-ASPECT", "1.0"),
        ("-NORMALS", "TRUE" if props.mof_use_normals else "FALSE"),
        ("-UDIMS", "1"),
        ("-OVERLAP", "TRUE" if props.mof_overlap_identical else "FALSE"),
        ("-MIRROR", "TRUE" if props.mof_overlap_mirrored else "FALSE"),
        ("-WORLDSCALE", "TRUE" if props.mof_world_scale else "FALSE"),
        ("-DENSITY", str(props.bake_resolution)),
        ("-CENTER", "0.0", "0.0", "0.0"),
        ("-SUPRESS", "TRUE" if props.mof_suppress_validation else "FALSE"),
        ("-QUAD", "TRUE"),
        ("-WELD", "FALSE"),
        ("-FLAT", "TRUE"),
        ("-CONE", "TRUE"),
        ("-CONERATIO", "0.5"),
        ("-GRIDS", "TRUE"),
        ("-STRIP", "TRUE"),
        ("-PATCH", "TRUE"),
        ("-PLANES", "TRUE"),
        ("-FLATT", "0.9"),
        ("-MERGE", "TRUE"),
        ("-MERGELIMIT", "0.0"),
        ("-PRESMOOTH", "TRUE"),
        ("-SOFTUNFOLD", "TRUE"),
        ("-TUBES", "TRUE"),
        ("-JUNCTIONSDEBUG", "TRUE"),
        ("-EXTRADEBUG", "FALSE"),
        ("-ABF", "TRUE"),
        ("-SMOOTH", "TRUE" if props.mof_smooth else "FALSE"),
        ("-REPAIRSMOOTH", "TRUE"),
        ("-REPAIR", "TRUE"),
        ("-SQUARE", "TRUE"),
        ("-RELAX", "TRUE"),
        ("-RELAX_ITERATIONS", "50"),
        ("-EXPAND", "0.07"),
        ("-CUTDEBUG", "TRUE"),
        ("-STRETCH", "TRUE"),
        ("-MATCH", "TRUE"),
        ("-PACKING", "TRUE"),
        ("-RASTERIZATION", "64"),
        ("-PACKING_ITERATIONS", "3"),
        ("-SCALETOFIT", "0.5"),
        ("-VALIDATE", "FALSE"),
    ]
    for param in params:
        cmd.extend(param)
    return cmd


def _principled_count_for_output(mat):
    """Count Principled BSDF nodes reachable upstream from the material's
    ACTIVE Material Output (Surface input), following links into node groups.
    Returns (count, has_active_output)."""
    if not mat or not mat.use_nodes or not mat.node_tree:
        return 0, False
    out = next((n for n in mat.node_tree.nodes
                if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
    if out is None:
        return 0, False
    counted = set()

    def walk(sock, group_stack, depth):
        if depth > 200 or sock is None:
            return 0
        found = 0
        for link in sock.links:
            node = link.from_node
            if node.as_pointer() in counted:
                continue
            counted.add(node.as_pointer())
            if node.type == 'BSDF_PRINCIPLED':
                found += 1
            elif node.type == 'GROUP' and node.node_tree:
                gout = next((g for g in node.node_tree.nodes
                             if g.type == 'GROUP_OUTPUT' and g.is_active_output), None)
                if gout is not None:
                    try:
                        idx = list(node.outputs).index(link.from_socket)
                    except ValueError:
                        idx = 0
                    if idx < len(gout.inputs):
                        found += walk(gout.inputs[idx], group_stack + [node], depth + 1)
                continue
            elif node.type == 'GROUP_INPUT' and group_stack:
                parent = group_stack[-1]
                for inp in parent.inputs:
                    found += walk(inp, group_stack[:-1], depth + 1)
                continue
            for inp in node.inputs:
                found += walk(inp, group_stack, depth + 1)
        return found

    return walk(out.inputs['Surface'], [], 0), True


def scan_export_materials(context):
    """Pre-export check of every material in the collections that will be
    exported. Returns a list of problem descriptions (empty = all good)."""
    problems = []
    colls = []

    def walk_layer(layer_coll):
        if layer_coll.exclude:
            return
        if layer_coll.collection.name != "Lighting":
            colls.append(layer_coll.collection)
        for child in layer_coll.children:
            walk_layer(child)

    for child in context.view_layer.layer_collection.children:
        walk_layer(child)

    objs_done = set()
    mats_done = set()
    for coll in colls:
        for obj in coll.all_objects:
            if obj.type != 'MESH' or obj.as_pointer() in objs_done:
                continue
            objs_done.add(obj.as_pointer())
            for slot in obj.material_slots:
                mat = slot.material
                if not mat or mat.as_pointer() in mats_done:
                    continue
                mats_done.add(mat.as_pointer())
                count, has_output = _principled_count_for_output(mat)
                if not mat.use_nodes:
                    problems.append(f"'{mat.name}': does not use nodes (no Principled BSDF)")
                elif not has_output:
                    problems.append(f"'{mat.name}': no active Material Output")
                elif count == 0:
                    problems.append(f"'{mat.name}': no Principled BSDF on the active Material Output")
                elif count > 1:
                    problems.append(f"'{mat.name}': {count} Principled BSDFs on the active Material Output")
                elif not any(n.type == 'BSDF_PRINCIPLED' for n in mat.node_tree.nodes):
                    problems.append(f"'{mat.name}': Principled BSDF is inside a node group "
                                    f"(bake reads only top-level nodes)")
    return problems


_LAST_EXPORT_REPORT = None


def _glb_stats(filepath):
    """Parse a GLB file: vertex count, materials, images, max texture size."""
    import struct, json as _json
    with open(filepath, 'rb') as f:
        data = f.read()
    jlen = struct.unpack_from('<II', data, 12)[0]
    gltf = _json.loads(data[20:20 + jlen].decode('utf-8'))
    bin_start = 20 + jlen + 8 if len(data) > 20 + jlen + 8 else None

    acc_ids = set()
    for mesh in gltf.get('meshes', []):
        for prim in mesh.get('primitives', []):
            a = prim.get('attributes', {}).get('POSITION')
            if a is not None:
                acc_ids.add(a)
    accessors = gltf.get('accessors', [])
    verts = sum(accessors[a].get('count', 0) for a in acc_ids if a < len(accessors))

    views = gltf.get('bufferViews', [])
    images = gltf.get('images', [])
    max_res = 0
    for img in images:
        bv = img.get('bufferView')
        if bv is None or bin_start is None or bv >= len(views):
            continue
        off = bin_start + views[bv].get('byteOffset', 0)
        chunk = data[off:off + min(views[bv].get('byteLength', 0), 4096)]
        w = h = 0
        if chunk[:8] == b'\x89PNG\r\n\x1a\n':
            w, h = struct.unpack_from('>II', chunk, 16)
        elif chunk[:2] == b'\xff\xd8':  # JPEG: find SOF marker
            i = 2
            while i + 9 < len(chunk):
                if chunk[i] != 0xFF:
                    i += 1
                    continue
                marker = chunk[i + 1]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack_from('>HH', chunk, i + 5)
                    break
                i += 2 + struct.unpack_from('>H', chunk, i + 2)[0]
        max_res = max(max_res, w, h)
    return {'verts': verts, 'materials': len(gltf.get('materials', [])),
            'textures': len(images), 'max_res': max_res}


def _store_export_report(op, time_str):
    """Collect stats of the finished export into the session report."""
    global _LAST_EXPORT_REPORT
    import datetime
    files, errors, stats = [], [], None
    for fp in getattr(op, '_exported_files', None) or []:
        try:
            size_mb = os.path.getsize(fp) / (1024 * 1024)
            files.append((os.path.basename(fp), f"{size_mb:.1f} MB"))
            s = _glb_stats(fp)
            if stats is None:
                stats = s
            else:
                stats['verts'] += s['verts']
                stats['materials'] += s['materials']
                stats['textures'] += s['textures']
                stats['max_res'] = max(stats['max_res'], s['max_res'])
        except Exception as e:
            errors.append(f"Could not analyze {os.path.basename(fp)}: {e}")
    checks = None
    if stats:
        checks = {'verts_ok': stats['verts'] < 100000,
                  'res_ok': stats['max_res'] <= 2048,
                  'mats_ok': stats['materials'] <= 3}
    _LAST_EXPORT_REPORT = {
        'files': files,
        'folder': bpy.path.abspath(bpy.context.scene.glb_export_props.export_path),
        'time': time_str,
        'collections': len(getattr(op, 'processed_objects', None) or []),
        'stats': stats, 'checks': checks, 'errors': errors,
        'when': datetime.datetime.now().strftime("%H:%M"),
    }
    print("=== EXPORT REPORT ===")
    for n, s in files:
        print(f"  {n}  {s}")
    if stats:
        verdict = "PASS" if all(checks.values()) else "NO PASS"
        print(f"  Verts: {stats['verts']:,} | MaxTex: {stats['max_res']} | "
              f"Textures: {stats['textures']} | Materials: {stats['materials']} | {verdict}")
    print(f"  Time: {time_str}")


class GLB_UL_CustomUVBakeTargets(UIList):
    bl_idname = "GLB_UL_CustomUVBakeTargets"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        if item.object_ref:
            row.prop(item, "object_ref", text="", icon='OBJECT_DATA')
            row.prop(item, "uv_map_name", text="")
        else:
            row.prop(item, "object_ref", text="", icon='OBJECT_DATA')
            row.label(text="(pick an object)")


class GLB_OT_ScanCustomUVTargets(Operator):
    bl_idname = "glb_export.scan_custom_uv_targets"
    bl_label = "Scan Collection"
    bl_description = "Scan view-layer-enabled collections for mesh objects with 2+ UV maps"

    def execute(self, context):
        props = context.scene.glb_export_props
        existing = {t.object_ref.name for t in props.custom_uv_bake_targets if t.object_ref}
        found = 0

        def walk(layer_coll):
            nonlocal found
            # Skip collections unchecked in the view layer
            if layer_coll.exclude:
                return
            coll = layer_coll.collection
            for obj in coll.objects:
                if obj.type == 'MESH' and obj.data and len(obj.data.uv_layers) > 1:
                    if obj.name not in existing:
                        new_item = props.custom_uv_bake_targets.add()
                        new_item.object_ref = obj
                        existing.add(obj.name)
                        found += 1
            for child in layer_coll.children:
                walk(child)

        for layer_coll in context.view_layer.layer_collection.children:
            walk(layer_coll)

        self.report({'INFO'}, f"Found {found} new object(s) with multiple UV maps")
        return {'FINISHED'}


class GLB_OT_AddCustomUVTarget(Operator):
    bl_idname = "glb_export.add_custom_uv_target"
    bl_label = "Add Target"
    bl_description = "Add a new empty entry to the list"

    def execute(self, context):
        props = context.scene.glb_export_props
        props.custom_uv_bake_targets.add()
        props.custom_uv_bake_index = len(props.custom_uv_bake_targets) - 1
        return {'FINISHED'}


class GLB_OT_RemoveCustomUVTarget(Operator):
    bl_idname = "glb_export.remove_custom_uv_target"
    bl_label = "Remove Target"
    bl_description = "Remove the selected entry from the list"

    @classmethod
    def poll(cls, context):
        props = context.scene.glb_export_props
        return len(props.custom_uv_bake_targets) > 0

    def execute(self, context):
        props = context.scene.glb_export_props
        idx = props.custom_uv_bake_index
        if 0 <= idx < len(props.custom_uv_bake_targets):
            props.custom_uv_bake_targets.remove(idx)
            props.custom_uv_bake_index = max(0, idx - 1)
        return {'FINISHED'}


class GLB_OT_ScanAlphaCollections(Operator):
    bl_idname = "glb_export.scan_alpha_collections"
    bl_label = "Scan Collections for Alpha"
    bl_description = "Find enabled (checkboxed) collections whose materials use alpha"

    def execute(self, context):
        props = context.scene.glb_export_props
        found = []

        def walk(layer_coll):
            if layer_coll.exclude:
                return
            coll = layer_coll.collection
            if coll.name != "Lighting" and collection_has_alpha(coll):
                found.append(coll)
            for child in layer_coll.children:
                walk(child)

        for layer_coll in context.view_layer.layer_collection.children:
            walk(layer_coll)

        old = {item.collection_ref: (item.alpha_mode, item.alpha_threshold, item.double_sided)
               for item in props.alpha_collections if item.collection_ref}
        props.alpha_collections.clear()
        for coll in found:
            item = props.alpha_collections.add()
            item.collection_ref = coll
            if coll in old:
                item.alpha_mode, item.alpha_threshold, item.double_sided = old[coll]

        self.report({'INFO'}, f"Found {len(found)} collection(s) with alpha")
        return {'FINISHED'}

class GLB_OT_UnwrapSelected(Operator):
    bl_idname = "glb_export.unwrap_selected"
    bl_label = "Unwrap"
    bl_description = ("Unwrap selected objects with the method and settings above. "
                      "In Edit Mode only the selected faces are unwrapped. "
                      "MOF runs in the background - press ESC to cancel")
    bl_options = {'REGISTER', 'UNDO'}

    _timer = None
    _queue = None
    _job = None
    _current = None
    _in_edit = False
    _target_names = None
    _done_count = 0
    _start_time = 0.0

    @classmethod
    def poll(cls, context):
        if _UNWRAP_RUNNING:
            return False
        props = context.scene.glb_export_props
        if props.uv_unwrap_method == 'NONE':
            return False
        if context.mode == 'EDIT_MESH':
            return True
        return context.mode == 'OBJECT' and any(
            o.type == 'MESH' for o in context.selected_objects)

    def _pack_uvs(self, props, average=True):
        """Normalize + pack the currently selected UVs (must be in Edit Mode)."""
        sync_prev = bpy.context.scene.tool_settings.use_uv_select_sync
        bpy.context.scene.tool_settings.use_uv_select_sync = False
        try:
            bpy.ops.uv.select_all(action='SELECT')
            if average:
                bpy.ops.uv.average_islands_scale()
            bpy.ops.uv.pack_islands(
                margin=props.pack_margin,
                rotate=props.pack_rotate,
                shape_method=props.pack_shape_method,
                scale=props.pack_scale,
                rotate_method=props.pack_rotation_method,
                margin_method=props.pack_margin_method,
                pin=props.pack_lock_pinned,
                pin_method=props.pack_lock_method,
                merge_overlap=props.pack_merge_overlapping,
                udim_source=props.pack_udim_target,
            )
        except Exception as e:
            self.report({'WARNING'}, f"UV pack failed: {e}")
        finally:
            bpy.context.scene.tool_settings.use_uv_select_sync = sync_prev

    def _make_prepped_dup(self, context, obj, face_indices):
        """Duplicate obj (selected faces only if given) and preprocess exactly
        like the export: apply transforms, weld doubles, scale to fit 1m."""
        dup = obj.copy()
        dup.data = obj.data.copy()
        context.collection.objects.link(dup)

        if face_indices is not None:
            keep = set(face_indices)
            bm = bmesh.new()
            bm.from_mesh(dup.data)
            bm.faces.ensure_lookup_table()
            del_faces = [f for f in bm.faces if f.index not in keep]
            bmesh.ops.delete(bm, geom=del_faces, context='FACES')
            bm.to_mesh(dup.data)
            bm.free()

        bpy.ops.object.select_all(action='DESELECT')
        dup.select_set(True)
        context.view_layer.objects.active = dup
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
        bm = bmesh.new()
        bm.from_mesh(dup.data)
        bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=0.00001)
        bm.to_mesh(dup.data)
        bm.free()
        dup.data.update()

        bbox = [dup.matrix_world @ Vector(c) for c in dup.bound_box]
        dims = [max(c[i] for c in bbox) - min(c[i] for c in bbox) for i in range(3)]
        max_dim = max(dims)
        if max_dim > 0:
            dup.scale *= (1.0 / max_dim)
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
            center = [(max(c[i] for c in bbox) + min(c[i] for c in bbox)) * 0.5 / max_dim
                      for i in range(3)]
            dup.location[0] -= center[0]
            dup.location[1] -= center[1]
            dup.location[2] -= center[2]
        return dup

    def _mof_start(self, context, props, dup):
        """Prepare dup and launch MOF in the background. Returns a job dict or None."""
        addon_dir = os.path.dirname(os.path.realpath(__file__))
        mof_zip_path = os.path.join(addon_dir, "resources", "MinistryOfFlat_Release.zip")
        if not os.path.exists(mof_zip_path):
            self.report({'ERROR'}, "MinistryOfFlat_Release.zip not found in resources folder")
            return None
        try:
            extract_path = tempfile.mkdtemp(prefix="glb_mof_")
            with zipfile.ZipFile(mof_zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_path)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to extract MOF: {e}")
            return None
        exe = None
        for root, dirs, files in os.walk(extract_path):
            for file in files:
                if file.lower() == "unwrapconsole3.exe":
                    exe = os.path.join(root, file)
                    break
            if exe:
                break
        if not exe:
            self.report({'ERROR'}, "MOF executable not found in zip")
            shutil.rmtree(extract_path, ignore_errors=True)
            return None

        bpy.ops.object.select_all(action='DESELECT')
        dup.select_set(True)
        context.view_layer.objects.active = dup

        if props.mof_triangulate:
            triang_mod = dup.modifiers.new(name="Triangulate", type='TRIANGULATE')
            triang_mod.min_vertices = 5
            triang_mod.keep_custom_normals = True
            bpy.ops.object.modifier_apply(modifier="Triangulate")

        if props.mof_separate_hard_edges:
            bpy.ops.object.mode_set(mode='EDIT')
            bm = bmesh.from_edit_mesh(dup.data)
            for edge in bm.edges:
                if not edge.smooth:
                    edge.seam = True
            bmesh.update_edit_mesh(dup.data)
            bpy.ops.object.mode_set(mode='OBJECT')

        temp_dir = bpy.app.tempdir
        name_safe = dup.name.replace(" ", "_")
        in_path = os.path.join(temp_dir, f"{name_safe}.obj")
        out_path = os.path.join(temp_dir, f"{name_safe}_unwrapped.obj")
        try:
            bpy.ops.wm.obj_export(
                filepath=in_path,
                export_selected_objects=True,
                export_materials=False,
                forward_axis='Y',
                up_axis='Z'
            )
        except Exception as e:
            self.report({'ERROR'}, f"Export failed: {e}")
            shutil.rmtree(extract_path, ignore_errors=True)
            return None

        cmd = build_mof_command(props, exe, in_path, out_path)
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except Exception as e:
            self.report({'ERROR'}, f"Error starting MOF: {e}")
            shutil.rmtree(extract_path, ignore_errors=True)
            return None
        return {"proc": proc, "extract_path": extract_path,
                "in_path": in_path, "out_path": out_path}

    def _mof_finish(self, context, dup, job):
        """Import the MOF result and transfer UVs onto the duplicate."""
        if job["proc"].returncode != 0 and not os.path.exists(job["out_path"]):
            self.report({'ERROR'}, f"MOF failed with code: {job['proc'].returncode}")
            return False
        try:
            bpy.ops.wm.obj_import(filepath=job["out_path"], forward_axis='Y', up_axis='Z')
        except Exception as e:
            self.report({'ERROR'}, f"Import failed: {e}")
            return False
        imported_obj = context.active_object
        if not (imported_obj and imported_obj.type == 'MESH'):
            return False
        if not dup.data.uv_layers:
            dup.data.uv_layers.new()
        if imported_obj.data.uv_layers and dup.data.uv_layers.active:
            imported_obj.data.uv_layers[0].name = dup.data.uv_layers.active.name
        context.view_layer.objects.active = dup
        dt_mod = dup.modifiers.new(name="DataTransfer", type='DATA_TRANSFER')
        dt_mod.object = imported_obj
        dt_mod.use_loop_data = True
        dt_mod.data_types_loops = {'UV'}
        dt_mod.loop_mapping = 'TOPOLOGY'
        bpy.ops.object.modifier_apply(modifier=dt_mod.name)
        bpy.data.objects.remove(imported_obj, do_unlink=True)
        return True

    def _cleanup_job(self, job, kill=False):
        if not job:
            return
        try:
            if kill and job["proc"].poll() is None:
                job["proc"].kill()
        except Exception:
            pass
        for fp in (job["in_path"], job["out_path"]):
            try:
                if os.path.exists(fp):
                    os.remove(fp)
            except Exception:
                pass
        shutil.rmtree(job["extract_path"], ignore_errors=True)

    def _remove_dup(self):
        dup = bpy.data.objects.get(self._current["dup_name"]) if self._current else None
        if dup:
            mesh = dup.data
            bpy.data.objects.remove(dup, do_unlink=True)
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)

    def _copy_uvs_back(self, obj, dup, face_list):
        src_layer = dup.data.uv_layers.active
        if src_layer is None or len(dup.data.polygons) != len(face_list):
            self.report({'WARNING'}, f"{obj.name}: topology changed, UVs not transferred")
            return False
        if not obj.data.uv_layers:
            obj.data.uv_layers.new()
        src = src_layer.data
        dst = obj.data.uv_layers.active.data
        for pi, dp in zip(face_list, dup.data.polygons):
            if pi >= len(obj.data.polygons):
                self.report({'WARNING'}, f"{obj.name}: topology changed, UVs not transferred")
                return False
            po = obj.data.polygons[pi]
            if po.loop_total == dp.loop_total:
                for k in range(po.loop_total):
                    dst[po.loop_start + k].uv = src[dp.loop_start + k].uv
        return True

    def _start_next(self, context, props):
        while self._queue:
            entry = self._queue.pop(0)
            obj = bpy.data.objects.get(entry["name"])
            if obj is None:
                continue
            dup = self._make_prepped_dup(context, obj, entry["faces"])
            job = self._mof_start(context, props, dup)
            if job is None:
                mesh = dup.data
                bpy.data.objects.remove(dup, do_unlink=True)
                if mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
                continue
            self._current = {"obj_name": entry["name"],
                             "face_list": entry["faces"] if entry["faces"] is not None
                             else list(range(len(dup.data.polygons))),
                             "dup_name": dup.name}
            self._job = job
            dup.hide_set(True)
            return True
        return False

    def _set_status(self, context):
        import time
        secs = int(time.time() - self._start_time)
        name = self._current["obj_name"] if self._current else ""
        context.workspace.status_text_set(
            f"MOF unwrapping '{name}'... {secs}s  -  press ESC to cancel")

    def _finish(self, context, cancelled):
        global _UNWRAP_RUNNING
        wm = context.window_manager
        if self._timer:
            wm.event_timer_remove(self._timer)
            self._timer = None
        context.workspace.status_text_set(None)
        _UNWRAP_RUNNING = False

        props = context.scene.glb_export_props
        bpy.ops.object.select_all(action='DESELECT')
        first = None
        for name in self._target_names:
            o = bpy.data.objects.get(name)
            if o:
                o.select_set(True)
                if first is None:
                    first = o
        if first:
            context.view_layer.objects.active = first
        if self._in_edit and first:
            bpy.ops.object.mode_set(mode='EDIT')

        if not cancelled and self._done_count:
            if self._in_edit:
                self._pack_uvs(props)
            elif first:
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')
                self._pack_uvs(props)
                bpy.ops.object.mode_set(mode='OBJECT')

        for window in wm.windows:
            for area in window.screen.areas:
                area.tag_redraw()

        if cancelled:
            self.report({'INFO'}, "Unwrap cancelled - nothing changed on remaining objects")
        else:
            self.report({'INFO'}, f"MOF unwrapped {self._done_count} object(s)")

    def modal(self, context, event):
        global _UNWRAP_CANCEL_REQUESTED
        if event.type == 'ESC' or _UNWRAP_CANCEL_REQUESTED:
            _UNWRAP_CANCEL_REQUESTED = False
            self._cleanup_job(self._job, kill=True)
            self._job = None
            self._remove_dup()
            self._finish(context, cancelled=True)
            return {'CANCELLED'}

        if event.type == 'TIMER' and self._job:
            if self._job["proc"].poll() is None:
                self._set_status(context)
                return {'PASS_THROUGH'}

            dup = bpy.data.objects.get(self._current["dup_name"])
            obj = bpy.data.objects.get(self._current["obj_name"])
            ok = False
            if dup and obj:
                dup.hide_set(False)
                ok = self._mof_finish(context, dup, self._job)
                if ok:
                    ok = self._copy_uvs_back(obj, dup, self._current["face_list"])
            self._cleanup_job(self._job)
            self._job = None
            self._remove_dup()
            self._current = None
            if ok:
                self._done_count += 1

            props = context.scene.glb_export_props
            if self._start_next(context, props):
                self._set_status(context)
                return {'PASS_THROUGH'}
            self._finish(context, cancelled=False)
            return {'FINISHED'}

        return {'PASS_THROUGH'}

    def execute(self, context):
        global _UNWRAP_RUNNING, _UNWRAP_CANCEL_REQUESTED
        _UNWRAP_CANCEL_REQUESTED = False
        import time
        props = context.scene.glb_export_props
        in_edit = (context.mode == 'EDIT_MESH')
        if in_edit:
            targets = [o for o in context.objects_in_mode if o.type == 'MESH']
        else:
            targets = [o for o in context.selected_objects if o.type == 'MESH']
        if not targets:
            self.report({'WARNING'}, "No mesh object selected")
            return {'CANCELLED'}

        # ---------- SMART UV PROJECT (instant, unchanged) ----------
        if props.uv_unwrap_method == 'SMART':
            smart_kwargs = {
                'angle_limit': math.radians(props.uv_angle_limit),
                'island_margin': props.uv_island_margin,
                'area_weight': props.uv_area_weight,
                'correct_aspect': props.uv_correct_aspect,
                'scale_to_bounds': props.uv_scale_to_bounds,
                'margin_method': props.uv_margin_method,
                'rotate_method': props.uv_rotation_method,
            }
            try:
                if in_edit:
                    bpy.ops.uv.smart_project(**smart_kwargs)
                    if props.enable_uv_pack:
                        self._pack_uvs(props, average=False)
                else:
                    bpy.ops.object.mode_set(mode='EDIT')
                    bpy.ops.mesh.select_all(action='SELECT')
                    bpy.ops.uv.smart_project(**smart_kwargs)
                    if props.enable_uv_pack:
                        self._pack_uvs(props, average=False)
                    bpy.ops.object.mode_set(mode='OBJECT')
            except Exception as e:
                if not in_edit and context.mode != 'OBJECT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                self.report({'ERROR'}, f"Smart UV Project failed: {e}")
                return {'CANCELLED'}
            self.report({'INFO'}, "Smart UV unwrap done")
            return {'FINISHED'}

        # ---------- MOF: background, modal ----------
        self._in_edit = in_edit
        self._target_names = [o.name for o in targets]
        self._queue = [{"name": o.name, "faces": None} for o in targets]
        self._done_count = 0

        if in_edit:
            bpy.ops.object.mode_set(mode='OBJECT')
            for entry in self._queue:
                o = bpy.data.objects.get(entry["name"])
                entry["faces"] = [p.index for p in o.data.polygons if p.select] if o else []
            self._queue = [e for e in self._queue if e["faces"]]
            if not self._queue:
                bpy.ops.object.mode_set(mode='EDIT')
                self.report({'WARNING'}, "No faces selected")
                return {'CANCELLED'}

        if not self._start_next(context, props):
            if in_edit:
                bpy.ops.object.mode_set(mode='EDIT')
            return {'CANCELLED'}

        self._start_time = time.time()
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.25, window=context.window)
        wm.modal_handler_add(self)
        _UNWRAP_RUNNING = True
        self._set_status(context)
        return {'RUNNING_MODAL'}


class GLB_OT_UnwrapCancel(Operator):
    bl_idname = "glb_export.unwrap_cancel"
    bl_label = "Cancel Unwrapping"
    bl_description = "Stop the running MOF unwrap and restore everything"

    @classmethod
    def poll(cls, context):
        return _UNWRAP_RUNNING

    def execute(self, context):
        global _UNWRAP_CANCEL_REQUESTED
        _UNWRAP_CANCEL_REQUESTED = True
        return {'FINISHED'}


class GLB_OT_HalveDouble(Operator):
    bl_idname = "glb_export.halve_double"
    bl_label = "Step Value"
    bl_description = "Step to the previous / next value"
    bl_options = {'INTERNAL'}

    prop_name: StringProperty()
    double: BoolProperty()

    _caps = {
        "bake_resolution": (256, 8192),
        "bake_samples": (2, 1024),
        "bake_margin": (1, 64),
        "ao_samples": (2, 1024),
    }
    _float_step = {"ao_distance": 0.1}

    def execute(self, context):
        props = context.scene.glb_export_props
        v = getattr(props, self.prop_name)
        if self.prop_name in self._float_step:
            st = self._float_step[self.prop_name]
            setattr(props, self.prop_name, round(v + st if self.double else v - st, 3))
            return {'FINISHED'}
        lo, hi = self._caps.get(self.prop_name, (1, 1 << 20))
        ladder, s = [], lo
        while s <= hi:
            ladder.append(s)
            s *= 2
        if self.double:
            nv = next((x for x in ladder if x > v), hi)
        else:
            nv = next((x for x in reversed(ladder) if x < v), lo)
        setattr(props, self.prop_name, nv)
        return {'FINISHED'}


class GLB_OT_ShowExportReport(Operator):
    bl_idname = "glb_export.show_report"
    bl_label = "Export Report"
    bl_description = "Show stats of the last export in this session"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        if _LAST_EXPORT_REPORT is None:
            self.report({'INFO'}, "No export in this session yet")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=420)

    def execute(self, context):
        return {'FINISHED'}

    def draw(self, context):
        r = _LAST_EXPORT_REPORT
        if not r:
            return
        layout = self.layout
        stats, checks = r['stats'], r['checks']
        if checks:
            row = layout.row()
            all_ok = all(checks.values())
            row.alert = not all_ok
            row.label(text="PASS" if all_ok else "NO PASS",
                      icon='CHECKMARK' if all_ok else 'ERROR')
        for name, size in r['files']:
            layout.label(text=f"{name}  -  {size}", icon='FILE')
        if not r['files']:
            layout.label(text="No file exported (Export was disabled)", icon='INFO')
        layout.label(text=f"Folder: {r['folder']}")
        layout.label(text=f"Time: {r['time']}    Collections: {r['collections']}    at {r['when']}")
        if stats:
            box = layout.box()
            def line(txt, ok):
                row = box.row()
                row.alert = not ok
                row.label(text=txt, icon='CHECKMARK' if ok else 'X')
            line(f"Vertices: {stats['verts']:,}", checks['verts_ok'])
            line(f"Max texture: {stats['max_res']}", checks['res_ok'])
            line(f"Textures: {stats['textures']}", True)
            line(f"Materials: {stats['materials']}", checks['mats_ok'])
        for e in r['errors']:
            layout.label(text=e, icon='ERROR')


class GLB_UL_AOExceptions(UIList):
    bl_idname = "GLB_UL_AOExceptions"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "object_ref", text="", icon='OBJECT_DATA')
        toggles = row.row(align=True)
        toggles.prop(item, "no_cast", text="No Cast", toggle=True)
        toggles.prop(item, "no_receive", text="No Receive", toggle=True)


class GLB_OT_AddAOException(Operator):
    bl_idname = "glb_export.add_ao_exception"
    bl_label = "Add Exception"
    bl_description = "Add a new empty entry to the list"

    def execute(self, context):
        props = context.scene.glb_export_props
        props.ao_exception_objects.add()
        props.ao_exception_index = len(props.ao_exception_objects) - 1
        ao_preview_refresh(context)
        return {'FINISHED'}


class GLB_OT_RemoveAOException(Operator):
    bl_idname = "glb_export.remove_ao_exception"
    bl_label = "Remove Exception"
    bl_description = "Remove the selected entry from the list"

    @classmethod
    def poll(cls, context):
        return len(context.scene.glb_export_props.ao_exception_objects) > 0

    def execute(self, context):
        props = context.scene.glb_export_props
        idx = props.ao_exception_index
        if 0 <= idx < len(props.ao_exception_objects):
            props.ao_exception_objects.remove(idx)
            props.ao_exception_index = max(0, idx - 1)
        ao_preview_refresh(context)
        return {'FINISHED'}


class GLB_OT_AddSelectedAOExceptions(Operator):
    bl_idname = "glb_export.add_selected_ao_exceptions"
    bl_label = "Add Selected Objects"
    bl_description = "Add all selected viewport objects to the AO exception list"

    def execute(self, context):
        props = context.scene.glb_export_props
        existing = {e.object_ref.name for e in props.ao_exception_objects if e.object_ref}
        added = 0
        for obj in context.selected_objects:
            if obj.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'} and obj.name not in existing:
                item = props.ao_exception_objects.add()
                item.object_ref = obj
                existing.add(obj.name)
                added += 1
        self.report({'INFO'}, f"Added {added} object(s) to AO exceptions")
        ao_preview_refresh(context)
        return {'FINISHED'}

def delayed_cleanup(cleanup_data):
    """Cleanup function that runs after a delay to avoid preview job crashes"""
    
    def do_cleanup():
        # Clean up processed objects
        for obj in cleanup_data.get('processed_objects', []):
            try:
                if obj and obj.name in bpy.data.objects:
                    bpy.data.objects.remove(obj, do_unlink=True)
            except:
                pass
        
        # Clean up temporary collections
        for temp_col_data in cleanup_data.get('temp_collections', []):
            try:
                temp_col = temp_col_data['collection']
                for obj in list(temp_col.objects):
                    try:
                        bpy.data.objects.remove(obj, do_unlink=True)
                    except:
                        pass
                bpy.data.collections.remove(temp_col)
            except:
                pass
        
        # Clean up temporary materials
        for mat in list(bpy.data.materials):
            if mat and "_temp" in mat.name:
                try:
                    bpy.data.materials.remove(mat)
                except:
                    pass
        
        # Clean up baked materials
        for mat in cleanup_data.get('baked_materials', []):
            try:
                if mat and mat.name in bpy.data.materials:
                    bpy.data.materials.remove(mat)
            except:
                pass
        
        # Clean up created images
        for img in cleanup_data.get('created_images', []):
            try:
                if img and img.name in bpy.data.images:
                    bpy.data.images.remove(img)
            except:
                pass
        
        # Clean up glTF Material Output node group
        if "glTF Material Output" in bpy.data.node_groups:
            try:
                bpy.data.node_groups.remove(bpy.data.node_groups["glTF Material Output"])
            except:
                pass
        
        return None  # Don't repeat the timer
    
    return do_cleanup

def update_uv_pack(self, context):
    # Prevent unchecking when MOF is selected
    if self.uv_unwrap_method == 'MOF' and not self.enable_uv_pack:
        self.enable_uv_pack = True

def update_uv_method(self, context):
    # Force enable packing when MOF is selected
    if self.uv_unwrap_method == 'MOF':
        self.enable_uv_pack = True
        self.show_packing_settings = True
    # Auto-disable packing when no unwrap method (user can re-enable)
    elif self.uv_unwrap_method == 'NONE':
        self.enable_uv_pack = False

# === PROPERTY GROUPS ===

def get_uv_maps_for_object(self, context):
    """Dynamic enum - returns UV maps of the referenced object."""
    items = []
    if self.object_ref and self.object_ref.type == 'MESH' and self.object_ref.data:
        for i, uv_layer in enumerate(self.object_ref.data.uv_layers):
            items.append((uv_layer.name, uv_layer.name, f"Bake to {uv_layer.name}", i))
    if not items:
        items.append(('NONE', "(no UV maps)", "Object has no UV maps", 0))
    return items


def poll_mesh_with_multiple_uvs(self, obj):
    if obj.type != 'MESH' or not obj.data:
        return False
    if len(obj.data.uv_layers) <= 1:
        return False
    view_layer = bpy.context.view_layer
    if view_layer.objects.get(obj.name) is None:
        return False
    return True


class GLBBakeUVTarget(PropertyGroup):
    object_ref: PointerProperty(
        name="Object",
        type=bpy.types.Object,
        description="Object to bake to a specific UV map",
        poll=poll_mesh_with_multiple_uvs,
    )
    uv_map_name: EnumProperty(
        name="Target UV Map",
        description="UV map this object's material will be baked into",
        items=get_uv_maps_for_object,
    )


def poll_ao_exception_object(self, obj):
    return obj.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}


def _ao_flag_update(self, context):
    # Live-refresh the AO preview when an exception flag is toggled
    if globals().get('_AO_PREVIEW'):
        ao_preview_refresh(context)


class GLBAOExceptionItem(PropertyGroup):
    object_ref: PointerProperty(
        name="Object",
        type=bpy.types.Object,
        description="Object affected by the AO exception flags",
        poll=poll_ao_exception_object,
        update=_ao_flag_update,
    )
    no_cast: BoolProperty(
        name="No Cast",
        description="This object will not darken other objects in the AO bake",
        default=True,
        update=_ao_flag_update
    )
    no_receive: BoolProperty(
        name="No Receive",
        description="This object's surface stays fully white in the AO map (receives no ambient occlusion)",
        default=False,
        update=_ao_flag_update
    )


_AO_PREVIEW = None                     # None = off, else session restore data
_AO_PREVIEW_PREFIX = "GLB_AO_Preview"  # reserved material name prefix
_AO_PREVIEW_SAMPLES = 32               # calibrated on real content vs the bake


def _ao_preview_mat(name, no_receive, no_cast, distance):
    """One of the 4 white preview materials, rebuilt fresh each start."""
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    out.location = (250, 0)
    if no_receive:
        rgb = nt.nodes.new('ShaderNodeRGB')
        rgb.outputs[0].default_value = (1, 1, 1, 1)
        nt.links.new(rgb.outputs[0], out.inputs['Surface'])
    else:
        ao = nt.nodes.new('ShaderNodeAmbientOcclusion')
        ao.samples = _AO_PREVIEW_SAMPLES
        ao.inputs['Distance'].default_value = distance
        nt.links.new(ao.outputs['Color'], out.inputs['Surface'])
    if no_cast:
        mat.blend_method = 'BLEND'
        if hasattr(mat, 'surface_render_method'):
            mat.surface_render_method = 'BLENDED'
    else:
        mat.blend_method = 'OPAQUE'
        if hasattr(mat, 'surface_render_method'):
            mat.surface_render_method = 'DITHERED'
    return mat


def _find_single_principled(mat):
    """The bake supports exactly one top-level Principled - same rule here."""
    if not mat or not mat.use_nodes or not mat.node_tree:
        return None
    ps = [n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED']
    return ps[0] if len(ps) == 1 else None


def _update_ao_preview_distance(self, context):
    """Live: AO Distance slider writes into every preview material's AO node."""
    if _AO_PREVIEW:
        for mat in bpy.data.materials:
            if mat.name.startswith(_AO_PREVIEW_PREFIX) and mat.use_nodes:
                for n in mat.node_tree.nodes:
                    if n.type == 'AMBIENT_OCCLUSION':
                        n.inputs['Distance'].default_value = self.ao_distance


def ao_preview_start(context, mode, warnings_out):
    """Swap materials for preview ones. mode = 'WHITE' or 'TEXTURED'."""
    global _AO_PREVIEW
    props = context.scene.glb_export_props
    d = props.ao_distance

    # sweep unused leftovers from a crashed previous session (prefix only)
    for m in list(bpy.data.materials):
        if m.name.startswith(_AO_PREVIEW_PREFIX) and m.users == 0:
            bpy.data.materials.remove(m)

    m_norm = _ao_preview_mat(_AO_PREVIEW_PREFIX, False, False, d)
    m_nocast = _ao_preview_mat(_AO_PREVIEW_PREFIX + "_NoCast", False, True, d)
    m_white = _ao_preview_mat(_AO_PREVIEW_PREFIX + "_White", True, False, d)
    m_white_nc = _ao_preview_mat(_AO_PREVIEW_PREFIX + "_White_NoCast", True, True, d)
    created = {m_norm.name, m_nocast.name, m_white.name, m_white_nc.name}

    flags = {}
    if props.ao_use_exceptions:
        for e in props.ao_exception_objects:
            if e.object_ref and (e.no_cast or e.no_receive):
                flags[e.object_ref.name] = (e.no_cast, e.no_receive)

    tx_cache = {}

    def textured_variant(orig, nc, nr):
        # duplicate of the real material; AO node injected inline before
        # Base Color (previous input rewired into the AO node's Color)
        key = (orig.name, nc, nr)
        if key in tx_cache:
            return tx_cache[key]
        dup = orig.copy()
        dup.name = f"{_AO_PREVIEW_PREFIX}_TX_{orig.name}"
        created.add(dup.name)
        if not nr:
            pr = _find_single_principled(dup)
            nt = dup.node_tree
            ao = nt.nodes.new('ShaderNodeAmbientOcclusion')
            ao.samples = _AO_PREVIEW_SAMPLES
            ao.inputs['Distance'].default_value = d
            ao.location = (pr.location.x - 240, pr.location.y + 60)
            bc = pr.inputs['Base Color']
            if bc.is_linked:
                link = bc.links[0]
                src = link.from_socket
                nt.links.remove(link)
                nt.links.new(src, ao.inputs['Color'])
            else:
                c = bc.default_value
                ao.inputs['Color'].default_value = (c[0], c[1], c[2], 1.0)
            nt.links.new(ao.outputs['Color'], bc)
        if nc:
            dup.blend_method = 'BLEND'
            if hasattr(dup, 'surface_render_method'):
                dup.surface_render_method = 'BLENDED'
        tx_cache[key] = dup
        return dup

    st = {"mode": mode, "objects": [], "created": created,
          "fast_gi": context.scene.eevee.use_fast_gi, "shading": []}
    fallback = []
    for obj in context.view_layer.objects:
        if obj.type != 'MESH' or not obj.visible_get():
            continue
        nc, nr = flags.get(obj.name, (False, False))
        white = (m_white_nc if (nc and nr) else m_white if nr
                 else m_nocast if nc else m_norm)
        orig = [s.material.name if s.material else None for s in obj.material_slots]
        added = False
        if not obj.material_slots:
            obj.data.materials.append(white)
            added = True
        elif mode == 'WHITE':
            for slot in obj.material_slots:
                slot.material = white
        else:  # TEXTURED
            fell = False
            for slot in obj.material_slots:
                src = slot.material
                if src is None or (not nr and _find_single_principled(src) is None):
                    slot.material = white
                    fell = fell or (src is not None)
                else:
                    slot.material = textured_variant(src, nc, nr)
            if fell:
                fallback.append(obj.name)
        st["objects"].append((obj.name, orig, added))

    context.scene.eevee.use_fast_gi = True
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            sh = area.spaces.active.shading
            if sh.type != 'MATERIAL':
                st["shading"].append((area.as_pointer(), sh.type))
                sh.type = 'MATERIAL'
    _AO_PREVIEW = st
    if warnings_out is not None and fallback:
        warnings_out.append("No single Principled BSDF, shown as white AO: "
                            + ", ".join(sorted(set(fallback))[:8]))


def ao_preview_stop(context):
    """Restore every slot, setting and shading; delete only session materials."""
    global _AO_PREVIEW
    st = _AO_PREVIEW
    _AO_PREVIEW = None
    if not st:
        return
    for obj_name, mats, added in st["objects"]:
        obj = bpy.data.objects.get(obj_name)
        if obj is None:
            continue
        try:
            if added:
                obj.data.materials.clear()
            else:
                for i, mname in enumerate(mats):
                    if i < len(obj.material_slots):
                        obj.material_slots[i].material = (
                            bpy.data.materials.get(mname) if mname else None)
        except Exception:
            pass
    try:
        context.scene.eevee.use_fast_gi = st["fast_gi"]
    except Exception:
        pass
    for area_ptr, prev_type in st["shading"]:
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.as_pointer() == area_ptr and area.type == 'VIEW_3D':
                    area.spaces.active.shading.type = prev_type
    for mname in list(st.get("created", [])):
        mat = bpy.data.materials.get(mname)
        if mat and mat.users == 0:
            bpy.data.materials.remove(mat)


def ao_preview_refresh(context):
    """Re-apply the running preview (used by live exception toggles)."""
    st = _AO_PREVIEW
    if not st:
        return
    mode = st["mode"]
    ao_preview_stop(context)
    ao_preview_start(context, mode, None)


from bpy.app.handlers import persistent


@persistent
def _ao_preview_autostop(*args):
    # Safety: stop before save / on undo, redo, render, file load
    global _AO_PREVIEW
    try:
        ao_preview_stop(bpy.context)
    except Exception:
        _AO_PREVIEW = None


def _ao_preview_toggle(context, op, mode):
    if _AO_PREVIEW:
        same = _AO_PREVIEW.get("mode") == mode
        ao_preview_stop(context)
        if same:
            for w in context.window_manager.windows:
                for a in w.screen.areas:
                    a.tag_redraw()
            return {'FINISHED'}
    warns = []
    ao_preview_start(context, mode, warns)
    for w in warns:
        op.report({'WARNING'}, w)
    for w in context.window_manager.windows:
        for a in w.screen.areas:
            a.tag_redraw()
    return {'FINISHED'}


class GLB_OT_AOPreviewToggle(Operator):
    bl_idname = "glb_export.ao_preview"
    bl_label = "Preview AO"
    bl_description = ("Live white AO preview in Material Preview, honoring AO "
                      "Distance and the exception flags. Click again to restore")
    bl_options = {'INTERNAL'}

    def execute(self, context):
        return _ao_preview_toggle(context, self, 'WHITE')


class GLB_OT_AOPreviewTexturedToggle(Operator):
    bl_idname = "glb_export.ao_preview_textured"
    bl_label = "Preview AO Textured"
    bl_description = ("Live AO preview on top of the original textures "
                      "(AO injected before Base Color on duplicates). "
                      "Click again to restore")
    bl_options = {'INTERNAL'}

    def execute(self, context):
        return _ao_preview_toggle(context, self, 'TEXTURED')


class GLBAlphaCollectionItem(PropertyGroup):
    collection_ref: PointerProperty(type=bpy.types.Collection, name="Collection")
    alpha_mode: EnumProperty(name="Alpha Mode", items=GLB_ALPHA_MODE_ITEMS, default='BLEND')
    alpha_threshold: FloatProperty(
        name="Threshold", default=0.5, min=0.0, max=1.0,
        description="Mask cutoff: pixels with alpha above this are visible, below are invisible")
    double_sided: BoolProperty(
        name="Double Sided", default=False,
        description="Render both sides of faces (needed for fur/foliage cards)")


class GLBExportProperties(PropertyGroup):
    
    # UI expand/collapse properties
    show_uv: BoolProperty(default=True)
    show_baking: BoolProperty(default=True)
    show_export: BoolProperty(default=True)
    
    import_folder_path: StringProperty(
        name="Import Folder",
        description="Folder containing blend files to import",
        default="",
        subtype='DIR_PATH'
    )
    
    # UV Unwrap Method Selection
    uv_unwrap_method: EnumProperty(
        name="UV Unwrap Method",
        description="Choose UV unwrapping method",
        items=[
            ('NONE', "None", "Skip UV unwrapping"),
            ('SMART', "Smart UV Project", "Use Blender's Smart UV Project"),
            ('MOF', "MOF UV Unwrap", "Use Ministry of Flat unwrapper"),
        ],
        default='MOF',
        update=update_uv_method
    )
    
    # MOF Settings
    mof_separate_hard_edges: BoolProperty(
        name="Separate Hard Edges",
        default=True,
        description="Split edges that are both marked as seam and set as hard"
    )
    
    mof_separate_marked_edges: BoolProperty(
        name="Separate Marked Edges",
        default=True,
        description="Split the mesh along all marked edges"
    )
    
    mof_overlap_identical: BoolProperty(
        name="Overlap Identical Parts",
        default=False,
        description="Allow identical mesh parts to overlap in UV space"
    )
    
    mof_overlap_mirrored: BoolProperty(
        name="Overlap Mirrored Parts",
        default=False,
        description="Allow mirrored parts to overlap in UV space"
    )
    
    mof_world_scale: BoolProperty(
        name="World Scale UV",
        default=True,
        description="Apply the world scale to UV coordinates"
    )
    
    mof_use_normals: BoolProperty(
        name="Use Normals",
        default=False,
        description="Enable the use of vertex normals during UV calculation"
    )
    
    mof_suppress_validation: BoolProperty(
        name="Suppress Validation",
        default=False,
        description="Disable validation checks in the external tool"
    )
    
    mof_smooth: BoolProperty(
        name="Smooth",
        default=False,
        description="Disable for Hard Surface models if you see stretching"
    )
    
    mof_keep_original: BoolProperty(
        name="Keep Original Mesh",
        default=False,
        description="Duplicate the original mesh before processing"
    )
    
    mof_triangulate: BoolProperty(
        name="Triangulate",
        default=False,
        description="Triangulate the mesh before export"
    )
    
    # Smart UV settings
    uv_angle_limit: FloatProperty(
        name="Angle Limit",
        description="Maximum angle between faces to treat as continuous",
        default=66.0,
        min=0.0,
        max=90.0,
        precision=1
    )
    
    uv_margin_method: EnumProperty(
        name="Margin Method",
        description="Method to use for margin between islands",
        items=[
            ('SCALED', 'Scaled', 'Margin scaled by island size'),
            ('ADD', 'Add', 'Fixed margin size'),
            ('FRACTION', 'Fraction', 'Margin as fraction of UV space')
        ],
        default='ADD'
    )
    
    uv_rotation_method: EnumProperty(
        name="Rotation Method",
        description="Rotation method for islands",
        items=[
            ('AXIS_ALIGNED', 'Axis-aligned', 'Rotate islands to the nearest axis'),
            ('AXIS_ALIGNED_X', 'Axis-aligned (Horizontal)', 'Align islands to the horizontal axis'),
            ('AXIS_ALIGNED_Y', 'Axis-aligned (Vertical)', 'Align islands to the vertical axis')
        ],
        default='AXIS_ALIGNED'
    )
    
    uv_island_margin: FloatProperty(
        name="Island Margin",
        description="Space between UV islands",
        default=0.005,
        min=0.0,
        max=1.0,
        precision=3
    )
    
    uv_area_weight: FloatProperty(
        name="Area Weight",
        description="Weight factor for face area",
        default=0.0,
        min=0.0,
        max=1.0
    )
    
    uv_correct_aspect: BoolProperty(
        name="Correct Aspect",
        description="Correct for aspect ratio",
        default=True
    )
    
    uv_scale_to_bounds: BoolProperty(
        name="Scale to Bounds",
        description="Scale UV coordinates to bounds",
        default=False
    )
    
    # UV Packing settings
    show_packing_settings: BoolProperty(
        name="Show Packing Settings",
        description="Show/hide packing settings",
        default=False
    )

    show_mof_settings: BoolProperty(
        name="Show MOF Settings",
        default=False
    )

    show_smart_settings: BoolProperty(
        name="Show Smart UV Settings",
        default=False
    )

    show_import_blend: BoolProperty(
        name="Show Import Blend Files",
        default=False
    )

    show_alpha_mode: BoolProperty(
        name="Show Alpha Mode Settings",
        default=False
    )
    
    enable_uv_pack: BoolProperty(
        name="Pack UVs",
        description="Pack UV islands after unwrapping",
        default=True,
        update=update_uv_pack
    )

    enable_custom_uv_bake: BoolProperty(
        name="Enable Custom UV Bake",
        description="Bake specific objects to a chosen UV map instead of the global UV method",
        default=False,
    )

    show_custom_uv_bake: BoolProperty(
        name="Show Custom UV Bake Settings",
        default=True,
    )

    custom_uv_bake_targets: CollectionProperty(
        type=GLBBakeUVTarget,
    )

    custom_uv_bake_index: IntProperty(
        name="Active Target Index",
        default=0,
    )

    alpha_mode: EnumProperty(
        name="Alpha Mode",
        description="Default alpha mode for baked materials with transparency (per-collection overrides in the list)",
        items=GLB_ALPHA_MODE_ITEMS,
        default='BLEND',
    )

    alpha_threshold: FloatProperty(
        name="Threshold", default=0.5, min=0.0, max=1.0,
        description="Mask cutoff: pixels with alpha above this are visible, below are invisible",
    )
    
    alpha_collections: CollectionProperty(type=GLBAlphaCollectionItem)
    export_running: BoolProperty(default=False, options={'SKIP_SAVE'})
    export_progress: FloatProperty(default=0.0, min=0.0, max=1.0,
                                   subtype='FACTOR', options={'SKIP_SAVE'})
    export_status: StringProperty(default="", options={'SKIP_SAVE'})

    pack_shape_method: EnumProperty(
        name="Shape Method",
        description="Method to use for packing UV islands",
        items=[
            ('CONCAVE', 'Exact Shape (Concave)', 'Use exact shape including concave areas'),
            ('CONVEX', 'Convex Hull', 'Use convex hull of islands'),
            ('AABB', 'Bounding Box', 'Use axis-aligned bounding box')
        ],
        default='CONCAVE'
    )
    
    pack_scale: BoolProperty(
        name="Scale",
        description="Scale islands to fit UV space",
        default=True
    )
    
    pack_rotate: BoolProperty(
        name="Rotate",
        description="Rotate islands for best fit",
        default=True
    )
    
    pack_rotation_method: EnumProperty(
        name="Rotation Method",
        description="Method to use for rotating UV islands",
        items=[
            ('ANY', 'Any', 'Allow any rotation angle'),
            ('CARDINAL', 'Cardinal', 'Only 90 degree rotations'),
            ('AXIS_ALIGNED', 'Axis Aligned', 'Align to closest axis')
        ],
        default='ANY'
    )
    
    pack_margin_method: EnumProperty(
        name="Margin Method",
        description="Method to use for margin between islands",
        items=[
            ('SCALED', 'Scaled', 'Margin scaled by island size'),
            ('ADD', 'Add', 'Fixed margin size'),
            ('FRACTION', 'Fraction', 'Margin as fraction of UV space')
        ],
        default='ADD'
    )
    
    pack_margin: FloatProperty(
        name="Margin",
        description="Margin between packed UV islands",
        default=0.007,
        min=0.0,
        max=1.0,
        precision=3
    )
    
    pack_lock_pinned: BoolProperty(
        name="Lock Pinned Islands",
        description="Don't move or rotate pinned islands",
        default=False
    )
    
    pack_lock_method: EnumProperty(
        name="Lock Method",
        description="Which islands to lock",
        items=[
            ('SCALE', 'Scale', 'Lock scale'),
            ('ROTATION', 'Rotation', 'Lock rotation'),
            ('ROTATION_SCALE', 'Rotation & Scale', 'Lock rotation and scale'),
            ('LOCKED', 'Locked', 'Lock all transformations')
        ],
        default='LOCKED'
    )
    
    pack_merge_overlapping: BoolProperty(
        name="Merge Overlapping",
        description="Merge overlapping islands before packing",
        default=False
    )
    
    pack_udim_target: EnumProperty(
        name="Pack to",
        description="Target UDIM tile for packing",
        items=[
            ('CLOSEST_UDIM', 'Closest UDIM', 'Pack to closest UDIM tile'),
            ('ACTIVE_UDIM', 'Active UDIM', 'Pack to active UDIM tile'),
            ('ORIGINAL_AABB', 'Original AABB', 'Keep in original bounding box')
        ],
        default='CLOSEST_UDIM'
    )
    
    # Baking settings
    enable_baking: BoolProperty(
        name="Bake Materials",
        description="Bake multiple materials into texture maps",
        default=True
    )

    bake_ambient_occlusion: BoolProperty(
        name="Ambient Occlusion",
        description="Add ambient occlusion to materials before baking",
        default=True
    )

    ao_samples: IntProperty(
        name="AO Samples",
        description="Number of samples for ambient occlusion calculation",
        default=256,
        min=1,
        max=4096
    )

    ao_distance: FloatProperty(
        name="AO Distance",
        description="Distance to trace rays for ambient occlusion",
        default=0.1,
        min=0.0,
        max=100.0,
        subtype='DISTANCE',
        update=_update_ao_preview_distance
    )
    
    show_ao_exceptions: BoolProperty(default=False)

    ao_use_exceptions: BoolProperty(
        name="AO Exceptions",
        description="Objects in the list will not cast ambient occlusion onto other objects",
        default=False,
        update=_ao_flag_update
    )

    ao_exception_objects: CollectionProperty(type=GLBAOExceptionItem)

    ao_exception_index: IntProperty(default=0)

    bake_resolution: IntProperty(
        name="Resolution",
        description="Texture resolution for baking",
        default=2048,
        min=128,
        max=16384,
        soft_max=8192
    )

    bake_samples: IntProperty(
        name="Samples",
        description="Number of samples for baking",
        default=64,
        min=1,
        max=4096
    )

    bake_margin: IntProperty(
        name="Margin",
        description="Baking margin in pixels",
        default=32,
        min=0,
        max=64,
        subtype='PIXEL'
    )
    
    # Export Settings
    export_enabled: BoolProperty(
        name="Export GLB Files",
        description="Export processed objects as GLB files",
        default=True
    )
    
    export_path: StringProperty(
        name="Export Path",
        description="Folder where GLB files will be exported",
        default="//exports/",
        subtype='DIR_PATH'
    )

# === OPERATORS ===

class GLB_OT_CleanupProcessedCollections(Operator):
    """Delete all _processed collections and purge unused data"""
    bl_idname = "glb_export.cleanup_processed_collections"
    bl_label = "Delete All Processed Collections"
    
    def execute(self, context):
        processed_collections = []
        
        # Find all _processed collections
        for collection in bpy.data.collections:
            if collection.name.endswith("_processed"):
                processed_collections.append(collection)
        
        if not processed_collections:
            self.report({'INFO'}, "No processed collections found")
            return {'FINISHED'}
        
        # Delete them
        for collection in processed_collections:
            bpy.data.collections.remove(collection)
        
        # Purge unused data
        bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
        
        self.report({'INFO'}, f"Deleted {len(processed_collections)} processed collections and purged unused data")
        return {'FINISHED'}


class GLB_OT_OpenExportFolder(Operator):
    """Open the export folder in file explorer"""
    bl_idname = "glb_export.open_folder"
    bl_label = "Open Export Folder"
    
    def execute(self, context):
        export_path = bpy.path.abspath(context.scene.glb_export_props.export_path)
        
        if not os.path.exists(export_path):
            self.report({'WARNING'}, "Export folder doesn't exist yet")
            return {'CANCELLED'}
        
        # Open folder in system file explorer
        import subprocess
        import sys
        
        if sys.platform == "win32":
            subprocess.Popen(f'explorer "{export_path}"')
        elif sys.platform == "darwin":  # macOS
            subprocess.Popen(["open", export_path])
        else:  # linux
            subprocess.Popen(["xdg-open", export_path])
        
        return {'FINISHED'}


class GLB_OT_ProcessAndExport(Operator):
    """Process selected collections"""
    bl_idname = "glb_export.process_export"
    bl_label = "Process and Export"
    bl_options = {'INTERNAL'}
    
    _timer = None
    _current_collection = 0
    _total_collections = 0
    _collections_to_process = []
    _is_cancelled = False
    
    def invoke(self, context, event):
        self._material_problems = scan_export_materials(context)
        if self._material_problems:
            return context.window_manager.invoke_props_dialog(self, width=460)
        return self.execute(context)

    def draw(self, context):
        problems = getattr(self, '_material_problems', None)
        if not problems:
            return
        layout = self.layout
        layout.label(text="Material problems found:", icon='ERROR')
        box = layout.box()
        for msg in self._material_problems[:14]:
            box.label(text=msg)
        if len(self._material_problems) > 14:
            box.label(text=f"...and {len(self._material_problems) - 14} more")
        layout.label(text="OK = continue export anyway.   ESC or click outside = cancel.")

    def execute(self, context):
        import time as _time
        self._t_start = _time.time()
        self._exported_files = []
        ao_preview_stop(context)
        collections_to_process = []
        
        self.original_exclude_states = {}
        
        def store_all_states(layer_col):
            self.original_exclude_states[layer_col] = layer_col.exclude
            for child in layer_col.children:
                store_all_states(child)
        
        store_all_states(context.view_layer.layer_collection)
        
        def find_collections_to_process(layer_collection, path=[]):
            current_path = path + [layer_collection]
            if (
                not layer_collection.exclude and 
                layer_collection.collection.name != "Lighting" and
                layer_collection != context.view_layer.layer_collection
            ):
                collections_to_process.append({
                    'collection': layer_collection.collection,
                    'layer_collection': layer_collection,
                    'path': current_path.copy()
                })
            for child in layer_collection.children:
                find_collections_to_process(child, current_path)
        
        find_collections_to_process(context.view_layer.layer_collection)

        if not collections_to_process:
            self.report({'ERROR'}, "No visible collections found!")
            return {'CANCELLED'}
        
        print(f"=== PROCESSING {len(collections_to_process)} COLLECTIONS ===")
        for item in collections_to_process:
            print(f"  - {item['collection'].name}")
        
        self.processed_objects = []
        self.temp_collections = []
        self.created_images = [] 
        self.baked_materials = [] 
        self.deferred_ao_parts = []
        self.collections_data = collections_to_process
        
        # Set engine to Cycles ONCE for the whole export. Avoids repeated shader
        # recompilation when toggling engine per collection.
        self._original_engine = context.scene.render.engine
        self._original_samples = context.scene.cycles.samples
        if context.scene.render.engine != 'CYCLES':
            context.scene.render.engine = 'CYCLES'
        context.scene.cycles.samples = context.scene.glb_export_props.bake_samples
        
        collection_names = [col_data['collection'].name for col_data in collections_to_process]
        
        self._collections_to_process = collection_names
        self._current_collection = 0
        self._total_collections = len(collection_names)
        self._is_cancelled = False
        self._phase = "DUPLICATING"
        
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.3, window=context.window)
        wm.modal_handler_add(self)
        
        return {'RUNNING_MODAL'}
    
    def update_progress(self, context, message, current=None, total=None):
        """Update progress with fallback to console"""
        props = context.scene.glb_export_props
        try:
            if current and total:
                progress = int((current / total) * 100)
                full_message = f"[{current}/{total}] {progress}% - {message}"
            else:
                full_message = message
            props.export_status = message
            context.workspace.status_text_set(full_message)
            for area in context.screen.areas:
                area.tag_redraw()
        except:
            pass  # Status text not available in this context
        
        # Always print to console as fallback
        if current and total:
            progress = int((current / total) * 100)
            print(f"[{progress}%] {message}")
        else:
            print(f"[Processing] {message}")
    
    def modal(self, context, event):
        if event.type in {'ESC'}:
            self.cancel(context)
            return {'CANCELLED'}
        
        if event.type == 'TIMER':
            if self._phase == "DUPLICATING":
                props = context.scene.glb_export_props
                props.export_running = True
                props.export_progress = 0.0
                props.export_status = "Preparing collections..."
                self.duplicate_all_collections(context)
                self._phase = "PROCESSING"
                self._current_collection = 0
                
            elif self._phase == "PROCESSING":
                if self._current_collection < self._total_collections:
                    collection_name = self._collections_to_process[self._current_collection]
                    
                    for temp_col in self.temp_collections:
                        if temp_col['original_name'] == collection_name:
                            progress = int((self._current_collection / self._total_collections) * 100)
                            context.workspace.status_text_set(
                                f"BATCH PROCESSING | {self._current_collection + 1} of {self._total_collections} files | {progress}% | "
                                f"File: {collection_name} | Material: PROCESSING"
                            )
                            props_ui = context.scene.glb_export_props
                            props_ui.export_progress = self._current_collection / self._total_collections
                            props_ui.export_status = (
                                f"{int(100 * self._current_collection / self._total_collections)}%  "
                                f"({self._current_collection + 1}/{self._total_collections})  {collection_name}")
                            for area in context.screen.areas:
                                area.tag_redraw()
                            
                            try:
                                if getattr(self, '_col_gen', None) is None:
                                    self._col_gen = self.process_temp_collection(
                                        context, 
                                        temp_col['collection'],
                                        collection_name,
                                        self._current_collection + 1, 
                                        self._total_collections
                                    )
                                try:
                                    next(self._col_gen)
                                    # MOF running in background - keep UI alive,
                                    # stay on this collection until it finishes
                                    context.workspace.status_text_set(
                                        f"MOF unwrapping '{collection_name}' in background - ESC to cancel")
                                    props_bar = context.scene.glb_export_props
                                    if hasattr(props_bar, "export_status"):
                                        props_bar.export_status = f"MOF: {collection_name}  (ESC to cancel)"
                                        for area in context.screen.areas:
                                            area.tag_redraw()
                                    return {'PASS_THROUGH'}
                                except StopIteration as stop:
                                    self._col_gen = None
                                    processed_obj = stop.value
                                    if processed_obj:
                                        self.processed_objects.append(processed_obj)
                            except Exception as e:
                                self._col_gen = None
                                job = getattr(self, '_mof_job', None)
                                if job:
                                    GLB_OT_UnwrapSelected._cleanup_job(self, job, kill=True)
                                    self._mof_job = None
                                self.report({'WARNING'}, f"Failed to process {collection_name}: {str(e)}")
                            break
                    
                    self._current_collection += 1
                else:
                    self.merge_deferred_ao_parts(context)
                    if self.processed_objects and context.scene.glb_export_props.export_enabled:
                        self.export_combined_glb(context)
                    props_done = context.scene.glb_export_props
                    props_done.export_progress = 1.0
                    import time as _time
                    _el = int(_time.time() - getattr(self, '_t_start', _time.time()))
                    _t_str = f"{_el // 60}m {_el % 60}s" if _el >= 60 else f"{_el}s"
                    props_done.export_status = f"100%  Finished in {_t_str}"
                    self.report({'INFO'}, f"Export finished in {_t_str}")
                    for area in context.screen.areas:
                        area.tag_redraw()

                    def _hide_export_bar():
                        try:
                            bpy.context.scene.glb_export_props.export_running = False
                            for w in bpy.context.window_manager.windows:
                                for a in w.screen.areas:
                                    a.tag_redraw()
                        except Exception:
                            pass
                        return None
                    bpy.app.timers.register(_hide_export_bar, first_interval=0.5)
                    _store_export_report(self, _t_str)
                    bpy.ops.glb_export.show_report('INVOKE_DEFAULT')
                    self.finish(context)
                    return {'FINISHED'}
        
        return {'RUNNING_MODAL'}
    
    def duplicate_all_collections(self, context):
        print("=== DUPLICATING ALL COLLECTIONS ===")

        props = context.scene.glb_export_props
        ao_exception_flags = {}
        if props.ao_use_exceptions and props.bake_ambient_occlusion:
            for e in props.ao_exception_objects:
                if e.object_ref and (e.no_cast or e.no_receive):
                    ao_exception_flags[e.object_ref.name] = (e.no_cast, e.no_receive)
        
        def hide_all_except_lighting(layer_col):
            if layer_col.collection.name != "Lighting":
                layer_col.exclude = True
            for child in layer_col.children:
                hide_all_except_lighting(child)
        
        hide_all_except_lighting(context.view_layer.layer_collection)
        
        all_duplicated_objects = []
        
        for col_data in self.collections_data:
            original_collection = col_data['collection']
            
            processable_types = {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}
            processable_objects = [obj for obj in original_collection.all_objects if obj.type in processable_types]
            
            if not processable_objects:
                print(f"Skipping '{original_collection.name}' - no processable objects")
                continue
            
            new_collection = bpy.data.collections.new(name=f"{original_collection.name}_temp_process")
            context.scene.collection.children.link(new_collection)

            material_mapping = {}

            for obj in original_collection.objects:
                if obj.type == 'EMPTY':
                    continue
                new_obj = obj.copy()
                new_obj.data = obj.data.copy()
                
                for i, slot in enumerate(new_obj.material_slots):
                    if slot.material:
                        original_mat = slot.material
                        if original_mat.name not in material_mapping:
                            new_mat = original_mat.copy()
                            new_mat.name = f"{original_mat.name}_temp"
                            material_mapping[original_mat.name] = new_mat
                        new_obj.data.materials[i] = material_mapping[original_mat.name]
                
                new_collection.objects.link(new_obj)
                all_duplicated_objects.append(new_obj)
                
                new_obj["original_name"] = obj.name
                exc_flags = ao_exception_flags.get(obj.name)
                if exc_flags:
                    if exc_flags[0]:
                        new_obj["ao_exception"] = True
                    if exc_flags[1]:
                        new_obj["ao_no_receive"] = True
                new_obj["original_location"] = obj.location.copy()
                new_obj["original_rotation"] = obj.rotation_euler.copy()
                new_obj["original_scale"] = obj.scale.copy()
            
            self.temp_collections.append({
                'collection': new_collection,
                'original_name': original_collection.name
            })
            
            def make_visible(layer_col, target_collection):
                if layer_col.collection == target_collection:
                    layer_col.exclude = False
                    return True
                for child in layer_col.children:
                    if make_visible(child, target_collection):
                        return True
                return False
            
            make_visible(context.view_layer.layer_collection, new_collection)
            
            print(f"Created temporary collection: {new_collection.name}")
        
        print("\n=== CLEARING PARENT RELATIONSHIPS ===")

        # Clear all parent relationships first
        for obj in all_duplicated_objects:
            if obj.parent:
                world_matrix = obj.matrix_world.copy()
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                bpy.ops.object.parent_clear(type='CLEAR_KEEP_TRANSFORM')
                obj.matrix_world = world_matrix

        print("\n=== CONVERTING ALL TO MESH AND APPLYING MODIFIERS ===")

        # Convert ALL objects to mesh (this applies modifiers on mesh objects)
        for obj in all_duplicated_objects:
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            
            original_type = obj.type
            # Convert to mesh - for mesh objects this applies modifiers
            bpy.ops.object.convert(target='MESH')
            
            if original_type == 'MESH':
                print(f"Applied modifiers on mesh object: {obj.name}")
            else:
                print(f"Converted {obj.name} from {original_type} to mesh")

        print("\n=== SCALING ALL OBJECTS TO FIT 1M ===")
        
        if all_duplicated_objects:
            min_coords = [float('inf')] * 3
            max_coords = [float('-inf')] * 3
            
            for obj in all_duplicated_objects:
                if obj.type == 'MESH':
                    bpy.context.view_layer.objects.active = obj
                    bpy.context.view_layer.update()
                    
                    bbox_corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
                    
                    for corner in bbox_corners:
                        for i in range(3):
                            min_coords[i] = min(min_coords[i], corner[i])
                            max_coords[i] = max(max_coords[i], corner[i])
            
            dimensions = [max_coords[i] - min_coords[i] for i in range(3)]
            max_dimension = max(dimensions)
            
            if max_dimension > 0:
                scale_factor = 1.0 / max_dimension
                
                for obj in all_duplicated_objects:
                    # Store values needed to compensate Texture Coordinate during bake
                    obj["_glb_max_dim"] = max_dimension
                    obj["_glb_loc_before_transform"] = list(obj.location)
                    
                    obj.scale *= scale_factor
                    obj.location *= scale_factor
                    
                    bpy.context.view_layer.objects.active = obj
                    bpy.ops.object.select_all(action='DESELECT')
                    obj.select_set(True)
                    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
                
                center = [(min_coords[i] + max_coords[i]) * 0.5 * scale_factor for i in range(3)]
                
                for obj in all_duplicated_objects:
                    obj["_glb_center"] = list(center)
                    
                    obj.location[0] -= center[0]
                    obj.location[1] -= center[1]
                    obj.location[2] -= center[2]
                    
                    bpy.context.view_layer.objects.active = obj
                    bpy.ops.object.select_all(action='DESELECT')
                    obj.select_set(True)
                    bpy.ops.object.transform_apply(location=True, rotation=False, scale=False)
                
                print(f"Scaled all objects by factor: {scale_factor:.3f}")
                print(f"Centered at origin")
        
        print("=== ALL COLLECTIONS DUPLICATED, SCALED AND VISIBLE ===")
        
    def process_temp_collection(self, context, temp_collection, original_name, current_idx, total_count):
        props = context.scene.glb_export_props
        
        print(f"\n=== PROCESSING COLLECTION: {original_name} ===")
        
        # Remove lights and empties
        objects_to_remove = []
        for obj in temp_collection.objects:
            if obj.type == 'EMPTY':
                objects_to_remove.append(obj)
            elif obj.type == 'LIGHT':
                objects_to_remove.append(obj)
        
        for obj in objects_to_remove:
            bpy.data.objects.remove(obj, do_unlink=True)
        
        print(f"Removed {len(objects_to_remove)} lights and empties")
        
        # Collect mesh objects
        mesh_objects = [obj for obj in temp_collection.objects if obj.type == 'MESH']
        print(f"Have {len(mesh_objects)} mesh objects to process")

        # Mark AO-exception geometry with vertex groups so it survives the join
        if props.ao_use_exceptions and props.bake_ambient_occlusion:
            for obj in mesh_objects:
                if obj.get("ao_exception") and "AO_EXCEPT_TMP" not in obj.vertex_groups:
                    vg = obj.vertex_groups.new(name="AO_EXCEPT_TMP")
                    vg.add(list(range(len(obj.data.vertices))), 1.0, 'REPLACE')
                if obj.get("ao_no_receive") and "AO_NORECV_TMP" not in obj.vertex_groups:
                    vg = obj.vertex_groups.new(name="AO_NORECV_TMP")
                    vg.add(list(range(len(obj.data.vertices))), 1.0, 'REPLACE')
        
        # Go directly to merging vertices
        self.update_progress(context, "Merging vertices...", current_idx, total_count)
        
        for obj in mesh_objects:
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.remove_doubles(threshold=0.00001)
            bpy.ops.object.mode_set(mode='OBJECT')
        
        for obj in mesh_objects:
            if obj.animation_data:
                obj.animation_data_clear()
            
            obj.delta_location = (0, 0, 0)
            obj.delta_rotation_euler = (0, 0, 0)
            obj.delta_scale = (1, 1, 1)
        
        for obj in mesh_objects:
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
        
        self.update_progress(context, "Joining meshes...", current_idx, total_count)
        
        if len(mesh_objects) > 0:
            bpy.ops.object.select_all(action='DESELECT')
            for obj in mesh_objects:
                obj.select_set(True)
            
            bpy.context.view_layer.objects.active = mesh_objects[0]
            
            mesh_objects = sorted(mesh_objects, key=lambda o: o.name)
            
            # Custom UV bake: peel listed objects out of the auto-unwrap.
            # Their chosen UV map is preserved (pinned, rotation kept) and merged
            # into the collection's single UV map before packing & baking.
            custom_uv_peeled = []
            custom_bake_only = False
            has_custom_pinned = False
            single_custom_only = False
            if props.enable_custom_uv_bake and len(props.custom_uv_bake_targets) > 0:
                target_map = {}
                for t in props.custom_uv_bake_targets:
                    if t.object_ref and t.uv_map_name:
                        target_map[t.object_ref.name] = t.uv_map_name

                if target_map:
                    all_objs = list(mesh_objects)
                    peeled = [o for o in all_objs if o.get("original_name", o.name) in target_map]
                    remaining = [o for o in all_objs if o not in peeled]

                    for pobj in peeled:
                        uv_name = target_map[pobj.get("original_name", pobj.name)]
                        if pobj.type == 'MESH' and uv_name in pobj.data.uv_layers:
                            custom_uv_peeled.append((pobj, uv_name))
                        else:
                            print(f"Warning: UV map '{uv_name}' not found on {pobj.name}; merging into main join")
                            remaining.append(pobj)

                    # Re-select remaining objects for the join
                    bpy.ops.object.select_all(action='DESELECT')
                    for o in remaining:
                        o.select_set(True)
                    if remaining:
                        context.view_layer.objects.active = remaining[0]

                    # All collection objects are custom-UV: join them now, skip auto-unwrap
                    if not remaining and custom_uv_peeled:
                        custom_bake_only = True
                        has_custom_pinned = True
                        single_custom_only = len(custom_uv_peeled) == 1
                        for pobj, uv_name in custom_uv_peeled:
                            self.prepare_custom_uv_object(None, pobj, uv_name)
                        bpy.ops.object.select_all(action='DESELECT')
                        for pobj, _ in custom_uv_peeled:
                            if pobj.name in bpy.data.objects:
                                pobj.select_set(True)
                        first_pobj = custom_uv_peeled[0][0]
                        context.view_layer.objects.active = first_pobj
                        if len(custom_uv_peeled) > 1:
                            bpy.ops.object.join()
                        joined_obj = context.active_object
                        joined_obj.name = original_name
                        if "UVMap" in joined_obj.data.uv_layers:
                            joined_obj.data.uv_layers.active = joined_obj.data.uv_layers["UVMap"]
                        custom_uv_peeled = []  # already merged

            if not custom_bake_only:
                bpy.ops.object.join()

                # Get the joined object
                joined_obj = context.active_object
            if not joined_obj:
                print("WARNING: No object after joining! Skipping this collection.")
                return None
            joined_obj.name = original_name

            # FIRST - Remove unused material slots
            bpy.context.view_layer.objects.active = joined_obj
            bpy.ops.object.select_all(action='DESELECT')
            joined_obj.select_set(True)
            bpy.ops.object.material_slot_remove_unused()
            print(f"Removed unused material slots")

            # THEN - Check if any remaining materials use UV coordinates
            materials_use_uvs = False
            for slot in joined_obj.material_slots:
                if slot.material and slot.material.use_nodes:
                    for node in slot.material.node_tree.nodes:
                        # Check for any node that uses UV coordinates
                        if node.type in ['TEX_COORD', 'UVMAP']:
                            # Check if UV output is connected
                            for output in node.outputs:
                                if output.name == 'UV' and output.is_linked:
                                    materials_use_uvs = True
                                    break
                        # Also check for image textures and procedural textures using UV
                        elif node.type in ['TEX_IMAGE', 'TEX_BRICK', 'TEX_CHECKER', 'TEX_GRADIENT', 
                                         'TEX_MAGIC', 'TEX_MUSGRAVE', 'TEX_NOISE', 'TEX_VORONOI', 'TEX_WAVE']:
                            # These might use UV coordinates even without explicit UV node
                            materials_use_uvs = True
                            break
                    if materials_use_uvs:
                        break

            print(f"Materials use UV coordinates: {materials_use_uvs}")

            # Handle UV maps based on detection
            if (props.uv_unwrap_method != 'NONE' or (props.enable_uv_pack and materials_use_uvs)) and not custom_bake_only:
                self.update_progress(context, "UV unwrapping...", current_idx, total_count)
                
                if materials_use_uvs:
                    print("Materials use UV coordinates - preserving for baking")
                    
                    new_uv = joined_obj.data.uv_layers.new(name="GLB_Bake")
                    joined_obj.data.uv_layers.active = new_uv
                    
                else:
                    print("No UV dependencies - recreating UV maps")
                    
                    while joined_obj.data.uv_layers:
                        joined_obj.data.uv_layers.remove(joined_obj.data.uv_layers[0])
                    
                    joined_obj.data.uv_layers.new(name="UVMap")
                    joined_obj.data.uv_layers.active = joined_obj.data.uv_layers["UVMap"]
                
                # NOW APPLY THE UNWRAPPING (only once, to the active UV layer)
                if props.uv_unwrap_method == 'MOF':
                    # MOF runs as a background process; each yield returns
                    # control to the UI so Blender stays responsive and
                    # ESC can cancel during the unwrap
                    job = GLB_OT_UnwrapSelected._mof_start(self, context, props, joined_obj)
                    if job is None:
                        print("Warning: MOF unwrap failed to start, skipping UV unwrap")
                    else:
                        self._mof_job = job
                        while job["proc"].poll() is None:
                            yield 'MOF_WAIT'
                        self._mof_job = None
                        ok = GLB_OT_UnwrapSelected._mof_finish(self, context, joined_obj, job)
                        GLB_OT_UnwrapSelected._cleanup_job(self, job)
                        if ok:
                            print("Applied MOF UV Unwrap")
                        else:
                            print("Warning: MOF unwrap failed, skipping UV unwrap")
                
                elif props.uv_unwrap_method == 'SMART':
                    bpy.ops.object.mode_set(mode='EDIT')
                    bpy.ops.mesh.select_all(action='SELECT')
                    
                    try:
                        smart_uv_kwargs = {
                            'angle_limit': math.radians(props.uv_angle_limit),
                            'island_margin': props.uv_island_margin,
                            'area_weight': props.uv_area_weight,
                            'correct_aspect': props.uv_correct_aspect,
                            'scale_to_bounds': props.uv_scale_to_bounds,
                            'margin_method': props.uv_margin_method,
                            'rotate_method': props.uv_rotation_method
                        }
                        bpy.ops.uv.smart_project(**smart_uv_kwargs)
                        print("Applied Smart UV Project")
                    except Exception as e:
                        print(f"Warning: Could not apply Smart UV Project: {e}")
                    
                    bpy.ops.object.mode_set(mode='OBJECT')
            
            # Merge custom-UV objects into the main object BEFORE packing,
            # so the whole collection shares one UV map -> one material
            if custom_uv_peeled and joined_obj and joined_obj.name in bpy.data.objects:
                if props.uv_unwrap_method == 'MOF' and not custom_bake_only:
                    # Normalize auto-island scale BEFORE customs join in,
                    # so the chosen layouts keep their own island proportions
                    try:
                        context.view_layer.objects.active = joined_obj
                        bpy.ops.object.select_all(action='DESELECT')
                        joined_obj.select_set(True)
                        bpy.ops.object.mode_set(mode='EDIT')
                        bpy.ops.mesh.select_all(action='SELECT')
                        bpy.ops.uv.select_all(action='SELECT')
                        bpy.ops.uv.average_islands_scale()
                        bpy.ops.object.mode_set(mode='OBJECT')
                    except Exception as e:
                        print(f"Warning: average islands scale failed: {e}")
                        bpy.ops.object.mode_set(mode='OBJECT')

                for pobj, uv_name in custom_uv_peeled:
                    if pobj.name in bpy.data.objects:
                        self.prepare_custom_uv_object(joined_obj, pobj, uv_name)
                        has_custom_pinned = True

                bpy.ops.object.select_all(action='DESELECT')
                joined_obj.select_set(True)
                context.view_layer.objects.active = joined_obj
                for pobj, _ in custom_uv_peeled:
                    if pobj.name in bpy.data.objects:
                        pobj.select_set(True)
                try:
                    bpy.ops.object.join()
                    joined_obj = context.active_object
                    print("Merged custom-UV objects before packing")
                except Exception as e:
                    print(f"Warning: could not merge custom-UV objects: {e}")

                if "GLB_Bake" in joined_obj.data.uv_layers:
                    joined_obj.data.uv_layers.active = joined_obj.data.uv_layers["GLB_Bake"]
                elif "UVMap" in joined_obj.data.uv_layers:
                    joined_obj.data.uv_layers.active = joined_obj.data.uv_layers["UVMap"]

            # UV PACKING - runs after customs are merged; forced on when they exist
            if ((props.uv_unwrap_method != 'NONE' or materials_use_uvs) and props.enable_uv_pack and not custom_bake_only) or (has_custom_pinned and not single_custom_only):
                self.update_progress(context, "Packing UVs...", current_idx, total_count)
                try:
                    context.view_layer.objects.active = joined_obj
                    bpy.ops.object.select_all(action='DESELECT')
                    joined_obj.select_set(True)
                    
                    bpy.ops.object.mode_set(mode='EDIT')
                    bpy.ops.mesh.select_all(action='SELECT')
                    bpy.ops.uv.select_all(action='SELECT')
                    
                    if props.uv_unwrap_method == 'MOF' and not has_custom_pinned:
                        bpy.ops.uv.average_islands_scale()
                    
                    pack_kwargs = {
                        'margin': props.pack_margin,
                        'rotate': props.pack_rotate,
                        'shape_method': props.pack_shape_method,
                        'scale': props.pack_scale,
                        'rotate_method': props.pack_rotation_method,
                        'margin_method': props.pack_margin_method,
                        'pin': props.pack_lock_pinned or has_custom_pinned,
                        'pin_method': 'ROTATION' if has_custom_pinned else props.pack_lock_method,
                        'merge_overlap': props.pack_merge_overlapping,
                        'udim_source': props.pack_udim_target
                    }
                    bpy.ops.uv.pack_islands(**pack_kwargs)
                    
                    bpy.ops.object.mode_set(mode='OBJECT')
                    print("Packed UV islands")
                except Exception as e:
                    print(f"Warning: Could not pack UVs: {e}")
                    bpy.ops.object.mode_set(mode='OBJECT')

            if props.enable_baking:
                original_materials = []
                for slot in joined_obj.material_slots:
                    if slot.material:
                        original_materials.append(slot.material)

                self.update_progress(context, f"File: {original_name} | Material: BAKING", current_idx, total_count)
                
                denoising_settings = {}
                
                if hasattr(context.scene.cycles, 'use_viewport_denoising'):
                    denoising_settings['use_viewport_denoising'] = context.scene.cycles.use_viewport_denoising
                
                if hasattr(context.scene.cycles, 'use_denoising'):
                    denoising_settings['use_denoising'] = context.scene.cycles.use_denoising
                elif hasattr(context.scene.cycles, 'use_denoise'):
                    denoising_settings['use_denoise'] = context.scene.cycles.use_denoise
                
                if hasattr(context.scene.cycles, 'use_adaptive_sampling'):
                    denoising_settings['use_adaptive_sampling'] = context.scene.cycles.use_adaptive_sampling
                
                try:
                    
                    if hasattr(context.scene.cycles, 'use_viewport_denoising'):
                        context.scene.cycles.use_viewport_denoising = False
                    
                    if hasattr(context.scene.cycles, 'use_denoising'):
                        context.scene.cycles.use_denoising = False
                    elif hasattr(context.scene.cycles, 'use_denoise'):
                        context.scene.cycles.use_denoise = False
                    
                    if hasattr(context.scene.cycles, 'use_adaptive_sampling'):
                        context.scene.cycles.use_adaptive_sampling = False
                    
                    materials = [slot.material for slot in joined_obj.material_slots if slot.material]
                    
                    if materials:
                        uv_state = self.begin_uv_safe_bake(joined_obj, materials)
                        bake_data = self.analyze_materials(materials)
                        
                        self.prepare_materials_for_baking(materials, bake_data)
                        
                        bake_data = self.analyze_materials(materials)
                        
                        new_mat = bpy.data.materials.new(name=f"{joined_obj.name}_Baked")
                        self.baked_materials.append(new_mat)
                        new_mat.use_nodes = True
                        new_nodes = new_mat.node_tree.nodes
                        new_links = new_mat.node_tree.links
                        
                        new_nodes.clear()
                        
                        output_node = new_nodes.new('ShaderNodeOutputMaterial')
                        output_node.location = (300, 0)
                        
                        principled = new_nodes.new('ShaderNodeBsdfPrincipled')
                        principled.location = (0, 0)
                        
                        new_links.new(principled.outputs['BSDF'], output_node.inputs['Surface'])
                        
                        y_offset = 300
                        
                        # Inject Mapping nodes to compensate for the addon's scale + center
                        # transforms, so procedural textures using Texture Coordinate -> Object
                        # (or Geometry -> Position) bake at original size on the joined object.
                        coord_splices = self.inject_coord_compensation(joined_obj)
                        
                        if bake_data['color']['needs_baking']:
                            print("Baking color...")
                            color_image = self.create_image(f"{joined_obj.name}_Color", props.bake_resolution, 'sRGB')
                            self.bake_channel(joined_obj, materials, color_image, 'EMIT', 'Base Color', bake_data['color'])
                            
                            tex_node = new_nodes.new('ShaderNodeTexImage')
                            tex_node.image = color_image
                            tex_node.location = (-400, y_offset)
                            new_links.new(tex_node.outputs['Color'], principled.inputs['Base Color'])
                            y_offset -= 300
                        else:
                            principled.inputs['Base Color'].default_value = bake_data['color']['uniform_value']
                        
                        if bake_data['metallic']['needs_baking']:
                            print("Baking metallic...")
                            metallic_image = self.create_image(f"{joined_obj.name}_Metallic", props.bake_resolution, 'Non-Color')
                            self.bake_channel(joined_obj, materials, metallic_image, 'EMIT', 'Metallic', bake_data['metallic'])
                            
                            tex_node = new_nodes.new('ShaderNodeTexImage')
                            tex_node.image = metallic_image
                            tex_node.location = (-400, y_offset)
                            new_links.new(tex_node.outputs['Color'], principled.inputs['Metallic'])
                            y_offset -= 300
                        else:
                            principled.inputs['Metallic'].default_value = bake_data['metallic']['uniform_value']
                        
                        if bake_data['roughness']['needs_baking']:
                            print("Baking roughness...")
                            roughness_image = self.create_image(f"{joined_obj.name}_Roughness", props.bake_resolution, 'Non-Color')
                            self.bake_channel(joined_obj, materials, roughness_image, 'EMIT', 'Roughness', bake_data['roughness'])
                            
                            tex_node = new_nodes.new('ShaderNodeTexImage')
                            tex_node.image = roughness_image
                            tex_node.location = (-400, y_offset)
                            new_links.new(tex_node.outputs['Color'], principled.inputs['Roughness'])
                            y_offset -= 300
                        else:
                            principled.inputs['Roughness'].default_value = bake_data['roughness']['uniform_value']
                        
                        if bake_data['normal']['needs_baking']:
                            print("Baking normal...")
                            normal_image = self.create_image(f"{joined_obj.name}_Normal", props.bake_resolution, 'Non-Color')
                            self.bake_normal(joined_obj, materials, normal_image)

                            tex_node = new_nodes.new('ShaderNodeTexImage')
                            tex_node.image = normal_image
                            tex_node.location = (-600, y_offset)

                            normal_map_node = new_nodes.new('ShaderNodeNormalMap')
                            normal_map_node.location = (-200, y_offset)
                            normal_map_node.inputs['Strength'].default_value = 1.0

                            new_links.new(tex_node.outputs['Color'], normal_map_node.inputs['Color'])
                            new_links.new(normal_map_node.outputs['Normal'], principled.inputs['Normal'])
                            y_offset -= 300

                        # Alpha handling
                        if not bake_data['alpha'].get('skip', False):
                            if bake_data['alpha']['needs_baking']:
                                print("Baking alpha...")
                                alpha_image = self.create_image(f"{joined_obj.name}_Alpha", props.bake_resolution, 'Non-Color')
                                self.bake_channel(joined_obj, materials, alpha_image, 'EMIT', 'Alpha', bake_data['alpha'])

                                tex_node = new_nodes.new('ShaderNodeTexImage')
                                tex_node.image = alpha_image
                                tex_node.location = (-400, y_offset)
                                new_links.new(tex_node.outputs['Color'], principled.inputs['Alpha'])
                                y_offset -= 300
                            else:
                                principled.inputs['Alpha'].default_value = bake_data['alpha']['uniform_value']
                            # Set the material to BLEND mode so transparency is honored on export.
                            # Force backface culling so glTF exports doubleSided=False, otherwise
                            # back faces render through front faces in BLEND mode and look like
                            # ghost transparency on actually-opaque areas of the texture.
                            a_mode, a_thr, a_ds = resolve_alpha_mode(props, original_name)
                            apply_alpha_mode(new_mat, a_mode, a_thr, a_ds)

                        
                        if props.bake_ambient_occlusion:
                            ao_parts, main_no_cast, main_no_receive = self.prepare_ao_parts(context, joined_obj)
                            ao_receives = (not main_no_receive) or any(not p['no_receive'] for p in ao_parts)
                            
                            if ao_receives:
                                print("Baking ambient occlusion...")
                                ao_image = self.create_image(f"{joined_obj.name}_AO", props.bake_resolution, 'Non-Color')
                                ao_image.generated_color = (1.0, 1.0, 1.0, 1.0)
                                self.run_ao_bakes(context, joined_obj, ao_image, ao_parts, main_no_receive)
                                
                                self.create_gltf_output_node(new_mat, ao_image)
                            else:
                                print("All geometry is set to No Receive - skipping AO texture")
                            
                            for entry in ao_parts:
                                # Split-off parts must end up with the same final baked material
                                ao_part = entry['object']
                                ao_part.data.materials.clear()
                                ao_part.data.materials.append(new_mat)
                        
                        # Remove the injected Mapping nodes from the source materials.
                        # joined_obj's material slots will be replaced by new_mat below,
                        # but the source materials still live in bpy.data.materials.
                        self.remove_coord_compensation(coord_splices)
                        self.end_uv_safe_bake(joined_obj, uv_state)
                        
                        joined_obj.data.materials.clear()
                        joined_obj.data.materials.append(new_mat)

                        print("Baked materials into textures")

                        # After successful baking, clean up UVs
                        if (materials_use_uvs and (props.uv_unwrap_method != 'NONE' or props.enable_uv_pack)) or has_custom_pinned:
                            keep_name = joined_obj.data.uv_layers.active.name if joined_obj.data.uv_layers.active else None
                            
                            uv_names_to_remove = [uv.name for uv in joined_obj.data.uv_layers
                                                  if uv.name != keep_name]
                            for uv_name in uv_names_to_remove:
                                if uv_name in joined_obj.data.uv_layers:
                                    joined_obj.data.uv_layers.remove(joined_obj.data.uv_layers[uv_name])
                            
                            if keep_name and keep_name in joined_obj.data.uv_layers:
                                joined_obj.data.uv_layers[keep_name].name = "UVMap"
                            
                            print("Cleaned up UV maps after baking")

                    else:
                        print("No materials to bake")
                        
                except Exception as e:
                    print(f"Error during baking: {str(e)}")
                    
                    # If a bake threw mid-way, clean up any Mapping splices we injected
                    try:
                        self.remove_coord_compensation(coord_splices)
                    except (NameError, Exception):
                        pass

                    try:
                        self.end_uv_safe_bake(joined_obj, uv_state)
                    except (NameError, Exception):
                        pass
                    
                    joined_obj.data.materials.clear()
                    for mat in original_materials:
                        joined_obj.data.materials.append(mat)
                    print("Restored original materials after baking failure")
                    
                    self.report({'WARNING'}, f"Baking failed for {original_name}: {str(e)}")
                    
                finally:
                    for attr, value in denoising_settings.items():
                        if hasattr(context.scene.cycles, attr):
                            setattr(context.scene.cycles, attr, value)

            elif props.bake_ambient_occlusion:
                # AO-only mode: keep existing materials & UV, just add baked AO
                self.update_progress(context, f"File: {original_name} | AO-only bake", current_idx, total_count)
                try:
                    ao_parts, main_no_cast, main_no_receive = self.prepare_ao_parts(context, joined_obj)
                    ao_receives = (not main_no_receive) or any(not p['no_receive'] for p in ao_parts)
                    
                    if ao_receives:
                        ao_image = self.create_image(f"{joined_obj.name}_AO", props.bake_resolution, 'Non-Color')
                        ao_image.generated_color = (1.0, 1.0, 1.0, 1.0)
                        self.run_ao_bakes(context, joined_obj, ao_image, ao_parts, main_no_receive)
                        for slot in joined_obj.material_slots:
                            if slot.material:
                                self.create_gltf_output_node(slot.material, ao_image)
                        print(f"AO-only bake complete for {joined_obj.name}")
                    else:
                        print("All geometry is set to No Receive - skipping AO texture")
                except Exception as e:
                    print(f"AO-only bake failed: {e}")
                    self.report({'WARNING'}, f"AO bake failed for {original_name}: {e}")

            progress = int((current_idx / total_count) * 100)
            context.workspace.status_text_set(
                f"BATCH PROCESSING | {current_idx} of {total_count} files | {progress}% | "
                f"File: {original_name} | Material: DONE"
            )
            
            context.scene.collection.objects.link(joined_obj)
            temp_collection.objects.unlink(joined_obj)
            
            return joined_obj
        
        return None
    
    def export_combined_glb(self, context):
        props = context.scene.glb_export_props
        export_path = bpy.path.abspath(props.export_path)
        
        if not os.path.exists(export_path):
            try:
                os.makedirs(export_path)
            except Exception as e:
                self.report({'ERROR'}, f"Failed to create export directory: {str(e)}")
                return
        
        original_selection = context.selected_objects[:]
        original_active = context.view_layer.objects.active
        
        bpy.ops.object.select_all(action='DESELECT')
        
        valid_objects = []
        for obj in self.processed_objects:
            try:
                if obj and obj.name in bpy.data.objects:
                    if obj.type != 'MESH' or len(obj.data.polygons) == 0:
                        print(f"Skipping '{obj.name}' - no faces, not exported")
                        continue
                    obj.select_set(True)
                    valid_objects.append(obj)
            except ReferenceError:
                print(f"Object reference invalid, skipping")
                continue
        
        if not valid_objects:
            self.report({'ERROR'}, "No valid objects to export")
            return
        
        context.view_layer.objects.active = valid_objects[0]
        filename = f"{valid_objects[0].name}.glb"
        
        filepath = os.path.join(export_path, filename)
        
        try:
            bpy.ops.export_scene.gltf(
                filepath=filepath,
                export_format='GLB',
                use_selection=True,
                export_cameras=False,
                export_lights=False
            )
            print(f"Exported: {filepath}")
            self.report({'INFO'}, f"Exported: {filename}")
            self._exported_files.append(filepath)
        except Exception as e:
            print(f"Failed to export: {str(e)}")
            self.report({'WARNING'}, f"Failed to export: {str(e)}")
        
        bpy.ops.object.select_all(action='DESELECT')
        for obj in original_selection:
            if obj.name in bpy.data.objects:
                obj.select_set(True)
        if original_active and original_active.name in bpy.data.objects:
            context.view_layer.objects.active = original_active

    def cancel(self, context):
        # Kill a background MOF process if one is running
        try:
            job = getattr(self, '_mof_job', None)
            if job:
                GLB_OT_UnwrapSelected._cleanup_job(self, job, kill=True)
                self._mof_job = None
            gen = getattr(self, '_col_gen', None)
            if gen:
                gen.close()
                self._col_gen = None
        except Exception:
            pass
        # Restore engine/samples to what they were before the export started
        if hasattr(self, '_original_engine'):
            context.scene.render.engine = self._original_engine
            context.scene.cycles.samples = self._original_samples
        wm = context.window_manager
        wm.event_timer_remove(self._timer)
        
        # Restore visibility immediately
        if hasattr(self, 'original_exclude_states'):
            for layer_col, was_excluded in self.original_exclude_states.items():
                try:
                    layer_col.exclude = was_excluded
                except:
                    pass
        
        context.workspace.status_text_set(None)
        context.scene.glb_export_props.export_running = False
        self.report({'WARNING'}, 'Processing cancelled - cleanup scheduled')
        
        # Prepare data for delayed cleanup
        cleanup_data = {
            'processed_objects': getattr(self, 'processed_objects', []),
            'temp_collections': getattr(self, 'temp_collections', []),
            'baked_materials': getattr(self, 'baked_materials', []),
            'created_images': getattr(self, 'created_images', []),
        }
        
        # Schedule cleanup after 2 seconds
        bpy.app.timers.register(delayed_cleanup(cleanup_data), first_interval=1.5)
        
    def finish(self, context):
        # Restore engine/samples to what they were before the export started
        if hasattr(self, '_original_engine'):
            context.scene.render.engine = self._original_engine
            context.scene.cycles.samples = self._original_samples

        wm = context.window_manager
        wm.event_timer_remove(self._timer)
        
        # Restore visibility immediately
        if hasattr(self, 'original_exclude_states'):
            for layer_col, was_excluded in self.original_exclude_states.items():
                try:
                    layer_col.exclude = was_excluded
                except:
                    pass
        
        context.workspace.status_text_set(None)
        self.report({'INFO'}, f'Successfully processed {self._current_collection} collections into combined GLB')
        
        # Prepare data for delayed cleanup
        cleanup_data = {
            'processed_objects': getattr(self, 'processed_objects', []),
            'temp_collections': getattr(self, 'temp_collections', []),
            'baked_materials': getattr(self, 'baked_materials', []),
            'created_images': getattr(self, 'created_images', []),
        }
        
        # Schedule cleanup after 2 seconds to let preview jobs finish
        bpy.app.timers.register(delayed_cleanup(cleanup_data), first_interval=1.5)
    
    def analyze_materials(self, materials):
        """Analyze materials to determine what needs baking"""
        data = {
            'color': {'needs_baking': False, 'uniform_value': (0.8, 0.8, 0.8, 1.0), 'has_connections': []},
            'metallic': {'needs_baking': False, 'uniform_value': 0.0, 'has_connections': []},
            'roughness': {'needs_baking': False, 'uniform_value': 0.5, 'has_connections': []},
            'normal': {'needs_baking': False, 'has_connections': []},
            'alpha': {'needs_baking': False, 'uniform_value': 1.0, 'has_connections': []},
        }
        
        # Check each material
        for mat in materials:
            if not mat.use_nodes:
                continue
                
            principled = self.get_principled_node(mat)
            if not principled:
                continue
            
            # Check Base Color
            color_input = principled.inputs['Base Color']
            if color_input.is_linked:
                data['color']['has_connections'].append(True)
            else:
                data['color']['has_connections'].append(False)
                if not data['color']['needs_baking']:
                    if len(materials) == 1:
                        data['color']['uniform_value'] = color_input.default_value[:]
                    elif 'first_value' in data['color']:  
                        if data['color']['first_value'] != color_input.default_value[:]:
                            data['color']['needs_baking'] = True
                    else:
                        data['color']['first_value'] = color_input.default_value[:]
                        data['color']['uniform_value'] = color_input.default_value[:]
            
            # Check Metallic
            metallic_input = principled.inputs['Metallic']
            if metallic_input.is_linked:
                data['metallic']['has_connections'].append(True)
            else:
                data['metallic']['has_connections'].append(False)
                if not data['metallic']['needs_baking']:
                    if len(materials) == 1:
                        data['metallic']['uniform_value'] = metallic_input.default_value
                    elif 'first_value' in data['metallic']:  # CORRECT!
                        if abs(data['metallic']['first_value'] - metallic_input.default_value) > 0.001:
                            data['metallic']['needs_baking'] = True
                    else:
                        data['metallic']['first_value'] = metallic_input.default_value
                        data['metallic']['uniform_value'] = metallic_input.default_value
            
            # Check Roughness
            roughness_input = principled.inputs['Roughness']
            if roughness_input.is_linked:
                data['roughness']['has_connections'].append(True)
            else:
                data['roughness']['has_connections'].append(False)
                if not data['roughness']['needs_baking']:
                    if len(materials) == 1:
                        data['roughness']['uniform_value'] = roughness_input.default_value
                    elif 'first_value' in data['roughness']:  # CORRECT!
                        if abs(data['roughness']['first_value'] - roughness_input.default_value) > 0.001:
                            data['roughness']['needs_baking'] = True
                    else:
                        data['roughness']['first_value'] = roughness_input.default_value
                        data['roughness']['uniform_value'] = roughness_input.default_value
            
            # Check Normal
            normal_input = principled.inputs['Normal']
            if normal_input.is_linked:
                data['normal']['has_connections'].append(True)
                data['normal']['needs_baking'] = True

            # Check Alpha
            alpha_input = principled.inputs['Alpha']
            if alpha_input.is_linked:
                data['alpha']['has_connections'].append(True)
            else:
                data['alpha']['has_connections'].append(False)
                if not data['alpha']['needs_baking']:
                    if len(materials) == 1:
                        data['alpha']['uniform_value'] = alpha_input.default_value
                    elif 'first_value' in data['alpha']:
                        if abs(data['alpha']['first_value'] - alpha_input.default_value) > 0.001:
                            data['alpha']['needs_baking'] = True
                    else:
                        data['alpha']['first_value'] = alpha_input.default_value
                        data['alpha']['uniform_value'] = alpha_input.default_value

        # Final check - if any material has connections, we need to bake
        if any(data['color']['has_connections']):
            data['color']['needs_baking'] = True
        if any(data['metallic']['has_connections']):
            data['metallic']['needs_baking'] = True
        if any(data['roughness']['has_connections']):
            data['roughness']['needs_baking'] = True
        if any(data['alpha']['has_connections']):
            data['alpha']['needs_baking'] = True

        # Special case: if all alphas are effectively fully opaque (>= 0.999), skip alpha
        # entirely so we don't accidentally set the material to BLEND mode.
        if not data['alpha']['needs_baking'] and data['alpha']['uniform_value'] >= 0.999:
            data['alpha']['skip'] = True
        else:
            data['alpha']['skip'] = False

        return data
    
    def prepare_materials_for_baking(self, materials, bake_data):
        """Convert differing values to nodes before baking"""
        
        # Process Base Color
        if bake_data['color']['needs_baking'] and not any(bake_data['color']['has_connections']):
            print("Converting differing Base Color values to RGB nodes...")
            for mat in materials:
                if not mat.use_nodes:
                    continue
                
                principled = self.get_principled_node(mat)
                if not principled:
                    continue
                
                color_input = principled.inputs['Base Color']
                if not color_input.is_linked:
                    # Create RGB node with current color value
                    nodes = mat.node_tree.nodes
                    links = mat.node_tree.links
                    
                    rgb_node = nodes.new('ShaderNodeRGB')
                    rgb_node.outputs['Color'].default_value = color_input.default_value[:]
                    rgb_node.location = (principled.location[0] - 300, principled.location[1])
                    rgb_node.label = "Bake Prep Color"
                    
                    # Connect RGB node to Base Color
                    links.new(rgb_node.outputs['Color'], color_input)
                    print(f"   - Created RGB node for {mat.name}: {color_input.default_value[:]}")
        
        # Process Metallic
        if bake_data['metallic']['needs_baking'] and not any(bake_data['metallic']['has_connections']):
            print("Converting differing Metallic values to Value nodes...")
            for mat in materials:
                if not mat.use_nodes:
                    continue
                
                principled = self.get_principled_node(mat)
                if not principled:
                    continue
                
                metallic_input = principled.inputs['Metallic']
                if not metallic_input.is_linked:
                    # Create Value node with current metallic value
                    nodes = mat.node_tree.nodes
                    links = mat.node_tree.links
                    
                    value_node = nodes.new('ShaderNodeValue')
                    value_node.outputs['Value'].default_value = metallic_input.default_value
                    value_node.location = (principled.location[0] - 300, principled.location[1] - 100)
                    value_node.label = "Bake Prep Metallic"
                    
                    # Connect Value node to Metallic
                    links.new(value_node.outputs['Value'], metallic_input)
                    print(f"   - Created Value node for {mat.name}: {metallic_input.default_value}")
        
        # Process Roughness
        if bake_data['roughness']['needs_baking'] and not any(bake_data['roughness']['has_connections']):
            print("Converting differing Roughness values to Value nodes...")
            for mat in materials:
                if not mat.use_nodes:
                    continue
                
                principled = self.get_principled_node(mat)
                if not principled:
                    continue
                
                roughness_input = principled.inputs['Roughness']
                if not roughness_input.is_linked:
                    # Create Value node with current roughness value
                    nodes = mat.node_tree.nodes
                    links = mat.node_tree.links
                    
                    value_node = nodes.new('ShaderNodeValue')
                    value_node.outputs['Value'].default_value = roughness_input.default_value
                    value_node.location = (principled.location[0] - 300, principled.location[1] - 200)
                    value_node.label = "Bake Prep Roughness"
                    
                    # Connect Value node to Roughness
                    links.new(value_node.outputs['Value'], roughness_input)
                    print(f"   - Created Value node for {mat.name}: {roughness_input.default_value}")

        # Process Alpha
        if bake_data['alpha']['needs_baking'] and not any(bake_data['alpha']['has_connections']):
            print("Converting differing Alpha values to Value nodes...")
            for mat in materials:
                if not mat.use_nodes:
                    continue

                principled = self.get_principled_node(mat)
                if not principled:
                    continue

                alpha_input = principled.inputs['Alpha']
                if not alpha_input.is_linked:
                    nodes = mat.node_tree.nodes
                    links = mat.node_tree.links

                    value_node = nodes.new('ShaderNodeValue')
                    value_node.outputs['Value'].default_value = alpha_input.default_value
                    value_node.location = (principled.location[0] - 300, principled.location[1] - 300)
                    value_node.label = "Bake Prep Alpha"

                    links.new(value_node.outputs['Value'], alpha_input)
                    print(f"   - Created Value node for {mat.name}: {alpha_input.default_value}")
    
    def inject_coord_compensation(self, obj):
        """Inject Mapping nodes after every Texture Coordinate -> Object output, so that
        procedural textures (Brick, Noise, etc.) using object coordinates bake at the
        original scale and alignment despite the addon's scale/center transforms.
        Returns a list of splice records to be undone by remove_coord_compensation()."""
        max_dim = obj.get("_glb_max_dim")
        loc_before = obj.get("_glb_loc_before_transform")
        center = obj.get("_glb_center")
        
        if not max_dim or not loc_before or not center:
            return []
        
        # Compensation math (Mapping in Point mode does: out = scale * in + location):
        #   current_local = (orig_local + loc_before) * (1/max_dim) - center
        #   we want output = orig_local
        #   => scale = max_dim, location = max_dim * center - loc_before
        comp_scale = (float(max_dim), float(max_dim), float(max_dim))
        comp_location = (
            float(max_dim) * center[0] - loc_before[0],
            float(max_dim) * center[1] - loc_before[1],
            float(max_dim) * center[2] - loc_before[2],
        )
        
        splices = []
        materials = [slot.material for slot in obj.material_slots
                     if slot.material and slot.material.use_nodes]
        
        # Outputs that carry world/local positional data and get distorted by the
        # addon's scale/center transforms. Other coordinate sources are left alone:
        #   Generated     -> auto-normalizes to the mesh bbox (scale-invariant)
        #   Normal        -> direction vector (scale-invariant)
        #   UV            -> 2D, already in [0,1]
        #   Camera/Window/Reflection -> view-dependent, not mesh-dependent
        compensation_targets = {
            'TEX_COORD': ('Object',),
            'NEW_GEOMETRY': ('Position',),
        }
        
        for mat in materials:
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            
            for src_node in list(nodes):
                output_names = compensation_targets.get(src_node.type)
                if not output_names:
                    continue
                
                for out_name in output_names:
                    out_socket = src_node.outputs.get(out_name)
                    if not out_socket or not out_socket.is_linked:
                        continue
                    
                    # Snapshot existing consumers before we modify links
                    original_targets = [link.to_socket for link in out_socket.links]
                    
                    # Create a Mapping node and route all consumers through it
                    mapping_node = nodes.new('ShaderNodeMapping')
                    mapping_node.vector_type = 'POINT'
                    mapping_node.label = "_glb_compensation"
                    mapping_node.location = (src_node.location.x + 200, src_node.location.y - 100)
                    mapping_node.inputs['Scale'].default_value = comp_scale
                    mapping_node.inputs['Location'].default_value = comp_location
                    
                    # Disconnect originals, route through Mapping, reconnect downstream
                    for link in list(out_socket.links):
                        links.remove(link)
                    links.new(out_socket, mapping_node.inputs['Vector'])
                    for to_socket in original_targets:
                        links.new(mapping_node.outputs['Vector'], to_socket)
                    
                    splices.append({
                        'material': mat,
                        'mapping_node': mapping_node,
                        'src_socket': out_socket,
                        'targets': original_targets,
                    })
        
        return splices
    
    def remove_coord_compensation(self, splices):
        """Undo inject_coord_compensation: remove Mapping nodes and reconnect originals."""
        for splice in splices:
            try:
                mat = splice['material']
                mapping_node = splice['mapping_node']
                src_socket = splice['src_socket']
                targets = splice['targets']
                links = mat.node_tree.links
                
                # Drop links touching the Mapping node
                for link in list(mapping_node.inputs['Vector'].links):
                    links.remove(link)
                for link in list(mapping_node.outputs['Vector'].links):
                    links.remove(link)
                
                # Reconnect src directly to original consumers
                for to_socket in targets:
                    links.new(src_socket, to_socket)
                
                mat.node_tree.nodes.remove(mapping_node)
            except Exception as e:
                print(f"Warning: failed to remove coord compensation splice: {e}")

    def get_principled_node(self, material):
        """Find Principled BSDF node in material"""
        for node in material.node_tree.nodes:
            if node.type == 'BSDF_PRINCIPLED':
                return node
        return None
    
    def create_image(self, name, resolution, color_space):
        """Create a new image for baking"""
        image = bpy.data.images.new(name, resolution, resolution)
        image.colorspace_settings.name = color_space
        self.created_images.append(image)
        return image
    
    def begin_uv_safe_bake(self, obj, materials, target_uv_name=None):
        """Set bake target UV active + render-active, pin only IMPLICIT UV readers
        (empty UV Map nodes, TexCoord UV outputs, image textures with unlinked
        Vector) to the previously render-active UV. Named UV Map nodes are left
        untouched. Returns state for end_uv_safe_bake()."""
        state = {'pins': [], 'orig_render_uv': None}
        uv_layers = obj.data.uv_layers
        if not uv_layers:
            return state

        orig = next((uv for uv in uv_layers if uv.active_render), None)
        orig_name = orig.name if orig else uv_layers[0].name
        state['orig_render_uv'] = orig_name

        target = uv_layers.get(target_uv_name) if target_uv_name else uv_layers.active
        if target is not None:
            uv_layers.active = target
            target.active_render = True

        for mat in materials:
            if not mat or not mat.use_nodes:
                continue
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            for node in list(nodes):
                if node.type == 'GROUP':
                    msg = (f"Material '{mat.name}' contains node group '{node.name}' - "
                           f"UV nodes inside groups are not handled, bake may read wrong UVs")
                    print(f"WARNING: {msg}")
                    try:
                        self.report({'WARNING'}, msg)
                    except Exception:
                        pass
                elif node.type == 'UVMAP' and node.uv_map == "":
                    node.uv_map = orig_name
                    state['pins'].append(('EMPTY_UVMAP', mat, node))
                elif node.type == 'TEX_COORD':
                    uv_out = node.outputs.get('UV')
                    if uv_out and uv_out.is_linked:
                        pin = nodes.new('ShaderNodeUVMap')
                        pin.uv_map = orig_name
                        pin.label = "_glb_uv_pin"
                        pin.location = (node.location.x + 180, node.location.y - 160)
                        consumers = [l.to_socket for l in uv_out.links]
                        for l in list(uv_out.links):
                            links.remove(l)
                        for s in consumers:
                            links.new(pin.outputs['UV'], s)
                        state['pins'].append(('TC_UV', mat, pin, uv_out, consumers))
                elif node.type == 'TEX_IMAGE':
                    vec = node.inputs.get('Vector')
                    if vec and not vec.is_linked:
                        pin = nodes.new('ShaderNodeUVMap')
                        pin.uv_map = orig_name
                        pin.label = "_glb_uv_pin"
                        pin.location = (node.location.x - 250, node.location.y)
                        links.new(pin.outputs['UV'], vec)
                        state['pins'].append(('EMPTY_VECTOR', mat, pin))
        return state

    def end_uv_safe_bake(self, obj, state):
        """Undo begin_uv_safe_bake() in reverse order, restore render UV."""
        for entry in reversed(state.get('pins', [])):
            kind = entry[0]
            try:
                if kind == 'EMPTY_UVMAP':
                    _, mat, node = entry
                    node.uv_map = ""
                elif kind == 'TC_UV':
                    _, mat, pin, uv_out, consumers = entry
                    links = mat.node_tree.links
                    for l in list(pin.outputs['UV'].links):
                        links.remove(l)
                    for s in consumers:
                        links.new(uv_out, s)
                    mat.node_tree.nodes.remove(pin)
                elif kind == 'EMPTY_VECTOR':
                    _, mat, pin = entry
                    links = mat.node_tree.links
                    for l in list(pin.outputs['UV'].links):
                        links.remove(l)
                    mat.node_tree.nodes.remove(pin)
            except Exception as e:
                print(f"Warning: UV pin cleanup failed: {e}")
        orig_name = state.get('orig_render_uv')
        if orig_name and orig_name in obj.data.uv_layers:
            obj.data.uv_layers[orig_name].active_render = True
    
    def bake_channel(self, obj, materials, target_image, bake_type, channel_name, bake_data):
        props = bpy.context.scene.glb_export_props
        
        connections_to_restore = []
        temp_nodes = []
        
        for i, mat in enumerate(materials):
            if not mat.use_nodes:
                continue
            
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            principled = self.get_principled_node(mat)
            output_node = None
            
            for node in nodes:
                if node.type == 'OUTPUT_MATERIAL':
                    output_node = node
                    break
            
            if not principled or not output_node:
                continue
            
            # Store original output connection
            original_output_link = None
            if output_node.inputs['Surface'].is_linked:
                original_output_link = output_node.inputs['Surface'].links[0]
                connections_to_restore.append({
                    'material': mat,
                    'from_socket': original_output_link.from_socket,
                    'to_socket': output_node.inputs['Surface']
                })
            
            # Create texture node for baking target
            tex_node = nodes.new('ShaderNodeTexImage')
            tex_node.image = target_image
            tex_node.select = True
            temp_nodes.append((mat, tex_node))
            
            nodes.active = tex_node
            
            channel_input = principled.inputs[channel_name]
            
            if channel_input.is_linked:
                link = channel_input.links[0]
                from_socket = link.from_socket
                
                # Disconnect from principled and connect to output for baking
                links.remove(link)
                links.new(from_socket, output_node.inputs['Surface'])
                connections_to_restore.append({
                    'material': mat,
                    'from_socket': from_socket,
                    'to_socket': channel_input,
                    'restore_after': True
                })
            else:
                # Handle uniform values
                if channel_name == 'Base Color':
                    value_node = nodes.new('ShaderNodeRGB')
                    value_node.outputs['Color'].default_value = channel_input.default_value
                    links.new(value_node.outputs['Color'], output_node.inputs['Surface'])
                else:
                    value_node = nodes.new('ShaderNodeValue')
                    value_node.outputs['Value'].default_value = channel_input.default_value
                    links.new(value_node.outputs['Value'], output_node.inputs['Surface'])
                
                temp_nodes.append((mat, value_node))
        
        # Ensure all target texture nodes are selected
        for mat, tex_node in temp_nodes:
            if tex_node.type == 'TEX_IMAGE':
                tex_node.select = True
        
        # Perform the bake
        bpy.ops.object.bake(type=bake_type, use_clear=True, margin=props.bake_margin)
        
        # Restore all connections
        for conn in connections_to_restore:
            mat = conn['material']
            if 'restore_uv_map' in conn:
                # Restore UV map selection
                conn['uv_node'].uv_map = conn['original_uv_map']
                print(f"Restored UV Map node to: {conn['original_uv_map']}")
            elif 'restore_after' in conn:
                # Restore node connections
                mat.node_tree.links.new(conn['from_socket'], conn['to_socket'])
            else:
                # Restore regular connections
                mat.node_tree.links.new(conn['from_socket'], conn['to_socket'])
        
        # Clean up temporary nodes
        for mat, node in temp_nodes:
            mat.node_tree.nodes.remove(node)
    
    def bake_normal(self, obj, materials, target_image):
        props = bpy.context.scene.glb_export_props
        
        metallic_data = []
        temp_nodes = []
        
        for mat in materials:
            if not mat.use_nodes:
                continue
                
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            principled = self.get_principled_node(mat)
            
            if not principled:
                continue
            
            tex_node = nodes.new('ShaderNodeTexImage')
            tex_node.image = target_image
            tex_node.select = True
            temp_nodes.append((mat, tex_node))
            
            nodes.active = tex_node
            
            metallic_input = principled.inputs['Metallic']
            if metallic_input.is_linked:
                link = metallic_input.links[0]
                metallic_data.append({
                    'material': mat,
                    'from_socket': link.from_socket,
                    'to_socket': metallic_input,
                    'was_linked': True
                })
                links.remove(link)
            else:
                metallic_data.append({
                    'material': mat,
                    'original_value': metallic_input.default_value,
                    'was_linked': False
                })
            
            metallic_input.default_value = 0.0
        
        bpy.ops.object.bake(type='NORMAL', use_clear=True, margin=props.bake_margin)
        
        for data in metallic_data:
            mat = data['material']
            principled = self.get_principled_node(mat)
            
            if data['was_linked']:
                mat.node_tree.links.new(data['from_socket'], data['to_socket'])
            else:
                principled.inputs['Metallic'].default_value = data['original_value']
        
        for mat, node in temp_nodes:
            mat.node_tree.nodes.remove(node)
    
    def bake_ambient_occlusion(self, obj, target_image, use_clear=True):
        props = bpy.context.scene.glb_export_props
        
        temp_nodes = []
        
        materials = [slot.material for slot in obj.material_slots if slot.material]
        
        for mat in materials:
            if not mat.use_nodes:
                continue
                
            nodes = mat.node_tree.nodes
            
            tex_node = nodes.new('ShaderNodeTexImage')
            tex_node.image = target_image
            tex_node.select = True
            temp_nodes.append((mat, tex_node))
            
            nodes.active = tex_node
        
        original_samples = bpy.context.scene.cycles.samples
        bpy.context.scene.cycles.samples = props.ao_samples
        
        if bpy.context.scene.world and hasattr(bpy.context.scene.world, 'light_settings'):
            bpy.context.scene.world.light_settings.distance = props.ao_distance
        
        # Hide exception objects so they cast no occlusion (never hide the bake target)
        temp_hidden = []
        if props.ao_use_exceptions:
            for other in bpy.context.view_layer.objects:
                if other == obj or other.hide_render:
                    continue
                if other.get("ao_exception"):
                    other.hide_render = True
                    temp_hidden.append(other)
        
        try:
            bpy.ops.object.bake(type='AO', use_clear=use_clear, margin=props.bake_margin)
        finally:
            for other in temp_hidden:
                try:
                    other.hide_render = False
                except ReferenceError:
                    pass
            
            bpy.context.scene.cycles.samples = original_samples
            
            for mat, node in temp_nodes:
                mat.node_tree.nodes.remove(node)

    def separate_ao_combo(self, context, joined_obj, combo, cast_idx, recv_idx):
        """Separate the geometry matching one (no_cast, no_receive) flag
        combination into its own temporary object. Returns it, or None."""
        bpy.ops.object.select_all(action='DESELECT')
        joined_obj.select_set(True)
        context.view_layer.objects.active = joined_obj
        
        before = set(bpy.data.objects)
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_mode(type='VERT')
        bpy.ops.mesh.select_all(action='DESELECT')
        
        bm = bmesh.from_edit_mesh(joined_obj.data)
        deform = bm.verts.layers.deform.active
        if deform is None:
            bpy.ops.object.mode_set(mode='OBJECT')
            return None
        for v in bm.verts:
            dv = v[deform]
            v.select = ((cast_idx in dv, recv_idx in dv) == combo)
        bm.select_flush(True)
        bmesh.update_edit_mesh(joined_obj.data)
        
        bpy.ops.mesh.separate(type='SELECTED')
        bpy.ops.object.mode_set(mode='OBJECT')
        
        new_objs = [o for o in bpy.data.objects if o not in before]
        return new_objs[0] if new_objs else None

    def prepare_ao_parts(self, context, joined_obj):
        """Split AO-exception geometry off the joined object by behavior.
        No Cast geometry must be hidden while other objects bake; No Receive
        geometry is never baked (the AO image starts white, so it stays white).
        Returns (parts, main_no_cast, main_no_receive); each part is a dict
        {'object', 'no_cast', 'no_receive'}. Re-joins happen at the very end."""
        props = context.scene.glb_export_props
        parts = []
        main_no_cast = False
        main_no_receive = False
        
        if not (props.ao_use_exceptions and joined_obj.type == 'MESH'):
            return parts, main_no_cast, main_no_receive
        
        vg_cast = joined_obj.vertex_groups.get("AO_EXCEPT_TMP")
        vg_recv = joined_obj.vertex_groups.get("AO_NORECV_TMP")
        
        if vg_cast is None and vg_recv is None:
            # The join may have inherited tags from one source object
            if "ao_exception" in joined_obj:
                del joined_obj["ao_exception"]
            if "ao_no_receive" in joined_obj:
                del joined_obj["ao_no_receive"]
            return parts, main_no_cast, main_no_receive
        
        cast_idx = vg_cast.index if vg_cast else -1
        recv_idx = vg_recv.index if vg_recv else -1
        
        # Count vertices per flag combination
        combo_counts = {(False, False): 0, (True, False): 0, (False, True): 0, (True, True): 0}
        for v in joined_obj.data.vertices:
            groups = {g.group for g in v.groups}
            combo_counts[(cast_idx in groups, recv_idx in groups)] += 1
        
        present = [c for c, n in combo_counts.items() if n > 0]
        if not present:
            return parts, main_no_cast, main_no_receive
        
        # The biggest chunk stays as the main object, the rest is split off
        main_combo = max(present, key=lambda c: combo_counts[c])
        main_no_cast, main_no_receive = main_combo
        
        for combo in present:
            if combo == main_combo:
                continue
            part = self.separate_ao_combo(context, joined_obj, combo, cast_idx, recv_idx)
            if part is not None:
                nc, nr = combo
                if nc:
                    part["ao_exception"] = True
                    part.hide_render = True   # casts nothing; hidden until the final re-join
                else:
                    part.hide_render = False  # keeps casting onto everything
                if nr:
                    part["ao_no_receive"] = True
                parts.append({'object': part, 'no_cast': nc, 'no_receive': nr})
                self.deferred_ao_parts.append({'main': joined_obj, 'part': part, 'uv_name': None})
        
        # Normalize the main object's own tags to its combo
        if main_no_cast:
            joined_obj["ao_exception"] = True
        elif "ao_exception" in joined_obj:
            del joined_obj["ao_exception"]
        if main_no_receive:
            joined_obj["ao_no_receive"] = True
        elif "ao_no_receive" in joined_obj:
            del joined_obj["ao_no_receive"]
        
        bpy.ops.object.select_all(action='DESELECT')
        joined_obj.select_set(True)
        context.view_layer.objects.active = joined_obj
        
        return parts, main_no_cast, main_no_receive

    def run_ao_bakes(self, context, joined_obj, ao_image, ao_parts, main_no_receive):
        """Bake AO onto every piece that receives it. Non-receiving geometry
        is simply not baked - the AO image is initialized white, so its UV
        islands stay pure white."""
        bake_uv = joined_obj.data.uv_layers.active.name if joined_obj.data.uv_layers.active else None
        if not main_no_receive:
            bpy.ops.object.select_all(action='DESELECT')
            joined_obj.select_set(True)
            context.view_layer.objects.active = joined_obj
            self.bake_ambient_occlusion(joined_obj, ao_image, use_clear=False)
        
        for entry in ao_parts:
            if entry['no_receive']:
                continue
            part = entry['object']
            if part.name not in bpy.data.objects:
                continue
            was_hidden = part.hide_render
            part.hide_render = False
            bpy.ops.object.select_all(action='DESELECT')
            part.select_set(True)
            context.view_layer.objects.active = part
            if bake_uv and bake_uv in part.data.uv_layers:
                part.data.uv_layers.active = part.data.uv_layers[bake_uv]
            
            self.bake_ambient_occlusion(part, ao_image, use_clear=False)
            
            part.hide_render = was_hidden
        
        bpy.ops.object.select_all(action='DESELECT')
        joined_obj.select_set(True)
        context.view_layer.objects.active = joined_obj

    def prepare_custom_uv_object(self, joined_obj, pobj, uv_name):
        """Prepare a custom-UV object for merging into the main object:
        its chosen UV map becomes a pinned layer named 'UVMap' (the bake
        layer), so the packer preserves its island rotation."""
        mesh = pobj.data
        if uv_name not in mesh.uv_layers:
            if mesh.uv_layers.active is None:
                return
            uv_name = mesh.uv_layers.active.name
        
        # Align default-sampling layer name with the main object's render layer,
        # so textures on this object still sample correctly after the join
        if joined_obj is not None:
            render_name = next((l.name for l in joined_obj.data.uv_layers if l.active_render), None)
            if render_name and render_name != "UVMap" and render_name not in mesh.uv_layers:
                p_render = next((l for l in mesh.uv_layers if l.active_render), None)
                if p_render is not None:
                    if uv_name == p_render.name:
                        uv_name = render_name
                    p_render.name = render_name
        
        # Free up the name "UVMap" for the bake layer (unless the chosen map IS "UVMap")
        if "UVMap" in mesh.uv_layers and uv_name != "UVMap":
            existing = [l.name for l in mesh.uv_layers]
            n = 1
            while f"UVMap_C{n:02d}" in existing:
                n += 1
            mesh.uv_layers["UVMap"].name = f"UVMap_C{n:02d}"
        
        if uv_name == "UVMap":
            bake_layer = mesh.uv_layers["UVMap"]
        else:
            src = mesh.uv_layers[uv_name]
            src_uvs = [0.0] * (len(mesh.loops) * 2)
            src.data.foreach_get("uv", src_uvs)
            bake_layer = mesh.uv_layers.new(name="UVMap", do_init=False)
            bake_layer.data.foreach_set("uv", src_uvs)
        
        # Pin every UV so the packer preserves island rotation
        for d in bake_layer.data:
            d.pin_uv = True
        
        mesh.uv_layers.active = bake_layer

    def merge_deferred_ao_parts(self, context):
        """After ALL collections are baked, re-join deferred AO-exception
        geometry into its final object and strip helper tags"""
        props = context.scene.glb_export_props
        
        for entry in getattr(self, 'deferred_ao_parts', []):
            main = entry['main']
            part = entry['part']
            uv_name = entry['uv_name']
            try:
                if not (main and main.name in bpy.data.objects):
                    continue
                if not (part and part.name in bpy.data.objects):
                    continue
            except ReferenceError:
                continue
            
            try:
                part.hide_render = False
                
                if uv_name:
                    if uv_name not in main.data.uv_layers:
                        main.data.uv_layers.new(name=uv_name)
                else:
                    # Split-off part: drop UV layers the main object no longer has
                    main_uvs = {uv.name for uv in main.data.uv_layers}
                    for name in [uv.name for uv in part.data.uv_layers]:
                        if name not in main_uvs:
                            part.data.uv_layers.remove(part.data.uv_layers[name])
                
                bpy.ops.object.select_all(action='DESELECT')
                part.select_set(True)
                main.select_set(True)
                context.view_layer.objects.active = main
                bpy.ops.object.join()
                print(f"Merged AO-exception part back into {main.name}")
            except Exception as e:
                print(f"Warning: could not merge deferred AO part: {e}")
        
        self.deferred_ao_parts = []
        
        if props.ao_use_exceptions:
            for obj in getattr(self, 'processed_objects', []):
                try:
                    if obj and obj.name in bpy.data.objects:
                        if obj.type == 'MESH':
                            vg = obj.vertex_groups.get("AO_EXCEPT_TMP")
                            if vg:
                                obj.vertex_groups.remove(vg)
                            vg = obj.vertex_groups.get("AO_NORECV_TMP")
                            if vg:
                                obj.vertex_groups.remove(vg)
                        if "ao_exception" in obj:
                            del obj["ao_exception"]
                        if "ao_no_receive" in obj:
                            del obj["ao_no_receive"]
                except ReferenceError:
                    pass

    def create_gltf_output_node(self, material, ao_image, uv_map_name=None):
        """Create glTF Material Output node and connect AO"""
        if not material.use_nodes:
            return
            
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        
        gltf_node = None
        for node in nodes:
            if node.type == 'GROUP' and node.node_tree and node.node_tree.name == "glTF Material Output":
                gltf_node = node
                break
        
        if not gltf_node:
            if "glTF Material Output" not in bpy.data.node_groups:
                node_group = bpy.data.node_groups.new(name="glTF Material Output", type='ShaderNodeTree')
                
                node_group.interface.new_socket(name="Occlusion", in_out='INPUT', socket_type='NodeSocketFloat')
                
                group_input = node_group.nodes.new('NodeGroupInput')
                group_input.location = (0, 0)
                
                group_output = node_group.nodes.new('NodeGroupOutput')
                group_output.location = (200, 0)
            
            gltf_node = nodes.new('ShaderNodeGroup')
            gltf_node.node_tree = bpy.data.node_groups["glTF Material Output"]
            gltf_node.location = (300, -300)
            gltf_node.name = "glTF Material Output"
        
        ao_tex_node = None
        for node in nodes:
            if node.type == 'TEX_IMAGE' and node.image == ao_image:
                ao_tex_node = node
                break
        
        if not ao_tex_node:
            ao_tex_node = nodes.new('ShaderNodeTexImage')
            ao_tex_node.image = ao_image
            ao_tex_node.location = (0, -300)
            
            # If a specific UV map was requested, pin the texture's Vector to it
            if uv_map_name:
                uv_node = nodes.new('ShaderNodeUVMap')
                uv_node.uv_map = uv_map_name
                uv_node.location = (-250, -300)
                links.new(uv_node.outputs['UV'], ao_tex_node.inputs['Vector'])

        links.new(ao_tex_node.outputs['Color'], gltf_node.inputs['Occlusion'])

def natural_sort_key(text):
    """Generate a key for natural sorting that handles numbers properly"""
    def atoi(text):
        return int(text) if text.isdigit() else text
    return [atoi(c) for c in re.split(r'(\d+)', text)]


class GLB_OT_ImportBlendFiles(Operator):
    """Import all blend files from selected folder into organized collections"""
    bl_idname = "glb_export.import_blend_files"
    bl_label = "Import Blend Files"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        import_path = bpy.path.abspath(context.scene.glb_export_props.import_folder_path)
        
        if not import_path or not os.path.exists(import_path):
            self.report({'ERROR'}, "Please select a valid folder")
            return {'CANCELLED'}
        
        blend_files = sorted([f for f in os.listdir(import_path) if f.endswith('.blend')], key=natural_sort_key)
        
        if not blend_files:
            self.report({'WARNING'}, "No .blend files found in selected folder")
            return {'CANCELLED'}
        
        imported_count = 0
        skipped_count = 0
        
        for blend_file in blend_files:
            collection_name = blend_file.replace('.blend', '')
            
            if collection_name in bpy.data.collections:
                print(f"Skipping {blend_file} - collection '{collection_name}' already exists")
                skipped_count += 1
                continue
            
            filepath = os.path.join(import_path, blend_file)
            
            new_collection = bpy.data.collections.new(name=collection_name)
            context.scene.collection.children.link(new_collection)
            
            try:
                before_collections = set(bpy.data.collections[:])
                before_objects = set(bpy.data.objects[:])
                before_materials = set(bpy.data.materials[:])
                
                with bpy.data.libraries.load(filepath, link=False) as (data_from, data_to):
                    data_to.collections = data_from.collections[:]
                    data_to.objects = data_from.objects[:]
                    data_to.materials = data_from.materials[:]
                
                after_collections = set(bpy.data.collections[:]) - before_collections
                after_objects = set(bpy.data.objects[:]) - before_objects
                after_materials = set(bpy.data.materials[:]) - before_materials
                
                if after_collections:
                    root_collections = []
                    for col in after_collections:
                        is_child_of_imported = False
                        for other_col in after_collections:
                            if other_col != col:
                                for child in other_col.children:
                                    if child.name == col.name:
                                        is_child_of_imported = True
                                        break
                            if is_child_of_imported:
                                break
                        
                        if not is_child_of_imported:
                            root_collections.append(col)
                    
                    for col in root_collections:
                        try:
                            if col.name in context.scene.collection.children:
                                context.scene.collection.children.unlink(col)
                        except:
                            pass
                        
                        if col not in new_collection.children[:]:
                            new_collection.children.link(col)
                else:
                    for obj in after_objects:
                        if obj not in new_collection.objects[:]:
                            new_collection.objects.link(obj)
                        
                        try:
                            if obj.name in context.scene.collection.objects:
                                context.scene.collection.objects.unlink(obj)
                        except:
                            pass
                
                for obj in after_objects:
                    try:
                        if obj.name in context.scene.collection.objects:
                            context.scene.collection.objects.unlink(obj)
                    except:
                        pass
                
                print(f"Imported {blend_file} into collection '{collection_name}'")
                print(f"  - {len(after_collections)} collections")
                print(f"  - {len(after_objects)} objects")
                print(f"  - {len(after_materials)} materials")
                
                # Clear render visibility keyframes and enable rendering for all imported items
                for obj in after_objects:
                    # Remove animation data for hide_render and hide_viewport
                    if obj.animation_data:
                        if obj.animation_data.action:
                            fcurves_to_remove = []
                            for fcurve in obj.animation_data.action.fcurves:
                                if fcurve.data_path == "hide_render" or fcurve.data_path == "hide_viewport":
                                    fcurves_to_remove.append(fcurve)
                            for fcurve in fcurves_to_remove:
                                obj.animation_data.action.fcurves.remove(fcurve)
                    
                    # Enable rendering and visibility
                    obj.hide_render = False      # Camera icon - enabled
                    obj.hide_viewport = False    # Monitor icon - enabled
                    obj.hide_set(False)          # Eye icon - visible
                    
                    # Enable rendering
                    obj.hide_render = False
                    obj.hide_viewport = False

                for col in after_collections:
                    # Collections don't have animation_data, just set visibility directly
                    col.hide_render = False
                    col.hide_viewport = False

                print(f"  - Cleared render visibility keyframes and enabled rendering")
                
                imported_count += 1
                
            except Exception as e:
                self.report({'ERROR'}, f"Failed to import {blend_file}: {str(e)}")
                if new_collection and not new_collection.objects and not new_collection.children:
                    try:
                        bpy.data.collections.remove(new_collection)
                    except:
                        pass
        
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        
        context.view_layer.update()
        
        self.report({'INFO'}, f"Imported {imported_count} files, skipped {skipped_count} existing")
        return {'FINISHED'}


class GLB_OT_SelectImportFolder(Operator):
    """Select folder containing blend files to import"""
    bl_idname = "glb_export.select_import_folder"
    bl_label = "Select Import Folder"
    
    directory: StringProperty(
        name="Directory",
        description="Directory to import from"
    )
    
    filter_folder: BoolProperty(
        default=True,
        options={'HIDDEN'}
    )
    
    def execute(self, context):
        context.scene.glb_export_props.import_folder_path = self.directory
        return {'FINISHED'}
    
    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}
    
    
class GLB_OT_ClearImportPath(Operator):
    """Clear the import folder path"""
    bl_idname = "glb_export.clear_import_path"
    bl_label = "Clear Path"
    
    def execute(self, context):
        context.scene.glb_export_props.import_folder_path = ""
        return {'FINISHED'}

# === PANELS ===

class GLB_PT_ExportPanel(Panel):
    """Main panel for GLB Export Tool"""
    bl_label = "Collection(s) to GLB"
    bl_idname = "GLB_PT_export_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Col2GLB"
    
    def draw(self, context):
        layout = self.layout
        props = context.scene.glb_export_props
        
        # Import section at the top
        import_box = layout.box()
        import_col = import_box.column()
        row = import_col.row(align=True)
        row.prop(props, "show_import_blend",
                 icon='TRIA_DOWN' if props.show_import_blend else 'TRIA_RIGHT',
                 icon_only=True, emboss=False)
        row.label(text="Import Blend Files", icon='IMPORT')
        
        if props.show_import_blend:
            row = import_col.row(align=True)
            row.prop(props, "import_folder_path", text="")
            if props.import_folder_path:
                row.operator("glb_export.clear_import_path", icon='X', text="")
            
            import_col.operator("glb_export.import_blend_files", text="Import Files", icon='IMPORT')
        
        # UV Unwrap settings
        layout.separator()
        box = layout.box()
        row = box.row()
        row.prop(props, "show_uv", icon='TRIA_DOWN' if props.show_uv else 'TRIA_RIGHT', icon_only=True, emboss=False)
        row.label(text="UV Unwrap Options")
        if props.show_uv:
            uv_box = box.box()
            uv_col = uv_box.column(align=False)
            
            # UV Method dropdown
            row = uv_col.row(align=True)
            row.prop(props, "uv_unwrap_method", text="")
            
            uv_col.separator(factor=0.5)
            
            # In the UV Unwrap Options section, after the method dropdown:
            if props.uv_unwrap_method == 'MOF':
                # Check if MOF file exists
                addon_dir = os.path.dirname(os.path.realpath(__file__))
                mof_zip_path = os.path.join(addon_dir, "resources", "MinistryOfFlat_Release.zip")
                
                if not os.path.exists(mof_zip_path):
                    error_box = uv_col.box()
                    error_col = error_box.column()
                    error_col.alert = True  # Makes text red
                    error_col.label(text="MOF file missing!", icon='ERROR')
                    error_col.label(text="Place MinistryOfFlat_Release.zip in:")
                    error_col.label(text="addon/resources/ folder")
            
            # Show settings based on selected method
            if props.uv_unwrap_method == 'SMART':
                row = uv_col.row(align=True)
                row.prop(props, "show_smart_settings",
                         icon='TRIA_DOWN' if props.show_smart_settings else 'TRIA_RIGHT',
                         icon_only=True, emboss=False)
                row.label(text="Smart UV Project Settings")
                if props.show_smart_settings:
                    row = uv_col.row(align=True)
                    row.prop(props, "uv_angle_limit")
                    
                    row = uv_col.row(align=True)
                    row.prop(props, "uv_margin_method", text="")
                    
                    row = uv_col.row(align=True)
                    row.prop(props, "uv_rotation_method", text="")
                    
                    row = uv_col.row(align=True)
                    row.prop(props, "uv_island_margin")
                    
                    row = uv_col.row(align=True)
                    row.prop(props, "uv_area_weight")
                    
                    row = uv_col.row(align=True)
                    row.prop(props, "uv_correct_aspect")
                    
                    row = uv_col.row(align=True)
                    row.prop(props, "uv_scale_to_bounds")
            
            elif props.uv_unwrap_method == 'MOF':
                row = uv_col.row(align=True)
                row.prop(props, "show_mof_settings",
                         icon='TRIA_DOWN' if props.show_mof_settings else 'TRIA_RIGHT',
                         icon_only=True, emboss=False)
                row.label(text="MOF General Settings")
                if props.show_mof_settings:
                    row = uv_col.row(align=True)
                    row.prop(props, "mof_separate_hard_edges")
                    
                    row = uv_col.row(align=True)
                    row.prop(props, "mof_separate_marked_edges")
                    
                    row = uv_col.row(align=True)
                    row.prop(props, "mof_overlap_identical")
                    
                    row = uv_col.row(align=True)
                    row.prop(props, "mof_overlap_mirrored")
                    
                    row = uv_col.row(align=True)
                    row.prop(props, "mof_world_scale")
                    
                    row = uv_col.row(align=True)
                    row.prop(props, "mof_use_normals")
                    
                    row = uv_col.row(align=True)
                    row.prop(props, "mof_suppress_validation")
                    
                    row = uv_col.row(align=True)
                    row.prop(props, "mof_smooth")
                    
                    row = uv_col.row(align=True)
                    row.prop(props, "mof_keep_original")
                    
                    row = uv_col.row(align=True)
                    row.prop(props, "mof_triangulate")
            
            # Packing checkbox with expand arrow
            uv_col.separator()
            row = uv_col.row(align=True)
            if props.uv_unwrap_method == 'MOF':
                row.label(text="Pack UVs", icon='CHECKBOX_HLT')
            else:
                row.prop(props, "enable_uv_pack")

            # Always show expand arrow when pack is enabled
            if props.enable_uv_pack:
                row.prop(props, "show_packing_settings",
                        text="",
                        icon='TRIA_DOWN' if props.show_packing_settings else 'TRIA_RIGHT',
                        emboss=False)
            
            # Show packing settings if enabled and expanded
            if props.enable_uv_pack and props.show_packing_settings:
                # Use the same uv_col, no new box
                uv_col.separator(factor=0.5)
                
                # Shape Method
                row = uv_col.row(align=True)
                row.prop(props, "pack_shape_method", text="")
                
                # Scale checkbox
                row = uv_col.row(align=True)
                row.prop(props, "pack_scale")
                
                # Rotate checkbox
                row = uv_col.row(align=True)
                row.prop(props, "pack_rotate")
                
                # Rotation Method (only if rotate is enabled)
                if props.pack_rotate:
                    row = uv_col.row(align=True)
                    row.prop(props, "pack_rotation_method", text="")
                
                # Margin Method
                row = uv_col.row(align=True)
                row.prop(props, "pack_margin_method", text="")
                
                # Margin value
                row = uv_col.row(align=True)
                row.prop(props, "pack_margin")
                
                # Lock Pinned Islands
                row = uv_col.row(align=True)
                row.prop(props, "pack_lock_pinned")
                
                # Lock Method (only if pin is enabled)
                if props.pack_lock_pinned:
                    row = uv_col.row(align=True)
                    row.prop(props, "pack_lock_method", text="")
                
                # Merge Overlapping
                row = uv_col.row(align=True)
                row.prop(props, "pack_merge_overlapping")
                
                # Pack to
                row = uv_col.row(align=True)
                row.prop(props, "pack_udim_target", text="")

            # Unwrap button
            uv_col.separator()
            row = uv_col.row()
            row.scale_y = 1.3
            if _UNWRAP_RUNNING:
                row.alert = True
                row.operator("glb_export.unwrap_cancel",
                             text="Unwrapping...  Click or ESC to cancel", icon='X')
            else:
                row.operator("glb_export.unwrap_selected", icon='UV')

        # Custom UV Bake section
        layout.separator()
        box = layout.box()
        row = box.row()
        row.prop(props, "show_custom_uv_bake",
                 icon='TRIA_DOWN' if props.show_custom_uv_bake else 'TRIA_RIGHT',
                 icon_only=True, emboss=False)
        row.label(text="Custom UV Bake (per-object)")
        if props.show_custom_uv_bake:
            box.prop(props, "enable_custom_uv_bake")

            if props.enable_custom_uv_bake:
                box.operator("glb_export.scan_custom_uv_targets", icon='VIEWZOOM')

                list_row = box.row()
                list_row.template_list(
                    "GLB_UL_CustomUVBakeTargets", "",
                    props, "custom_uv_bake_targets",
                    props, "custom_uv_bake_index",
                    rows=3,
                )

                btn_col = list_row.column(align=True)
                btn_col.operator("glb_export.add_custom_uv_target", icon='ADD', text="")
                btn_col.operator("glb_export.remove_custom_uv_target", icon='REMOVE', text="")

        # Baking settings
        layout.separator()
        box = layout.box()
        row = box.row()
        row.prop(props, "show_baking", icon='TRIA_DOWN' if props.show_baking else 'TRIA_RIGHT', icon_only=True, emboss=False)
        row.label(text="Material Baking")
        if props.show_baking:
            def switch_row(parent, prop_id, label):
                sp = parent.row().split(factor=0.4)
                lab = sp.row()
                lab.alignment = 'RIGHT'
                lab.label(text=label)
                sp.prop(props, prop_id,
                        text="On" if getattr(props, prop_id) else "Off",
                        toggle=True)

            def num_row(parent, prop_id, label):
                sp = parent.row(align=True).split(factor=0.4)
                lab = sp.row()
                lab.alignment = 'RIGHT'
                lab.label(text=label)
                fr = sp.row(align=True)
                o = fr.operator("glb_export.halve_double", text="", icon='TRIA_LEFT')
                o.prop_name = prop_id
                o.double = False
                fr.prop(props, prop_id, text="")
                o = fr.operator("glb_export.halve_double", text="", icon='TRIA_RIGHT')
                o.prop_name = prop_id
                o.double = True

            switch_row(box, "enable_baking", "Bake Materials")

            if props.enable_baking or props.bake_ambient_occlusion:
                col = box.column(align=True)
                num_row(col, "bake_resolution", "Resolution")
                num_row(col, "bake_samples", "Samples")
                num_row(col, "bake_margin", "Margin")

            switch_row(box, "bake_ambient_occlusion", "Ambient Occlusion")
            if props.bake_ambient_occlusion:
                ao_col = box.column(align=True)
                num_row(ao_col, "ao_samples", "AO Samples")
                num_row(ao_col, "ao_distance", "AO Distance")

                # AO Exceptions (expandable)
                row = box.row()
                row.prop(props, "show_ao_exceptions",
                         icon='TRIA_DOWN' if props.show_ao_exceptions else 'TRIA_RIGHT',
                         icon_only=True, emboss=False)
                row.prop(props, "ao_use_exceptions")
                if props.show_ao_exceptions:
                    sub = box.column()
                    sub.enabled = props.ao_use_exceptions
                    sub.operator("glb_export.add_selected_ao_exceptions", icon='RESTRICT_SELECT_OFF')
                    list_row = sub.row()
                    list_row.template_list(
                        "GLB_UL_AOExceptions", "",
                        props, "ao_exception_objects",
                        props, "ao_exception_index",
                        rows=3,
                    )
                    btn_col = list_row.column(align=True)
                    btn_col.operator("glb_export.add_ao_exception", icon='ADD', text="")
                    btn_col.operator("glb_export.remove_ao_exception", icon='REMOVE', text="")

                prow = box.row(align=True)
                on_w = bool(_AO_PREVIEW and _AO_PREVIEW.get("mode") == 'WHITE')
                on_t = bool(_AO_PREVIEW and _AO_PREVIEW.get("mode") == 'TEXTURED')
                prow.operator("glb_export.ao_preview",
                              text="Stop Preview" if on_w else "Preview AO",
                              icon='SHADING_SOLID', depress=on_w)
                prow.operator("glb_export.ao_preview_textured",
                              text="Stop Preview" if on_t else "Preview Textured",
                              icon='SHADING_TEXTURE', depress=on_t)

            if props.enable_baking:
                box.separator()
                row = box.row(align=True)
                row.prop(props, "show_alpha_mode",
                         icon='TRIA_DOWN' if props.show_alpha_mode else 'TRIA_RIGHT',
                         icon_only=True, emboss=False)
                row.label(text="Alpha Mode")
                if props.show_alpha_mode:
                    acol = box.column()
                    acol.prop(props, "alpha_mode", text="Default")
                    if props.alpha_mode == 'MASK':
                        acol.prop(props, "alpha_threshold")
                    acol.operator("glb_export.scan_alpha_collections", icon='VIEWZOOM')
                    for item in props.alpha_collections:
                        if not item.collection_ref:
                            continue
                        row = acol.row(align=True)
                        row.label(text=item.collection_ref.name, icon='OUTLINER_COLLECTION')
                        row.prop(item, "alpha_mode", text="")
                        if item.alpha_mode == 'MASK':
                            row.prop(item, "alpha_threshold", text="")
                        row.prop(item, "double_sided", text="2-Sided", toggle=True)
        
        # Export settings
        layout.separator()
        box = layout.box()
        row = box.row()
        row.prop(props, "show_export", icon='TRIA_DOWN' if props.show_export else 'TRIA_RIGHT', icon_only=True, emboss=False)
        row.label(text="Export Settings")
        if props.show_export:
            box.prop(props, "export_enabled")
        
            if props.export_enabled:
                box.prop(props, "export_path", text="")
        
        # Process button
        layout.separator()
        col = layout.column()
        col.scale_y = 2.0
        
        if props.export_running:
            col.progress(factor=props.export_progress, type='BAR',
                         text=props.export_status)
            hint = layout.row()
            hint.alignment = 'CENTER'
            hint.label(text="ESC to cancel (works between steps)", icon='INFO')
        else:
            button_text = "Process Visible Collections"
            if props.export_enabled:
                button_text = "Process & Export"
            col.operator("glb_export.process_export", text=button_text, icon='PLAY')
            report_row = layout.row()
            report_row.alignment = 'RIGHT'
            report_row.operator("glb_export.show_report",
                                text="Last Export Report", icon='INFO')


# ============================================================
#  NORMAL BAKE: HIGH -> LOW  (selected-to-active, v1.5.0)
# ============================================================
import numpy as _np
from math import acos as _acos, degrees as _degrees

_nbake_state = {"shell": None, "lines": {}, "pts": {}, "budget": [],
                "stats": "", "advice": [], "counts": (0, 0)}
_nbake_draw_handle = None
_NBAKE_COLS = {"good": (0.1, 0.85, 0.25), "minor": (1.0, 0.65, 0.05),
               "bad": (1.0, 0.15, 0.1), "miss": (0.6, 0.6, 0.6)}

def _nbake_redraw():
    for w in bpy.context.window_manager.windows:
        for a in w.screen.areas:
            if a.type == 'VIEW_3D':
                a.tag_redraw()

def _nbake_world_samples(obj, dg, limit=400):
    ob = obj.evaluated_get(dg)
    me = ob.to_mesh()
    mw = ob.matrix_world
    nmat = mw.inverted_safe().transposed().to_3x3()
    polys, verts = me.polygons, me.vertices
    step = max(1, len(polys) // limit)
    out = []
    for i in range(0, len(polys), step):
        p = polys[i]
        nf = (nmat @ p.normal).normalized()
        sm = Vector((0, 0, 0))
        for vi in p.vertices:
            sm += verts[vi].normal
        nsm = (nmat @ sm).normalized() if sm.length > 1e-9 else nf
        out.append((mw @ p.center, nf, nsm))
    ob.to_mesh_clear()
    return out

def _nbake_gather(context, limit=400):
    p = context.scene.glb_nbake_props
    dg = context.evaluated_depsgraph_get()
    raw = _nbake_world_samples(p.target, dg, limit)
    src = p.source.evaluated_get(dg)
    inv = src.matrix_world.inverted_safe()
    i3 = inv.to_3x3()
    nmat = inv.transposed().to_3x3()
    mw = src.matrix_world
    diag = max(p.target.dimensions.length, 1e-6)
    data = []
    for co, nf, nsm in raw:
        best = bd = tu = tdn = bn = None
        for sgn in (1.0, -1.0):
            hit, loc, nor, idx = src.ray_cast(inv @ co, (i3 @ (nf * sgn)).normalized(), distance=1.0e18)
            if hit:
                w = mw @ loc
                t = (w - co).length
                if t <= diag * 0.35:
                    if sgn > 0: tu = t
                    else: tdn = t
                    if bd is None or t < bd:
                        bd = t; best = w
                        bn = (nmat @ nor).normalized()
        data.append((co, nf, nsm, best, bd, tu, tdn, bn))
    return {"data": data, "src": src, "inv": inv, "i3": i3,
            "nmat": nmat, "mw": mw, "diag": diag}

def _nbake_classify(g, ext, dist, cage):
    src, inv, i3 = g["src"], g["inv"], g["i3"]
    nmat, mw, diag = g["nmat"], g["mw"], g["diag"]
    D = dist if (dist > 0.0 and not cage) else 1.0e18
    eps = diag * 0.004
    L = {"good": [], "minor": [], "bad": []}
    P = {"good": [], "minor": [], "bad": [], "miss": []}
    B = []
    ngood = nminor = nsev = nh = nbur = nfar = noov = 0
    for co, nf, nsm, best, bd, tu, tdn, bn in g["data"]:
        nd = nsm if cage else nf
        o = co + nd * ext
        dw = -nd
        if best is None:
            noov += 1
        hit, loc, nor, idx = src.ray_cast(inv @ o, (i3 @ dw).normalized(), distance=1.0e18)
        ok = False
        if hit:
            wloc = mw @ loc
            t = (wloc - o).length
            if t <= D:
                wn = (nmat @ nor).normalized()
                backface = wn.dot(dw) > 0.0
                if best is None or bn is None:
                    k = "bad"; nsev += 1; nfar += 1
                else:
                    ang = _degrees(_acos(max(-1.0, min(1.0, wn.dot(bn)))))
                    perr = (wloc - best).length
                    if backface or ang > 25.0 or perr > diag * 0.05:
                        k = "bad"; nsev += 1
                        if backface and t < ext * 1.5: nbur += 1
                        else: nfar += 1
                    elif ang > 8.0 or perr > max(diag * 0.012, (bd or 0.0) * 0.75):
                        k = "minor"; nminor += 1
                    else:
                        k = "good"; ngood += 1
                L[k].append(o); L[k].append(wloc)
                P[k].append(wloc + nd * eps)
                ok = True
        if not ok:
            nh += 1
            P["miss"].append(co + nd * eps)
            bend = o + dw * (D if D < 1.0e17 else ext + diag * 0.35)
            B.append(o); B.append(bend)
    return {"lines": L, "pts": P, "budget": B, "g": ngood, "minor": nminor,
            "w": nsev, "h": nh, "buried": nbur, "far": nfar, "nooverlap": noov}

def _nbake_run_rays(context):
    p = context.scene.glb_nbake_props
    st = _nbake_state
    st["lines"] = {}; st["pts"] = {}; st["budget"] = []; st["stats"] = ""; st["advice"] = []
    if not (p.source and p.target and p.show_rays):
        return
    g = _nbake_gather(context, 400)
    r = _nbake_classify(g, p.extrusion, p.max_ray_distance, p.use_cage)
    st["lines"], st["pts"], st["budget"] = r["lines"], r["pts"], r["budget"]
    tot = max(1, r["g"] + r["minor"] + r["w"] + r["h"])
    st["stats"] = "%d clean / %d minor / %d SEVERE / %d holes" % (r["g"], r["minor"], r["w"], r["h"])
    adv = []
    if r["nooverlap"] > tot * 0.5:
        adv.append(("warn", "Objects barely overlap - are they aligned?"))
    if r["w"] == 0 and r["h"] == 0:
        adv.append(("ok", "No severe errors - bake will look clean"))
        if r["minor"] > 0:
            adv.append(("info", "%d minor deviations - invisible in render" % r["minor"]))
    else:
        if r["h"] > 0:
            adv.append(("warn", "Gray holes: raise Max ray distance" if not p.use_cage else "Gray holes: raise Extrusion"))
        if r["buried"] > 0:
            adv.append(("warn", "%d buried: RAISE Extrusion" % r["buried"]))
        if r["far"] > 0:
            adv.append(("warn", "%d far-grabs: LOWER Extrusion" % r["far"]))
        if r["buried"] > 0 and r["far"] > 0:
            adv.append(("info", "Both kinds: use Bake exploded"))
    st["advice"] = adv

def _nbake_build_shell(context):
    p = context.scene.glb_nbake_props
    _nbake_state["shell"] = None
    t = p.target
    if not (t and t.type == 'MESH'):
        return
    dg = context.evaluated_depsgraph_get()
    ob = t.evaluated_get(dg)
    me = ob.to_mesh()
    mw = ob.matrix_world
    nmat = mw.inverted_safe().transposed().to_3x3()
    e = p.extrusion
    vs = [(mw @ v.co) + (nmat @ v.normal).normalized() * e for v in me.vertices]
    lines = []
    step = max(1, len(me.edges) // 30000)
    for i in range(0, len(me.edges), step):
        a, b = me.edges[i].vertices
        lines.append(vs[a]); lines.append(vs[b])
    ob.to_mesh_clear()
    if lines:
        import gpu
        sh = gpu.shader.from_builtin('UNIFORM_COLOR')
        from gpu_extras.batch import batch_for_shader
        _nbake_state["shell"] = (sh, batch_for_shader(sh, 'LINES', {"pos": lines}))

def _nbake_draw_callback():
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
        p = bpy.context.scene.glb_nbake_props
        st = _nbake_state
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('LESS_EQUAL')
        sh = gpu.shader.from_builtin('UNIFORM_COLOR')
        if p.show_shell and st.get("shell"):
            s2, b = st["shell"]
            gpu.state.line_width_set(1.0)
            s2.bind(); s2.uniform_float("color", (0.2, 0.7, 1.0, 0.3))
            b.draw(s2)
        if st.get("budget") and p.show_rays:
            gpu.state.line_width_set(1.0)
            b = batch_for_shader(sh, 'LINES', {"pos": st["budget"]})
            sh.bind(); sh.uniform_float("color", (0.85, 0.85, 0.85, 0.10))
            b.draw(sh)
        gpu.state.line_width_set(1.8)
        for k in ("good", "minor", "bad"):
            ln = st["lines"].get(k) if st.get("lines") else None
            if ln:
                b = batch_for_shader(sh, 'LINES', {"pos": ln})
                sh.bind(); sh.uniform_float("color", _NBAKE_COLS[k] + (0.6,))
                b.draw(sh)
        for k, sz in (("miss", 4.5), ("good", 5.5), ("minor", 6.5), ("bad", 7.5)):
            pt = st["pts"].get(k) if st.get("pts") else None
            if pt:
                gpu.state.point_size_set(sz)
                b = batch_for_shader(sh, 'POINTS', {"pos": pt})
                sh.bind(); sh.uniform_float("color", _NBAKE_COLS[k] + (1.0,))
                b.draw(sh)
        gpu.state.blend_set('NONE')
        gpu.state.line_width_set(1.0)
        gpu.state.point_size_set(1.0)
        gpu.state.depth_test_set('NONE')
    except Exception:
        pass

def _nbake_poll_mesh(self, obj):
    return obj.type == 'MESH'

def _nbake_eval_polycount(obj, dg):
    ob = obj.evaluated_get(dg)
    me = ob.to_mesh()
    n = len(me.polygons)
    ob.to_mesh_clear()
    return n

def _nbake_upd(self, context):
    _nbake_build_shell(context)
    _nbake_run_rays(context)
    _nbake_redraw()

def _nbake_upd_full(self, context):
    _nbake_upd(self, context)
    try:
        dg = context.evaluated_depsgraph_get()
        p = context.scene.glb_nbake_props
        s = _nbake_eval_polycount(p.source, dg) if p.source else 0
        t = _nbake_eval_polycount(p.target, dg) if p.target else 0
        _nbake_state["counts"] = (s, t)
    except Exception:
        _nbake_state["counts"] = (0, 0)

class GLBNormalBakeProps(PropertyGroup):
    source: PointerProperty(type=bpy.types.Object, name="High-poly", poll=_nbake_poll_mesh, update=_nbake_upd_full)
    target: PointerProperty(type=bpy.types.Object, name="Low-poly", poll=_nbake_poll_mesh, update=_nbake_upd_full)
    extrusion: FloatProperty(name="Extrusion", description="How far above the surface rays start",
                             default=0.05, min=0.0, soft_max=1.0, subtype='DISTANCE', update=_nbake_upd)
    max_ray_distance: FloatProperty(name="Max ray distance", description="How far rays may travel (0 = unlimited)",
                                    default=0.15, min=0.0, soft_max=3.0, subtype='DISTANCE', update=_nbake_upd)
    use_cage: BoolProperty(name="Cage (fixes hard edges)", default=True, update=_nbake_upd)
    show_shell: BoolProperty(name="Show ray-start shell", default=False, update=lambda s, c: _nbake_redraw())
    show_rays: BoolProperty(name="Live ray preview", default=False, update=_nbake_upd)
    resolution: EnumProperty(name="Resolution",
                             items=[('512', '512', ''), ('1024', '1024', ''), ('2048', '2048', ''), ('4096', '4096', '')],
                             default='1024')

class GLB_OT_NBakeSwap(Operator):
    bl_idname = "glb_export.nbake_swap"
    bl_label = "Swap high/low"
    def execute(self, context):
        p = context.scene.glb_nbake_props
        a, b = p.source, p.target
        p.source = b; p.target = a
        return {'FINISHED'}

class GLB_OT_NBakeAuto(Operator):
    bl_idname = "glb_export.nbake_auto"
    bl_label = "Auto-set values"
    bl_description = "Measure the gap, test candidates, keep the best-scoring values"
    def execute(self, context):
        p = context.scene.glb_nbake_props
        if not (p.source and p.target):
            self.report({'ERROR'}, "Pick high-poly and low-poly first")
            return {'CANCELLED'}
        g = _nbake_gather(context, 500)
        diag = g["diag"]
        ups = sorted([s[5] for s in g["data"] if s[5] is not None])
        downs = sorted([s[6] for s in g["data"] if s[6] is not None])
        if not ups and not downs:
            self.report({'WARNING'}, "No overlap found between the two meshes")
            return {'CANCELLED'}
        dn95 = downs[int(0.95 * (len(downs) - 1))] if downs else 0.0
        cands = {diag * 0.01}
        for f in (0.5, 0.75, 0.9, 0.95):
            v = (ups[int(f * (len(ups) - 1))] if ups else 0.0) * 1.2 + diag * 0.004
            cands.add(min(v, diag * 0.1))
        best = (None, None, None)
        for ext in sorted(cands):
            dist = ext + dn95 * 1.2 + diag * 0.006
            r = _nbake_classify(g, ext, dist, p.use_cage)
            score = r["g"] + 0.5 * r["minor"] - 3.0 * r["w"] - 1.5 * r["h"]
            if best[0] is None or score > best[0]:
                best = (score, ext, dist)
        p.extrusion = best[1]
        p.max_ray_distance = best[2]
        return {'FINISHED'}

class GLB_OT_NBake(Operator):
    bl_idname = "glb_export.nbake_run"
    bl_label = "Bake normal map"
    bl_description = "Bake high-poly detail onto the low-poly as a tangent normal map"
    def execute(self, context):
        p = context.scene.glb_nbake_props
        if not (p.source and p.target):
            self.report({'ERROR'}, "Pick high-poly and low-poly first")
            return {'CANCELLED'}
        if not p.target.data.uv_layers:
            self.report({'ERROR'}, "Low-poly has no UV map - unwrap it first")
            return {'CANCELLED'}
        res = int(p.resolution)
        img_name = "%s_NormalHL" % p.target.name
        img = bpy.data.images.get(img_name)
        if img and (img.size[0] != res):
            bpy.data.images.remove(img); img = None
        if not img:
            img = bpy.data.images.new(img_name, res, res, alpha=False, float_buffer=True)
        img.colorspace_settings.name = 'Non-Color'
        if p.target.material_slots and p.target.material_slots[0].material:
            mat = p.target.material_slots[0].material
        else:
            mat = bpy.data.materials.new("%s_NBakeMat" % p.target.name)
            p.target.data.materials.append(mat)
        mat.use_nodes = True
        nt = mat.node_tree
        node = nt.nodes.get("NBake_Target")
        if not node:
            node = nt.nodes.new('ShaderNodeTexImage')
            node.name = "NBake_Target"
            node.label = "HL normal bake target"
            node.location = (-600, -300)
        node.image = img
        for n in nt.nodes: n.select = False
        node.select = True
        nt.nodes.active = node
        # unplug the target image node during bake (circular dependency guard)
        links_backup = [(l.from_socket, l.to_socket) for out in node.outputs for l in out.links]
        for fs, ts in links_backup:
            for l in list(nt.links):
                if l.from_socket == fs and l.to_socket == ts:
                    nt.links.remove(l)
        cage_obj = None
        src_hide, tgt_hide = p.source.hide_get(), p.target.hide_get()
        src_hr, tgt_hr = p.source.hide_render, p.target.hide_render
        prev_engine = context.scene.render.engine
        context.scene.render.engine = 'CYCLES'
        prev_samples = context.scene.cycles.samples
        context.scene.cycles.samples = 32
        margin = max(8, res // 64)
        try:
            # hidden objects can't be selected -> temporarily unhide
            p.source.hide_set(False); p.target.hide_set(False)
            p.source.hide_render = False; p.target.hide_render = False
            if context.object and context.object.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            if p.use_cage:
                # Blender 5.x auto-cage is unreliable -> build a real cage object
                dg = context.evaluated_depsgraph_get()
                tgt_ev = p.target.evaluated_get(dg)
                cage_me = bpy.data.meshes.new_from_object(tgt_ev)
                offs = [v.co + v.normal * p.extrusion for v in cage_me.vertices]
                for i, v in enumerate(cage_me.vertices):
                    v.co = offs[i]
                cage_obj = bpy.data.objects.new("NBake_TempCage", cage_me)
                cage_obj.matrix_world = p.target.matrix_world.copy()
                context.scene.collection.objects.link(cage_obj)
            bpy.ops.object.select_all(action='DESELECT')
            p.source.select_set(True)
            p.target.select_set(True)
            context.view_layer.objects.active = p.target
            kwargs = dict(type='NORMAL', use_selected_to_active=True,
                          use_cage=p.use_cage, cage_extrusion=p.extrusion,
                          max_ray_distance=(0.0 if p.use_cage else p.max_ray_distance),
                          normal_space='TANGENT', margin=margin, use_clear=True)
            if cage_obj:
                kwargs["cage_object"] = cage_obj.name
                kwargs["cage_extrusion"] = 0.0
            bpy.ops.object.bake(**kwargs)
            self.report({'INFO'}, "Done - image '%s'" % img_name)
        except Exception as ex:
            self.report({'ERROR'}, "Bake failed: %s" % str(ex))
            return {'CANCELLED'}
        finally:
            if cage_obj:
                cme = cage_obj.data
                bpy.data.objects.remove(cage_obj)
                bpy.data.meshes.remove(cme)
            for fs, ts in links_backup:
                try: nt.links.new(fs, ts)
                except Exception: pass
            p.source.hide_set(src_hide); p.target.hide_set(tgt_hide)
            p.source.hide_render = src_hr; p.target.hide_render = tgt_hr
            context.scene.render.engine = prev_engine
            context.scene.cycles.samples = prev_samples
        return {'FINISHED'}

def _nbake_loose_parts(me):
    n = len(me.vertices)
    adj = [[] for _ in range(n)]
    for e in me.edges:
        a, b = e.vertices
        adj[a].append(b); adj[b].append(a)
    seen = [False] * n
    parts = []
    for i in range(n):
        if seen[i]:
            continue
        stack = [i]; seen[i] = True; comp = [i]
        while stack:
            v = stack.pop()
            for w in adj[v]:
                if not seen[w]:
                    seen[w] = True
                    stack.append(w); comp.append(w)
        parts.append(comp)
    return parts

class GLB_OT_NBakeExploded(Operator):
    bl_idname = "glb_export.nbake_exploded"
    bl_label = "Bake exploded (fix contact areas)"
    bl_description = "Fly matched part-pairs apart, bake, restore exactly - stops rays jumping between nearby parts"
    def execute(self, context):
        p = context.scene.glb_nbake_props
        if not (p.source and p.target):
            self.report({'ERROR'}, "Pick high-poly and low-poly first")
            return {'CANCELLED'}
        tme, sme = p.target.data, p.source.data
        tparts = _nbake_loose_parts(tme)
        sparts = _nbake_loose_parts(sme)
        if len(tparts) < 2:
            return bpy.ops.glb_export.nbake_run()
        diag = max(p.target.dimensions.length, 1e-6)
        tmw, smw = p.target.matrix_world, p.source.matrix_world
        tcent = []
        for comp in tparts:
            c = Vector((0, 0, 0))
            for vi in comp: c += tme.vertices[vi].co
            tcent.append(tmw @ (c / len(comp)))
        offs_world = [Vector((i * diag * 2.5, 0, 0)) for i in range(len(tparts))]
        t_orig = _np.empty(len(tme.vertices) * 3, dtype=_np.float32)
        s_orig = _np.empty(len(sme.vertices) * 3, dtype=_np.float32)
        tme.vertices.foreach_get("co", t_orig)
        sme.vertices.foreach_get("co", s_orig)
        try:
            ti = tmw.inverted_safe().to_3x3()
            for pi, comp in enumerate(tparts):
                lo = ti @ offs_world[pi]
                for vi in comp:
                    tme.vertices[vi].co += lo
            si = smw.inverted_safe().to_3x3()
            for comp in sparts:
                c = Vector((0, 0, 0))
                for vi in comp: c += sme.vertices[vi].co
                cw = smw @ (c / len(comp))
                bi = min(range(len(tcent)), key=lambda i: (tcent[i] - cw).length_squared)
                lo = si @ offs_world[bi]
                for vi in comp:
                    sme.vertices[vi].co += lo
            tme.update(); sme.update()
            result = bpy.ops.glb_export.nbake_run()
            if 'FINISHED' in result:
                self.report({'INFO'}, "Exploded bake done (%d part pairs)" % len(tparts))
        finally:
            tme.vertices.foreach_set("co", t_orig)
            sme.vertices.foreach_set("co", s_orig)
            tme.update(); sme.update()
            p.target.update_tag(); p.source.update_tag()
        return {'FINISHED'}

class GLB_PT_NormalBakePanel(Panel):
    bl_label = "Normal Bake: High → Low"
    bl_idname = "GLB_PT_normal_bake_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Col2GLB"
    bl_options = {'DEFAULT_CLOSED'}
    def draw(self, context):
        p = context.scene.glb_nbake_props
        st = _nbake_state
        l = self.layout
        col = l.column(align=True)
        col.prop(p, "source")
        col.prop(p, "target")
        cnt = st.get("counts", (0, 0))
        if p.source and p.target and cnt and cnt[0] and cnt[0] < cnt[1]:
            b = l.box()
            b.label(text="High-poly has FEWER polys than low-poly", icon='ERROR')
            b.operator("glb_export.nbake_swap", icon='ARROW_LEFTRIGHT')
        l.operator("glb_export.nbake_auto", icon='SHADERFX')
        box = l.box()
        box.prop(p, "show_shell")
        box.prop(p, "show_rays")
        box.prop(p, "extrusion")
        box.prop(p, "use_cage")
        row = box.row()
        row.enabled = not p.use_cage
        row.prop(p, "max_ray_distance")
        if not p.use_cage and 0.0 < p.max_ray_distance < p.extrusion:
            box.label(text="Distance < extrusion: rays die mid-air!", icon='ERROR')
        if st.get("stats"):
            l.label(text=st["stats"])
        for kind, txt in st.get("advice", []):
            ic = 'CHECKMARK' if kind == "ok" else ('ERROR' if kind == "warn" else 'INFO')
            l.label(text=txt, icon=ic)
        l.separator()
        l.prop(p, "resolution")
        l.operator("glb_export.nbake_run", icon='RENDER_STILL')
        l.operator("glb_export.nbake_exploded", icon='MOD_EXPLODE')
# ============================================================
#  END NORMAL BAKE SECTION
# ============================================================


# === REGISTRATION ===
classes = (   
    GLBBakeUVTarget,
    GLBAOExceptionItem,
    GLBAlphaCollectionItem,
    GLBExportProperties,
    GLB_UL_CustomUVBakeTargets,
    GLB_OT_ScanCustomUVTargets,
    GLB_OT_AddCustomUVTarget,
    GLB_OT_RemoveCustomUVTarget,
    GLB_OT_ScanAlphaCollections,
    GLB_OT_UnwrapSelected,
    GLB_OT_UnwrapCancel,
    GLB_OT_ShowExportReport,
    GLB_OT_HalveDouble,
    GLB_OT_AOPreviewToggle,
    GLB_OT_AOPreviewTexturedToggle,
    GLB_UL_AOExceptions,
    GLB_OT_AddAOException,
    GLB_OT_RemoveAOException,
    GLB_OT_AddSelectedAOExceptions,
    GLB_OT_CleanupProcessedCollections,
    GLB_OT_OpenExportFolder,
    GLB_OT_ClearImportPath,
    GLB_OT_ImportBlendFiles,
    GLB_OT_SelectImportFolder,
    GLB_OT_ProcessAndExport,
    GLB_PT_ExportPanel,
    GLBNormalBakeProps,
    GLB_OT_NBakeSwap,
    GLB_OT_NBakeAuto,
    GLB_OT_NBake,
    GLB_OT_NBakeExploded,
    GLB_PT_NormalBakePanel,
)

def register():
    for handler_list in (bpy.app.handlers.save_pre, bpy.app.handlers.undo_post,
                         bpy.app.handlers.redo_post, bpy.app.handlers.render_init,
                         bpy.app.handlers.load_pre):
        if _ao_preview_autostop not in handler_list:
            handler_list.append(_ao_preview_autostop)
    for cls in classes:
        bpy.utils.register_class(cls)
    
    bpy.types.Scene.glb_export_props = bpy.props.PointerProperty(type=GLBExportProperties)
    bpy.types.Scene.glb_nbake_props = bpy.props.PointerProperty(type=GLBNormalBakeProps)
    global _nbake_draw_handle
    _nbake_draw_handle = bpy.types.SpaceView3D.draw_handler_add(_nbake_draw_callback, (), 'WINDOW', 'POST_VIEW')

    # Check for MOF resource file
    addon_dir = os.path.dirname(os.path.realpath(__file__))
    mof_zip_path = os.path.join(addon_dir, "resources", "MinistryOfFlat_Release.zip")
    
    if not os.path.exists(mof_zip_path):
        print("=" * 60)
        print("WARNING: MinistryOfFlat_Release.zip not found!")
        print(f"Expected location: {mof_zip_path}")
        print("MOF UV unwrapping will not be available.")
        print("To enable MOF unwrapping, place MinistryOfFlat_Release.zip")
        print(f"in: {os.path.join(addon_dir, 'resources')}")
        print("=" * 60)

def unregister():
    for handler_list in (bpy.app.handlers.save_pre, bpy.app.handlers.undo_post,
                         bpy.app.handlers.redo_post, bpy.app.handlers.render_init,
                         bpy.app.handlers.load_pre):
        if _ao_preview_autostop in handler_list:
            handler_list.remove(_ao_preview_autostop)
    try:
        ao_preview_stop(bpy.context)
    except Exception:
        pass
    global _nbake_draw_handle
    if _nbake_draw_handle:
        bpy.types.SpaceView3D.draw_handler_remove(_nbake_draw_handle, 'WINDOW')
        _nbake_draw_handle = None
    del bpy.types.Scene.glb_nbake_props
    
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    
    del bpy.types.Scene.glb_export_props

if __name__ == "__main__":
    register()
