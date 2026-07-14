"""GLB exporter for Print3Dexporter.

Export workflow
---------------
For each placeholder object found in the collection:
1.  Create a temporary collection "TEMP" inside the selected collection.
2.  Duplicate the collection's camera and the single placeholder into TEMP.
3.  Apply transforms on the placeholder mesh to bake world-space positions.
4.  Create an Empty called "lockdown" in TEMP and parent every duplicated
    object under it.
5.  Export all objects in TEMP as a GLB file named after the placeholder suffix.
6.  Delete TEMP and all its contents — the scene is exactly as before.
7.  Repeat for each remaining placeholder.

Output path
-----------
The .blend file is expected to live inside a "BL" (or similar) sub-folder of
the project root.  Each GLB is written one level *above* that folder, inside a
sub-directory named after the collection with any trailing ``_<N>`` index
suffix stripped, then under a ``GLB`` sub-folder:

  <project_root>/<collection_base_name>/GLB/<safe_name>_<letter>.glb

Example:
  blend file  : project/BL/scene.blend
  collection  : canva_print_3D_flags_ca_us_2026-05_16
  slot A      : placeholder_a
  GLB output  : project/canva_print_3D_flags_ca_us_2026-05/GLB/<name>_a.glb

A ``<collection_name>.json`` is also written / updated in the GLB folder,
mapping each exported placeholder to its dimensions:
``{"<name>_<letter>": {"width": W, "height": H}, …}``.

Only placeholder objects actually PRESENT in the collection (the local copies
imported into its ``placeholders`` sub-collection) are exported — originals in
linked library files are never touched.  All transforms are applied to the
export duplicate before writing the GLB.

Returns a list of exported filepath strings.
Raises RuntimeError with a human-readable message on failure.
"""

import os
import re
import json
import bpy

