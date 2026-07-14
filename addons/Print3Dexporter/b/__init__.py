"""Print3Dexporter — Blender add-on entry point.

This file is the package root that Blender loads first.  Its jobs are:
  1. Declare bl_info so Blender recognises the add-on in Preferences.
  2. Import and force-reload all submodules (so F3 > Reload Scripts works
     without restarting Blender).
  3. Collect every class (PropertyGroups, Operators, Panels) into a single
     `classes` tuple and register/unregister them with Blender.
  4. Attach CollectionRenderSettings as a scene-level property so it
     survives file saves and is accessible from any context via
     context.scene.collection_render_settings.

Submodules (all under the `core/` package):
  constants   — version flags and shared name constants
  properties  — all PropertyGroup definitions
  utils       — compositor helpers, visibility helpers, world-shader builder
  glb_exporter— GLB export logic (placeholder-object workflow)
  operators   — all bpy.types.Operator subclasses
  panels      — all bpy.types.Panel subclasses and the UIList
"""

bl_info = {
    "name": "Print3Dexporter",
    "author": "",
    "version": (1, 0, 56),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > CP3D",
    "description": "Render collections with multiple output layers using Cryptomatte",
    "category": "Render",
}
# NOTE: keep this version tuple in sync with core/constants.py VERSION.

import bpy
from bpy.props import PointerProperty

import importlib
from .core import (
    constants, properties, utils, icons, glb_exporter, placeholder_importer,
    convert_compositor, converter, operators, panels,
)

# Always reload submodules so Reload Scripts (F3) picks up code changes
importlib.reload(constants)
importlib.reload(properties)
importlib.reload(utils)
importlib.reload(icons)
importlib.reload(glb_exporter)
importlib.reload(placeholder_importer)
importlib.reload(convert_compositor)
importlib.reload(converter)
importlib.reload(operators)
importlib.reload(panels)

from .core.constants import BLENDER_5
from .core.convert_compositor import ensure_convert_node_trees
from .core.properties import (
    CollectionRenderItem, CollectionRenderSettings, CP3D_LogItem,
    CP3D_MatteLink,
)
from .core.panels import (
    CP3D_UL_collection_list,
    CP3D_UL_log_list,
    CP3D_PT_main_panel,
    CP3D_PT_manual,
    CP3D_PT_convert_trees,
    CP3D_PT_crop_settings,
)
from .core.operators import (
    CP3D_OT_add_active_collection,
    CP3D_OT_remove_collection,
    CP3D_OT_clear_collections,
    CP3D_OT_move_collection_up,
    CP3D_OT_move_collection_down,
    CP3D_OT_use_render_resolution,
    CP3D_OT_pick_cryptomatte_object,
    CP3D_OT_pick_cryptomatte_viewport,
    CP3D_OT_export_glb,
    CP3D_OT_render_and_export,
    CP3D_OT_render_all,
    CP3D_OT_check_setup,
    CP3D_OT_cancel_render,
    CP3D_OT_add_crop_slot,
    CP3D_OT_remove_crop_slot,
    CP3D_OT_optimize_dims,
    CP3D_OT_swap_dims,
    CP3D_OT_copy_dims,
    CP3D_OT_paste_dims,
    CP3D_OT_select_placeholder,
    CP3D_OT_delete_placeholder,
    CP3D_OT_add_matte_link,
    CP3D_OT_remove_matte_link,
    CP3D_OT_import_placeholders,
    CP3D_OT_reload_placeholders,
    CP3D_OT_import_mattes,
    CP3D_OT_convert_highlight,
    CP3D_OT_convert_shadow,
    CP3D_OT_reset_highlight,
    CP3D_OT_reset_shadow,
    CP3D_OT_setup_convert_trees,
    CP3D_OT_clear_log,
)

