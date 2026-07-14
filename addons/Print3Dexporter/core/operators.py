"""Print3Dexporter — all bpy.types.Operator subclasses.

This module contains every operator registered by the add-on.  Operators are
the "actions" that buttons and menus trigger.

Operators in this file:
  CP3D_OT_add_active_collection  — add the outliner-selected collection to the list
  CP3D_OT_remove_collection      — remove the active list item
  CP3D_OT_clear_collections      — clear the entire list
  CP3D_OT_move_collection_up     — move active item one position up in the list
  CP3D_OT_move_collection_down   — move active item one position down in the list
  CP3D_OT_use_render_resolution  — copy scene render res to active item
  CP3D_OT_pick_cryptomatte_object   — pick crop object from active selection
  CP3D_OT_pick_cryptomatte_viewport — modal viewport eyedropper for crop object
  CP3D_OT_add_crop_slot          — add a placeholder object slot (max 10)
  CP3D_OT_remove_crop_slot       — remove the last placeholder slot
  CP3D_OT_export_glb             — run the GLB placeholder export
  CP3D_OT_render_all             — the main batch render operator (see below)

The most complex operator is CP3D_OT_render_all.  Its execute() method:
  1. Builds a render_list of (collection, name, res, suffix, output_name, item) tuples
     for every enabled pass across all enabled collections.
  2. Wraps the entire loop in try/finally so the scene is always restored to
     its original state — even if Cycles crashes or a Python error occurs.
  3. For each pass, temporarily changes collection visibility, lights, camera,
     compositor graph, and the per-collection World, then calls
     bpy.ops.render.render(write_still=True).
  4. Highlight and Shadow follow the Render pass directly: they re-render with
     Cycles' warm persistent_data cache and route the beauty image through the
     compositor's "Highlight" / "Shadow" node (an RGB-curve adjustment).
"""

import bpy
import os
import gc
import time

from bpy.types import Operator
from bpy.props import StringProperty, IntProperty


# ── Inter-collection pause (seconds) ──────────────────────────────────────────
# Between each Collection in the batch we briefly pause so Blender can let
# Cycles free its BVH cache, flush image buffers, and generally settle.
# Prevents the cumulative slowdown seen in long batches.  Keep small — it's
# a "breather", not a delay.
CP3D_INTER_COLLECTION_PAUSE = 0.5


# ── Render-done auto-clear ───────────────────────────────────────────────────
# After a successful batch render the UI shows a green "Done" state.
# A depsgraph handler is registered (with a short delay so our own property
# changes settle first) that clears the state on the NEXT user interaction.
#
# IMPORTANT — Reload Safety:
#   When the user clicks "Reload Scripts" (or presses F3 → Reload Scripts),
#   Blender calls unregister() then register().  Between those two calls the
#   PropertyGroup `collection_render_settings` is deleted from the Scene type.
#   If a stale _clear_render_done handler fires during that gap, accessing
#   scene.collection_render_settings would crash Blender.
#
#   To prevent this:
#     1. _clear_render_done wraps ALL logic in try/except.
#     2. unregister() in __init__.py removes any lingering handlers BEFORE
#        deleting the PropertyGroup.
#     3. On module reload (importlib.reload) we scrub stale handler references
#        left over from the previous module load (see cleanup block below).

# Scrub stale _clear_render_done handlers left from a previous module load.
# After importlib.reload(operators) the OLD function object is replaced, but
# Blender's handler list still holds a reference to it.  We identify them by
# __name__ and remove them so only the new function is ever registered.
_stale = [fn for fn in bpy.app.handlers.depsgraph_update_post
          if getattr(fn, '__name__', '') == '_clear_render_done']
for _fn in _stale:
    try:
        bpy.app.handlers.depsgraph_update_post.remove(_fn)
    except ValueError:
        pass
del _stale


def _clear_render_done(scene):
    """depsgraph handler — clears render_done on the first genuine user interaction.

    Wrapped in try/except so that a stale reference surviving across a script
    reload cannot crash Blender by accessing deleted PropertyGroups.
    """
    try:
        s = scene.collection_render_settings
        if s.render_done:
            s.render_done = False
            s.render_message = ""
            for item in s.collections:
                item.was_rendered = False
    except Exception:
        pass
    # Remove self so it doesn't fire again
    try:
        bpy.app.handlers.depsgraph_update_post.remove(_clear_render_done)
    except ValueError:
        pass
    # Force sidebar redraw so the green disappears immediately
    try:
        if bpy.context and bpy.context.screen:
            for area in bpy.context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass


# Seconds after which the red/green "Done" button state clears on its own.
# The depsgraph handler below clears it on the first scene interaction, but a
# plain UI click doesn't always touch the depsgraph — this timer guarantees
# the buttons return to their normal colour even if the user does nothing.
CP3D_RENDER_DONE_CLEAR_SECONDS = 5.0


def _clear_render_done_timeout():
    """Timer callback — unconditionally clear the render-done button state."""
    try:
        for scene in bpy.data.scenes:
            s = scene.collection_render_settings
            if s.render_done:
                s.render_done = False
                s.render_message = ""
                for item in s.collections:
                    item.was_rendered = False
    except Exception:
        pass
    # The interaction-based handler is no longer needed once we've cleared
    try:
        bpy.app.handlers.depsgraph_update_post.remove(_clear_render_done)
    except ValueError:
        pass
    except Exception:
        pass
    # Force sidebar redraw so the colour reverts immediately
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
    except Exception:
        pass
    return None   # one-shot timer


def _schedule_render_done_clear():
    """After a 1-second delay (so our own property writes settle),
    register a one-shot depsgraph handler that clears the green state
    on the next user interaction (click, select, anything) — plus a
    timeout timer that clears it unconditionally a few seconds later."""
    def _delayed():
        try:
            if _clear_render_done not in bpy.app.handlers.depsgraph_update_post:
                bpy.app.handlers.depsgraph_update_post.append(_clear_render_done)
        except Exception:
            pass
        return None   # don't repeat the timer
    try:
        bpy.app.timers.register(_delayed, first_interval=1.0)
    except Exception:
        pass
    try:
        bpy.app.timers.register(_clear_render_done_timeout,
                                first_interval=CP3D_RENDER_DONE_CLEAR_SECONDS)
    except Exception:
        pass

from .utils import (
    find_layer_collection,
    find_camera_in_collection,
    get_objects_recursive,
    setup_collection_visibility,
    setup_lights_for_collection,
    backup_child_lc_states,
    restore_child_lc_states,
    setup_crop_alpha_compositor,
    backup_compositor,
    restore_compositor,
    assign_placeholder_mat,
    isolate_object_for_render,
    restore_render_isolation,
    isolate_placeholders_collection,
    restore_placeholders_isolation,
    setup_matte_holdout_objects,
    restore_matte_holdout_objects,
    exclude_helper_collections_for_render,
    snapshot_datablock_names,
    purge_leaked_orphans,
    get_render_output_dir,
    get_node_tree,
    swap_compositor,
    create_temp_compositor,
    remove_temp_compositor,
    add_separate_pass_exr_outputs,
    finalize_separate_exr_outputs,
    _is_placeholders_col,
)


# ── Per-pass EXR output configuration ─────────────────────────────────────────
# Each entry is (RenderLayers output socket name, file suffix).  For every
# enabled pass listed here we get a separate .exr file named
# "<collection_name>_<pass_suffix>_<index>.exr".  Passes the user hasn't
# enabled on the view layer are silently skipped.
#
# To add more passes, append to this list — no other code changes needed.
# Common socket names:
#   'IndexOB', 'IndexMA', 'AO', 'DiffDir', 'GlossDir', 'TransDir',
#   'DiffInd', 'GlossInd', 'Mist', 'Normal', 'Position', 'Depth', …
CP3D_EXR_PASS_OUTPUTS = [
    ('IndexMA', 'IndexMA'),   # Material Index — the first one requested
]
from .glb_exporter import export_crop_glb
from .placeholder_importer import (
    import_placeholders, import_mattes, reload_placeholders,
)
from .convert_compositor import (
    ensure_convert_node_trees,
    dedupe_convert_groups,
    reset_highlight_node_tree,
    reset_shadow_node_tree,
    set_convert_value,
    HIGHLIGHT_TREE_NAME,
    SHADOW_TREE_NAME,
    CROP_TREE_NAME,
)
from .converter import convert_render_image, convert_crop_image, find_render_image_path
from .properties import log_add


# ── Outliner multi-selection helper ───────────────────────────────────────────

def _get_outliner_selected_collections(context):
    """Return all collections selected in any visible Outliner area.

    Uses bpy.context.temp_override to read selected_ids from each OUTLINER
    area without requiring the Outliner to be the active window area.  This
    lets the user Shift+Click multiple collections in the Outliner and then
    click the + button in the VIEW_3D N-panel to add them all at once.
    Falls back to an empty list when no Outliner is on screen or the API
    is unavailable.
    """
    selected = []
    scene_col = context.scene.collection
    seen = set()
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != 'OUTLINER':
                continue
            region = next(
                (r for r in area.regions if r.type == 'WINDOW'), None
            )
            if region is None:
                continue
            try:
                with bpy.context.temp_override(
                        window=window, area=area, region=region):
                    ids = bpy.context.selected_ids
                    if ids:
                        for id_block in ids:
                            if (isinstance(id_block, bpy.types.Collection)
                                    and id_block != scene_col
                                    and id(id_block) not in seen):
                                seen.add(id(id_block))
                                selected.append(id_block)
            except Exception:
                pass
    return selected


class CP3D_OT_add_active_collection(Operator):
    """Add the active (or all Outliner-selected) collection(s) to the render list.

    When multiple collections are selected in the Outliner via Shift+Click,
    all of them are added at once.  Falls back to the single active layer
    collection when nothing is multi-selected.
    """
    bl_idname = "cp3d.add_active_collection"
    bl_label = "Add Active Collection"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.collection_render_settings
        scene_col = context.scene.collection

        # Try multi-select from the Outliner first
        cols_to_add = _get_outliner_selected_collections(context)

        # Fall back to the single active layer collection
        if not cols_to_add:
            active_lc = context.view_layer.active_layer_collection
            if not active_lc or active_lc.collection == scene_col:
                self.report({'WARNING'}, "Select a collection in the outliner first")
                return {'CANCELLED'}
            cols_to_add = [active_lc.collection]

        added, skipped = 0, 0
        for col in cols_to_add:
            if any(item.collection == col for item in settings.collections):
                skipped += 1
                continue
            item = settings.collections.add()
            item.collection = col
            item.resolution_x = context.scene.render.resolution_x
            item.resolution_y = context.scene.render.resolution_y
            added += 1

        if added:
            settings.active_index = len(settings.collections) - 1

        if added and skipped:
            self.report({'INFO'}, f"Added {added}, skipped {skipped} (already in list)")
        elif added:
            self.report({'INFO'}, f"Added {added} collection(s)")
        elif skipped:
            self.report({'INFO'}, f"All {skipped} selected collection(s) already in list")
        else:
            self.report({'WARNING'}, "Select a collection in the outliner first")
            return {'CANCELLED'}

        return {'FINISHED'}