from .utils import (
    find_camera_in_collection, get_objects_recursive, _is_placeholders_col,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _strip_index_suffix(name):
    """Strip a trailing ``_<integer>`` index suffix from *name*.

    Used to derive the project sub-folder from a versioned collection name:
      'canva_print_3D_flags_ca_us_2026-05_16'  →  'canva_print_3D_flags_ca_us_2026-05'
      'my_collection'                           →  'my_collection'
    """
    return re.sub(r'_\d+$', '', name)

def _find_layer_collection(layer_col, collection):
    """Recursively find the LayerCollection wrapping *collection*."""
    if layer_col.collection == collection:
        return layer_col
    for child in layer_col.children:
        result = _find_layer_collection(child, collection)
        if result:
            return result
    return None


def _ensure_temp_col_visible(context, temp_col):
    """Make sure the temp collection is not excluded from the view layer."""
    lc = _find_layer_collection(context.view_layer.layer_collection, temp_col)
    if lc:
        lc.exclude = False
        lc.hide_viewport = False


def _is_placeholder(obj):
    """Return True if *obj* is a mesh whose object name or mesh-data name
    contains 'placeholder' (case-insensitive).  Consistent with the importer."""
    if obj.type != 'MESH':
        return False
    if 'placeholder' in obj.name.lower():
        return True
    if obj.data and 'placeholder' in obj.data.name.lower():
        return True
    return False


def _gather_placeholders(collection):
    """Return all placeholder mesh objects from *collection* and all its
    descendants.  Checks both object name and mesh-data name."""
    found = []
    for obj in get_objects_recursive(collection):
        if _is_placeholder(obj):
            found.append(obj)
    return found


def _find_placeholders_subcol(col):
    """Return *col*'s ``placeholders`` sub-collection (auto-numbering aware)."""
    for child in col.children:
        if _is_placeholders_col(child.name):
            return child
    return None


def _resolve_in_collection(col, obj_name):
    """Resolve *obj_name* to a LOCAL object living inside *col*'s tree.

    The export must use the placeholder copies that were IMPORTED into the
    collection — never the originals from a linked library file.  A plain
    ``bpy.data.objects.get(name)`` is a global lookup that can return the
    linked original when both exist, so this resolver:

      1. searches the collection's ``placeholders`` sub-collection first
         (where the importer puts the local copies),
      2. then the rest of the collection tree,
      3. skips library-linked objects entirely (``obj.library is not None``),
      4. and finally accepts an auto-suffixed variant (``<name>.001`` …) if
         the exact name is not present (e.g. after a re-import renamed it).

    Returns the object or None.
    """
    ph_col = _find_placeholders_subcol(col)
    pools = []
    if ph_col is not None:
        pools.append(list(ph_col.all_objects))
    pools.append(get_objects_recursive(col))

    # Exact-name match (local objects only)
    for pool in pools:
        for o in pool:
            if o.name == obj_name and o.library is None:
                return o
    # Auto-suffix match: "<obj_name>.NNN"
    pat = re.compile(re.escape(obj_name) + r'\.\d+$')
    for pool in pools:
        for o in pool:
            if o.library is None and pat.match(o.name):
                return o
    return None


def _extract_placeholder_suffix(obj):
    """Extract the letter suffix (a-j) from a placeholder object or data name.

    Matches the last ``_<letter>`` segment before any Blender auto-number
    suffix (.001, .002, …).  Returns the lowercase letter or None.

    Examples:
      'placeholder_a'      → 'a'
      'Placeholder_B.001'  → 'b'
      'my_placeholder_c'   → 'c'
    """
    for name in (obj.name, obj.data.name if obj.data else ''):
        m = re.search(r'_([a-j])(?:\.\d+)?$', name.lower())
        if m:
            return m.group(1)
    return None


def _duplicate_object(obj, temp_col):
    """Create a local duplicate of *obj* (handles both local and linked objects).
    The duplicate is linked into *temp_col* and returned.
    Hide flags are cleared so the duplicate is always visible and selectable."""
    dup = obj.copy()
    if obj.data:
        dup.data = obj.data.copy()
    # Clear hide flags so the duplicate is visible and selectable for export
    dup.hide_viewport = False
    dup.hide_render = False
    dup.hide_select = False
    temp_col.objects.link(dup)
    return dup


def _clear_parent_keep_transform(dup, source_obj):
    """Clear *dup*'s inherited parent while KEEPING its world transform.

    ``obj.copy()`` carries over the source's ``parent`` pointer.  In a linked
    scene a placeholder (or the camera) is often parented to an Empty that
    rotates / moves it — and the export code later re-parents the duplicate to
    the "lockdown" empty, which silently DROPS that Empty's contribution and
    shifts the exported mesh out of place.

    This is the Python equivalent of Blender's *Clear Parent and Keep
    Transform*: take the source object's full evaluated world matrix (parent
    chain included), drop the parent link, and write the matrix back so the
    duplicate sits exactly where the original appears in the scene.  The
    world position is later baked into the mesh by transform_apply.
    """
    world = source_obj.matrix_world.copy()   # includes the parent chain
    dup.parent = None
    dup.matrix_parent_inverse.identity()
    dup.matrix_world = world


# ── export list + JSON helpers ──────────────────────────────────────────────

def _build_export_list(item, col):
    """Return a list of (letter, obj, width, height) tuples to export.

    Primary (slot-driven): iterate crop slots a–j; each enabled slot with an
    assigned object is exported under its slot letter and carries that slot's
    Width / Height.  This guarantees the ``_a`` / ``_b`` … appendix.

    Objects are resolved INSIDE the collection tree only (preferring the
    imported copies in the ``placeholders`` sub-collection) — the originals in
    a linked library file are never picked up.  See :func:`_resolve_in_collection`.

    Fallback (no slots configured): gather every local placeholder from the
    ``placeholders`` sub-collection (or the collection tree if none exists) and
    use the letter parsed from its name, with Width / Height defaulting to 0.
    """
    export_list = []
    for letter in 'abcdefghij':
        if not getattr(item, f"crop_{letter}_enabled", False):
            continue
        obj_name = getattr(item, f"crop_{letter}_name", "")
        if not obj_name:
            continue
        obj = _resolve_in_collection(col, obj_name)
        if obj is None:
            continue
        width  = getattr(item, f"crop_{letter}_width", 0.0)
        height = getattr(item, f"crop_{letter}_height", 0.0)
        export_list.append((letter, obj, width, height))
    if export_list:
        return export_list
    # Fallback: name-based gather — restricted to the imported (local) copies.
    ph_col = _find_placeholders_subcol(col)
    for obj in _gather_placeholders(ph_col if ph_col is not None else col):
        if obj.library is not None:
            continue   # never export objects living in a linked library file
        letter = _extract_placeholder_suffix(obj)
        if letter:
            export_list.append((letter, obj, 0.0, 0.0))
    return export_list


def count_export_glbs(item):
    """Return how many GLB files :func:`export_crop_glb` would write for *item*.

    Mirrors the export's own :func:`_build_export_list` logic so the panel can
    show the count on the Export GLB button (one GLB per exportable slot, or the
    name-based placeholder fallback).  Returns 0 for an item with no collection.
    """
    col = item.collection
    if col is None:
        return 0
    try:
        return len(_build_export_list(item, col))
    except Exception:
        return 0


def _update_placeholder_json(json_path, entries):
    """Merge *entries* into the JSON file at *json_path*.

    The file holds one object keyed by exported-placeholder identifier:

        {
          "<name>_a": {"width": 4000, "height": 3000},
          "<name>_b": {"width": 2000, "height": 1000}
        }

    Existing keys from previous exports are preserved; a matching identifier
    has its values replaced, a new identifier is added.  Returns the JSON path.

    *entries* is a list of (identifier:str, width, height).
    """
    data = {}
    if os.path.exists(json_path):
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except Exception as e:
            print(f"[CP3D] Could not read existing JSON ({json_path}): {e}")
    # Replace / add the entries for this export (values written as integers)
    for ident, width, height in entries:
        data[ident] = {"width": int(width), "height": int(height)}
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return json_path


# ── main export function ──────────────────────────────────────────────────────

def export_crop_glb(context, item):
    """Run the full placeholder-based GLB export for *item*.

    Each placeholder is exported as a separate GLB named by its slot letter:
      slot A  →  <safe_name>_a.glb   (object inside also named <safe_name>_a)
      slot B  →  <safe_name>_b.glb
    A ``<collection_name>.json`` mapping every exported placeholder to its
    Width / Height is written / updated in the GLB folder.

    Parameters
    ----------
    context : bpy.types.Context
    item    : CollectionRenderItem

    Returns
    -------
    list[str] — absolute paths to all exported .glb files.
    """
    scene = context.scene
    col = item.collection

    if not col:
        raise RuntimeError("No collection assigned to this item")

    if not bpy.data.filepath:
        raise RuntimeError("Save the .blend file before exporting")

    # ── build output directory ────────────────────────────────────────────────
    blend_dir   = os.path.dirname(bpy.data.filepath)
    project_dir = os.path.dirname(blend_dir)
    name        = col.name
    safe_name   = bpy.path.clean_name(name)
    folder_base = bpy.path.clean_name(_strip_index_suffix(col.name))
    out_dir     = os.path.join(project_dir, folder_base, "GLB")
    os.makedirs(out_dir, exist_ok=True)

    # ── find source objects (before we touch anything) ────────────────────────
    cam_obj = find_camera_in_collection(col)
    export_list = _build_export_list(item, col)

    if not export_list:
        raise RuntimeError(
            "No placeholder objects to export — assign placeholder objects to "
            f"the crop slots, or add 'placeholder_<letter>' objects to '{col.name}'"
        )

    exported_paths = []
    json_entries = []

    for letter, ph_obj, width, height in export_list:
        identifier = f"{safe_name}_{letter}"
        filename   = f"{identifier}.glb"
        filepath   = os.path.join(out_dir, filename)

        # ── create TEMP collection inside the selected collection ─────────────
        temp_col = bpy.data.collections.new("TEMP")
        col.children.link(temp_col)
        _ensure_temp_col_visible(context, temp_col)

        created_objects = []

        try:
            # ── duplicate camera into TEMP ────────────────────────────────────
            dup_cam = None
            if cam_obj:
                dup_cam = _duplicate_object(cam_obj, temp_col)
                # Clear Parent (Keep Transform): the camera may be parented to
                # an Empty in the linked scene — keep its world placement.
                _clear_parent_keep_transform(dup_cam, cam_obj)
                created_objects.append(dup_cam)

            # ── duplicate this single placeholder into TEMP ───────────────────
            dup_ph = _duplicate_object(ph_obj, temp_col)
            # Clear Parent (Keep Transform): placeholders in linked scenes are
            # often parented to (and rotated by) an Empty.  Without this the
            # later re-parent to "lockdown" drops that Empty's transform and
            # the exported mesh lands in the wrong position.
            _clear_parent_keep_transform(dup_ph, ph_obj)
            created_objects.append(dup_ph)
            # Name the exported object with the letter appendix (e.g. <name>_a)
            dup_ph.name = identifier
            if dup_ph.data:
                dup_ph.data.name = identifier

            # ── apply transforms to the placeholder mesh ──────────────────────
            context.view_layer.update()
            bpy.ops.object.select_all(action='DESELECT')
            if dup_ph.type == 'MESH':
                try:
                    dup_ph.select_set(True)
                except RuntimeError:
                    pass
                context.view_layer.objects.active = dup_ph
                bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
            bpy.ops.object.select_all(action='DESELECT')

            # ── create "lockdown" empty and parent everything under it ────────
            lockdown = bpy.data.objects.new("lockdown", None)
            lockdown.empty_display_type = 'PLAIN_AXES'
            temp_col.objects.link(lockdown)
            created_objects.append(lockdown)

            dup_ph.parent = lockdown
            dup_ph.matrix_parent_inverse = lockdown.matrix_world.inverted()
            if dup_cam:
                dup_cam.parent = lockdown
                dup_cam.matrix_parent_inverse = lockdown.matrix_world.inverted()

            # ── backup selection state ────────────────────────────────────────
            prev_selected = [o for o in scene.objects if o.select_get()]
            prev_active = context.view_layer.objects.active

            try:
                context.view_layer.update()
                bpy.ops.object.select_all(action='DESELECT')
                for obj in created_objects:
                    try:
                        obj.select_set(True)
                    except RuntimeError:
                        pass
                context.view_layer.objects.active = lockdown

                bpy.ops.export_scene.gltf(
                    filepath=filepath,
                    use_selection=True,
                    # Export ONLY the active scene.  Without this the glTF
                    # exporter writes EVERY Blender scene as a separate glTF
                    # scene (e.g. a "Thumbnail" scene), which the importer then
                    # recreates as extra "Scene.0xx" / "Thumbnail" collections
                    # on every import.  Limiting to the active scene keeps the
                    # GLB to just the selected placeholder + camera.
                    use_active_scene=True,
                    export_format='GLB',
                    export_apply=True,
                    export_cameras=True,
                    export_lights=False,
                    export_materials='NONE',
                )

            finally:
                # ── restore selection ─────────────────────────────────────────
                bpy.ops.object.select_all(action='DESELECT')
                for obj in prev_selected:
                    try:
                        obj.select_set(True)
                    except Exception:
                        pass
                if prev_active:
                    try:
                        context.view_layer.objects.active = prev_active
                    except Exception:
                        pass

        finally:
            # ── delete TEMP collection with hierarchy ─────────────────────────
            for obj in reversed(created_objects):
                try:
                    bpy.data.objects.remove(obj, do_unlink=True)
                except Exception:
                    pass
            try:
                bpy.data.collections.remove(temp_col)
            except Exception:
                pass

        exported_paths.append(filepath)
        json_entries.append((identifier, width, height))

    # ── write / update the <collection_name>.json in the GLB folder ───────────
    if json_entries:
        try:
            json_path = os.path.join(out_dir, f"{folder_base}.json")
            _update_placeholder_json(json_path, json_entries)
            print(f"[CP3D] Updated JSON: {json_path}")
        except Exception as e:
            print(f"[CP3D] Could not write {safe_name}.json: {e}")

    return exported_paths
