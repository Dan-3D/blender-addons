bl_info = {
    "name": "Placeholder Mesh Tool",
    "blender": (4, 2, 0),
    "category": "Mesh",
    "version": (2, 5, 0),
    "author": "Popi",
    "description": "Automate placeholder mesh creation for WebGL label/print previews",
}

import bpy
import bmesh
import numpy as np
from math import atan2, cos, sin, pi


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLACEHOLDER_MAT  = "placeholder"
CHECKER_MAT      = "checker_material"
CHECKER_IMG_NAME = "placeholder_1_checker_v5"


# ---------------------------------------------------------------------------
# Scene Properties
# ---------------------------------------------------------------------------

UV_METHODS = [
    ('FOLLOW_ACTIVE_QUADS',  "Follow Active Quads",        "Rectify active quad, then unwrap with Follow Active Quads"),
    ('KEEP_ORIGINAL',        "Keep Original UVs",          "Do not re-unwrap"),
    ('RECTIFY',              "Rectify (Planar)",            "Project from face normal"),
    ('MINIMIZE_STRETCH',     "Minimize Stretch",            "Unwrap using Minimize Stretch"),
    ('UNWRAP',               "Unwrap (Angle Based)",        "Standard angle-based unwrap"),
    ('CONFORMAL',            "Unwrap (Conformal)",          "Standard conformal unwrap"),
    ('SMART_UV',             "Smart UV Project",            "Automatic projection"),
    ('LIGHTMAP',             "Lightmap Pack",               "Non-overlapping layout"),
    ('PROJECT_VIEW',         "Project from View",           "Project from current view"),
    ('PROJECT_VIEW_BOUNDS',  "Project from View (Bounds)",  "Project from view, scale to bounds"),
    ('CUBE',                 "Cube Projection",             "Project from cube"),
    ('CYLINDER',             "Cylinder Projection",         "Project from cylinder"),
    ('SPHERE',               "Sphere Projection",           "Project from sphere"),
]


class PlaceholderToolSettings(bpy.types.PropertyGroup):
    uv_method: bpy.props.EnumProperty(name="UV Method", items=UV_METHODS, default='FOLLOW_ACTIVE_QUADS')
    smart_angle: bpy.props.FloatProperty(name="Angle Limit", default=1.15192, min=0.0, max=3.14159, subtype='ANGLE')
    smart_margin: bpy.props.FloatProperty(name="Island Margin", default=0.001, min=0.0, max=1.0)
    lightmap_margin: bpy.props.FloatProperty(name="Margin", default=0.1, min=0.0, max=1.0)
    cube_size: bpy.props.FloatProperty(name="Cube Size", default=1.0, min=0.001)
    proj_direction: bpy.props.EnumProperty(
        name="Direction",
        items=[
            ('VIEW_ON_EQUATOR', "View on Equator", ""),
            ('VIEW_ON_POLES',   "View on Poles",   ""),
            ('ALIGN_TO_OBJECT', "Align to Object", ""),
        ],
        default='VIEW_ON_EQUATOR',
    )
    use_checker: bpy.props.BoolProperty(name="Assign Checker Material", default=False)


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------

def get_or_create_placeholder_material():
    """Plain gray material shared by all placeholders."""
    mat = bpy.data.materials.get(PLACEHOLDER_MAT)
    if mat:
        return mat
    mat = bpy.data.materials.new(name=PLACEHOLDER_MAT)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    out  = nodes.new("ShaderNodeOutputMaterial");  out.location  = (400, 0)
    bsdf = nodes.new("ShaderNodeBsdfPrincipled");  bsdf.location = (0, 0)
    bsdf.inputs["Base Color"].default_value = (0.8, 0.8, 0.8, 1.0)
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    return mat