# ── Collection list management ────────────────────────────────────────────────

class CP3D_OT_remove_collection(Operator):
    """Remove the active collection from the render list."""
    bl_idname = "cp3d.remove_collection"
    bl_label = "Remove"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.collection_render_settings
        if 0 <= s.active_index < len(s.collections):
            s.collections.remove(s.active_index)
            # Clamp index so it doesn't point past the end of the shrunk list
            s.active_index = min(s.active_index, len(s.collections) - 1)
        return {'FINISHED'}


class CP3D_OT_clear_collections(Operator):
    """Remove all collections from the render list."""
    bl_idname = "cp3d.clear_collections"
    bl_label = "Clear All"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.collection_render_settings
        s.collections.clear()
        s.active_index = 0
        return {'FINISHED'}


class CP3D_OT_move_collection_up(Operator):
    """Move the active collection one position up in the render list."""
    bl_idname = "cp3d.move_collection_up"
    bl_label = "Move Up"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        s = ctx.scene.collection_render_settings
        return s.active_index > 0

    def execute(self, context):
        s = context.scene.collection_render_settings
        idx = s.active_index
        s.collections.move(idx, idx - 1)
        s.active_index = idx - 1
        return {'FINISHED'}


class CP3D_OT_move_collection_down(Operator):
    """Move the active collection one position down in the render list."""
    bl_idname = "cp3d.move_collection_down"
    bl_label = "Move Down"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        s = ctx.scene.collection_render_settings
        return s.active_index < len(s.collections) - 1

    def execute(self, context):
        s = context.scene.collection_render_settings
        idx = s.active_index
        s.collections.move(idx, idx + 1)
        s.active_index = idx + 1
        return {'FINISHED'}


class CP3D_OT_use_render_resolution(Operator):
    """Copy the current scene render resolution into the active list item."""
    bl_idname = "cp3d.use_render_resolution"
    bl_label = "Use Render Res"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        s = ctx.scene.collection_render_settings
        return 0 <= s.active_index < len(s.collections)

    def execute(self, ctx):
        s = ctx.scene.collection_render_settings
        i = s.collections[s.active_index]
        i.resolution_x = ctx.scene.render.resolution_x
        i.resolution_y = ctx.scene.render.resolution_y
        return {'FINISHED'}


# ── Cryptomatte / crop object pickers ─────────────────────────────────────────

class CP3D_OT_pick_cryptomatte_object(Operator):
    """Set a crop slot's object name from the currently active scene object."""
    bl_idname = "cp3d.pick_cryptomatte_object"
    bl_label = "Pick"
    bl_options = {'REGISTER', 'UNDO'}

    # Which crop slot to write to (A, B, or C) — set by the button in the panel
    slot: StringProperty()

    def execute(self, ctx):
        s = ctx.scene.collection_render_settings
        if not ctx.active_object:
            return {'CANCELLED'}
        if not (0 <= s.active_index < len(s.collections)):
            return {'CANCELLED'}
        item = s.collections[s.active_index]
        nm = ctx.active_object.name
        if self.slot in 'ABCDEFGHIJ':
            setattr(item, f"crop_{self.slot.lower()}_name", nm)
        return {'FINISHED'}


class CP3D_OT_pick_cryptomatte_viewport(Operator):
    """Interactive viewport eyedropper — click an object to assign it to a crop slot."""
    bl_idname = "cp3d.pick_cryptomatte_viewport"
    bl_label = "Pick Viewport"
    bl_options = {'REGISTER', 'UNDO'}

    # Which crop slot to write to — set when the button calls this operator
    slot: StringProperty()

    def modal(self, ctx, ev):
        if ev.type == 'LEFTMOUSE' and ev.value == 'PRESS':
            co = (ev.mouse_region_x, ev.mouse_region_y)
            rg = ctx.region
            rv = ctx.region_data
            if rg and rv:
                from bpy_extras import view3d_utils
                # Convert 2D screen coordinates to a 3D ray for raycasting
                vv = view3d_utils.region_2d_to_vector_3d(rg, rv, co)
                ro = view3d_utils.region_2d_to_origin_3d(rg, rv, co)
                ok, lc, nm, ix, ob, mx = ctx.scene.ray_cast(
                    ctx.evaluated_depsgraph_get(), ro, vv
                )
                if ok and ob:
                    s = ctx.scene.collection_render_settings
                    if 0 <= s.active_index < len(s.collections):
                        item = s.collections[s.active_index]
                        if self.slot in 'ABCDEFGHIJ':
                            setattr(item, f"crop_{self.slot.lower()}_name", ob.name)
                    ctx.window.cursor_modal_restore()
                    return {'FINISHED'}
            ctx.window.cursor_modal_restore()
            return {'CANCELLED'}
        elif ev.type in {'RIGHTMOUSE', 'ESC'}:
            # User cancelled the pick — restore cursor and exit without changes
            ctx.window.cursor_modal_restore()
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def invoke(self, ctx, ev):
        if ctx.area.type != 'VIEW_3D':
            return {'CANCELLED'}
        # Change the cursor to an eyedropper to signal the modal is active
        ctx.window.cursor_modal_set('EYEDROPPER')
        ctx.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}


# ── Crop slot management ──────────────────────────────────────────────────────

class CP3D_OT_add_crop_slot(Operator):
    """Add another placeholder object slot (max 10)."""
    bl_idname = "cp3d.add_crop_slot"
    bl_label = "Add Slot"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        s = ctx.scene.collection_render_settings
        if not (0 <= s.active_index < len(s.collections)):
            return False
        # Button is greyed out once all ten slots are already active
        return s.collections[s.active_index].crop_slot_count < 10

    def execute(self, ctx):
        s = ctx.scene.collection_render_settings
        item = s.collections[s.active_index]
        item.crop_slot_count += 1
        # Enable the newly revealed slot so it participates in renders
        _letters = 'abcdefghij'
        new_idx = item.crop_slot_count - 1   # 0-based index of the new slot
        if 1 <= new_idx <= 9:
            setattr(item, f"crop_{_letters[new_idx]}_enabled", True)
        return {'FINISHED'}


class CP3D_OT_remove_crop_slot(Operator):
    """Remove the last placeholder object slot, clearing its name and enabled state."""
    bl_idname = "cp3d.remove_crop_slot"
    bl_label = "Remove Slot"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        s = ctx.scene.collection_render_settings
        if not (0 <= s.active_index < len(s.collections)):
            return False
        # Button is greyed out when only one slot remains
        return s.collections[s.active_index].crop_slot_count > 1

    def execute(self, ctx):
        s = ctx.scene.collection_render_settings
        item = s.collections[s.active_index]
        # Clear the slot being removed so it doesn't silently reappear
        _letters = 'abcdefghij'
        remove_idx = item.crop_slot_count - 1   # 0-based index of slot to remove
        if 1 <= remove_idx <= 9:
            sl = _letters[remove_idx]
            setattr(item, f"crop_{sl}_enabled", False)
            setattr(item, f"crop_{sl}_name", "")
        item.crop_slot_count -= 1
        return {'FINISHED'}


# ── Per-slot Width / Height tools (Optimize / Swap / Copy / Paste) ────────────

# Target band for the Optimize tool.  Both dimensions should end up within
# [DIM_LOW, DIM_HIGH]; DIM_IDEAL is the sweet-spot we scale toward.
DIM_LOW, DIM_HIGH, DIM_IDEAL = 1000, 8000, 4000


def _optimize_dims(w, h):
    """Return (new_w, new_h): *w*,*h* scaled to sit in [DIM_LOW, DIM_HIGH].

    The aspect ratio is always preserved (both are multiplied by the same
    factor).  The larger dimension is aimed at DIM_IDEAL (~4000), clamped so
    neither dimension leaves the band:

      - ratio ≤ 4:1  → larger becomes ~DIM_IDEAL, smaller stays ≥ DIM_LOW.
      - 4:1 < ratio ≤ 8:1 → smaller pinned to DIM_LOW, larger ≤ DIM_HIGH.
      - ratio > 8:1  → impossible to keep BOTH in band while preserving ratio;
                       the larger is pinned to DIM_HIGH so the smaller is as
                       large as it can be (it drops below DIM_LOW — the one
                       break the spec explicitly allows).

    Zero/negative inputs are returned unchanged (nothing sensible to scale).
    """
    if w <= 0 or h <= 0:
        return w, h
    maxv = float(max(w, h))
    minv = float(min(w, h))
    k_min = DIM_LOW / minv     # smallest factor keeping the smaller dim ≥ LOW
    k_max = DIM_HIGH / maxv     # largest factor keeping the larger dim ≤ HIGH
    k_ideal = DIM_IDEAL / maxv  # factor bringing the larger dim to IDEAL
    if k_min <= k_max:
        # Feasible band exists — target IDEAL, clamped into [k_min, k_max].
        k = min(max(k_ideal, k_min), k_max)
    else:
        # Ratio too extreme (> 8:1): pin the larger dim to the HIGH ceiling so
        # the smaller is maximised (still < LOW — the allowed break).
        k = k_max
    return max(1, int(round(w * k))), max(1, int(round(h * k)))


def _active_slot_item(ctx):
    """Return the active CollectionRenderItem, or None if the index is invalid."""
    s = ctx.scene.collection_render_settings
    if 0 <= s.active_index < len(s.collections):
        return s.collections[s.active_index]
    return None


_CROP_SLOT_LETTERS = 'abcdefghij'
_CROP_SLOT_FIELDS = ('name', 'width', 'height', 'enabled', 'matte')


