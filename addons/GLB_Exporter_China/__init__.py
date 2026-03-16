bl_info = {
    "name": "GLB Exporter",
    "author": "Dan & Popi from 3D Content Team",
    "version": (1, 0, 2),
    "blender": (4, 2, 0),
    "description": "Export collections as GLB with automatic scaling and material baking",
    "category": "Import-Export",
}

import bpy
import os
import time
from bpy.props import StringProperty, BoolProperty, IntProperty, FloatProperty, PointerProperty
from bpy.types import Panel, Operator, PropertyGroup
from mathutils import Vector

def delayed_cleanup(cleanup_data):
    """Cleanup function that runs after a delay to avoid preview job crashes"""
    
    def do_cleanup():
        for obj in cleanup_data.get('processed_objects', []):
            try:
                if obj and obj.name in bpy.data.objects:
                    bpy.data.objects.remove(obj, do_unlink=True)
            except:
                pass
        
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
        
        for mat in list(bpy.data.materials):
            if mat and "_temp" in mat.name:
                try:
                    bpy.data.materials.remove(mat)
                except:
                    pass
        
        for mat in cleanup_data.get('baked_materials', []):
            try:
                if mat and mat.name in bpy.data.materials:
                    bpy.data.materials.remove(mat)
            except:
                pass
        
        for img in cleanup_data.get('created_images', []):
            try:
                if img and img.name in bpy.data.images:
                    bpy.data.images.remove(img)
            except:
                pass
        
        if "glTF Material Output" in bpy.data.node_groups:
            try:
                bpy.data.node_groups.remove(bpy.data.node_groups["glTF Material Output"])
            except:
                pass
        
        return None
    
    return do_cleanup


