bl_info = {
    "name": "Params Import & GLTF Export",
    "blender": (4, 3, 2),
    "category": "Object",
    "author": "Filip&Dan",
    "description": "Parses a text file for video type and parameters, then exports a GLTF.",
    "version": (1, 0, 0),
}

import bpy
import os
import re
from bpy.types import Operator, Panel
from bpy.props import StringProperty

PARAM_RANGES = {
    "Highlight_Correction": (0.0, 2.0, 0.01),
    "Shadow_Correction":    (0.0, 2.0, 0.01),
    "Green_Bias":           (0.0, 1.0, 0.001),
    "Cutoff_Lower":         (0.0, 1.0, 0.001),
    "Cutoff_Upper":         (0.0, 1.0, 0.001),
    "Blur_Sigma":           (0.0, 1.0, 0.01),
    "Blur_Range":           (0.1, 20.0, 0.1),
}

original_names = {}

# -------------------------------------------------------------------
# 1) Parsing Logic - Updated for new structure
# -------------------------------------------------------------------
def parse_text_file(content):
    is_3d = False
    placeholder_name = "placeholder"
    lockdown_name = "lockdown"
    placeholder_params = {}
    lockdown_params = {}

    current_block = None  # e.g. "VIDEO_MOCKUP" or "CUTOUT_VIDEO"

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#"):
            if line.startswith("#VIDEO_MOCKUP"):
                current_block = "VIDEO_MOCKUP"
            elif line.startswith("#CUTOUT_VIDEO"):
                current_block = "CUTOUT_VIDEO"
            elif line.startswith("#NLA_TRACKS:"):
                # Handle both old and new format
                parts = line.split(":", 1)
                if len(parts) == 2:
                    val = parts[1].strip().lower()
                    is_3d = (val == "true")
            continue

        if line.startswith("$PLACEHOLDER:"):
            parts = line.split(":", 1)
            if len(parts) == 2:
                placeholder_name = parts[1].strip()
            continue

        if line.startswith("$LOCKDOWN:"):
            parts = line.split(":", 1)
            if len(parts) == 2:
                lockdown_name = parts[1].strip()
            continue

        # Parameter lines - handle both old and new format
        if ":" in line and not line.startswith("#") and not line.startswith("$"):
            parts = line.split(":", 1)
            if len(parts) == 2:
                param = parts[0].strip()
                val_str = parts[1].strip()
                
                if param in PARAM_RANGES:
                    try:
                        float_val = round(float(val_str), 3)
                    except ValueError:
                        continue
                    
                    if current_block == "VIDEO_MOCKUP":
                        placeholder_params[param] = float_val
                    elif current_block == "CUTOUT_VIDEO":
                        lockdown_params[param] = float_val

    return is_3d, placeholder_name, placeholder_params, lockdown_name, lockdown_params

# -------------------------------------------------------------------
# 2) Create Custom Properties - Updated to handle suffixes
# -------------------------------------------------------------------
def parse_and_create_custom_properties(param_dict, object_name):
    """Assign each param as a custom property to object_name (if found in the active view layer)."""
    
    # First try the exact name
    obj = bpy.context.view_layer.objects.get(object_name)
    
    # If not found and the name has a suffix, try without suffix
    if not obj and '_' in object_name:
        base_name = object_name.split('_')[0]
        obj = bpy.context.view_layer.objects.get(base_name)
    
    if not obj:
        print(f"Warning: No object named '{object_name}' or '{object_name.split('_')[0]}' found in active view layer.")
        return

    if "_RNA_UI" not in obj:
        obj["_RNA_UI"] = {}

    for param, value in param_dict.items():
        if param not in PARAM_RANGES:
            continue
        min_val, max_val, step_val = PARAM_RANGES[param]
        obj[param] = value
        obj["_RNA_UI"][param] = {
            "min": min_val,
            "max": max_val,
            "soft_min": min_val,
            "soft_max": max_val,
            "default": value,
            "precision": 3,
            "description": f"Imported from file for {param}",
            "step": step_val,
        }

    # If you want them hidden in the UI, remove the _RNA_UI dictionary:
    if "_RNA_UI" in obj:
        del obj["_RNA_UI"]

    print(f"Custom properties updated on '{obj.name}': {list(param_dict.keys())}")

# -------------------------------------------------------------------
# 3) Remove numeric suffixes (e.g. .001) in the active view layer
# -------------------------------------------------------------------
def remove_suffix_numbers():
    active_scene = bpy.context.scene
    view_layer = bpy.context.view_layer

    def rename_conflicts(target_name):
        # If the same name occurs in other scenes, rename them to avoid collisions
        for scn in bpy.data.scenes:
            if scn == active_scene:
                continue
            for vl in scn.view_layers:
                for ob in vl.objects:
                    if ob.name == target_name:
                        new_name = f"{scn.name}_{ob.name}"
                        if ob not in original_names:
                            original_names[ob] = ob.name
                        ob.name = new_name

    for ob in view_layer.objects:
        old_name = ob.name
        new_name = re.sub(r'\.\d+$', '', old_name)
        if new_name != old_name:
            if ob not in original_names:
                original_names[ob] = old_name
            rename_conflicts(new_name)
            ob.name = new_name