def _remove_crop_slot_shift(item, slot_letter):
    """Remove crop slot *slot_letter* (A–J) from *item*, shifting later slots up.

    Deletes the slot at the given letter's index and moves every subsequent
    active slot's data (name / width / height / enabled / legacy matte) down one
    position so the list has no empty gap.  Matte links are updated too: links
    on the removed slot are dropped, and links on later slots are re-lettered
    down by one.  crop_slot_count is decremented (never below 1 — a single
    empty slot always remains).
    """
    di = _CROP_SLOT_LETTERS.index(slot_letter.lower())
    n = max(1, min(item.crop_slot_count, len(_CROP_SLOT_LETTERS)))

    # 1. Shift slot data up: slot j ← slot j+1, for j from di to n-2.
    for j in range(di, n - 1):
        dst = _CROP_SLOT_LETTERS[j]
        src = _CROP_SLOT_LETTERS[j + 1]
        for suf in _CROP_SLOT_FIELDS:
            setattr(item, f"crop_{dst}_{suf}", getattr(item, f"crop_{src}_{suf}"))

    # 2. Reset the now-vacated last slot (index n-1) to defaults.
    last = _CROP_SLOT_LETTERS[n - 1]
    setattr(item, f"crop_{last}_name", "")
    setattr(item, f"crop_{last}_width", 0)
    setattr(item, f"crop_{last}_height", 0)
    setattr(item, f"crop_{last}_enabled", True)
    setattr(item, f"crop_{last}_matte", "")

    # 3. Fix per-slot matte links: drop links on the removed slot, and shift
    #    links on later slots down one letter to follow their placeholder.
    removed_letter = _CROP_SLOT_LETTERS[di].upper()
    to_remove = []
    for idx, link in enumerate(item.matte_links):
        ls = link.slot.lower()
        li = _CROP_SLOT_LETTERS.index(ls) if ls in _CROP_SLOT_LETTERS else -1
        if li == di:
            to_remove.append(idx)
        elif li > di:
            link.slot = _CROP_SLOT_LETTERS[li - 1].upper()
    for idx in reversed(to_remove):
        item.matte_links.remove(idx)

    # 4. Collapse the count (keep at least one slot).
    if item.crop_slot_count > 1:
        item.crop_slot_count -= 1


class _CP3D_SlotDimOp(Operator):
    """Base for the per-slot dimension tools — shared poll + field helpers.

    NOTE: the ``slot`` property must be declared on each *concrete* subclass,
    not here — Blender's class registration only reads a class's OWN
    ``__annotations__`` and does not inherit property annotations from base
    classes.  Methods (poll, _fields) ARE inherited normally.
    """
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        s = ctx.scene.collection_render_settings
        return 0 <= s.active_index < len(s.collections)

    def _fields(self):
        """Return the (width_attr, height_attr) property names for this slot."""
        sl = self.slot.lower()
        return f"crop_{sl}_width", f"crop_{sl}_height"


class CP3D_OT_optimize_dims(_CP3D_SlotDimOp):
    """Scale Width & Height into 1000–8000 (ideal ~4000), preserving the ratio."""
    bl_idname = "cp3d.optimize_dims"
    bl_label = "Optimize"
    slot: StringProperty(default="A")   # 'A'..'J' — set by the panel button

    def execute(self, ctx):
        item = _active_slot_item(ctx)
        if item is None:
            return {'CANCELLED'}
        wa, ha = self._fields()
        w = getattr(item, wa)
        h = getattr(item, ha)
        if w <= 0 or h <= 0:
            self.report({'WARNING'},
                        f"Slot {self.slot}: set Width and Height before optimizing")
            return {'CANCELLED'}
        nw, nh = _optimize_dims(w, h)
        setattr(item, wa, nw)
        setattr(item, ha, nh)
        self.report({'INFO'}, f"Slot {self.slot}: {w}×{h} → {nw}×{nh}")
        return {'FINISHED'}


class CP3D_OT_swap_dims(_CP3D_SlotDimOp):
    """Swap this slot's Width and Height values."""
    bl_idname = "cp3d.swap_dims"
    bl_label = "Swap"
    slot: StringProperty(default="A")   # 'A'..'J' — set by the panel button

    def execute(self, ctx):
        item = _active_slot_item(ctx)
        if item is None:
            return {'CANCELLED'}
        wa, ha = self._fields()
        w = getattr(item, wa)
        h = getattr(item, ha)
        setattr(item, wa, h)
        setattr(item, ha, w)
        return {'FINISHED'}


class CP3D_OT_copy_dims(_CP3D_SlotDimOp):
    """Copy this slot's Width / Height to the clipboard (paste into any slot)."""
    bl_idname = "cp3d.copy_dims"
    bl_label = "Copy"
    slot: StringProperty(default="A")   # 'A'..'J' — set by the panel button

    def execute(self, ctx):
        item = _active_slot_item(ctx)
        if item is None:
            return {'CANCELLED'}
        s = ctx.scene.collection_render_settings
        wa, ha = self._fields()
        s.dim_clip_width = getattr(item, wa)
        s.dim_clip_height = getattr(item, ha)
        s.dim_clip_set = True
        self.report({'INFO'},
                    f"Copied {s.dim_clip_width}×{s.dim_clip_height}")
        return {'FINISHED'}


class CP3D_OT_paste_dims(_CP3D_SlotDimOp):
    """Paste the clipboard Width / Height into this slot (works across collections)."""
    bl_idname = "cp3d.paste_dims"
    bl_label = "Paste"
    slot: StringProperty(default="A")   # 'A'..'J' — set by the panel button

    @classmethod
    def poll(cls, ctx):
        s = ctx.scene.collection_render_settings
        if not (0 <= s.active_index < len(s.collections)):
            return False
        return s.dim_clip_set   # nothing to paste until a Copy has happened

    def execute(self, ctx):
        item = _active_slot_item(ctx)
        if item is None:
            return {'CANCELLED'}
        s = ctx.scene.collection_render_settings
        wa, ha = self._fields()
        setattr(item, wa, s.dim_clip_width)
        setattr(item, ha, s.dim_clip_height)
        self.report({'INFO'},
                    f"Pasted {s.dim_clip_width}×{s.dim_clip_height} into slot {self.slot}")
        return {'FINISHED'}


class CP3D_OT_select_placeholder(_CP3D_SlotDimOp):
    """Select this slot's placeholder object in the viewport / Outliner."""
    bl_idname = "cp3d.select_placeholder"
    bl_label = "Select"
    slot: StringProperty(default="A")   # 'A'..'J' — set by the panel button

    def execute(self, ctx):
        item = _active_slot_item(ctx)
        if item is None:
            return {'CANCELLED'}
        sl = self.slot.lower()
        name = getattr(item, f"crop_{sl}_name", "")
        obj = bpy.data.objects.get(name) if name else None
        if obj is None:
            self.report({'WARNING'},
                        f"Slot {self.slot}: no placeholder object assigned")
            return {'CANCELLED'}
        # Deselect everything, then select + activate the placeholder so it is
        # highlighted in both the 3D viewport and the Outliner.
        try:
            bpy.ops.object.select_all(action='DESELECT')
        except RuntimeError:
            pass
        try:
            obj.select_set(True)
            ctx.view_layer.objects.active = obj
        except RuntimeError:
            self.report({'WARNING'},
                        f"'{obj.name}' is not in the current view layer "
                        "(its collection may be excluded)")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Selected {obj.name}")
        return {'FINISHED'}


class CP3D_OT_delete_placeholder(_CP3D_SlotDimOp):
    """Delete this slot's placeholder object from the scene and clear the slot."""
    bl_idname = "cp3d.delete_placeholder"
    bl_label = "Delete Placeholder"
    slot: StringProperty(default="A")   # 'A'..'J' — set by the panel button

    def execute(self, ctx):
        item = _active_slot_item(ctx)
        if item is None:
            return {'CANCELLED'}
        sl = self.slot.lower()
        name = getattr(item, f"crop_{sl}_name", "")
        obj = bpy.data.objects.get(name) if name else None
        if obj is None:
            self.report({'WARNING'},
                        f"Slot {self.slot}: no placeholder object to delete")
            return {'CANCELLED'}
        objname = obj.name
        try:
            # Remove the object data-block entirely (unlinks from all
            # collections).  Undoable via Ctrl+Z (bl_options REGISTER|UNDO).
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception as e:
            self.report({'ERROR'}, f"Could not delete '{objname}': {e}")
            return {'CANCELLED'}
        # Remove the slot from the list entirely — shift every later slot up one
        # so no empty gap is left (like the Remove Slot button, but targeting
        # this specific slot instead of the last one).
        _remove_crop_slot_shift(item, sl)
        s = ctx.scene.collection_render_settings
        log_add(s, 'CHECK', f"Deleted placeholder '{objname}' (slot {self.slot})")
        self.report({'INFO'}, f"Deleted {objname}")
        return {'FINISHED'}


class CP3D_OT_select_matte(Operator):
    """Select a holdout object in the viewport / Outliner (by object name).

    The holdout-object list has no per-slot letter (holdouts are a flat list),
    so this selects by object name — the same viewport/Outliner selection
    behaviour as CP3D_OT_select_placeholder, just keyed differently.
    """
    bl_idname = "cp3d.select_matte"
    bl_label = "Select"
    bl_options = {'REGISTER', 'UNDO'}

    name: StringProperty(default="")   # holdout object name — set by the panel button

    def execute(self, ctx):
        obj = bpy.data.objects.get(self.name) if self.name else None
        if obj is None:
            self.report({'WARNING'}, f"Holdout object '{self.name}' not found")
            return {'CANCELLED'}
        try:
            bpy.ops.object.select_all(action='DESELECT')
        except RuntimeError:
            pass
        try:
            obj.select_set(True)
            ctx.view_layer.objects.active = obj
        except RuntimeError:
            self.report({'WARNING'},
                        f"'{obj.name}' is not in the current view layer "
                        "(its collection may be excluded)")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Selected {obj.name}")
        return {'FINISHED'}


# ── Holdout link management (one placeholder slot → many holdouts) ────────────

class CP3D_OT_add_matte_link(Operator):
    """Add an empty holdout link to a placeholder slot (pick the holdout after)."""
    bl_idname = "cp3d.add_matte_link"
    bl_label = "Add Holdout"
    bl_options = {'REGISTER', 'UNDO'}

    slot: StringProperty()   # 'A'..'J' — set by the panel button

    def execute(self, ctx):
        s = ctx.scene.collection_render_settings
        if not (0 <= s.active_index < len(s.collections)):
            return {'CANCELLED'}
        item = s.collections[s.active_index]
        link = item.matte_links.add()
        link.slot = self.slot
        link.name = ""
        return {'FINISHED'}


class CP3D_OT_remove_matte_link(Operator):
    """Remove the holdout link at *index* from the active collection."""
    bl_idname = "cp3d.remove_matte_link"
    bl_label = "Remove Holdout"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(default=-1)

    def execute(self, ctx):
        s = ctx.scene.collection_render_settings
        if not (0 <= s.active_index < len(s.collections)):
            return {'CANCELLED'}
        item = s.collections[s.active_index]
        if 0 <= self.index < len(item.matte_links):
            item.matte_links.remove(self.index)
        return {'FINISHED'}