class GLBExportProperties(PropertyGroup):
    
    show_baking: BoolProperty(default=True)
    show_export: BoolProperty(default=True)
    
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
        subtype='DISTANCE'
    )
    
    bake_resolution: IntProperty(
        name="Resolution",
        description="Texture resolution for baking",
        default=2048,
        min=128,
        max=8192,
        soft_max=4096
    )

    bake_samples: IntProperty(
        name="Samples",
        description="Number of samples for baking",
        default=100,
        min=1,
        max=4096
    )

    bake_margin: IntProperty(
        name="Margin",
        description="Baking margin in pixels",
        default=64,
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


class GLB_OT_CleanupProcessedCollections(Operator):
    """Delete all _processed collections and purge unused data"""
    bl_idname = "glb_export.cleanup_processed_collections"
    bl_label = "Delete All Processed Collections"
    
    def execute(self, context):
        processed_collections = []
        
        for collection in bpy.data.collections:
            if collection.name.endswith("_processed"):
                processed_collections.append(collection)
        
        if not processed_collections:
            self.report({'INFO'}, "No processed collections found")
            return {'FINISHED'}
        
        for collection in processed_collections:
            bpy.data.collections.remove(collection)
        
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
        
        import subprocess
        import sys
        
        if sys.platform == "win32":
            subprocess.Popen(f'explorer "{export_path}"')
        elif sys.platform == "darwin":
            subprocess.Popen(["open", export_path])
        else:
            subprocess.Popen(["xdg-open", export_path])
        
        return {'FINISHED'}


class GLB_OT_ProcessAndExport(Operator):
    """Process visible collections and export as GLB"""
    bl_idname = "glb_export.process_export"
    bl_label = "Process and Export"
    bl_options = {'REGISTER', 'UNDO'}
    
    _timer = None
    _current_collection = 0
    _total_collections = 0
    _collections_to_process = []
    _is_cancelled = False
    
    def execute(self, context):
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
        self.collections_data = collections_to_process
        
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
        try:
            if current and total:
                progress = int((current / total) * 100)
                full_message = f"[{current}/{total}] {progress}% - {message}"
            else:
                full_message = message
            context.workspace.status_text_set(full_message)
        except:
            pass
        
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
                            
                            try:
                                processed_obj = self.process_temp_collection(
                                    context, 
                                    temp_col['collection'],
                                    collection_name,
                                    self._current_collection + 1, 
                                    self._total_collections
                                )
                                if processed_obj:
                                    self.processed_objects.append(processed_obj)
                            except Exception as e:
                                self.report({'WARNING'}, f"Failed to process {collection_name}: {str(e)}")
                            break
                    
                    self._current_collection += 1
                else:
                    if self.processed_objects and context.scene.glb_export_props.export_enabled:
                        self.export_combined_glb(context)
                    self.finish(context)
                    return {'FINISHED'}
        
        return {'RUNNING_MODAL'}
    
    def duplicate_all_collections(self, context):
        print("=== DUPLICATING ALL COLLECTIONS ===")
        
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

        for obj in all_duplicated_objects:
            if obj.parent:
                world_matrix = obj.matrix_world.copy()
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                bpy.ops.object.parent_clear(type='CLEAR_KEEP_TRANSFORM')
                obj.matrix_world = world_matrix

        print("\n=== CONVERTING ALL TO MESH AND APPLYING MODIFIERS ===")

        for obj in all_duplicated_objects:
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            
            original_type = obj.type
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
                    obj.scale *= scale_factor
                    obj.location *= scale_factor
                    
                    bpy.context.view_layer.objects.active = obj
                    bpy.ops.object.select_all(action='DESELECT')
                    obj.select_set(True)
                    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
                
                center = [(min_coords[i] + max_coords[i]) * 0.5 * scale_factor for i in range(3)]
                
                for obj in all_duplicated_objects:
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
        
        objects_to_remove = []
        for obj in temp_collection.objects:
            if obj.type == 'EMPTY':
                objects_to_remove.append(obj)
            elif obj.type == 'LIGHT':
                objects_to_remove.append(obj)
        
        for obj in objects_to_remove:
            bpy.data.objects.remove(obj, do_unlink=True)
        
        print(f"Removed {len(objects_to_remove)} lights and empties")
        
        mesh_objects = [obj for obj in temp_collection.objects if obj.type == 'MESH']
        print(f"Have {len(mesh_objects)} mesh objects to process")
        
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
            
            bpy.ops.object.join()

            joined_obj = context.active_object
            if not joined_obj:
                print("WARNING: No object after joining! Skipping this collection.")
                return None
            joined_obj.name = original_name
            context.view_layer.update()

            bpy.context.view_layer.objects.active = joined_obj
            bpy.ops.object.select_all(action='DESELECT')
            joined_obj.select_set(True)
            bpy.ops.object.material_slot_remove_unused()
            print(f"Removed unused material slots")
            
            if props.enable_baking:
                original_materials = []
                for slot in joined_obj.material_slots:
                    if slot.material:
                        original_materials.append(slot.material)
                
                self.update_progress(context, f"File: {original_name} | Material: BAKING", current_idx, total_count)

                original_engine = context.scene.render.engine
                original_samples = context.scene.cycles.samples

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
                    context.scene.render.engine = 'CYCLES'
                    context.scene.cycles.samples = props.bake_samples
                    context.view_layer.update()
                    
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
                            
                        if props.bake_ambient_occlusion:
                            print("Baking ambient occlusion...")
                            ao_image = self.create_image(f"{joined_obj.name}_AO", props.bake_resolution, 'Non-Color')
                            self.bake_ambient_occlusion(joined_obj, ao_image)
                            
                            self.create_gltf_output_node(new_mat, ao_image)
                        
                        joined_obj.data.materials.clear()
                        joined_obj.data.materials.append(new_mat)

                        print("Baked materials into textures")
                        
                        # Give GPU time to release resources
                        time.sleep(0.5)

                    else:
                        print("No materials to bake")
                        
                except Exception as e:
                    print(f"Error during baking: {str(e)}")
                    
                    joined_obj.data.materials.clear()
                    for mat in original_materials:
                        joined_obj.data.materials.append(mat)
                    print("Restored original materials after baking failure")
                    
                    self.report({'WARNING'}, f"Baking failed for {original_name}: {str(e)}")
                    
                    # Give GPU time to release resources
                    time.sleep(0.5)
                    
                finally:
                    context.scene.render.engine = original_engine
                    context.scene.cycles.samples = original_samples
                    
                    for attr, value in denoising_settings.items():
                        if hasattr(context.scene.cycles, attr):
                            setattr(context.scene.cycles, attr, value)
            
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
        wm = context.window_manager
        wm.event_timer_remove(self._timer)
        
        if hasattr(self, 'original_exclude_states'):
            for layer_col, was_excluded in self.original_exclude_states.items():
                try:
                    layer_col.exclude = was_excluded
                except:
                    pass
        
        context.workspace.status_text_set(None)
        self.report({'WARNING'}, 'Processing cancelled - cleanup scheduled')
        
        cleanup_data = {
            'processed_objects': getattr(self, 'processed_objects', []),
            'temp_collections': getattr(self, 'temp_collections', []),
            'baked_materials': getattr(self, 'baked_materials', []),
            'created_images': getattr(self, 'created_images', []),
        }
        
        bpy.app.timers.register(delayed_cleanup(cleanup_data), first_interval=1.5)
        
    def finish(self, context):
        wm = context.window_manager
        wm.event_timer_remove(self._timer)
        props = context.scene.glb_export_props
        
        if hasattr(self, 'original_exclude_states'):
            for layer_col, was_excluded in self.original_exclude_states.items():
                try:
                    layer_col.exclude = was_excluded
                except:
                    pass
        
        context.workspace.status_text_set(None)
        
        if props.export_enabled:
            self.report({'INFO'}, f'Successfully processed {self._current_collection} collections and exported GLB')
            
            cleanup_data = {
                'processed_objects': getattr(self, 'processed_objects', []),
                'temp_collections': getattr(self, 'temp_collections', []),
                'baked_materials': getattr(self, 'baked_materials', []),
                'created_images': getattr(self, 'created_images', []),
            }
            
            bpy.app.timers.register(delayed_cleanup(cleanup_data), first_interval=1.5)
        else:
            self.report({'INFO'}, f'Successfully processed {self._current_collection} collections (kept in scene)')
            
            cleanup_data = {
                'processed_objects': [],
                'temp_collections': getattr(self, 'temp_collections', []),
                'baked_materials': [],
                'created_images': [],
            }
            
            bpy.app.timers.register(delayed_cleanup(cleanup_data), first_interval=1.5)
    
    def analyze_materials(self, materials):
        """Analyze materials to determine what needs baking"""
        data = {
            'color': {'needs_baking': False, 'uniform_value': (0.8, 0.8, 0.8, 1.0), 'has_connections': []},
            'metallic': {'needs_baking': False, 'uniform_value': 0.0, 'has_connections': []},
            'roughness': {'needs_baking': False, 'uniform_value': 0.5, 'has_connections': []},
            'normal': {'needs_baking': False, 'has_connections': []}
        }
        
        for mat in materials:
            if not mat.use_nodes:
                continue
                
            principled = self.get_principled_node(mat)
            if not principled:
                continue
            
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
            
            metallic_input = principled.inputs['Metallic']
            if metallic_input.is_linked:
                data['metallic']['has_connections'].append(True)
            else:
                data['metallic']['has_connections'].append(False)
                if not data['metallic']['needs_baking']:
                    if len(materials) == 1:
                        data['metallic']['uniform_value'] = metallic_input.default_value
                    elif 'first_value' in data['metallic']:
                        if abs(data['metallic']['first_value'] - metallic_input.default_value) > 0.001:
                            data['metallic']['needs_baking'] = True
                    else:
                        data['metallic']['first_value'] = metallic_input.default_value
                        data['metallic']['uniform_value'] = metallic_input.default_value
            
            roughness_input = principled.inputs['Roughness']
            if roughness_input.is_linked:
                data['roughness']['has_connections'].append(True)
            else:
                data['roughness']['has_connections'].append(False)
                if not data['roughness']['needs_baking']:
                    if len(materials) == 1:
                        data['roughness']['uniform_value'] = roughness_input.default_value
                    elif 'first_value' in data['roughness']:
                        if abs(data['roughness']['first_value'] - roughness_input.default_value) > 0.001:
                            data['roughness']['needs_baking'] = True
                    else:
                        data['roughness']['first_value'] = roughness_input.default_value
                        data['roughness']['uniform_value'] = roughness_input.default_value
            
            normal_input = principled.inputs['Normal']
            if normal_input.is_linked:
                data['normal']['has_connections'].append(True)
                data['normal']['needs_baking'] = True
        
        if any(data['color']['has_connections']):
            data['color']['needs_baking'] = True
        if any(data['metallic']['has_connections']):
            data['metallic']['needs_baking'] = True
        if any(data['roughness']['has_connections']):
            data['roughness']['needs_baking'] = True
        
        return data
    
    def prepare_materials_for_baking(self, materials, bake_data):
        """Convert differing values to nodes before baking"""
        
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
                    nodes = mat.node_tree.nodes
                    links = mat.node_tree.links
                    
                    rgb_node = nodes.new('ShaderNodeRGB')
                    rgb_node.outputs['Color'].default_value = color_input.default_value[:]
                    rgb_node.location = (principled.location[0] - 300, principled.location[1])
                    rgb_node.label = "Bake Prep Color"
                    
                    links.new(rgb_node.outputs['Color'], color_input)
                    print(f"   - Created RGB node for {mat.name}: {color_input.default_value[:]}")
        
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
                    nodes = mat.node_tree.nodes
                    links = mat.node_tree.links
                    
                    value_node = nodes.new('ShaderNodeValue')
                    value_node.outputs['Value'].default_value = metallic_input.default_value
                    value_node.location = (principled.location[0] - 300, principled.location[1] - 100)
                    value_node.label = "Bake Prep Metallic"
                    
                    links.new(value_node.outputs['Value'], metallic_input)
                    print(f"   - Created Value node for {mat.name}: {metallic_input.default_value}")
        
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
                    nodes = mat.node_tree.nodes
                    links = mat.node_tree.links
                    
                    value_node = nodes.new('ShaderNodeValue')
                    value_node.outputs['Value'].default_value = roughness_input.default_value
                    value_node.location = (principled.location[0] - 300, principled.location[1] - 200)
                    value_node.label = "Bake Prep Roughness"
                    
                    links.new(value_node.outputs['Value'], roughness_input)
                    print(f"   - Created Value node for {mat.name}: {roughness_input.default_value}")
    
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
            
            original_output_link = None
            if output_node.inputs['Surface'].is_linked:
                original_output_link = output_node.inputs['Surface'].links[0]
                connections_to_restore.append({
                    'material': mat,
                    'from_socket': original_output_link.from_socket,
                    'to_socket': output_node.inputs['Surface']
                })
            
            tex_node = nodes.new('ShaderNodeTexImage')
            tex_node.image = target_image
            tex_node.select = True
            temp_nodes.append((mat, tex_node))
            
            nodes.active = tex_node
            
            channel_input = principled.inputs[channel_name]
            
            if channel_input.is_linked:
                link = channel_input.links[0]
                from_node = link.from_node
                from_socket = link.from_socket
                
                links.remove(link)
                links.new(from_socket, output_node.inputs['Surface'])
                connections_to_restore.append({
                    'material': mat,
                    'from_socket': from_socket,
                    'to_socket': channel_input,
                    'restore_after': True
                })
            else:
                if channel_name == 'Base Color':
                    value_node = nodes.new('ShaderNodeRGB')
                    value_node.outputs['Color'].default_value = channel_input.default_value
                    links.new(value_node.outputs['Color'], output_node.inputs['Surface'])
                else:
                    value_node = nodes.new('ShaderNodeValue')
                    value_node.outputs['Value'].default_value = channel_input.default_value
                    links.new(value_node.outputs['Value'], output_node.inputs['Surface'])
                
                temp_nodes.append((mat, value_node))
        
        for mat, tex_node in temp_nodes:
            if tex_node.type == 'TEX_IMAGE':
                tex_node.select = True
        
        bpy.ops.object.bake(type=bake_type, use_clear=True, margin=props.bake_margin)
        
        for conn in connections_to_restore:
            mat = conn['material']
            if 'restore_after' in conn:
                mat.node_tree.links.new(conn['from_socket'], conn['to_socket'])
            else:
                mat.node_tree.links.new(conn['from_socket'], conn['to_socket'])
        
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
    
    def bake_ambient_occlusion(self, obj, target_image):
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
        
        bpy.ops.object.bake(type='AO', use_clear=True, margin=props.bake_margin)
        
        bpy.context.scene.cycles.samples = original_samples
        
        for mat, node in temp_nodes:
            mat.node_tree.nodes.remove(node)

    def create_gltf_output_node(self, material, ao_image):
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
        
        links.new(ao_tex_node.outputs['Color'], gltf_node.inputs['Occlusion'])


class GLB_PT_ExportPanel(Panel):
    """Main panel for GLB Exporter"""
    bl_label = "GLB Exporter"
    bl_idname = "GLB_PT_export_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GLB Export"
    
    def draw(self, context):
        layout = self.layout
        props = context.scene.glb_export_props
        
        # Baking settings
        box = layout.box()
        row = box.row()
        row.prop(props, "show_baking", icon='TRIA_DOWN' if props.show_baking else 'TRIA_RIGHT', icon_only=True, emboss=False)
        row.label(text="Material Baking")
        if props.show_baking:
            box.prop(props, "enable_baking")
        
            if props.enable_baking:
                col = box.column(align=True)
                col.prop(props, "bake_resolution")
                col.prop(props, "bake_samples")
                col.prop(props, "bake_margin")
                
                col.separator()
                col.prop(props, "bake_ambient_occlusion")
                if props.bake_ambient_occlusion:
                    ao_col = col.column(align=True)
                    ao_col.prop(props, "ao_samples")
                    ao_col.prop(props, "ao_distance")
        
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
        
        button_text = "Process Visible Collections"
        if props.export_enabled:
            button_text = "Process & Export"
        col.operator("glb_export.process_export", text=button_text, icon='PLAY')


classes = (
    GLBExportProperties,
    GLB_OT_CleanupProcessedCollections,
    GLB_OT_OpenExportFolder,
    GLB_OT_ProcessAndExport,
    GLB_PT_ExportPanel,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    
    bpy.types.Scene.glb_export_props = bpy.props.PointerProperty(type=GLBExportProperties)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    
    del bpy.types.Scene.glb_export_props

if __name__ == "__main__":
    register()