# ── Class registration list ───────────────────────────────────────────────────
# Order matters: PropertyGroups must come before any Operator or Panel that
# references their bl_rna.  Panels are at the end because they only draw data
# that already exists by the time the UI is built.
classes = (
    # Properties (must be first — operators/panels reference them)
    CP3D_MatteLink,          # referenced by CollectionRenderItem.matte_links
    CollectionRenderItem,
    CP3D_LogItem,            # referenced by CollectionRenderSettings.log_entries
    CollectionRenderSettings,
    # UI lists
    CP3D_UL_collection_list,
    CP3D_UL_log_list,
    # Operators
    CP3D_OT_add_active_collection,
    CP3D_OT_remove_collection,
    CP3D_OT_clear_collections,
    CP3D_OT_move_collection_up,
    CP3D_OT_move_collection_down,
    CP3D_OT_use_render_resolution,
    CP3D_OT_pick_cryptomatte_object,
    CP3D_OT_pick_cryptomatte_viewport,
    CP3D_OT_export_glb,
    CP3D_OT_render_and_export,
    CP3D_OT_render_all,
    CP3D_OT_check_setup,
    CP3D_OT_cancel_render,
    CP3D_OT_add_crop_slot,
    CP3D_OT_remove_crop_slot,
    CP3D_OT_optimize_dims,
    CP3D_OT_swap_dims,
    CP3D_OT_copy_dims,
    CP3D_OT_paste_dims,
    CP3D_OT_select_placeholder,
    CP3D_OT_delete_placeholder,
    CP3D_OT_add_matte_link,
    CP3D_OT_remove_matte_link,
    CP3D_OT_import_placeholders,
    CP3D_OT_reload_placeholders,
    CP3D_OT_import_mattes,
    CP3D_OT_convert_highlight,
    CP3D_OT_convert_shadow,
    CP3D_OT_reset_highlight,
    CP3D_OT_reset_shadow,
    CP3D_OT_setup_convert_trees,
    CP3D_OT_clear_log,
    # Panels
    CP3D_PT_main_panel,
    CP3D_PT_manual,
    CP3D_PT_convert_trees,
    CP3D_PT_crop_settings,
)


# ── Auto-append Convert node trees after every .blend file load ───────────────

@bpy.app.handlers.persistent
def _load_convert_trees(_scene):
    """Append Convert_Highlight and Convert_Shadow from Convert.blend if absent."""
    ensure_convert_node_trees()


# ── Register / Unregister ─────────────────────────────────────────────────────

def register():
    """Register all classes and attach the settings PropertyGroup to Scene."""
    # Load the custom Canva panel-header icon first so panels can draw it.
    icons.register_icons()
    for cls in classes:
        bpy.utils.register_class(cls)
    # Attaching as a PointerProperty on Scene makes settings persistent across
    # undo steps and saves them with the .blend file automatically.
    bpy.types.Scene.collection_render_settings = PointerProperty(
        type=CollectionRenderSettings
    )
    # Append the Convert node trees for the currently open file and for every
    # file opened from now on (load_post fires after each .blend load).
    bpy.app.handlers.load_post.append(_load_convert_trees)
    # Defer the immediate append by one main-loop tick: bpy.data is still in
    # restricted mode during register() so node_groups is not accessible yet.
    bpy.app.timers.register(ensure_convert_node_trees, first_interval=0.0)
    print(f"[CP3D] v{constants.VERSION_STRING} registered (B5={BLENDER_5})")


def unregister():
    """Unregister in reverse order so dependents are removed before their bases."""
    # ── Clean up load_post handler ────────────────────────────────────────────
    try:
        bpy.app.handlers.load_post.remove(_load_convert_trees)
    except ValueError:
        pass
    # ── Clean up depsgraph handlers FIRST — before removing PropertyGroups ─────
    # If _clear_render_done fires after collection_render_settings is deleted
    # (between unregister and register during a script reload), it would crash
    # Blender by accessing a non-existent scene attribute.
    _stale_handlers = [
        fn for fn in bpy.app.handlers.depsgraph_update_post
        if getattr(fn, '__name__', '') == '_clear_render_done'
    ]
    for fn in _stale_handlers:
        try:
            bpy.app.handlers.depsgraph_update_post.remove(fn)
        except ValueError:
            pass
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.collection_render_settings
    # Free the custom-icon preview collection.
    icons.unregister_icons()


# Allows running this file directly from Blender's text editor for quick testing
if __name__ == "__main__":
    register()