# ── Placeholder importer ─────────────────────────────────────────────────────

class CP3D_OT_import_placeholders(Operator):
    """Import placeholder objects into crop slots for the target collection(s).

    Finds meshes with 'placeholder' in their name, copies them into a
    'placeholders' sub-collection and assigns them to the crop slots.  Runs for
    every checkmark-selected collection in the list; if none are checkmarked it
    falls back to the active (highlighted) one.
    """
    bl_idname = "cp3d.import_placeholders"
    bl_label = "Import Placeholders"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        s = ctx.scene.collection_render_settings
        if not (0 <= s.active_index < len(s.collections)):
            return False
        if any(i.selected and i.collection for i in s.collections):
            return True
        return s.collections[s.active_index].collection is not None

    def execute(self, ctx):
        s = ctx.scene.collection_render_settings
        # Every checkmark-selected collection; else fall back to the active one.
        targets = [i for i in s.collections if i.selected and i.collection]
        if not targets and 0 <= s.active_index < len(s.collections):
            active = s.collections[s.active_index]
            if active.collection:
                targets = [active]
        if not targets:
            self.report({'WARNING'}, "No collection to import into")
            return {'CANCELLED'}

        total, ok = 0, 0
        for item in targets:
            try:
                count = import_placeholders(ctx, item)
                total += count
                ok += 1
                log_add(s, 'CHECK',
                        f"{item.collection.name}: imported {count} placeholder(s)")
            except RuntimeError as e:
                # A missing object in the linked file (or any import failure)
                # must NOT raise a modal popup — self.report({'ERROR'}) pops a
                # dialog.  Log a red error line to the Log window instead.
                log_add(s, 'ERROR',
                        f"Import Placeholders {item.collection.name}: {e}")

        if ok == 0:
            return {'CANCELLED'}
        self.report({'INFO'},
                    f"Imported {total} placeholder(s) across {ok} collection(s)")
        return {'FINISHED'}


class CP3D_OT_reload_placeholders(Operator):
    """Re-assign crop slots from the placeholders already in the collection.

    Imports nothing — re-reads the existing 'placeholders' sub-collection and
    re-assigns those objects to the crop slots.  Use after hand-editing the
    placeholders (added / removed / renamed objects) to resync the slots.
    """
    bl_idname = "cp3d.reload_placeholders"
    bl_label = "Reload Placeholders"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        s = ctx.scene.collection_render_settings
        if not (0 <= s.active_index < len(s.collections)):
            return False
        return s.collections[s.active_index].collection is not None

    def execute(self, ctx):
        s = ctx.scene.collection_render_settings
        item = s.collections[s.active_index]
        try:
            count = reload_placeholders(ctx, item)
            self.report({'INFO'}, f"Reloaded {count} placeholder(s)")
            log_add(s, 'CHECK', f"Reloaded {count} placeholder(s) from collection")
            return {'FINISHED'}
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            log_add(s, 'ERROR', f"Reload Placeholders: {e}")
            return {'CANCELLED'}


# ── Holdout importer ──────────────────────────────────────────────────────────

class CP3D_OT_import_mattes(Operator):
    """Import holdout objects from linked sub-collections into a Holdout group.

    Mirrors :class:`CP3D_OT_import_placeholders` but for meshes whose name
    contains ``holdout`` or the legacy ``matte`` (case-insensitive).  Walks
    the selected collection tree (including library-linked sub-collections
    and collection-instance Empties), creates local copies with their world
    transforms baked in, and places them in a ``Holdout`` sub-collection of
    the selected collection.  No slot or material assignment.

    Runs for every checkmark-selected collection in the list; if none are
    checkmarked it falls back to the active (highlighted) one.
    """
    bl_idname = "cp3d.import_mattes"
    bl_label = "Import Holdouts"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        s = ctx.scene.collection_render_settings
        if not (0 <= s.active_index < len(s.collections)):
            return False
        if any(i.selected and i.collection for i in s.collections):
            return True
        return s.collections[s.active_index].collection is not None

    def execute(self, ctx):
        s = ctx.scene.collection_render_settings
        # Every checkmark-selected collection; else fall back to the active one.
        targets = [i for i in s.collections if i.selected and i.collection]
        if not targets and 0 <= s.active_index < len(s.collections):
            active = s.collections[s.active_index]
            if active.collection:
                targets = [active]
        if not targets:
            self.report({'WARNING'}, "No collection to import into")
            return {'CANCELLED'}

        total, ok = 0, 0
        for item in targets:
            try:
                count = import_mattes(ctx, item)
                total += count
                ok += 1
                log_add(s, 'CHECK',
                        f"{item.collection.name}: imported {count} holdout(s)")
            except RuntimeError as e:
                # A missing object in the linked file (or any import failure)
                # must NOT raise a modal popup — self.report({'ERROR'}) pops a
                # dialog.  Log a red error line to the Log window instead.
                log_add(s, 'ERROR',
                        f"Import Holdouts {item.collection.name}: {e}")

        if ok == 0:
            return {'CANCELLED'}
        self.report({'INFO'},
                    f"Imported {total} holdout(s) across {ok} collection(s)")
        return {'FINISHED'}


# ── Convert Highlight / Shadow operators ─────────────────────────────────────

class CP3D_OT_convert_highlight(Operator):
    """Post-process the active collection's _render.png through Convert_Highlight."""
    bl_idname = "cp3d.convert_highlight"
    bl_label  = "Convert → Highlight"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        s = ctx.scene.collection_render_settings
        if not (0 <= s.active_index < len(s.collections)):
            return False
        item = s.collections[s.active_index]
        return item.collection is not None and find_render_image_path(item) is not None

    def execute(self, ctx):
        s    = ctx.scene.collection_render_settings
        item = s.collections[s.active_index]
        name = bpy.path.clean_name(item.collection.name)
        try:
            fp = convert_render_image(ctx, item, name, 'highlight')
            self.report({'INFO'}, f"Converted: {fp}")
            log_add(s, 'EXPORT', f"Converted {os.path.basename(fp)}")
            return {'FINISHED'}
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            log_add(s, 'ERROR', f"Convert Highlight: {e}")
            return {'CANCELLED'}


class CP3D_OT_convert_shadow(Operator):
    """Post-process the active collection's _render.png through Convert_Shadow."""
    bl_idname = "cp3d.convert_shadow"
    bl_label  = "Convert → Shadow"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        s = ctx.scene.collection_render_settings
        if not (0 <= s.active_index < len(s.collections)):
            return False
        item = s.collections[s.active_index]
        return item.collection is not None and find_render_image_path(item) is not None

    def execute(self, ctx):
        s    = ctx.scene.collection_render_settings
        item = s.collections[s.active_index]
        name = bpy.path.clean_name(item.collection.name)
        try:
            fp = convert_render_image(ctx, item, name, 'shadow')
            self.report({'INFO'}, f"Converted: {fp}")
            log_add(s, 'EXPORT', f"Converted {os.path.basename(fp)}")
            return {'FINISHED'}
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            log_add(s, 'ERROR', f"Convert Shadow: {e}")
            return {'CANCELLED'}


class CP3D_OT_reset_highlight(Operator):
    """Rebuild Convert_Highlight node tree, restoring all default values."""
    bl_idname = "cp3d.reset_highlight"
    bl_label  = "Reset Highlight Tree"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, ctx):
        s = ctx.scene.collection_render_settings
        if not reset_highlight_node_tree():
            # Load failed (missing file / group) — the existing tree was left
            # untouched, so nothing disappears.  Surface the error clearly.
            msg = (f"Could not reload '{HIGHLIGHT_TREE_NAME}' from "
                   f"Convert.blend — check console for details")
            self.report({'ERROR'}, msg)
            log_add(s, 'ERROR', f"Reset Highlight: {msg}")
            return {'CANCELLED'}
        # Re-apply the current slider value so the freshly reloaded group's
        # Value node matches the UI instead of snapping back to its 0.5 default.
        set_convert_value('highlight', s.convert_highlight_value)
        self.report({'INFO'}, f"'{HIGHLIGHT_TREE_NAME}' reset to defaults")
        return {'FINISHED'}


class CP3D_OT_reset_shadow(Operator):
    """Rebuild Convert_Shadow node tree, restoring all default values."""
    bl_idname = "cp3d.reset_shadow"
    bl_label  = "Reset Shadow Tree"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, ctx):
        s = ctx.scene.collection_render_settings
        if not reset_shadow_node_tree():
            # Load failed (missing file / group) — the existing tree was left
            # untouched, so nothing disappears.  Surface the error clearly.
            msg = (f"Could not reload '{SHADOW_TREE_NAME}' from "
                   f"Convert.blend — check console for details")
            self.report({'ERROR'}, msg)
            log_add(s, 'ERROR', f"Reset Shadow: {msg}")
            return {'CANCELLED'}
        # Re-apply the current slider value so the freshly reloaded group's
        # Value node matches the UI instead of snapping back to its 0.5 default.
        set_convert_value('shadow', s.convert_shadow_value)
        self.report({'INFO'}, f"'{SHADOW_TREE_NAME}' reset to defaults")
        return {'FINISHED'}


class CP3D_OT_setup_convert_trees(Operator):
    """Append Convert_Highlight and Convert_Shadow from Convert.blend if missing."""
    bl_idname = "cp3d.setup_convert_trees"
    bl_label  = "Setup Compositor"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, ctx):
        ensure_convert_node_trees()
        missing = [n for n in (HIGHLIGHT_TREE_NAME, SHADOW_TREE_NAME,
                               CROP_TREE_NAME)
                   if bpy.data.node_groups.get(n) is None]
        if missing:
            self.report({'ERROR'},
                        f"Could not load: {', '.join(missing)} — check console "
                        f"for details (node group name mismatch or missing file?)")
            return {'CANCELLED'}
        # Sync freshly loaded Value nodes to the current slider values.
        s = ctx.scene.collection_render_settings
        set_convert_value('highlight', s.convert_highlight_value)
        set_convert_value('shadow', s.convert_shadow_value)
        self.report({'INFO'}, "Convert node trees ready")
        return {'FINISHED'}


