bl_info = {
    "name": "GLB Texture Resizer",
    "author": "Popi (3D Content Team) + ClaudeAI",
    "version": (1, 2, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > GLB Resizer",
    "description": "Batch resize textures in GLB files",
    "category": "Import-Export",
}

import bpy
import os
import math
import tempfile
import numpy as np
from bpy.props import StringProperty, IntProperty, BoolProperty
from bpy_extras.io_utils import ImportHelper

class GLB_RESIZER_OT_process(bpy.types.Operator):
    """Process GLB files and resize textures"""
    bl_idname = "glb_resizer.process"
    bl_label = "Proceed"
    
    def find_texture_usage_in_materials(self, image):
        """Find where and how a texture is used in materials"""
        usage_info = []
        
        for mat in bpy.data.materials:
            if mat.use_nodes and mat.node_tree:
                for node in mat.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image == image:
                        # Check what socket this connects to
                        for link in mat.node_tree.links:
                            if link.from_node == node:
                                to_socket = link.to_socket.name
                                usage_info.append({
                                    'material': mat.name,
                                    'node': node.name,
                                    'connected_to': to_socket,
                                    'color_space': node.image.colorspace_settings.name if node.image else 'Unknown'
                                })
        
        return usage_info
    
    def determine_texture_type(self, image, usage_info):
        """Determine if texture should be color or non-color based on usage and name"""
        is_color = True
        reason = "default"
        
        # First check by name - this is often more reliable for GLB files
        img_name_lower = image.name.lower()
        
        # ORM texture detection (Occlusion, Roughness, Metallic combined)
        orm_patterns = ['orm', 'occlusionroughnessmetallic', 'occlusion_roughness_metallic']
        for pattern in orm_patterns:
            if pattern in img_name_lower:
                is_color = False
                reason = f"ORM texture detected (name contains '{pattern}')"
                return is_color, reason
        
        # Other non-color patterns
        non_color_patterns = ['normal', 'norm', 'nrm', 'bump', 'height', 'metallic', 'metal', 
                            'roughness', 'rough', 'ao', 'ambient', 'occlusion', 'displacement', 
                            'disp', 'spec', 'specular', 'gloss']
        
        for pattern in non_color_patterns:
            if pattern in img_name_lower:
                is_color = False
                reason = f"name contains '{pattern}'"
                return is_color, reason
        
        # Color patterns that should override
        color_patterns = ['diffuse', 'albedo', 'diff', 'col', 'basecolor', 'base_color']
        for pattern in color_patterns:
            if pattern in img_name_lower:
                is_color = True
                reason = f"name contains '{pattern}'"
                return is_color, reason
        
        # If name doesn't give clear indication, check shader connections
        # But be aware that Separate RGB nodes can confuse this
        for usage in usage_info:
            socket_name = usage['connected_to'].lower()
            # Direct connection to Base Color is definitely color
            if 'base color' in socket_name and 'separate' not in str(usage.get('node', '')).lower():
                is_color = True
                reason = f"directly connected to {usage['connected_to']}"
                break
        
        # If still default and connected to "Color" socket of Separate RGB, likely non-color
        if reason == "default":
            for usage in usage_info:
                if usage['connected_to'] == 'Color' and len(usage_info) == 1:
                    # Single connection to Color socket often means it's going through Separate RGB
                    is_color = False
                    reason = "connected to Color socket (likely through Separate RGB for channel data)"
        
        return is_color, reason
    
    def convert_texture_to_8bit(self, image, is_color_texture):
        """Convert texture to 8-bit using save/load method with proper settings"""
        print(f"\n=== Converting {image.name} to 8-bit ===")
        print(f"  Original format: {image.file_format}")
        print(f"  Is float: {image.is_float}")
        print(f"  Color space: {image.colorspace_settings.name}")
        print(f"  Detected as: {'Color' if is_color_texture else 'Non-Color'} texture")
        
        # Create temp file path
        temp_dir = tempfile.gettempdir()
        temp_filename = f"{image.name}.png"  # Použijeme přímo originální název
        temp_path = os.path.join(temp_dir, temp_filename)
        
        # Store original properties
        original_colorspace = image.colorspace_settings.name
        
        # Get current scene settings
        scene = bpy.context.scene
        settings = scene.render.image_settings
        
        # Store original render settings
        original_format = settings.file_format
        original_color_mode = settings.color_mode  
        original_color_depth = settings.color_depth
        original_view_transform = scene.view_settings.view_transform
        original_look = scene.view_settings.look
        original_exposure = scene.view_settings.exposure
        original_gamma = scene.view_settings.gamma
        
        try:
            # Set up for 8-bit PNG export
            settings.file_format = 'PNG'
            settings.color_mode = 'RGBA' if image.channels == 4 else 'RGB'
            settings.color_depth = '8'
            
            # CRITICAL: Set proper view transform for the texture type
            if not is_color_texture:
                # Non-color data should use Raw/None to preserve values
                scene.view_settings.view_transform = 'Raw'
                scene.view_settings.look = 'None'
                scene.view_settings.exposure = 0
                scene.view_settings.gamma = 1.0
                print(f"  Using Raw view transform for non-color data")
            else:
                # Color textures should use Standard transform
                scene.view_settings.view_transform = 'Standard'
                scene.view_settings.look = 'None'
                scene.view_settings.exposure = 0
                scene.view_settings.gamma = 1.0
                print(f"  Using Standard view transform for color data")
            
            # Save using save_render which respects our settings
            print(f"  Saving to: {temp_path}")
            image.save_render(temp_path, scene=scene)
            
            # Verify file was created
            if not os.path.exists(temp_path):
                raise Exception("Failed to save temporary file")
            
            file_size = os.path.getsize(temp_path)
            print(f"  Saved successfully, file size: {file_size} bytes")
            
            # Load the 8-bit image
            new_image = bpy.data.images.load(temp_path)
            new_image.name = image.name  # Použijeme stejný název jako originál
            
            # IMPORTANT: Set color space AFTER loading
            if not is_color_texture:
                new_image.colorspace_settings.name = 'Non-Color'
                print(f"  Set loaded image colorspace to Non-Color")
            else:
                # Try to preserve original colorspace, fallback to sRGB
                try:
                    new_image.colorspace_settings.name = original_colorspace
                except:
                    new_image.colorspace_settings.name = 'sRGB'
                print(f"  Set loaded image colorspace to {new_image.colorspace_settings.name}")
            
            # Verify the loaded image
            print(f"  Loaded image: {new_image.name}")
            print(f"  Is float: {new_image.is_float} (should be False)")
            print(f"  Size: {new_image.size[0]}x{new_image.size[1]}")
            
            # Pack the new image
            new_image.pack()
            print(f"  Packed new image")
            
            # Replace in all materials
            replaced_count = 0
            for mat in bpy.data.materials:
                if mat.use_nodes and mat.node_tree:
                    for node in mat.node_tree.nodes:
                        if node.type == 'TEX_IMAGE' and node.image == image:
                            # Replace the image
                            node.image = new_image
                            replaced_count += 1
                            print(f"  Replaced in material: {mat.name}, node: {node.name}")
            
            print(f"  Total replacements: {replaced_count}")
            
            # Remove the old image
            old_name = image.name
            bpy.data.images.remove(image)
            print(f"  Removed old image: {old_name}")
            
            # Clean up temp file
            if os.path.exists(temp_path):
                os.remove(temp_path)
                
            return True
            
        except Exception as e:
            print(f"  ERROR during conversion: {str(e)}")
            import traceback
            traceback.print_exc()
            return False
            
        finally:
            # Always restore render settings
            settings.file_format = original_format
            settings.color_mode = original_color_mode
            settings.color_depth = original_color_depth
            scene.view_settings.view_transform = original_view_transform
            scene.view_settings.look = original_look
            scene.view_settings.exposure = original_exposure
            scene.view_settings.gamma = original_gamma
            
            # Clean up temp file if it exists
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except:
                    pass
    
    def execute(self, context):
        props = context.scene.glb_resizer_props
        
        if not props.input_folder:
            self.report({'ERROR'}, "Please select an input folder")
            return {'CANCELLED'}
        
        # Create output folder
        output_folder = os.path.join(props.input_folder, "resized_models")
        if not os.path.exists(output_folder):
            os.makedirs(output_folder)
        
        # Get all GLB files
        glb_files = [f for f in os.listdir(props.input_folder) 
                     if f.lower().endswith('.glb')]
        
        if not glb_files:
            self.report({'WARNING'}, "No GLB files found in the selected folder")
            return {'CANCELLED'}
        
        processed_count = 0
        resized_count = 0
        
        for glb_file in glb_files:
            # Clear scene
            bpy.ops.object.select_all(action='SELECT')
            bpy.ops.object.delete()
            
            # Purge unused data to free memory
            bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
            
            # Import GLB
            file_path = os.path.join(props.input_folder, glb_file)
            try:
                bpy.ops.import_scene.gltf(filepath=file_path)
            except Exception as e:
                self.report({'WARNING'}, f"Failed to import {glb_file}: {str(e)}")
                continue
            
            # Process textures
            texture_resized = False
            texture_converted = False
            max_size = props.texture_size
            
            print(f"\n{'='*50}")
            print(f"Processing {glb_file}:")
            print(f"Found {len(bpy.data.images)} images in total")
            print(f"{'='*50}")
            
            # Process images - create a list first to avoid modifying collection while iterating
            images_to_process = []
            for img in bpy.data.images:
                if img.users > 0 and img.name not in ['Render Result', 'Viewer Node', 'Compositor']:
                    images_to_process.append(img)
            
            for img in images_to_process:
                # Skip if image was already removed (by conversion process)
                if img.name not in bpy.data.images:
                    continue
                    
                width, height = img.size
                print(f"\nProcessing image: {img.name}")
                print(f"  Size: {width}x{height}")
                print(f"  Source: {img.source}")
                print(f"  File format: {img.file_format}")
                
                # Find where this texture is used
                usage_info = self.find_texture_usage_in_materials(img)
                if usage_info:
                    print(f"  Used in materials:")
                    for usage in usage_info:
                        print(f"    - {usage['material']}: {usage['connected_to']}")
                
                # Determine texture type
                is_color_texture, reason = self.determine_texture_type(img, usage_info)
                print(f"  Texture type: {'Color' if is_color_texture else 'Non-Color'} (reason: {reason})")
                
                # Check if resize is needed
                if props.resize_images and (width > max_size or height > max_size):
                    # Calculate new dimensions
                    if width > height:
                        new_width = max_size
                        new_height = int(height * (max_size / width))
                    else:
                        new_height = max_size
                        new_width = int(width * (max_size / height))
                    
                    # Ensure dimensions are at least 1
                    new_width = max(1, new_width)
                    new_height = max(1, new_height)
                    
                    # Resize image
                    img.scale(new_width, new_height)
                    texture_resized = True
                    
                    print(f"  Resized: {width}x{height} -> {new_width}x{new_height}")
                
                # Convert to 8-bit if requested
                if props.convert_to_8bit:
                    # Check if conversion is actually needed
                    needs_conversion = False
                    
                    if img.is_float:
                        needs_conversion = True
                        print(f"  Needs 8-bit conversion: float format")
                    elif hasattr(img, 'depth') and img.depth > 8:
                        needs_conversion = True
                        print(f"  Needs 8-bit conversion: {img.depth}-bit depth")
                    elif img.file_format not in ['PNG', 'JPEG', 'BMP']:
                        needs_conversion = True
                        print(f"  Needs 8-bit conversion: format {img.file_format}")
                    
                    if needs_conversion:
                        if self.convert_texture_to_8bit(img, is_color_texture):
                            texture_converted = True
                        else:
                            print(f"  WARNING: Failed to convert {img.name}")
                    else:
                        print(f"  Already 8-bit, skipping conversion")
            
            # Export if textures were resized or converted
            if texture_resized or (texture_converted and props.convert_to_8bit):
                output_path = os.path.join(output_folder, glb_file)
                try:
                    print(f"\nExporting to: {output_path}")
                    bpy.ops.export_scene.gltf(
                        filepath=output_path,
                        export_format='GLB',
                        export_texcoords=True,
                        export_normals=True,
                        export_materials='EXPORT'
                    )
                    resized_count += 1
                    print(f"Export successful!")
                except Exception as e:
                    self.report({'WARNING'}, f"Failed to export {glb_file}: {str(e)}")
                    print(f"Export failed: {str(e)}")
            
            processed_count += 1
        
        # Clear scene after processing
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete()
        
        # Final purge to clean all remaining data
        bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
        
        self.report({'INFO'}, f"Processed {processed_count} files, resized {resized_count} models")
        return {'FINISHED'}

class GLB_RESIZER_OT_select_folder(bpy.types.Operator, ImportHelper):
    """Select input folder containing GLB files"""
    bl_idname = "glb_resizer.select_folder"
    bl_label = "Select Folder"
    
    # ImportHelper properties
    filename_ext = "."
    use_filter_folder = True
    
    def execute(self, context):
        # Get the directory path
        folder_path = os.path.dirname(self.filepath)
        context.scene.glb_resizer_props.input_folder = folder_path
        return {'FINISHED'}

class GLB_RESIZER_PT_panel(bpy.types.Panel):
    """Creates a Panel in the 3D viewport sidebar"""
    bl_label = "GLB Texture Resizer"
    bl_idname = "GLB_RESIZER_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GLB Resizer"
    
    def draw(self, context):
        layout = self.layout
        props = context.scene.glb_resizer_props
        
        # Input folder
        row = layout.row()
        row.operator("glb_resizer.select_folder", text="Input Folder", icon='FOLDER_REDIRECT')
        
        if props.input_folder:
            box = layout.box()
            box.label(text="Selected folder:", icon='FILE_FOLDER')
            box.label(text=props.input_folder)
        
        # Resize images checkbox
        row = layout.row()
        row.prop(props, "resize_images")
        
        # Texture size (only enabled when resize is checked)
        row = layout.row()
        row.enabled = props.resize_images
        row.prop(props, "texture_size")
        
        # Convert to 8-bit option
        row = layout.row()
        row.prop(props, "convert_to_8bit")
        
        # Process button
        layout.separator()
        row = layout.row()
        row.scale_y = 2.0
        row.operator("glb_resizer.process", icon='PLAY')

class GLBResizerProperties(bpy.types.PropertyGroup):
    input_folder: StringProperty(
        name="Input Folder",
        description="Folder containing GLB files",
        default="",
        subtype='DIR_PATH'
    )
    
    resize_images: BoolProperty(
        name="Resize Images",
        description="Enable texture resizing",
        default=True
    )
    
    texture_size: IntProperty(
        name="Texture Size",
        description="Maximum texture size in pixels",
        default=2048,
        min=128,
        max=8192
    )
    
    convert_to_8bit: BoolProperty(
        name="Convert to 8-bit",
        description="Convert textures to 8-bit color depth",
        default=False
    )

classes = [
    GLBResizerProperties,
    GLB_RESIZER_OT_select_folder,
    GLB_RESIZER_OT_process,
    GLB_RESIZER_PT_panel,
]

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    
    bpy.types.Scene.glb_resizer_props = bpy.props.PointerProperty(type=GLBResizerProperties)

def unregister():
    for cls in classes:
        bpy.utils.unregister_class(cls)
    
    del bpy.types.Scene.glb_resizer_props

if __name__ == "__main__":
    register()