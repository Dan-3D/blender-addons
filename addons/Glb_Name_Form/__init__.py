bl_info = {
    "name": "GLBNameForm",
    "author": "Popi",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "View3D > N-Panel > GLBNameForm",
    "description": "Export active collection as GLB with structured naming convention",
    "category": "Import-Export",
}

import bpy
import os
import re
from datetime import datetime


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class GLBNameFormProperties(bpy.types.PropertyGroup):

    name_prefix: bpy.props.StringProperty(
        name="Name",
        description="Base name of the asset",
        default="",
    )

    date: bpy.props.StringProperty(
        name="Date",
        description="Date in YYYY-MM format",
        default="",
    )

    mockup_number: bpy.props.IntProperty(
        name="Mockup Number",
        description="Mockup index number",
        default=1,
        min=1,
    )

    blend_mode: bpy.props.EnumProperty(
        name="Blending Mode",
        description="b-Blending mode suffix",
        items=[
            ("multiply", "Multiply", "b-multiply"),
            ("alpha",    "Alpha",    "b-alpha"),
        ],
        default="multiply",
    )

    export_path: bpy.props.StringProperty(
        name="Export Folder",
        description="Directory where the GLB file will be saved",
        default="//",
        subtype="DIR_PATH",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def count_placeholders(scene) -> int:
    """Count objects matching placeholder_1, placeholder_2, … in the scene."""
    pattern = re.compile(r"^placeholder_(\d+)$", re.IGNORECASE)
    indices = set()
    for obj in scene.objects:
        m = pattern.match(obj.name)
        if m:
            indices.add(int(m.group(1)))
    return len(indices)


def build_filename(props, placeholder_count: int) -> str:
    date_str = props.date.strip() or datetime.now().strftime("%Y-%m")
    return (
        f"{props.name_prefix}"
        f"__{date_str}"
        f"__{props.mockup_number}"
        f"__b-{props.blend_mode}"
        f"__p-{placeholder_count}"
        f".glb"
    )


def get_active_collection(context):
    """Return the currently active collection from the outliner."""
    return context.view_layer.active_layer_collection.collection


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class GLBNAMEFORM_OT_export_glb(bpy.types.Operator):
    bl_idname = "glb_name_form.export_glb"
    bl_label = "Export GLB"
    bl_description = "Export the active collection as a GLB file with the generated name"

    def execute(self, context):
        props = context.scene.glb_name_form_props
        scene = context.scene

        # --- Validate name_prefix ---
        if not props.name_prefix.strip():
            self.report({"ERROR"}, "Name cannot be empty.")
            return {"CANCELLED"}

        # --- Validate / resolve export path ---
        export_dir = bpy.path.abspath(props.export_path)
        if not os.path.isdir(export_dir):
            try:
                os.makedirs(export_dir, exist_ok=True)
            except Exception as e:
                self.report({"ERROR"}, f"Cannot create export folder: {e}")
                return {"CANCELLED"}

        # --- Count placeholders ---
        placeholder_count = count_placeholders(scene)

        # --- Build file path ---
        filename = build_filename(props, placeholder_count)
        filepath = os.path.join(export_dir, filename)

        # --- Collect objects from active collection (recursive) ---
        collection = get_active_collection(context)

        def collect_objects(col):
            objs = list(col.objects)
            for child in col.children:
                objs.extend(collect_objects(child))
            return objs

        col_objects = collect_objects(collection)

        if not col_objects:
            self.report({"ERROR"}, f'Collection "{collection.name}" contains no objects.')
            return {"CANCELLED"}

        # --- Temporarily select only collection objects ---
        original_selection = [o for o in scene.objects if o.select_get()]
        original_active = context.view_layer.objects.active

        bpy.ops.object.select_all(action="DESELECT")
        for obj in col_objects:
            try:
                obj.select_set(True)
            except RuntimeError:
                pass

        if col_objects:
            context.view_layer.objects.active = col_objects[0]

        # --- Export ---
        bpy.ops.export_scene.gltf(
            filepath=filepath,
            export_format="GLB",
            use_selection=True,
            export_apply=True,
        )

        # --- Restore selection ---
        bpy.ops.object.select_all(action="DESELECT")
        for obj in original_selection:
            try:
                obj.select_set(True)
            except RuntimeError:
                pass
        context.view_layer.objects.active = original_active

        self.report({"INFO"}, f"Exported: {filename}")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class GLBNAMEFORM_PT_panel(bpy.types.Panel):
    bl_label = "GLBNameForm"
    bl_idname = "GLBNAMEFORM_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GLBNameForm"

    def draw(self, context):
        layout = self.layout
        props = context.scene.glb_name_form_props
        scene = context.scene

        # ---- Active collection info ----
        active_col = context.view_layer.active_layer_collection.collection
        col_box = layout.box()
        col_box.label(text="Active Collection", icon="OUTLINER_COLLECTION")
        col_box.label(text=active_col.name)

        layout.separator()

        # ---- Form fields ----
        layout.label(text="Naming", icon="FONT_DATA")
        box = layout.box()
        box.prop(props, "name_prefix")

        # Date row with "Today" shortcut
        date_row = box.row(align=True)
        date_row.prop(props, "date")
        date_row.operator("glb_name_form.set_today", text="", icon="TIME")

        box.prop(props, "mockup_number")
        box.prop(props, "blend_mode")

        layout.separator()

        # ---- Placeholder count (read-only) ----
        ph_count = count_placeholders(scene)
        ph_box = layout.box()
        ph_box.label(text="Placeholders", icon="OBJECT_DATA")
        ph_row = ph_box.row()
        ph_row.label(text=f"Detected:  p-{ph_count}")
        ph_row.operator("glb_name_form.refresh_placeholders", text="", icon="FILE_REFRESH")

        layout.separator()

        # ---- Export path ----
        layout.label(text="Output", icon="EXPORT")
        layout.prop(props, "export_path")

        layout.separator()

        # ---- Preview of generated filename ----
        preview_name = build_filename(props, ph_count)
        preview_box = layout.box()
        preview_box.label(text="Preview:", icon="INFO")
        preview_box.label(text=preview_name)

        layout.separator()

        # ---- Export button ----
        layout.operator(
            "glb_name_form.export_glb",
            text="Export GLB",
            icon="EXPORT",
        )


# ---------------------------------------------------------------------------
# Small utility operators
# ---------------------------------------------------------------------------

class GLBNAMEFORM_OT_set_today(bpy.types.Operator):
    bl_idname = "glb_name_form.set_today"
    bl_label = "Set current month"
    bl_description = "Fill the Date field with the current year and month (YYYY-MM)"

    def execute(self, context):
        context.scene.glb_name_form_props.date = datetime.now().strftime("%Y-%m")
        return {"FINISHED"}


class GLBNAMEFORM_OT_refresh_placeholders(bpy.types.Operator):
    bl_idname = "glb_name_form.refresh_placeholders"
    bl_label = "Refresh placeholder count"
    bl_description = "Re-scan the scene for placeholder_N objects"

    def execute(self, context):
        count = count_placeholders(context.scene)
        self.report({"INFO"}, f"Found {count} placeholder(s).")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Register / Unregister
# ---------------------------------------------------------------------------

CLASSES = [
    GLBNameFormProperties,
    GLBNAMEFORM_OT_export_glb,
    GLBNAMEFORM_OT_set_today,
    GLBNAMEFORM_OT_refresh_placeholders,
    GLBNAMEFORM_PT_panel,
]


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.glb_name_form_props = bpy.props.PointerProperty(
        type=GLBNameFormProperties
    )


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.glb_name_form_props


if __name__ == "__main__":
    register()