class CP3D_OT_check_setup(Operator):
    """Validate every listed collection's setup and report to the log.

    Walks every ENABLED collection in the list and, for each, checks:
      - Placeholders sub-collection exists AND has at least one placeholder
        object assigned to an active slot.
      - Each active slot with a placeholder has non-zero Width AND Height.
      - At least one active slot has one or more linked holdout objects.
      - The collection has both a World and a Compositor node tree assigned.

    Severity (drives the log colour):
      FAIL (red)   — placeholders missing / slot missing width or height /
                     world or compositor unset.
      WARN (yellow)— no holdout objects linked to any slot.
      OK   (green) — the collection passes every check above.

    The check is read-only — it does not modify any state.  Progress and the
    per-collection verdict are written to the scrollable Log window.
    """
    bl_idname = "cp3d.check_setup"
    bl_label  = "Check Setup"
    bl_options = {'REGISTER'}

    # Slot letter list mirrors the placeholder property naming (A–J).
    _SLOTS = ('a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j')

    @classmethod
    def poll(cls, ctx):
        s = ctx.scene.collection_render_settings
        return len(s.collections) > 0

    def _iter_active_slots(self, item):
        """Yield (letter_upper, enabled, name, width, height, matte_names).

        Only yields slots within the current crop_slot_count so we don't
        validate rows the user has hidden via the slot-count spinner.  The
        legacy single-matte field is included in matte_names alongside every
        CP3D_MatteLink entry that targets this slot.
        """
        n = max(1, min(item.crop_slot_count, len(self._SLOTS)))
        for letter in self._SLOTS[:n]:
            enabled = bool(getattr(item, f"crop_{letter}_enabled", True))
            name    = getattr(item, f"crop_{letter}_name",   "") or ""
            width   = int(getattr(item, f"crop_{letter}_width",  0) or 0)
            height  = int(getattr(item, f"crop_{letter}_height", 0) or 0)
            legacy_matte = (getattr(item, f"crop_{letter}_matte", "") or "").strip()
            slot_upper = letter.upper()
            mattes = [
                lk.name for lk in item.matte_links
                if lk.slot == slot_upper and lk.name
            ]
            if legacy_matte and legacy_matte not in mattes:
                mattes.append(legacy_matte)
            yield slot_upper, enabled, name.strip(), width, height, mattes

    def execute(self, ctx):
        s = ctx.scene.collection_render_settings

        # ── Grey out the previous report + separate it with a divider line ──
        # Every existing entry is muted (drawn inactive/greyed) so the fresh
        # run stands out; a bright divider row is inserted between old and new.
        had_previous = len(s.log_entries) > 0
        for entry in s.log_entries:
            entry.muted = True
        if had_previous:
            log_add(s, 'DIVIDER', "─" * 28)

        targets = [i for i in s.collections if i.enabled and i.collection]
        if not targets:
            self.report({'WARNING'}, "No enabled collections to check")
            log_add(s, 'WARNING', "Check Setup: no enabled collections")
            return {'CANCELLED'}

        log_add(s, 'CHECK',
                f"── Check Setup: {len(targets)} collection(s) ──")

        total_ok = 0
        for item in targets:
            col = item.collection
            display = col.name

            fails = []   # red — blocking
            warns = []   # yellow — non-blocking

            # ── World / Compositor (both required) ────────────────────────
            if item.world is None:
                fails.append("World not set")
            if item.compositor_node_tree is None:
                fails.append("Compositor not set")

            # ── Placeholders sub-collection present? ──────────────────────
            has_placeholders_col = any(
                _is_placeholders_col(child.name) for child in col.children
            )
            if not has_placeholders_col:
                fails.append("no Placeholders sub-collection")

            # ── Per-slot placeholder + dimensions checks ──────────────────
            slots_with_object = 0
            slots_with_matte  = 0
            for slot, enabled, name, width, height, mattes in self._iter_active_slots(item):
                if not enabled:
                    continue
                if not name:
                    # An active slot with no placeholder assigned isn't
                    # necessarily an error (user may just not use it), but
                    # if NO slot has one at all we'll fail below.
                    continue
                slots_with_object += 1
                # Blender's Object lookup is by name; missing → fail
                if name not in bpy.data.objects:
                    fails.append(f"slot {slot}: object '{name}' not in scene")
                    continue
                # Width / Height range validation, per dimension:
                #   <= 0     → not set        (red FAIL)
                #   < 1000   → too small      (red FAIL)
                #   > 8000   → very large     (yellow WARN)
                for dim_label, dim_val in (("Width", width), ("Height", height)):
                    if dim_val <= 0:
                        fails.append(
                            f"slot {slot} ({name}): {dim_label} not set"
                        )
                    elif dim_val < 1000:
                        fails.append(
                            f"slot {slot} ({name}): {dim_label} {dim_val} "
                            f"< 1000 (too small)"
                        )
                    elif dim_val > 8000:
                        warns.append(
                            f"slot {slot} ({name}): {dim_label} {dim_val} "
                            f"> 8000 (very large)"
                        )
                if mattes:
                    slots_with_matte += 1

            # If Placeholders sub-collection exists but no slot has an object,
            # treat that as a fail — nothing to render/export.
            if has_placeholders_col and slots_with_object == 0:
                fails.append("no placeholder objects assigned to slots")

            # Holdout objects — warning only (crop still works without them).
            if slots_with_object > 0 and slots_with_matte == 0:
                warns.append("no holdout objects linked to any slot")

            # ── Emit per-collection report ────────────────────────────────
            # One RESULT header line carries the (light-blue) collection name
            # plus the overall status; the individual issue lines follow it,
            # indented and without repeating the name.  The '\x1f' unit
            # separator packs name + status into one message — CP3D_UL_log_list
            # splits on it to draw the highlighted name (must stay in sync).
            if fails:
                status = f"Setup FAILED — {len(fails)} issue(s)"
            elif warns:
                status = "Setup OK (with warnings)"
                total_ok += 1
            else:
                status = "Setup OK"
                total_ok += 1
            log_add(s, 'RESULT', f"{display}\x1f{status}")
            for msg in fails:
                log_add(s, 'FAIL', f"    {msg}")
            for msg in warns:
                log_add(s, 'WARN', f"    {msg}")

        # ── Summary line ─────────────────────────────────────────────────────
        n = len(targets)
        summary = f"Check Setup: {total_ok}/{n} collection(s) OK"
        if total_ok == n:
            log_add(s, 'OK', summary)
            self.report({'INFO'}, summary)
        else:
            log_add(s, 'FAIL', summary)
            self.report({'WARNING'}, summary)

        # Auto-open the log window so the user can read the report immediately
        s.log_show = True
        return {'FINISHED'}


class CP3D_OT_export_glb(Operator):
    """Export Crop GLB for every ENABLED collection (left checkmark).

    Works exactly like the Render button's collection selection: the left
    enabled checkbox in the list decides which collections are exported.
    Each successfully exported collection is marked green in the list.
    """
    bl_idname = "cp3d.export_glb"
    bl_label = "Export GLB"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        s = ctx.scene.collection_render_settings
        # At least one enabled collection — mirrors CP3D_OT_render_all.
        return any(i.enabled and i.collection for i in s.collections)

    def execute(self, ctx):
        s = ctx.scene.collection_render_settings
        # Export every ENABLED collection (left checkmark) — same target
        # selection as the Render button.
        targets = [i for i in s.collections if i.enabled and i.collection]
        if not targets:
            self.report({'WARNING'}, "No enabled collection to export")
            return {'CANCELLED'}

        total_files = 0
        exported = 0
        for item in targets:
            try:
                paths = export_crop_glb(ctx, item)
                for fp in paths:
                    self.report({'INFO'}, f"Exported: {fp}")
                    log_add(s, 'EXPORT', f"Exported {os.path.basename(fp)}")
                total_files += len(paths)
                if paths:
                    item.was_rendered = True   # mark this collection green
                    exported += 1
            except RuntimeError as e:
                self.report({'ERROR'}, f"{item.collection.name}: {e}")
                log_add(s, 'ERROR', f"{item.collection.name}: {e}")

        if exported == 0:
            log_add(s, 'WARNING', "GLB export produced no files")
            return {'CANCELLED'}

        # Drive the same green-highlight feedback the render batch uses.
        s.render_done = True
        s.render_message = f"Exported {total_files} GLB ({exported} collection(s))"
        log_add(s, 'SUCCESS',
                f"Exported {total_files} GLB ({exported} collection(s))")
        _schedule_render_done_clear()
        return {'FINISHED'}


class CP3D_OT_render_and_export(Operator):
    """Run the full render batch, then export Crop GLB — both in one click."""
    bl_idname = "cp3d.render_and_export"
    bl_label = "Render + Export GLB"
    bl_options = {'REGISTER'}

    def execute(self, ctx):
        s = ctx.scene.collection_render_settings
        # Render all enabled collections first…
        res = bpy.ops.cp3d.render_all()
        if 'CANCELLED' in res:
            self.report({'WARNING'}, "Render cancelled — skipping GLB export")
            return {'CANCELLED'}
        render_msg = s.render_message or "Render complete"
        # …then export Crop GLB for the selected collections (if any are valid).
        if bpy.ops.cp3d.export_glb.poll():
            bpy.ops.cp3d.export_glb()
            export_msg = s.render_message or "GLB export complete"
        else:
            export_msg = "no GLB exported (no collection selected)"
        # Combined feedback: report BOTH the render and the export results
        combined = f"{render_msg}  |  {export_msg}"
        s.render_message = combined
        s.render_done = True
        # Re-arm the auto-clear so the red "Done" button state reverts to the
        # normal colour a few seconds after this combined run finishes.
        _schedule_render_done_clear()
        self.report({'INFO'}, combined)
        return {'FINISHED'}


# ── Cancel render operator ────────────────────────────────────────────────────

class CP3D_OT_cancel_render(Operator):
    """Request cancellation of the in-progress batch render.

    Cannot interrupt the current ``bpy.ops.render.render()`` call (that's a
    blocking Cycles operation), but sets a flag that the render loop checks
    between passes.  When the flag is True, the loop breaks cleanly, the
    finally block restores all scene state, and the UI returns to its normal
    layout.  In effect: "stop after finishing the current render".
    """
    bl_idname = "cp3d.cancel_render"
    bl_label = "Cancel Render"
    bl_options = {'REGISTER'}

    def execute(self, context):
        s = context.scene.collection_render_settings
        if s.render_in_progress:
            s.cancel_requested = True
            self.report({'INFO'}, "Cancel requested — stopping after current pass")
        return {'FINISHED'}


# ── Clear log operator ────────────────────────────────────────────────────────

class CP3D_OT_clear_log(Operator):
    """Clear all entries from the CP3D log window."""
    bl_idname = "cp3d.clear_log"
    bl_label = "Clear Log"
    bl_options = {'REGISTER'}

    def execute(self, context):
        s = context.scene.collection_render_settings
        s.log_entries.clear()
        s.log_active_index = 0
        return {'FINISHED'}


