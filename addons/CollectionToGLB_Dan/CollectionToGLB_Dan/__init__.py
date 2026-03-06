bl_info = {
    "name": "Collection(s) to GLB",
    "author": "Daniel Marcin from 3D Content Team (Prompted in Claude AI)",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "View3D > N-Panel > GLB Export",
    "description": "Export collections as GLB with automatic scaling and transforms",
    "category": "Import-Export",
}
 
import bpy
import bmesh
import os
import re
import json
import subprocess
import threading
import zipfile
import tempfile
import shutil
import addon_utils
import urllib.request
from bpy.props import StringProperty, BoolProperty, IntProperty, FloatProperty, EnumProperty, CollectionProperty, PointerProperty
from bpy.types import Panel, Operator, PropertyGroup, UIList
from mathutils import Vector
from bpy.props import BoolProperty

GITHUB_USER = "Dan-3D"
GITHUB_REPO = "blender-addons"
ADDON_FOLDER = "CollectionToGLB_Dan"
CURRENT_VERSION = (1, 0, 0)

update_available = False
latest_release_data = None
update_checking = False

def get_current_version():
    return CURRENT_VERSION

def version_tuple_to_string(v):
    return ".".join(map(str, v))

def check_for_update_background():
    global update_available, latest_release_data, update_checking
    update_checking = True
    try:
        url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/releases"
        req = urllib.request.Request(url, headers={'User-Agent': 'Blender'})
        response = urllib.request.urlopen(req, timeout=10)
        releases = json.loads(response.read().decode())
        
        for release in releases:
            tag = release.get("tag_name", "")
            if tag.startswith(ADDON_FOLDER):
                version_str = tag.replace(f"{ADDON_FOLDER}-v", "").replace(f"{ADDON_FOLDER}-", "")
                try:
                    latest_version = tuple(map(int, version_str.split(".")))
                    if latest_version > CURRENT_VERSION:
                        update_available = True
                        latest_release_data = release
                except:
                    pass
                break
    except Exception as e:
        print(f"Update check failed: {e}")
    update_checking = False

def download_and_install_update():
    global latest_release_data
    if not latest_release_data:
        return False, "No update data available"
    
    try:
        assets = latest_release_data.get("assets", [])
        download_url = None
        
        for asset in assets:
            if asset["name"].endswith(".zip"):
                download_url = asset["browser_download_url"]
                break
        
        if not download_url:
            return False, "No download URL found"
        
        temp_path = os.path.join(bpy.app.tempdir, f"{ADDON_FOLDER}_update.zip")
        urllib.request.urlretrieve(download_url, temp_path)
        
        addons_path = bpy.utils.user_resource('SCRIPTS', path="addons")
        addon_path = os.path.join(addons_path, ADDON_FOLDER)
        
        bpy.ops.preferences.addon_disable(module=ADDON_FOLDER)
        
        if os.path.exists(addon_path):
            shutil.rmtree(addon_path)
        
        with zipfile.ZipFile(temp_path, 'r') as zip_ref:
            zip_ref.extractall(addons_path)
        
        os.remove(temp_path)
        
        bpy.ops.preferences.addon_enable(module=ADDON_FOLDER)
        
        return True, "Update installed! Please restart Blender."
    except Exception as e:
        return False, f"Update failed: {e}"

class UPDATER_OT_check(bpy.types.Operator):
    bl_idname = "updater.check_update"
    bl_label = "Check for Updates"
    bl_description = "Check GitHub for addon updates"
    
    def execute(self, context):
        global update_available, update_checking
        
        if update_checking:
            self.report({'INFO'}, "Already checking for updates...")
            return {'FINISHED'}
        
        update_available = False
        thread = threading.Thread(target=check_for_update_background)
        thread.start()
        thread.join(timeout=15)
        
        if update_available:
            self.report({'INFO'}, "Update available!")
        else:
            self.report({'INFO'}, "You have the latest version")
        
        return {'FINISHED'}

class UPDATER_OT_install(bpy.types.Operator):
    bl_idname = "updater.install_update"
    bl_label = "Install Update"
    bl_description = "Download and install the latest version"
    
    def execute(self, context):
        success, message = download_and_install_update()
        if success:
            self.report({'INFO'}, message)
        else:
            self.report({'ERROR'}, message)
        return {'FINISHED'}