def build_directional_image(w=512, h=512):
    px = np.zeros((h, w, 4), dtype=np.float32)
    ch   = w // 16
    col  = (np.arange(w) // ch) % 2
    row  = (np.arange(h) // ch) % 2
    grid = (col[np.newaxis, :] ^ row[:, np.newaxis]).astype(np.float32)
    bg   = 0.502 + grid * 0.498
    px[:, :, 0] = bg
    px[:, :, 1] = bg
    px[:, :, 2] = bg
    px[:, :, 3] = 1.0
    ox     = w // 2
    oy     = h // 2
    arm    = int(w * 0.18)
    shaft  = max(3, int(w * 0.014))
    head_w = max(5, int(w * 0.035))
    head_l = max(6, int(w * 0.052))
    GREEN = [0.0, 0.85, 0.0, 1.0]
    RED   = [0.85, 0.0, 0.0, 1.0]
    def fill_rect(x0, y0, x1, y1, color):
        x0, x1 = max(0, x0), min(w, x1)
        y0, y1 = max(0, y0), min(h, y1)
        if x1 > x0 and y1 > y0:
            px[y0:y1, x0:x1] = color
    def fill_tri(pts, color):
        xs = np.array([p[0] for p in pts])
        ys = np.array([p[1] for p in pts])
        x0, x1 = max(0, int(xs.min())), min(w - 1, int(xs.max()))
        y0, y1 = max(0, int(ys.min())), min(h - 1, int(ys.max()))
        xg, yg = np.meshgrid(np.arange(x0, x1 + 1), np.arange(y0, y1 + 1))
        def side(ax, ay, bx, by):
            return (xg - bx) * (ay - by) - (ax - bx) * (yg - by)
        d0 = side(pts[0][0], pts[0][1], pts[1][0], pts[1][1])
        d1 = side(pts[1][0], pts[1][1], pts[2][0], pts[2][1])
        d2 = side(pts[2][0], pts[2][1], pts[0][0], pts[0][1])
        has_neg = (d0 < 0) | (d1 < 0) | (d2 < 0)
        has_pos = (d0 > 0) | (d1 > 0) | (d2 > 0)
        px[y0:y1 + 1, x0:x1 + 1][~(has_neg & has_pos)] = color
    fill_rect(ox - shaft, oy, ox + shaft, oy + arm, GREEN)
    tip_y = oy + arm + head_l
    fill_tri([(ox - head_w, oy + arm), (ox + head_w, oy + arm), (ox, tip_y)], GREEN)
    fill_rect(ox, oy - shaft, ox + arm, oy + shaft, RED)
    tip_x = ox + arm + head_l
    fill_tri([(ox + arm, oy - head_w), (ox + arm, oy + head_w), (tip_x, oy)], RED)
    np.clip(px, 0.0, 1.0, out=px)
    return px.ravel().tolist()


def get_or_create_checker_material():
    mat = bpy.data.materials.get(CHECKER_MAT)
    if mat:
        return mat
    mat = bpy.data.materials.new(name=CHECKER_MAT)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    out  = nodes.new("ShaderNodeOutputMaterial");  out.location  = (400, 0)
    bsdf = nodes.new("ShaderNodeBsdfPrincipled");  bsdf.location = (0, 0)
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    tex = nodes.new("ShaderNodeTexImage");  tex.location = (-400, 0)
    img = bpy.data.images.get(CHECKER_IMG_NAME)
    if not img:
        SIZE = 512
        img = bpy.data.images.new(CHECKER_IMG_NAME, width=SIZE, height=SIZE, alpha=False)
        img.pixels = build_directional_image(SIZE, SIZE)
        img.pack()
    tex.image = img
    links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    return mat


def assign_material(obj, use_checker):
    mat = get_or_create_checker_material() if use_checker else get_or_create_placeholder_material()
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    for poly in obj.data.polygons:
        poly.material_index = 0


# ---------------------------------------------------------------------------
# UV helpers
# ---------------------------------------------------------------------------

def get_next_placeholder_name():
    idx   = 1
    names = {o.name.lower() for o in bpy.data.objects}
    while f"placeholder_{idx}" in names:
        idx += 1
    return f"placeholder_{idx}"


def rectify_active_quad_uvs(bm, uv_layer):
    from mathutils import Vector
    face = bm.faces.active
    if face is None or len(face.loops) != 4:
        return False
    loops  = list(face.loops)
    cos3d  = [l.vert.co.copy() for l in loops]
    el     = [(cos3d[(i+1)%4] - cos3d[i]).length for i in range(4)]
    len_a  = (el[0] + el[2]) / 2
    len_b  = (el[1] + el[3]) / 2
    z      = Vector((0.0, 0.0, 1.0))
    def safe_norm(v):
        return v.normalized() if v.length > 1e-8 else v
    dir_a  = safe_norm((cos3d[1] - cos3d[0]) + (cos3d[2] - cos3d[3]))
    dir_b  = safe_norm((cos3d[2] - cos3d[1]) + (cos3d[3] - cos3d[0]))
    vert_a = abs(dir_a.dot(z))
    vert_b = abs(dir_b.dot(z))
    if vert_a >= vert_b:
        w, h = len_b, len_a
        bottom_first = cos3d[0].dot(z) <= cos3d[1].dot(z)
        rect = [(0,0),(0,h),(w,h),(w,0)] if bottom_first else [(0,h),(0,0),(w,0),(w,h)]
    else:
        w, h = len_a, len_b
        bottom_first = cos3d[1].dot(z) <= cos3d[2].dot(z)
        rect = [(w,0),(0,0),(0,h),(w,h)] if bottom_first else [(w,h),(0,h),(0,0),(w,0)]
    max_dim = max(w, h) if max(w, h) > 1e-6 else 1.0
    for i, loop in enumerate(loops):
        loop[uv_layer].uv.x = rect[i][0] / max_dim
        loop[uv_layer].uv.y = rect[i][1] / max_dim
    return True


def fix_uv_mirror(bm, uv_layer):
    flipped = total = 0
    for f in bm.faces:
        loops = list(f.loops)
        n = len(loops)
        uv_signed = 0.0
        for i in range(n):
            u0, v0 = loops[i][uv_layer].uv
            u1, v1 = loops[(i+1) % n][uv_layer].uv
            uv_signed += u0 * v1 - u1 * v0
        if f.normal.length > 1e-8:
            if (uv_signed < 0) != (f.normal.z < -0.5):
                flipped += 1
            total += 1
    if total > 0 and flipped > total / 2:
        for f in bm.faces:
            for loop in f.loops:
                loop[uv_layer].uv.x = -loop[uv_layer].uv.x


def rectify_uvs(bm, uv_layer):
    from mathutils import Vector
    avg_normal = Vector((0.0, 0.0, 0.0))
    total_area = 0.0
    for f in bm.faces:
        a = f.calc_area()
        avg_normal += f.normal * a
        total_area += a
    if total_area < 1e-8 or avg_normal.length < 1e-8:
        return
    avg_normal.normalize()
    ref = Vector((0.0, 0.0, 1.0))
    if abs(avg_normal.dot(ref)) > 0.99:
        ref = Vector((0.0, 1.0, 0.0))
    tangent   = avg_normal.cross(ref);     tangent.normalize()
    bitangent = avg_normal.cross(tangent); bitangent.normalize()
    for f in bm.faces:
        for loop in f.loops:
            co = loop.vert.co
            loop[uv_layer].uv.x = co.dot(tangent)
            loop[uv_layer].uv.y = co.dot(bitangent)


def auto_unwrap(obj, settings):
    prev_active   = bpy.context.view_layer.objects.active
    prev_selected = list(bpy.context.selected_objects)
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    if not obj.data.uv_layers:
        obj.data.uv_layers.new(name="UVMap")
    bpy.ops.mesh.mark_seam(clear=True)
    m = settings.uv_method
    if   m == 'MINIMIZE_STRETCH':    bpy.ops.uv.unwrap(method='MINIMUM_STRETCH', margin=0.001)
    elif m == 'UNWRAP':              bpy.ops.uv.unwrap(method='ANGLE_BASED',     margin=0.001)
    elif m == 'CONFORMAL':           bpy.ops.uv.unwrap(method='CONFORMAL',       margin=0.001)
    elif m == 'SMART_UV':            bpy.ops.uv.smart_project(angle_limit=settings.smart_angle, island_margin=settings.smart_margin)
    elif m == 'LIGHTMAP':            bpy.ops.uv.lightmap_pack(PREF_MARGIN_DIV=settings.lightmap_margin)
    elif m == 'PROJECT_VIEW':        bpy.ops.uv.project_from_view(scale_to_bounds=False)
    elif m == 'PROJECT_VIEW_BOUNDS': bpy.ops.uv.project_from_view(scale_to_bounds=True)
    elif m == 'CUBE':                bpy.ops.uv.cube_project(cube_size=settings.cube_size)
    elif m == 'CYLINDER':            bpy.ops.uv.cylinder_project(direction=settings.proj_direction)
    elif m == 'SPHERE':              bpy.ops.uv.sphere_project(direction=settings.proj_direction)
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    for o in prev_selected:
        o.select_set(True)
    bpy.context.view_layer.objects.active = prev_active


def align_uvs_to_local_z(bm, uv_layer):
    vert_uv_sums = {}
    vert_co      = {}
    for f in bm.faces:
        for loop in f.loops:
            vi = loop.vert.index
            uv = loop[uv_layer].uv
            if vi not in vert_uv_sums:
                vert_uv_sums[vi] = [0.0, 0.0, 0]
                vert_co[vi]      = loop.vert.co.copy()
            vert_uv_sums[vi][0] += uv.x
            vert_uv_sums[vi][1] += uv.y
            vert_uv_sums[vi][2] += 1
    if len(vert_co) < 2:
        return
    vert_uv = {vi: (s[0]/s[2], s[1]/s[2]) for vi, s in vert_uv_sums.items()}
    up_axis = None
    for axis in (2, 1, 0):
        vals = [vert_co[vi][axis] for vi in vert_co]
        if max(vals) - min(vals) > 1e-4:
            up_axis = axis
            break
    if up_axis is None:
        return
    sorted_verts = sorted(vert_co.keys(), key=lambda vi: vert_co[vi][up_axis])
    n   = len(sorted_verts)
    grp = max(1, n // 4)
    bot_group = sorted_verts[:grp]
    top_group = sorted_verts[-grp:]
    top_set   = set(top_group)
    bot_set   = set(bot_group)
    bot_u = sum(vert_uv[vi][0] for vi in bot_group) / len(bot_group)
    bot_v = sum(vert_uv[vi][1] for vi in bot_group) / len(bot_group)
    top_u = sum(vert_uv[vi][0] for vi in top_group) / len(top_group)
    top_v = sum(vert_uv[vi][1] for vi in top_group) / len(top_group)
    du, dv = top_u - bot_u, top_v - bot_v
    if abs(du) < 1e-8 and abs(dv) < 1e-8:
        return
    rot = round(-atan2(du, dv) / (pi / 2)) * (pi / 2)
    if abs(rot) < 0.001 or abs(rot - 2 * pi) < 0.001:
        rot = 0.0
    all_uvs = [loop[uv_layer].uv for f in bm.faces for loop in f.loops]
    cx = sum(uv.x for uv in all_uvs) / len(all_uvs)
    cy = sum(uv.y for uv in all_uvs) / len(all_uvs)
    def apply_rotation(angle):
        c, s = cos(angle), sin(angle)
        for f in bm.faces:
            for loop in f.loops:
                uv = loop[uv_layer].uv
                dx, dy = uv.x - cx, uv.y - cy
                uv.x   = cx + dx * c - dy * s
                uv.y   = cy + dx * s + dy * c
    if abs(rot) > 0.001:
        apply_rotation(rot)
    top_v_after = bot_v_after = 0.0
    top_cnt = bot_cnt = 0
    for f in bm.faces:
        for loop in f.loops:
            vi = loop.vert.index
            if vi in top_set:   top_v_after += loop[uv_layer].uv.y;  top_cnt += 1
            elif vi in bot_set: bot_v_after += loop[uv_layer].uv.y;  bot_cnt += 1
    if top_cnt > 0 and bot_cnt > 0:
        if (top_v_after / top_cnt) < (bot_v_after / bot_cnt) - 1e-6:
            apply_rotation(pi)


def stretch_uvs_to_bounds(bm, uv_layer):
    coords = [loop[uv_layer].uv.copy() for f in bm.faces for loop in f.loops]
    if not coords:
        return
    min_u = min(c.x for c in coords);  max_u = max(c.x for c in coords)
    min_v = min(c.y for c in coords);  max_v = max(c.y for c in coords)
    ru = max_u - min_u if max_u - min_u > 1e-6 else 1.0
    rv = max_v - min_v if max_v - min_v > 1e-6 else 1.0
    for f in bm.faces:
        for loop in f.loops:
            uv = loop[uv_layer].uv
            uv.x = (uv.x - min_u) / ru
            uv.y = (uv.y - min_v) / rv


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class MESH_OT_create_placeholder(bpy.types.Operator):
    """Separate selected faces into a placeholder mesh with directional UVs"""
    bl_idname  = "mesh.create_placeholder"
    bl_label   = "Create Placeholder"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        return (ctx.active_object is not None
                and ctx.active_object.type == 'MESH'
                and ctx.mode == 'EDIT_MESH')

    def execute(self, ctx):
        settings = ctx.scene.placeholder_tool

        src_obj = None
        for obj in ctx.objects_in_mode:
            if obj.type != 'MESH':
                continue
            bm_tmp = bmesh.from_edit_mesh(obj.data)
            if any(f.select for f in bm_tmp.faces):
                src_obj = obj
                break

        if src_obj is None:
            self.report({'WARNING'}, "No faces selected")
            return {'CANCELLED'}

        ctx.view_layer.objects.active = src_obj
        bm_check = bmesh.from_edit_mesh(src_obj.data)
        if not any(f.select for f in bm_check.faces):
            self.report({'WARNING'}, "No faces selected")
            return {'CANCELLED'}

        bpy.ops.mesh.duplicate()
        bpy.ops.mesh.separate(type='SELECTED')
        bpy.ops.object.mode_set(mode='OBJECT')

        new_obj = next((o for o in ctx.selected_objects if o != src_obj and o.type == 'MESH'), None)
        if new_obj is None:
            self.report({'ERROR'}, "Separation failed")
            return {'CANCELLED'}

        name = get_next_placeholder_name()
        new_obj.name      = name
        new_obj.data.name = name

        # ---- UV unwrap -------------------------------------------------------

        if settings.uv_method == 'FOLLOW_ACTIVE_QUADS':
            bpy.ops.object.select_all(action='DESELECT')
            new_obj.select_set(True)
            ctx.view_layer.objects.active = new_obj
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            if not new_obj.data.uv_layers:
                new_obj.data.uv_layers.new(name="UVMap")
            bm_edit  = bmesh.from_edit_mesh(new_obj.data)
            uv_layer = bm_edit.loops.layers.uv.verify()
            if bm_edit.faces.active is None or len(bm_edit.faces.active.loops) != 4:
                first_quad = next((f for f in bm_edit.faces if len(f.loops) == 4), None)
                if first_quad:
                    bm_edit.faces.active = first_quad
                    bmesh.update_edit_mesh(new_obj.data)
            ok = rectify_active_quad_uvs(bm_edit, uv_layer)
            bmesh.update_edit_mesh(new_obj.data)
            if ok:
                bpy.ops.uv.follow_active_quads(mode='LENGTH_AVERAGE')
            else:
                self.report({'WARNING'}, "Active face is not a quad — falling back to Angle Based unwrap")
                bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=0.001)
            bpy.ops.object.mode_set(mode='OBJECT')
            bm = bmesh.new()
            bm.from_mesh(new_obj.data)
            uv_layer = bm.loops.layers.uv.verify()
            fix_uv_mirror(bm, uv_layer)
            stretch_uvs_to_bounds(bm, uv_layer)
            bm.to_mesh(new_obj.data)
            bm.free()

        elif settings.uv_method == 'KEEP_ORIGINAL':
            pass  # leave UVs untouched

        else:
            if settings.uv_method != 'RECTIFY':
                auto_unwrap(new_obj, settings)
            bm = bmesh.new()
            bm.from_mesh(new_obj.data)
            uv_layer = bm.loops.layers.uv.verify()
            if settings.uv_method == 'RECTIFY':
                rectify_uvs(bm, uv_layer)
            else:
                align_uvs_to_local_z(bm, uv_layer)
            stretch_uvs_to_bounds(bm, uv_layer)
            bm.to_mesh(new_obj.data)
            bm.free()

        # ---- Material --------------------------------------------------------

        assign_material(new_obj, settings.use_checker)

        # ---- Done ------------------------------------------------------------

        bpy.ops.object.select_all(action='DESELECT')
        new_obj.select_set(True)
        ctx.view_layer.objects.active = new_obj
        bpy.ops.object.mode_set(mode='EDIT')

        self.report({'INFO'}, f"Created placeholder: {name}")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class VIEW3D_PT_placeholder_tool(bpy.types.Panel):
    bl_label       = "Placeholder Tool"
    bl_idname      = "VIEW3D_PT_placeholder_tool"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "Placeholder"

    def draw(self, ctx):
        layout   = self.layout
        settings = ctx.scene.placeholder_tool

        layout.label(text="Select faces in Edit Mode", icon='INFO')
        layout.separator()
        layout.operator(MESH_OT_create_placeholder.bl_idname, icon='MOD_UVPROJECT')
        layout.separator()

        box = layout.box()
        box.label(text="Unwrapping Methods", icon='UV')
        box.prop(settings, "uv_method", text="")
        m = settings.uv_method
        if m == 'SMART_UV':
            col = box.column(align=True)
            col.prop(settings, "smart_angle")
            col.prop(settings, "smart_margin")
        elif m == 'LIGHTMAP':
            box.prop(settings, "lightmap_margin")
        elif m == 'CUBE':
            box.prop(settings, "cube_size")
        elif m in ('CYLINDER', 'SPHERE'):
            box.prop(settings, "proj_direction")

        layout.separator()
        layout.prop(settings, "use_checker")
        if settings.use_checker:
            col = layout.column(align=True)
            col.label(text="Green Arrow = Top")
            col.label(text="Red Arrow = Right")


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

classes = (
    PlaceholderToolSettings,
    MESH_OT_create_placeholder,
    VIEW3D_PT_placeholder_tool,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.placeholder_tool = bpy.props.PointerProperty(type=PlaceholderToolSettings)

def unregister():
    del bpy.types.Scene.placeholder_tool
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()