# -------------------------------------------------------------------
# 4) Force Deselect in All Scenes
# -------------------------------------------------------------------
def deselect_all_objects_in_all_scenes():
    """
    Iterates over every scene & view layer in the current window, forcibly calling select_all('DESELECT').
    Leaves us back in the original scene & view layer when done.
    """
    win = bpy.context.window
    original_scene = win.scene
    original_vl = bpy.context.view_layer

    # Step 1: For each scene, for each view layer, do a select_all('DESELECT').
    for scn in bpy.data.scenes:
        win.scene = scn
        for vl in scn.view_layers:
            win.view_layer = vl
            bpy.ops.object.select_all(action='DESELECT')

    # Step 2: Restore original scene and view layer
    win.scene = original_scene
    win.view_layer = original_vl

# -------------------------------------------------------------------
# 5) Export - Updated to handle suffixed placeholder names
# -------------------------------------------------------------------
def export_gltf(is_3d, export_dir, placeholder_name="placeholder"):
    """
    - Fully deselect in all scenes via scene switching.
    - Return to original active scene/view layer.
    - Select placeholder/camera/lockdown if they exist in the active scene + view layer.
    - Export with 'use_selection=True'.
    - Stay in the original scene and view layer at the end.
    """
    win = bpy.context.window
    original_scene = win.scene
    original_vl = bpy.context.view_layer

    # 1) Deselect everything (switch scenes & view layers to ensure full deselect)
    deselect_all_objects_in_all_scenes()

    # Now we're back to original scene & view layer
    active_scene = win.scene
    view_layer = win.view_layer

    # 2) Select the needed objects only
    # Extract base name from placeholder_name (e.g., "placeholder_1" -> "placeholder")
    base_placeholder_name = placeholder_name.split('_')[0] if '_' in placeholder_name else placeholder_name
    
    export_names = [placeholder_name, base_placeholder_name, "camera", "lockdown"]
    valid_objs = []
    
    for name in export_names:
        obj = active_scene.objects.get(name)
        if obj and (name in view_layer.objects):
            obj.select_set(True)
            valid_objs.append(obj)

    # Make one object active to avoid operator errors
    if valid_objs:
        view_layer.objects.active = valid_objs[0]

    # 3) Perform the export
    gltf_path = os.path.join(export_dir, "placeholder.glb")
    bpy.ops.export_scene.gltf(
        filepath=gltf_path,
        export_format='GLB',
        use_selection=True,
        export_cameras=True,
        export_yup=is_3d,
        export_apply=True,
        export_animations=True,
        export_bake_animation=True,
        export_extras=True,
        export_animation_mode='NLA_TRACKS' if is_3d else 'ACTIONS',
    )
    print(f"Exported GLTF to {gltf_path} (NLA_TRACKS={is_3d}, Y-up={is_3d})")

    # 4) (Optional) Re-apply the original scene & view layer 
    #    in case the exporter changed them. 
    #    Actually we should still be in them, but let's be extra sure:
    win.scene = original_scene
    win.view_layer = original_vl

# -------------------------------------------------------------------
# 6) Restore Original Names
# -------------------------------------------------------------------
def restore_original_names():
    """Restores any object that was renamed in remove_suffix_numbers()."""
    for ob, old_name in original_names.items():
        if ob and ob.name != old_name:
            print(f"Restoring {ob.name} -> {old_name}")
            ob.name = old_name
    original_names.clear()

# -------------------------------------------------------------------
# OPERATOR
# -------------------------------------------------------------------
class FILE_OT_video_gltf_export(Operator):
    """Imports text-based params & exports a GLTF."""
    bl_idname = "file.video_gltf_export"
    bl_label = "Import & Export GLTF (Video)"
    filter_glob: StringProperty(default='*.txt', options={'HIDDEN'})
    filepath: StringProperty(
        name="File Path",
        description="Path to the text file",
        maxlen=1024,
        subtype='FILE_PATH'
    )

    def execute(self, context):
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                content = f.read()

            is_3d, placeholder_name, placeholder_params, lockdown_name, lockdown_params = parse_text_file(content)

            remove_suffix_numbers()

            if placeholder_params:
                parse_and_create_custom_properties(placeholder_params, placeholder_name)
            if lockdown_params:
                parse_and_create_custom_properties(lockdown_params, lockdown_name)

            export_dir = os.path.dirname(self.filepath)
            export_gltf(is_3d, export_dir, placeholder_name)

            restore_original_names()

            self.report({'INFO'}, "Export completed successfully!")
        except Exception as e:
            self.report({'ERROR'}, f"Error processing file: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

# -------------------------------------------------------------------
# PANEL
# -------------------------------------------------------------------
class VIDEO_PT_gltf_export_panel(Panel):
    """Creates a panel in the 3D View for your import/export tool."""
    bl_label = "Params Import & GLTF Export"
    bl_idname = "VIDEO_PT_gltf_export_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Params and Export'

    def draw(self, context):
        layout = self.layout
        layout.operator("file.video_gltf_export", text="Import Params & Export GLTF")

# -------------------------------------------------------------------
# REGISTRATION
# -------------------------------------------------------------------
classes = [
    FILE_OT_video_gltf_export,
    VIDEO_PT_gltf_export_panel,
]

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()