class UPDATER_OT_popup(bpy.types.Operator):
    bl_idname = "updater.update_popup"
    bl_label = "Update Available"
    
    def execute(self, context):
        return {'FINISHED'}
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=300)
    
    def draw(self, context):
        layout = self.layout
        layout.label(text=f"{ADDON_FOLDER} update available!", icon='INFO')
        layout.label(text=f"Current: v{version_tuple_to_string(CURRENT_VERSION)}")
        if latest_release_data:
            tag = latest_release_data.get("tag_name", "")
            layout.label(text=f"Latest: {tag}")
        layout.separator()
        layout.operator("updater.install_update", text="Install Update", icon='IMPORT')

class UPDATER_PT_panel(bpy.types.Panel):
    bl_label = "Addon Updates"
    bl_idname = "UPDATER_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Col2GLB"
    bl_options = {'DEFAULT_CLOSED'}
    
    def draw(self, context):
        layout = self.layout
        layout.label(text=f"Version: {version_tuple_to_string(CURRENT_VERSION)}")
        
        if update_checking:
            layout.label(text="Checking for updates...", icon='TIME')
        elif update_available:
            layout.label(text="Update available!", icon='ERROR')
            layout.operator("updater.install_update", icon='IMPORT')
        else:
            layout.operator("updater.check_update", icon='FILE_REFRESH')

def check_update_on_startup():
    thread = threading.Thread(target=check_for_update_background)
    thread.start()
    return None

def show_update_popup():
    if update_available:
        bpy.ops.updater.update_popup('INVOKE_DEFAULT')
    return None

@bpy.app.handlers.persistent
def startup_handler(dummy):
    bpy.app.timers.register(check_update_on_startup, first_interval=3.0)
    bpy.app.timers.register(show_update_popup, first_interval=8.0)

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

