bl_info = {
    "name": "Texture Resizer",
    "author": "Claude",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar (N-panel) > Tex Resize",
    "description": "Resize all image textures in the file to a chosen resolution",
    "category": "Material",
}

import bpy
import os

RESOLUTIONS = [
    ('256', "256px", "Resize long edge to 256px"),
    ('512', "512px", "Resize long edge to 512px"),
    ('1024', "1K (1024px)", "Resize long edge to 1024px"),
    ('2048', "2K (2048px)", "Resize long edge to 2048px"),
    ('4096', "4K (4096px)", "Resize long edge to 4096px"),
]


class TEXRESIZE_Props(bpy.types.PropertyGroup):
    target_size: bpy.props.EnumProperty(
        name="Target Size",
        items=RESOLUTIONS,
        default='2048',
    )
    only_larger: bpy.props.BoolProperty(
        name="Only downscale (skip smaller images)",
        description="Never upscale images that are already smaller than the target",
        default=True,
    )
    remove_alpha: bpy.props.BoolProperty(
        name="Remove alpha channel",
        description="Flatten alpha to fully opaque (1.0) and set alpha_mode to NONE. "
                    "Warning: images with cutout/transparent areas (leaves, glass, "
                    "decals) may show wrong colors where transparency used to be",
        default=False,
    )


class TEXRESIZE_OT_proceed(bpy.types.Operator):
    bl_idname = "texresize.proceed"
    bl_label = "Proceed"
    bl_description = "Resize all image textures to the chosen resolution (destructive - keep a backup!)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.texresize_props
        target = int(props.target_size)
        only_larger = props.only_larger
        remove_alpha = props.remove_alpha

        resized = 0
        skipped = 0
        errors = []

        for img in bpy.data.images:
            if img.name in ("Render Result", "Viewer Node"):
                continue
            w, h = img.size
            if w == 0 or h == 0:
                continue

            long_edge = max(w, h)
            needs_resize = not (only_larger and long_edge <= target)

            if not needs_resize and not remove_alpha:
                skipped += 1
                continue

            scale = target / long_edge if needs_resize else 1.0
            new_w = max(1, round(w * scale))
            new_h = max(1, round(h * scale))

            try:
                was_packed = img.packed_file is not None
                filepath_abs = bpy.path.abspath(img.filepath) if img.filepath else None

                if needs_resize:
                    img.scale(new_w, new_h)

                if remove_alpha and img.channels == 4:
                    pixels = list(img.pixels)
                    pixels[3::4] = [1.0] * (len(pixels) // 4)
                    img.pixels = pixels
                    img.alpha_mode = 'NONE'

                is_color_space = img.colorspace_settings.name not in (
                    'Non-Color', 'Raw', 'Linear'
                )

                if was_packed:
                    img.pack()
                elif filepath_abs and os.path.isdir(os.path.dirname(filepath_abs)):
                    if remove_alpha and is_color_space:
                        # Force a genuine 3-channel (RGB) file on disk.
                        # save_render() is the only API that can drop the
                        # alpha channel from the written file; regular
                        # img.save() always writes all 4 channels.
                        scene = context.scene
                        settings = scene.render.image_settings
                        old_format = settings.file_format
                        old_mode = settings.color_mode
                        try:
                            ext = os.path.splitext(filepath_abs)[1].lower()
                            settings.file_format = {
                                '.png': 'PNG', '.tga': 'TARGA',
                                '.tif': 'TIFF', '.tiff': 'TIFF',
                                '.bmp': 'BMP',
                            }.get(ext, 'PNG')
                            settings.color_mode = 'RGB'
                            img.save_render(filepath_abs, scene=scene)
                        finally:
                            settings.file_format = old_format
                            settings.color_mode = old_mode
                    else:
                        img.filepath_raw = filepath_abs
                        img.save()

                resized += 1
            except Exception as e:
                errors.append(f"{img.name}: {e}")

        msg = f"Resized {resized} image(s), skipped {skipped}."
        if errors:
            msg += f" {len(errors)} error(s) - see console."
            for e in errors:
                print("[Texture Resizer]", e)

        self.report({'WARNING'} if errors else {'INFO'}, msg)
        return {'FINISHED'}


class TEXRESIZE_PT_panel(bpy.types.Panel):
    bl_label = "Texture Resizer"
    bl_idname = "TEXRESIZE_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Tex Resize"

    def draw(self, context):
        layout = self.layout
        props = context.scene.texresize_props

        layout.label(text="Target resolution:")
        layout.prop(props, "target_size", text="")
        layout.prop(props, "only_larger")
        layout.prop(props, "remove_alpha")

        layout.separator()
        col = layout.column()
        col.scale_y = 1.6
        col.operator("texresize.proceed", text="Proceed", icon='FILE_REFRESH')

        layout.separator()
        layout.label(text=f"Images in file: {len(bpy.data.images)}")
        layout.label(text="Tip: backup your file first", icon='ERROR')


classes = (
    TEXRESIZE_Props,
    TEXRESIZE_OT_proceed,
    TEXRESIZE_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.texresize_props = bpy.props.PointerProperty(type=TEXRESIZE_Props)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.texresize_props


if __name__ == "__main__":
    register()