# ── Main batch render operator ────────────────────────────────────────────────

class CP3D_OT_render_all(Operator):
    """Render all enabled collections across every active output pass.

    Output layout (written to <blend_dir>/R/<N>/, where <N> is the trailing
    number in the .blend file name, or <blend_dir>/R/ when there is none):
      <name>_render.png      — beauty render (per-collection Compositor + World)
      <name>_highlight.png   — Render output routed through the "Highlight" node
      <name>_shadow.png      — Render output routed through the "Shadow" node
      <name>_crop_a.png      — white silhouette of placeholder A (with alpha)
      <name>_crop_b.png      — white silhouette of placeholder B
      <name>_crop_c.png      — white silhouette of placeholder C
      <name>_shadow_ins.png  — desaturated render masked by placeholder Cryptomatte

    Compositor / World handling (v1.0.24+):
      - Each collection picks its own Compositor NodeTree and (optionally) World
        via dropdowns; both apply to the Render and Render ISO passes.
      - Highlight and Shadow do NOT render fresh: they immediately follow the
        Render pass, reuse Cycles' warm persistent_data cache, and route the
        beauty image through the compositor's "Highlight" / "Shadow" node (an
        RGB-curve adjustment).  If that node is missing the pass is skipped with
        an error.  Both require the Render pass to be enabled.
    """
    bl_idname = "cp3d.render_all"
    bl_label = "Render All"
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.scene.collection_render_settings
        # Only process collections that are ticked on and have a collection assigned
        enabled_cols = [i for i in settings.collections if i.enabled and i.collection]
        if not enabled_cols:
            self.report({'WARNING'}, "No enabled collections")
            log_add(settings, 'WARNING', "Render aborted: no enabled collections")
            return {'CANCELLED'}
        if not bpy.data.filepath:
            self.report({'ERROR'}, "Save first")
            log_add(settings, 'ERROR', "Render aborted: save the .blend file first")
            return {'CANCELLED'}

        # (compositor_enabled is no longer used — the add-on manages compositor
        # nodes directly per-pass and restores from backup when done)

        # ── Snapshot ALL scene settings before touching anything ───────────────
        # Every field that any pass might change is saved here.  The finally block
        # at the bottom restores all of them unconditionally.
        orig = {
            'res_x': context.scene.render.resolution_x,
            'res_y': context.scene.render.resolution_y,
            'camera': context.scene.camera,
            'filepath': context.scene.render.filepath,
            'format': context.scene.render.image_settings.file_format,
            'color_mode': context.scene.render.image_settings.color_mode,
            'film_transparent': context.scene.render.film_transparent,
            'view_transform': context.scene.view_settings.view_transform,
            'look': context.scene.view_settings.look,
            'display_device': (
                context.scene.display_settings.display_device
                if hasattr(context.scene.display_settings, 'display_device') else None
            ),
            'world': context.scene.world,
            'persistent_data': context.scene.render.use_persistent_data,
            'glossy_direct': {},
            'crypto_obj': {},
            'crypto_mat': {},
            'col_vis': {}, 'light_vis': {}, 'shadow_states': {},
        }
        # Snapshot per-ViewLayer optional render pass enable states.
        # These are reset to False at the start of every pass iteration and
        # re-enabled only by passes that actually need them — so e.g. Crop
        # doesn't pay the cost of GlossDir or Cryptomatte that a previous
        # pass enabled.
        for vl in context.scene.view_layers:
            orig['glossy_direct'][vl.name] = vl.use_pass_glossy_direct
            orig['crypto_obj'][vl.name] = vl.use_pass_cryptomatte_object
            orig['crypto_mat'][vl.name] = vl.use_pass_cryptomatte_material
        # Snapshot collection visibility (exclude flags) for all top-level collections
        for col in context.scene.collection.children:
            lc = find_layer_collection(context.view_layer.layer_collection, col)
            if lc:
                orig['col_vis'][col.name] = lc.exclude
        # Snapshot light visibility and mesh shadow-catcher states for all objects
        for obj in bpy.data.objects:
            if obj.type == 'LIGHT':
                orig['light_vis'][obj.name] = obj.hide_render
            if obj.type == 'MESH':
                orig['shadow_states'][obj.name] = obj.is_shadow_catcher

        # ── Load Convert trees BEFORE the snapshot ─────────────────────────────
        # The Convert Highlight / Shadow / Crop passes append their node groups
        # from Convert.blend on first use.  Loading them up front puts their
        # names in the snapshot below, so the post-render orphan purge can
        # never mistake them for leaked data.  Also collapses any numbered
        # duplicates (Convert_Crop.001 …) left over from earlier renders.
        if any(i.render_crop or i.convert_highlight or i.convert_shadow
               for i in enabled_cols):
            ensure_convert_node_trees()

        # ── Snapshot world / node-group names for post-render orphan cleanup ───
        # Rendering can leak duplicated data-blocks (World.001, Convert_Shadow.001
        # …).  We record what exists now and remove any new orphans in the
        # finally block, so the counts stay exactly as they were pre-render.
        data_snapshot = snapshot_datablock_names()

        # ── Backup the user's compositor node tree ─────────────────────────────
        # All passes destroy and rebuild the compositor nodes.  This snapshot
        # lets us restore the user's original setup when we're done.
        comp_backup = backup_compositor(context)

        # ── Global render settings for all passes ─────────────────────────────
        context.scene.render.image_settings.file_format = 'PNG'
        context.scene.render.image_settings.color_mode = 'RGBA'
        if hasattr(context.scene, 'use_gpu_compositor'):
            context.scene.use_gpu_compositor = True
        if hasattr(context.scene.render, 'use_compositing'):
            context.scene.render.use_compositing = True

        # ── Build the ordered render list ─────────────────────────────────────
        # Each entry is a tuple: (collection, name, res_x, res_y, suffix, output_name, item)
        # suffix      = the filename stem piece (e.g. 'crop_a')
        # output_name = human-readable label used by compositor helpers (e.g. 'Crop A')
        # Highlight and Shadow follow Render directly: they re-save the Render
        # output routed through the compositor's "Highlight" / "Shadow" node and
        # reuse Cycles' warm persistent_data cache.  Both require the Render pass.
        render_list = []
        for item in enabled_cols:
            name = item.collection.name
            col = item.collection
            rx, ry = item.resolution_x, item.resolution_y
            if item.render_raw:
                render_list.append((col, name, rx, ry, 'render', 'Render', item))
            if item.render_crop:
                for _sl in 'ABCDEFGHIJ':
                    _sl_l = _sl.lower()
                    if (getattr(item, f"crop_{_sl_l}_enabled", False)
                            and getattr(item, f"crop_{_sl_l}_name", "")):
                        # 1) Cycles silhouette render → <name>_alfa_<sl>.png
                        #    (plain white + render-alpha, as before Convert_Crop)
                        render_list.append(
                            (col, name, rx, ry, f'alfa_{_sl_l}', f'Crop {_sl}', item)
                        )
                        # 2) Post-process that alpha through Convert_Crop →
                        #    <name>_crop_<sl>.png (slightly broadened alpha edge)
                        render_list.append(
                            (col, name, rx, ry, f'crop_{_sl_l}',
                             f'Convert Crop {_sl}', item)
                        )
            # Convert passes — post-process _render.png, no Cycles re-render
            if item.convert_highlight:
                render_list.append((col, name, rx, ry, 'highlight', 'Convert Highlight', item))
            if item.convert_shadow:
                render_list.append((col, name, rx, ry, 'shadow', 'Convert Shadow', item))

        total = len(render_list)
        # Renders go to  <blend_dir>/R/<N>/  where <N> is the trailing number in
        # the .blend file name (e.g. scene_01.blend → R/01/), or R/ if none.
        render_dir = get_render_output_dir()

        # ── Per-collection pass totals (for the pass progress bar) ─────────────
        # Counts how many render entries are produced by each item, so the
        # in-panel progress display can show "Passes 1/4" for the current
        # collection's total — not the global total.
        col_passes = {}   # {bpy.types.Collection: int}
        for _item in enabled_cols:
            cnt = 0
            if _item.render_raw:
                cnt += 1
            if _item.render_crop:
                for _sl in 'ABCDEFGHIJ':
                    _sl_l = _sl.lower()
                    if (getattr(_item, f"crop_{_sl_l}_enabled", False)
                            and getattr(_item, f"crop_{_sl_l}_name", "")):
                        cnt += 2   # _alfa Cycles render + Convert_Crop post-process
            if _item.convert_highlight:     cnt += 1
            if _item.convert_shadow:        cnt += 1
            col_passes[_item.collection] = cnt

        # ── Initialise live progress state (drives the CP3D panel progress UI) ─
        # The panel reads these in its draw() and shows two progress bars while
        # render_in_progress is True.  Redraw is triggered explicitly between
        # render calls (bpy.ops.render.render() blocks the UI during a render).
        settings.render_in_progress = True
        settings.cancel_requested = False
        settings.progress_col_idx = 0
        settings.progress_col_total = len(enabled_cols)
        settings.progress_col_name = ""
        settings.progress_pass_idx = 0
        settings.progress_pass_total = 0
        settings.progress_pass_name = ""
        self.report({'INFO'}, f"Print3Dexporter: starting {total} renders "
                              f"across {len(enabled_cols)} collection(s)")
        log_add(settings, 'RENDER',
                f"Render started: {total} pass(es), "
                f"{len(enabled_cols)} collection(s)")

        # Tracks which collection we're currently on, to detect transitions
        _current_col = None
        _pass_in_col = 0
        _current_col_idx = 0

        def _tag_redraw():
            """Tag all visible areas for redraw so the progress bars update."""
            try:
                for window in context.window_manager.windows:
                    for area in window.screen.areas:
                        area.tag_redraw()
            except Exception:
                pass

        # ── Outer try/finally: guarantees ALL scene settings are restored ─────
        # even if Cycles crashes, a Python error occurs, or the user cancels.
        try:
          for i, (col, name, rx, ry, suffix, output_name, item) in enumerate(render_list):
            # ── Cooperative cancellation check ────────────────────────────────
            # Runs before any per-pass work so nothing is set up / torn down
            # unnecessarily on a cancelled iteration.
            if settings.cancel_requested:
                cancel_msg = (f"Batch render cancelled after "
                              f"{i} of {total} pass(es)")
                print(f"[CP3D] {cancel_msg}")
                self.report({'WARNING'}, cancel_msg)
                log_add(settings, 'WARNING', cancel_msg)
                break

            # ── Collection transition: pause + GC + status updates ────────────
            # When we move from one collection to the next we give Blender a
            # breather: garbage-collect Python references, sleep briefly so
            # Cycles can free its BVH cache, and surface start / finish
            # messages in the Info editor.  This addresses cumulative
            # slowdown observed in long multi-collection batches.
            is_new_col = (col is not _current_col)
            if is_new_col and _current_col is not None:
                # Finished the previous collection — report + pause
                finish_msg = (f"✓ Finished collection "
                              f"{_current_col_idx}/{len(enabled_cols)} — "
                              f"pausing {CP3D_INTER_COLLECTION_PAUSE}s "
                              f"before next")
                print(f"[CP3D] {finish_msg}")
                self.report({'INFO'}, finish_msg)
                _tag_redraw()
                # Free Python-side references and let Blender process its UI
                # event queue.  Cycles' BVH is left cached (persistent_data
                # stays True) — when the new collection renders, Cycles
                # invalidates only the bits that actually changed.
                gc.collect()
                time.sleep(CP3D_INTER_COLLECTION_PAUSE)

            if is_new_col:
                _current_col = col
                _current_col_idx += 1
                _pass_in_col = 0
                start_msg = (f"▶ Starting collection "
                             f"{_current_col_idx}/{len(enabled_cols)}: {name}")
                print(f"[CP3D] {start_msg}")
                self.report({'INFO'}, start_msg)

            _pass_in_col += 1
            settings.progress_col_idx = _current_col_idx
            settings.progress_col_name = name
            settings.progress_pass_idx = _pass_in_col
            settings.progress_pass_total = col_passes.get(col, 1)
            settings.progress_pass_name = output_name
            _tag_redraw()

            # Per-pass console + Info editor status
            status_msg = (f"Render {i+1}/{total}: {name} — {output_name}  "
                          f"[Col {_current_col_idx}/{len(enabled_cols)}, "
                          f"Pass {_pass_in_col}/{col_passes.get(col, 1)}]")
            print(f"\n{'='*50}\n{status_msg}\n{'='*50}")
            self.report({'INFO'}, status_msg)

            # ── Convert passes fast-path ──────────────────────────────────────
            # These post-process an existing _render.png and don't need any
            # scene visibility changes or a Cycles/EEVEE geometry render.
            # They save/restore all settings internally, so we just call and
            # continue — skipping the rest of the per-pass setup/teardown.
            if output_name in ("Convert Highlight", "Convert Shadow"):
                _mode = 'highlight' if output_name == "Convert Highlight" else 'shadow'
                try:
                    _fp = convert_render_image(context, item, name, _mode)
                    print(f"Saved: {_fp}")
                    self.report({'INFO'}, f"Saved: {os.path.basename(_fp)}")
                    log_add(settings, 'EXPORT',
                            f"Converted {os.path.basename(_fp)}")
                except RuntimeError as _e:
                    self.report({'ERROR'}, str(_e))
                    log_add(settings, 'ERROR', f"{name} {output_name}: {_e}")
                continue

            # Convert Crop pass — post-process the just-rendered _alfa_<slot>.png
            # through Convert_Crop → _crop_<slot>.png (broadened alpha).  Same
            # self-contained fast-path as the Highlight / Shadow converts.
            if output_name.startswith("Convert Crop "):
                _sl = output_name[-1]   # 'A'..'J'
                try:
                    _fp = convert_crop_image(context, name, _sl)
                    print(f"Saved: {_fp}")
                    self.report({'INFO'}, f"Saved: {os.path.basename(_fp)}")
                    log_add(settings, 'EXPORT',
                            f"Converted {os.path.basename(_fp)}")
                except RuntimeError as _e:
                    self.report({'ERROR'}, str(_e))
                    log_add(settings, 'ERROR', f"{name} {output_name}: {_e}")
                continue

            context.scene.render.resolution_x = rx
            context.scene.render.resolution_y = ry
            cam = find_camera_in_collection(col)
            if cam:
                context.scene.camera = cam
            elif is_new_col:
                log_add(settings, 'WARNING',
                        f"{name}: no camera found in collection")

            # Backup and then force all sub-collections of this collection to be
            # visible (exclude=False) so nothing is accidentally hidden from the render
            child_lc_backup = backup_child_lc_states(context, col)
            setup_collection_visibility(context, col)
            # Hide lights that don't belong to this collection
            setup_lights_for_collection(col)

            # ── Reset optional render passes to baseline ──────────────────────
            # Each pass block below enables ONLY what it strictly needs.
            # Without this reset a flag enabled by one pass (e.g. glossy_direct
            # by Highlight, cryptomatte by Shadow Inside) would bleed into
            # subsequent passes and slow them down well past an F12 render.
            #
            # Note: we KEEP ``use_persistent_data = True`` for every pass.
            # Disabling it forces Cycles to rebuild the BVH (~5 min on a
            # heavy scene) before EVERY pass — that was the main reason
            # script renders were ~4× slower than manual F12.  Cycles
            # invalidates the BVH automatically when visibility actually
            # changes (e.g. Crop's isolate_object_for_render), so leaving
            # persistent_data on is safe and dramatically faster.
            context.scene.render.use_persistent_data = True
            for vl in context.scene.view_layers:
                vl.use_pass_glossy_direct = False
                vl.use_pass_cryptomatte_object = False
                vl.use_pass_cryptomatte_material = False

            # These are reset per-pass; only set if needed for this pass
            placeholders_vis_backup = {}
            render_isolation_backup = {}
            matte_render_backup = {}   # matte mesh hide_render (crop holdout)
            temp_comp = None        # temp NodeGroup for add-on-built passes

            od = render_dir
            os.makedirs(od, exist_ok=True)

            # ── Render (raw beauty) ──────────────────────────────────────────
            # Swap to the collection's compositor NodeTree (zero-copy on B5) and
            # apply its per-collection World override (if assigned).  The scene's
            # own view-transform / look / film-transparency are used as-is (set
            # explicitly from the snapshot so a prior collection's pass can't
            # bleed in).  This is the reference beauty pass that the Highlight /
            # Shadow passes re-save through a curve node.
            if output_name == "Render":
                context.scene.world = item.world if item.world else orig['world']
                context.scene.view_settings.view_transform = orig['view_transform']
                context.scene.view_settings.look = orig['look']
                if orig['display_device'] and hasattr(context.scene.display_settings, 'display_device'):
                    context.scene.display_settings.display_device = orig['display_device']
                context.scene.render.film_transparent = orig['film_transparent']
                if item.compositor_node_tree:
                    swap_compositor(context, item.compositor_node_tree)
                # Beauty pass = setup / object / background only.  Keep the
                # 'placeholders' and 'Matte' helper sub-collections out of the
                # image (restored by restore_child_lc_states at pass end).
                exclude_helper_collections_for_render(context, col)

            # ── Crop A / B / C ────────────────────────────────────────────────
            # Uses a TEMP NodeGroup so the user's compositor is never touched.
            # Renders ONLY the 'placeholders' sub-collection within the selected
            # collection.  All other child collections are excluded — EXCEPT a
            # 'Matte' sub-collection, which is kept visible as a holdout.  Which
            # matte actually masks is now PER-SLOT: only the matte linked to this
            # crop slot (item.crop_<slot>_matte) is left renderable; every other
            # matte is hidden.  If the slot has no linked matte, all mattes are
            # hidden so the placeholder renders un-masked.  Placeholder objects
            # get Placeholder_mat assigned (Diffuse front / Transparent back via
            # Backfacing) so they render as a clean white silhouette on a
            # transparent background.  Film Transparent is ON.
            elif (output_name.startswith("Crop ")
                  and len(output_name) == 6
                  and output_name[5] in 'ABCDEFGHIJ'):
                context.scene.world = orig['world']
                context.scene.view_settings.view_transform = 'Standard'
                context.scene.view_settings.look = 'None'
                context.scene.render.film_transparent = True
                context.scene.render.image_settings.color_mode = 'RGBA'
                # (persistent_data + GlossDir/Cryptomatte passes were already
                # reset to False at the top of this iteration — Crop needs none.)
                # Use a temp NodeGroup so the user's compositor is untouched.
                # This renders the RAW silhouette (saved as _alfa_<slot>); the
                # Convert_Crop alpha-broadening runs afterwards as a separate
                # post-process pass ("Convert Crop <slot>") → _crop_<slot>.
                # setup_crop_alpha_compositor wires:
                #   RGB(white) → SetAlpha(REPLACE_ALPHA) → output
                #   RenderLayers[Alpha] ↗ (Alpha input)
                # This explicit alpha wiring is needed because B5 compositor
                # NodeGroups don't reliably pass alpha through a plain
                # RenderLayers[Image] → output passthrough.
                temp_comp = create_temp_compositor(context)
                setup_crop_alpha_compositor(context, item)

                # Show only 'placeholders' child collection(s), hide everything else.
                # Handles Blender auto-numbering (placeholders.001, etc.)
                # The 'Matte' sub-collection is kept visible and flagged as a
                # HOLDOUT; per-slot object visibility (set just below) decides
                # which single matte actually masks this placeholder.
                placeholders_vis_backup = isolate_placeholders_collection(context, col)

                # Find the specific placeholder object for this crop slot
                _SL = output_name[5]           # 'A', 'B', … 'J'
                _sl = _SL.lower()              # 'a', 'b', … 'j'
                placeholder_name = getattr(item, f"crop_{_sl}_name", "")

                # Per-slot matte holdout: gather every matte linked to this slot
                # (one slot may have many).  Those mattes are made renderable so
                # they mask out the placeholder; all other mattes are hidden.
                # Includes the legacy single-matte field for backward compat.
                linked_mattes = [l.name for l in item.matte_links
                                 if l.slot == _SL and l.name]
                _legacy = getattr(item, f"crop_{_sl}_matte", "")
                if _legacy:
                    linked_mattes.append(_legacy)
                matte_render_backup = setup_matte_holdout_objects(col, linked_mattes)

                # Find the sub-collection that contains the target placeholder.
                # Primary: look up the object by name and check which child
                # collection of `col` it belongs to (robust against Blender
                # auto-renaming "placeholders" to "placeholders.001" etc.).
                # Fallback: name-based search for any child starting with
                # "placeholders" (handles pre-existing objects).
                placeholders_subcol = None
                target_obj = bpy.data.objects.get(placeholder_name) if placeholder_name else None
                if target_obj:
                    obj_cols = set(target_obj.users_collection)
                    for child in col.children:
                        if child in obj_cols:
                            placeholders_subcol = child
                            break
                if not placeholders_subcol:
                    from .utils import _is_placeholders_col
                    for child in col.children:
                        if _is_placeholders_col(child.name):
                            placeholders_subcol = child
                            break

                # Ensure Placeholder_mat is assigned to all placeholder meshes
                # (kept permanently — not restored after render)
                if placeholders_subcol:
                    for obj in get_objects_recursive(placeholders_subcol):
                        if obj.type == 'MESH':
                            assign_placeholder_mat(obj)

                # Isolate the specific placeholder for this slot — hide all
                # other placeholder objects so Crop A only shows A, etc.
                if placeholders_subcol and placeholder_name:
                    render_isolation_backup = isolate_object_for_render(
                        placeholders_subcol, placeholder_name
                    )

            # ── Render + dual-save (PNG main output + per-pass EXR files) ─────
            # Approach (v1.0.20+):
            #   • Main render stays PNG (via scene.render.filepath +
            #     image_settings.file_format='PNG').  write_still=True saves it.
            #   • A transient CompositorNodeOutputFile writes one separate
            #     single-layer .exr file PER render pass listed in
            #     CP3D_EXR_PASS_OUTPUTS (currently just Material Index).
            #
            # Why separate files instead of a single multi-layer EXR?
            # Blender 5's multi-layer support is unreliable at the Python
            # level — layer_slots don't always produce a real multi-layer
            # output.  file_slots (one .exr per pass) is the well-trodden,
            # reliable path and works identically on Blender 4 and 5.
            png_fp = os.path.join(od, f"{name}_{suffix}.png")
            exr_stem = os.path.join(od, f"{name}_{suffix}")   # no extension

            # Ensure main render writes PNG (per-pass blocks set color_mode
            # but the global format was set to PNG at operator start).
            _imgset = context.scene.render.image_settings
            _pass_format = _imgset.file_format
            _pass_depth  = _imgset.color_depth
            _imgset.file_format = 'PNG'
            _imgset.color_depth = '8'
            context.scene.render.filepath = png_fp

            # Inject File Output node writing the configured pass outputs.
            # Uses whatever compositor tree is currently active (CUS on
            # Render/Background, PASS on Highlight/Shadow, temp on Crop/Shadow
            # Inside).  The node + its dedicated RenderLayers source node are
            # removed immediately after the render, so user compositors stay
            # untouched on disk.
            _comp_tree = get_node_tree(context)
            _exr_fo = None
            _exr_records = []
            _exr_rl = None
            if _comp_tree is not None:
                try:
                    _exr_fo, _exr_records = add_separate_pass_exr_outputs(
                        _comp_tree, od, f"{name}_{suffix}",
                        CP3D_EXR_PASS_OUTPUTS,
                    )
                    # Track the RL source node so we can remove it too
                    _exr_rl = _comp_tree.nodes.get("_CP3D_RL_EXR_")
                except Exception as _e:
                    print(f"[CP3D] Could not inject per-pass EXR outputs: {_e}")
                    self.report({'WARNING'},
                                f"EXR setup failed for {name}_{suffix}")

            # Render — writes PNG (via write_still) + one .exr per enabled
            # pass in CP3D_EXR_PASS_OUTPUTS (via the File Output node) in a
            # single Cycles pass.  No re-render.
            bpy.ops.render.render(write_still=True)
            print(f"Saved: {png_fp}")
            self.report({'INFO'}, f"Saved: {name}_{suffix}.png")
            log_add(settings, 'RENDER', f"Saved {name}_{suffix}.png")

            # Rename each slot's frame-numbered output to the clean
            # "<stem>_<suffix>.exr" form, then remove the transient nodes.
            if _exr_fo is not None:
                saved_exrs = finalize_separate_exr_outputs(
                    context, od, _exr_records, exr_stem,
                )
                for exr_path in saved_exrs:
                    print(f"Saved: {exr_path}")
                    self.report({'INFO'}, f"Saved: {os.path.basename(exr_path)}")
                if not saved_exrs and _exr_records:
                    self.report({'WARNING'},
                                f"EXR pass outputs not found on disk for {name}_{suffix}")
                # Clean up both the File Output and its dedicated RL node
                try:
                    _comp_tree.nodes.remove(_exr_fo)
                except Exception:
                    pass
                if _exr_rl is not None:
                    try:
                        _comp_tree.nodes.remove(_exr_rl)
                    except Exception:
                        pass

            # Restore per-pass format settings
            _imgset.file_format = _pass_format
            _imgset.color_depth = _pass_depth

            # ── Restore per-pass state changes before moving to the next pass ─
            # (Placeholder_mat is kept permanently — no material restore needed)
            # Restore per-object hide_render (used to isolate a single placeholder)
            if render_isolation_backup:
                restore_render_isolation(render_isolation_backup)
            # Restore matte mesh hide_render (set for the crop holdout)
            if matte_render_backup:
                restore_matte_holdout_objects(matte_render_backup)
            # Restore placeholders sub-collection visibility (incl. matte holdout)
            if placeholders_vis_backup:
                restore_placeholders_isolation(context, col, placeholders_vis_backup)
            # (Cryptomatte / GlossDir / persistent_data are reset at the start
            # of the NEXT iteration — and restored to their original values
            # by the outer finally block.  No per-pass restore needed here.)
            # Remove temp NodeGroups used by add-on-built passes
            if temp_comp is not None:
                remove_temp_compositor(context, temp_comp)
            restore_child_lc_states(context, col, child_lc_backup)

          # ── Post-loop: announce the final collection's completion ──────────
          # (aligned with the `for` block — runs only after the loop exits
          # normally, not when the cancel branch `break`s out above.)
          if _current_col is not None:
              final_msg = (f"✓ Finished collection "
                           f"{_current_col_idx}/{len(enabled_cols)} "
                           f"— batch complete")
              print(f"[CP3D] {final_msg}")
              self.report({'INFO'}, final_msg)
              gc.collect()

        finally:
            # ── Restore ALL original scene settings ───────────────────────────
            # This block ALWAYS runs — after success, error, or cancellation.
            # It brings film_transparent, world, compositor, visibility, shadow
            # catchers, etc. back to exactly what they were before rendering.
            context.scene.render.resolution_x = orig['res_x']
            context.scene.render.resolution_y = orig['res_y']
            context.scene.camera = orig['camera']
            context.scene.render.filepath = orig['filepath']
            context.scene.render.image_settings.file_format = orig['format']
            context.scene.render.image_settings.color_mode = orig['color_mode']
            context.scene.render.film_transparent = orig['film_transparent']
            context.scene.view_settings.view_transform = orig['view_transform']
            context.scene.view_settings.look = orig['look']
            if orig['display_device'] and hasattr(context.scene.display_settings, 'display_device'):
                context.scene.display_settings.display_device = orig['display_device']
            context.scene.world = orig['world']
            context.scene.render.use_persistent_data = orig['persistent_data']
            # Restore optional render pass flags (GlossDir + Cryptomatte).
            # Done here (not per-pass) because the per-iteration reset-to-False
            # simplifies the pass blocks while keeping the user's state intact.
            for vl in context.scene.view_layers:
                if vl.name in orig['glossy_direct']:
                    vl.use_pass_glossy_direct = orig['glossy_direct'][vl.name]
                if vl.name in orig['crypto_obj']:
                    vl.use_pass_cryptomatte_object = orig['crypto_obj'][vl.name]
                if vl.name in orig['crypto_mat']:
                    vl.use_pass_cryptomatte_material = orig['crypto_mat'][vl.name]
            for cn_, ex in orig['col_vis'].items():
                c = bpy.data.collections.get(cn_)
                if c:
                    lc = find_layer_collection(context.view_layer.layer_collection, c)
                    if lc:
                        lc.exclude = ex
            for on, h in orig['light_vis'].items():
                o = bpy.data.objects.get(on)
                if o:
                    o.hide_render = h
            for on, st in orig['shadow_states'].items():
                o = bpy.data.objects.get(on)
                if o:
                    o.is_shadow_catcher = st
            # Restore the user's original compositor node tree from the snapshot
            # taken before rendering (B5: a simple reference swap-back).
            restore_compositor(context, comp_backup)
            # ── Remove any data-blocks the render leaked ──────────────────────
            # Worlds (World.001…) and node groups (Convert_Shadow.001…) that
            # appeared during the render and are now orphaned are deleted, so the
            # blend ends with exactly the world / node-group count it started.
            _pw, _pg = purge_leaked_orphans(data_snapshot)
            # Collapse any numbered Convert-group duplicates the purge missed
            # (fake-user copies aren't orphans, so they survive the purge).
            try:
                _pg += dedupe_convert_groups()
            except Exception:
                pass
            if _pw or _pg:
                print(f"[CP3D] Cleaned leaked data: {_pw} world(s), "
                      f"{_pg} node group(s)")
                log_add(settings, 'CHECK',
                        f"Cleaned {_pw} stray world(s), {_pg} node group(s)")
            # Clear the live progress UI state so the progress box disappears
            # and the panel returns to its normal layout.  Done unconditionally
            # so the UI never gets stuck in "rendering" mode after an error.
            settings.render_in_progress = False
            settings.cancel_requested = False
            settings.progress_col_idx = 0
            settings.progress_col_total = 0
            settings.progress_col_name = ""
            settings.progress_pass_idx = 0
            settings.progress_pass_total = 0
            settings.progress_pass_name = ""
            try:
                for window in context.window_manager.windows:
                    for area in window.screen.areas:
                        area.tag_redraw()
            except Exception:
                pass

        # ── Hide helper collections now the render is done ─────────────────
        # Exclude each rendered collection's 'placeholders' and 'Matte'
        # sub-collections so they're no longer shown in the viewport once the
        # batch finishes (they're re-included automatically the next time a
        # crop pass needs them).
        for _item in enabled_cols:
            if _item.collection:
                exclude_helper_collections_for_render(context, _item.collection)

        # ── Set render-done feedback state ────────────────────────────────
        # Mark which collections were rendered so the UIList can highlight them.
        rendered_names = set()
        for _, rn, _, _, _, _, _ in render_list:
            rendered_names.add(rn)
        for item in settings.collections:
            cname = item.collection.name if item.collection else ""
            item.was_rendered = cname in rendered_names
        settings.render_done = True
        settings.render_message = f"Done!  {total} renders completed"
        log_add(settings, 'SUCCESS', f"Render complete — {total} pass(es)")
        # Schedule auto-clear: after a 1s delay a depsgraph handler is added
        # that clears the green state on the NEXT user interaction.
        _schedule_render_done_clear()

        self.report({'INFO'}, f"Print3Dexporter: done — {total} render(s) completed")
        return {'FINISHED'}