# === PROPERTY GROUPS ===

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
            ('AXIS_ALIGNED', 'Axis-aligned (Vertical)', 'Align islands to vertical axis'),
            ('AXIS_ALIGNED_HORIZONTAL', 'Axis-aligned (Horizontal)', 'Align islands to horizontal axis'),
            ('ANY', 'Any', 'Allow any rotation for optimal packing')
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
    
    enable_uv_pack: BoolProperty(
        name="Pack UVs",
        description="Pack UV islands after unwrapping",
        default=True,
        update=update_uv_pack
    )
    
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
        """Update progress with fallback to console"""
        # Try to set status text in UI
        try:
            if current and total:
                progress = int((current / total) * 100)
                full_message = f"[{current}/{total}] {progress}% - {message}"
            else:
                full_message = message
            context.workspace.status_text_set(full_message)
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
        
        wm = context.window_manager
        collection_lights = []
        
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
            
            # Join
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
            if props.uv_unwrap_method != 'NONE':
                self.update_progress(context, "UV unwrapping...", current_idx, total_count)
                
                if materials_use_uvs:
                    print("Materials use UV coordinates - preserving for baking")
                    
                    existing_uv_names = [uv.name for uv in joined_obj.data.uv_layers]
                    if "UVMap" in existing_uv_names:
                        suffix_num = 1
                        while f"UVMap_{suffix_num:02d}" in existing_uv_names:
                            suffix_num += 1
                        joined_obj.data.uv_layers["UVMap"].name = f"UVMap_{suffix_num:02d}"
                    
                    new_uv = joined_obj.data.uv_layers.new(name="UVMap")
                    joined_obj.data.uv_layers.active = new_uv
                    
                else:
                    print("No UV dependencies - recreating UV maps")
                    
                    while joined_obj.data.uv_layers:
                        joined_obj.data.uv_layers.remove(joined_obj.data.uv_layers[0])
                    
                    joined_obj.data.uv_layers.new(name="UVMap")
                    joined_obj.data.uv_layers.active = joined_obj.data.uv_layers["UVMap"]
                
                # NOW APPLY THE UNWRAPPING (only once, to the active UV layer)
                if props.uv_unwrap_method == 'MOF':
                    if self.apply_mof_unwrap(context, joined_obj):
                        print("Applied MOF UV Unwrap")
                    else:
                        print("Warning: MOF unwrap failed, skipping UV unwrap")
                
                elif props.uv_unwrap_method == 'SMART':
                    bpy.ops.object.mode_set(mode='EDIT')
                    bpy.ops.mesh.select_all(action='SELECT')
                    
                    try:
                        smart_uv_kwargs = {
                            'angle_limit': props.uv_angle_limit,
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
                
                # UV PACKING
                if props.enable_uv_pack:
                    self.update_progress(context, "Packing UVs...", current_idx, total_count)
                    try:
                        context.view_layer.objects.active = joined_obj
                        bpy.ops.object.select_all(action='DESELECT')
                        joined_obj.select_set(True)
                        
                        bpy.ops.object.mode_set(mode='EDIT')
                        bpy.ops.mesh.select_all(action='SELECT')
                        bpy.ops.uv.select_all(action='SELECT')
                        
                        if props.uv_unwrap_method == 'MOF':
                            bpy.ops.uv.average_islands_scale()
                        
                        pack_kwargs = {
                            'margin': props.pack_margin,
                            'rotate': props.pack_rotate,
                            'shape_method': props.pack_shape_method,
                            'scale': props.pack_scale,
                            'rotate_method': props.pack_rotation_method,
                            'margin_method': props.pack_margin_method,
                            'pin': props.pack_lock_pinned,
                            'pin_method': props.pack_lock_method,
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

                        # After successful baking, clean up UVs
                        if materials_use_uvs and props.uv_unwrap_method != 'NONE':
                            uv_names_to_remove = []
                            for uv_layer in joined_obj.data.uv_layers:
                                if uv_layer.name != "UVMap":
                                    uv_names_to_remove.append(uv_layer.name)
                            
                            for uv_name in uv_names_to_remove:
                                if uv_name in joined_obj.data.uv_layers:
                                    joined_obj.data.uv_layers.remove(joined_obj.data.uv_layers[uv_name])
                            
                            print("Cleaned up UV maps after baking")

                    else:
                        print("No materials to bake")
                        
                except Exception as e:
                    print(f"Error during baking: {str(e)}")
                    
                    joined_obj.data.materials.clear()
                    for mat in original_materials:
                        joined_obj.data.materials.append(mat)
                    print("Restored original materials after baking failure")
                    
                    self.report({'WARNING'}, f"Baking failed for {original_name}: {str(e)}")
                    
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
        
        # Restore visibility immediately
        if hasattr(self, 'original_exclude_states'):
            for layer_col, was_excluded in self.original_exclude_states.items():
                try:
                    layer_col.exclude = was_excluded
                except:
                    pass
        
        context.workspace.status_text_set(None)
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
    
    def apply_mof_unwrap(self, context, obj):
        """Apply Ministry of Flat UV unwrapping to an object"""
        props = context.scene.glb_export_props
        
        # Get addon directory and MOF zip path
        addon_dir = os.path.dirname(os.path.realpath(__file__))
        mof_zip_path = os.path.join(addon_dir, "resources", "MinistryOfFlat_Release.zip")
        
        if not os.path.exists(mof_zip_path):
            self.report({'ERROR'}, "MinistryOfFlat_Release.zip not found in resources folder")
            return False
        
        # Extract MOF executable
        try:
            extract_path = tempfile.mkdtemp(prefix="glb_mof_")
            with zipfile.ZipFile(mof_zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_path)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to extract MOF: {e}")
            return False
        
        # Find the executable
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
            shutil.rmtree(extract_path)
            return False
        
        # Store original selection and mode
        original_mode = bpy.context.mode
        
        # Select only our object
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        
        # Triangulate if needed
        if props.mof_triangulate:
            bpy.ops.object.mode_set(mode='OBJECT')
            triang_mod = obj.modifiers.new(name="Triangulate", type='TRIANGULATE')
            triang_mod.min_vertices = 5
            triang_mod.keep_custom_normals = True
            bpy.ops.object.modifier_apply(modifier="Triangulate")
        
        # Apply hard edge seams if needed
        if props.mof_separate_hard_edges:
            bpy.ops.object.mode_set(mode='EDIT')
            bm = bmesh.from_edit_mesh(obj.data)
            for edge in bm.edges:
                if not edge.smooth:  # Sharp edge
                    edge.seam = True
            bmesh.update_edit_mesh(obj.data)
            bpy.ops.object.mode_set(mode='OBJECT')
        
        # Export to OBJ
        temp_dir = bpy.app.tempdir
        name_safe = obj.name.replace(" ", "_")
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
            shutil.rmtree(extract_path)
            return False
        
        # Build MOF command with all parameters
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
        
        # Run MOF
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0 and not os.path.exists(out_path):
                self.report({'ERROR'}, f"MOF failed with code: {result.returncode}")
                shutil.rmtree(extract_path)
                return False
        except Exception as e:
            self.report({'ERROR'}, f"Error running MOF: {e}")
            shutil.rmtree(extract_path)
            return False
        
        # Import the result
        try:
            bpy.ops.wm.obj_import(filepath=out_path, forward_axis='Y', up_axis='Z')
        except Exception as e:
            self.report({'ERROR'}, f"Import failed: {e}")
            shutil.rmtree(extract_path)
            return False
        
        # Transfer UVs back to original object
        imported_obj = context.active_object
        if imported_obj and imported_obj.type == 'MESH':
            # Ensure UV layer exists
            if not obj.data.uv_layers:
                obj.data.uv_layers.new()
            
            # Create data transfer modifier
            context.view_layer.objects.active = obj
            dt_mod = obj.modifiers.new(name="DataTransfer", type='DATA_TRANSFER')
            dt_mod.object = imported_obj
            dt_mod.use_loop_data = True
            dt_mod.data_types_loops = {'UV'}
            dt_mod.loop_mapping = 'TOPOLOGY'
            
            bpy.ops.object.modifier_apply(modifier=dt_mod.name)
            
            # Delete imported object
            bpy.data.objects.remove(imported_obj, do_unlink=True)
            
            # IMPORTANT: Scale UVs to fit within 0-1 range
#            context.view_layer.objects.active = obj
#            bpy.ops.object.mode_set(mode='EDIT')
#            bpy.ops.mesh.select_all(action='SELECT')
#            bpy.ops.uv.select_all(action='SELECT')
            
            # Average island scale first
#            bpy.ops.uv.average_islands_scale()
            
            # Pack to ensure everything is in 0-1 range
#            bpy.ops.uv.pack_islands(margin=0.001)
            
#            bpy.ops.object.mode_set(mode='OBJECT')
        
        # Cleanup
        for fp in (in_path, out_path):
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                except:
                    pass
        
        try:
            shutil.rmtree(extract_path)
        except:
            pass
        
        return True
    
    def analyze_materials(self, materials):
        """Analyze materials to determine what needs baking"""
        data = {
            'color': {'needs_baking': False, 'uniform_value': (0.8, 0.8, 0.8, 1.0), 'has_connections': []},
            'metallic': {'needs_baking': False, 'uniform_value': 0.0, 'has_connections': []},
            'roughness': {'needs_baking': False, 'uniform_value': 0.5, 'has_connections': []},
            'normal': {'needs_baking': False, 'has_connections': []}
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
        
        # Final check - if any material has connections, we need to bake
        if any(data['color']['has_connections']):
            data['color']['needs_baking'] = True
        if any(data['metallic']['has_connections']):
            data['metallic']['needs_baking'] = True
        if any(data['roughness']['has_connections']):
            data['roughness']['needs_baking'] = True
        
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
        self.created_images.append(image)  # Add this line
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
                from_node = link.from_node
                from_socket = link.from_socket
                
                # FIX: Check if this is a texture that needs UV remapping
                if from_node.type in ['TEX_IMAGE', 'TEX_NOISE', 'TEX_VORONOI', 'TEX_MUSGRAVE', 
                                      'TEX_WAVE', 'TEX_BRICK', 'TEX_CHECKER', 'TEX_GRADIENT', 'TEX_MAGIC']:
                    
                    original_uv = "UVMap_01"  # The renamed original UV
                    
                    # Only remap if the original UV exists (meaning we did UV unwrapping)
                    if original_uv in obj.data.uv_layers:
                        vector_input = from_node.inputs.get('Vector')
                        
                        if vector_input:
                            # Store the original connection for restoration
                            original_vector_link = None
                            if vector_input.is_linked:
                                original_vector_link = vector_input.links[0]
                                connections_to_restore.append({
                                    'material': mat,
                                    'from_socket': original_vector_link.from_socket,
                                    'to_socket': vector_input,
                                    'restore_after': True
                                })
                            
                            # Create or update UV Map node to use original UV
                            if vector_input.is_linked:
                                vec_link = vector_input.links[0]
                                vec_node = vec_link.from_node
                                
                                if vec_node.type == 'UVMAP':
                                    # Store original UV map selection
                                    connections_to_restore.append({
                                        'material': mat,
                                        'uv_node': vec_node,
                                        'original_uv_map': vec_node.uv_map,
                                        'restore_uv_map': True
                                    })
                                    # Update to use original UV for baking
                                    vec_node.uv_map = original_uv
                                    print(f"Updated existing UV Map node to use: {original_uv}")
                                    
                                elif vec_node.type == 'TEX_COORD':
                                    # Replace Texture Coordinate with UV Map node
                                    links.remove(vec_link)
                                    read_uv_node = nodes.new('ShaderNodeUVMap')
                                    read_uv_node.uv_map = original_uv
                                    read_uv_node.location = vec_node.location
                                    temp_nodes.append((mat, read_uv_node))
                                    links.new(read_uv_node.outputs['UV'], vector_input)
                                    print(f"Replaced Texture Coordinate with UV Map node using: {original_uv}")
                            else:
                                # No UV connected, create new UV Map node
                                read_uv_node = nodes.new('ShaderNodeUVMap')
                                read_uv_node.uv_map = original_uv
                                read_uv_node.location = (from_node.location[0] - 200, from_node.location[1])
                                temp_nodes.append((mat, read_uv_node))
                                links.new(read_uv_node.outputs['UV'], vector_input)
                                print(f"Created new UV Map node using: {original_uv}")
                
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
        import_col.label(text="Import Blend Files", icon='IMPORT')
        
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
                # Smart UV Project settings
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
                # MOF settings
                uv_col.label(text="MOF General Settings:")
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
        
        # Baking settings
        layout.separator()
        box = layout.box()
        row = box.row()
        row.prop(props, "show_baking", icon='TRIA_DOWN' if props.show_baking else 'TRIA_RIGHT', icon_only=True, emboss=False)
        row.label(text="Material Baking")
        if props.show_baking:
            box.prop(props, "enable_baking")
        
            if props.enable_baking:
                col = box.column(align=True)
                col.prop(props, "bake_ambient_occlusion")
                # Add AO settings when AO is enabled
                if props.bake_ambient_occlusion:
                    ao_col = col.column(align=True)
                    ao_col.prop(props, "ao_samples")
                    ao_col.prop(props, "ao_distance")
                
                col.separator()
                col.prop(props, "bake_resolution")
                col.prop(props, "bake_samples")
                col.prop(props, "bake_margin")
        
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

# === REGISTRATION ===
classes = (   
    GLBExportProperties,
    UPDATER_OT_check,
    UPDATER_OT_install,
    UPDATER_OT_popup,
    UPDATER_PT_panel,
    GLB_OT_CleanupProcessedCollections,
    GLB_OT_OpenExportFolder,
    GLB_OT_ClearImportPath,
    GLB_OT_ImportBlendFiles,
    GLB_OT_SelectImportFolder,
    GLB_OT_ProcessAndExport,
    GLB_PT_ExportPanel,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    
    bpy.types.Scene.glb_export_props = bpy.props.PointerProperty(type=GLBExportProperties)
    bpy.app.handlers.load_post.append(startup_handler)

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
    if startup_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(startup_handler)
    
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    
    del bpy.types.Scene.glb_export_props

if __name__ == "__main__":
    register()






