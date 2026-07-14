"""Print3Dexporter — PropertyGroup definitions.

Defines the two PropertyGroups that store all per-collection and scene-level
settings.  These are attached to bpy.types.Scene in __init__.py and saved
with the .blend file automatically.

Classes:
  CollectionRenderItem     — per-collection settings (one entry per row in
                             the list panel): resolution, output toggles,
                             crop/shadow/highlight sub-collection names, etc.
  CollectionRenderSettings — scene-level settings: the CollectionProperty
                             that holds all CollectionRenderItems, the active
                             index, compositor toggle, and shared colour-curve
                             values for highlight and shadow outputs.

The `_propagate_to_selected` mechanism lets you edit one item's output
toggles and have the change cascade to every other item whose `selected`
checkbox is ticked — useful for batch-setting render outputs across scenes.
"""

import bpy
from bpy.types import PropertyGroup
from bpy.props import (
    StringProperty, IntProperty, BoolProperty, FloatProperty,
    PointerProperty, CollectionProperty, EnumProperty,
)

from .constants import BLENDER_5


# ── Multi-select propagation ──────────────────────────────────────────────────

# Re-entrancy guard: Blender calls update callbacks immediately when a property
# changes.  Without this flag, setting item.render_raw inside the loop would
# fire _upd_render_raw again for each selected item, causing infinite recursion.
_cp3d_propagating = False


def _propagate_to_selected(attr, self, context):
    """Copy the value of `attr` from `self` to all other selected items.

    Called by every _upd_* callback below.  The `selected` flag on each
    CollectionRenderItem controls whether it is a propagation target.
    Only items with selected=True receive the change — the item that was
    actually edited always updates regardless (Blender sets it before the
    callback fires).
    """
    global _cp3d_propagating
    if _cp3d_propagating:
        # We are already inside a propagation loop — bail out immediately
        # to prevent the recursive cascade described above.
        return
    _cp3d_propagating = True
    try:
        s = context.scene.collection_render_settings
        val = getattr(self, attr)
        for item in s.collections:
            if item.selected:
                setattr(item, attr, val)
    finally:
        # Always clear the guard, even if an exception occurs mid-loop
        _cp3d_propagating = False


# One thin wrapper per propagatable property — Blender's update= argument
# requires a callable that takes (self, context), so we can't pass
# _propagate_to_selected directly (it takes three arguments).
def _upd_render_raw(self, ctx):        _propagate_to_selected('render_raw',        self, ctx)
def _upd_render_crop(self, ctx):       _propagate_to_selected('render_crop',       self, ctx)
def _upd_convert_hl(self, ctx):        _propagate_to_selected('convert_highlight', self, ctx)
def _upd_convert_shadow(self, ctx):    _propagate_to_selected('convert_shadow',    self, ctx)


# ── Convert intensity → Value node live-apply ─────────────────────────────────
# These slider callbacks push the value straight into the matching Value node
# inside the Convert_Highlight / Convert_Shadow group, so dragging the slider
# updates the compositor live.  Imported lazily so module reloads never hold a
# stale reference to set_convert_value.  Silently no-ops if the group/Value node
# isn't present yet (the value is re-applied at convert time regardless).
def _upd_convert_highlight_value(self, ctx):
    from .convert_compositor import set_convert_value
    set_convert_value('highlight', self.convert_highlight_value)

def _upd_convert_shadow_value(self, ctx):
    from .convert_compositor import set_convert_value
    set_convert_value('shadow', self.convert_shadow_value)


# ── Live Update: mirror active collection's World / Compositor to the scene ───
# When live_update is ON, the scene's World and compositing node group are
# switched to the active collection's chosen ones so the viewport previews them.
# The scene's originals are saved on enable and restored on disable.

def _apply_live_world_comp(context):
    """Push the active collection's World / Compositor onto the scene (if live)."""
    s = context.scene.collection_render_settings
    if not s.live_update:
        return
    if not (0 <= s.active_index < len(s.collections)):
        return
    item = s.collections[s.active_index]
    scene = context.scene
    scene.world = item.world if item.world else s.live_saved_world
    if BLENDER_5 and hasattr(scene, 'compositing_node_group'):
        scene.compositing_node_group = (item.compositor_node_tree
                                        if item.compositor_node_tree
                                        else s.live_saved_comp)


def _upd_live_update(self, ctx):
    scene = ctx.scene
    if self.live_update:
        # Snapshot the scene's current World / compositor so we can restore them
        self.live_saved_world = scene.world
        if BLENDER_5 and hasattr(scene, 'compositing_node_group'):
            self.live_saved_comp = scene.compositing_node_group
        _apply_live_world_comp(ctx)
    else:
        # Restore what the scene had before Live Update was switched on
        scene.world = self.live_saved_world
        if BLENDER_5 and hasattr(scene, 'compositing_node_group'):
            scene.compositing_node_group = self.live_saved_comp


def _upd_item_world(self, ctx):   _apply_live_world_comp(ctx)
def _upd_item_comp(self, ctx):    _apply_live_world_comp(ctx)


# ── Show: solo the active (blue-line) collection in the viewport ──────────────
# When show_isolate is ON, ONLY the active collection is shown (minus its
# 'placeholders' and 'Matte' sub-collections); all other collections are
# hidden.  The pre-toggle exclude state is saved as JSON for restore on disable.

def _apply_show_isolate(context):
    s = context.scene.collection_render_settings
    if not s.show_isolate:
        return
    if not (0 <= s.active_index < len(s.collections)):
        return
    item = s.collections[s.active_index]
    if not item.collection:
        return
    from .utils import (
        isolate_collection_except_helpers, set_active_camera_view,
        find_camera_in_collection,
    )
    isolate_collection_except_helpers(context, item.collection)
    # Look through the active collection's camera while soloed.
    set_active_camera_view(context, find_camera_in_collection(item.collection))


def _upd_show_isolate(self, ctx):
    import json
    from .utils import capture_all_lc_excludes, apply_all_lc_excludes
    if self.show_isolate:
        # Back up current exclude states + scene camera, then solo the active one
        self.show_backup = json.dumps(capture_all_lc_excludes(ctx))
        self.show_saved_camera = ctx.scene.camera
        _apply_show_isolate(ctx)
    else:
        if self.show_backup:
            try:
                apply_all_lc_excludes(ctx, json.loads(self.show_backup))
            except Exception:
                pass
        self.show_backup = ""
        # Restore the scene camera we had before Show was switched on
        ctx.scene.camera = self.show_saved_camera


def _upd_active_index(self, ctx):
    # Keep the live previews pointed at whatever collection is now active
    _apply_live_world_comp(ctx)
    _apply_show_isolate(ctx)


# ── Compositor NodeTree poll filter ──────────────────────────────────────────
# Shows every compositor NodeTree in the per-collection dropdown EXCEPT the
# reserved "PASS …" ones (those are designed to be routed-through by name for
# specific passes and shouldn't be picked as a collection's main compositor).
#
# History: this filter used to require names to START WITH "CUS", which meant
# that in any scene whose compositor node groups weren't named "CUS…" the
# dropdown came up empty — there was nothing to choose even when several
# compositors existed.  Dropping the prefix requirement fixes that; we only
# hide "PASS …" groups now.
#
# Note (Blender 4): the dropdown lists data-block NodeTrees (bpy.data.node_groups)
# only.  The scene's *embedded* compositor (scene.node_tree on B4) is not a
# data-block and therefore never appears here regardless of name — make the
# compositor a real node group if you need to pick it.

def _poll_cus_compositor(self, node_tree):
    """Accept any COMPOSITING node tree except the reserved 'PASS …' ones."""
    return (node_tree.type == 'COMPOSITING'
            and not node_tree.name.startswith('PASS'))


# ── Log store ──────────────────────────────────────────────────────────────
# A scrollable, collapsible log shown in the main panel between the render
# buttons and the collection list.  Operators push entries via log_add();
# CP3D_UL_log_list (panels.py) renders one row per entry with a level icon.

# Maximum entries kept — oldest are dropped past this so the file never bloats.
_CP3D_LOG_MAX = 300


class CP3D_LogItem(PropertyGroup):
    """One log line: a severity *level* and the *message* text.

    level is one of: INFO, CHECK, RENDER, EXPORT, WARNING, WARN, ERROR, FAIL,
    SUCCESS, OK.  The level only drives which icon CP3D_UL_log_list draws (and
    whether the row gets red tint) — it has no effect on behaviour.

    Traffic-light colours (used by Check Setup):
      OK / SUCCESS  → green icon
      WARN          → yellow icon
      FAIL / ERROR  → red icon + red row tint
    """
    level:   StringProperty(default='INFO')
    message: StringProperty(default="")
    # When True the row is drawn greyed-out (inactive).  Check Setup mutes every
    # existing entry before writing its fresh report so the newest run stands out.
    muted:   BoolProperty(default=False)


def log_add(settings, level, message):
    """Append one entry to *settings*.log_entries (trims to _CP3D_LOG_MAX).

    *settings* is a CollectionRenderSettings.  Safe to call from any operator
    or helper that already has the scene settings in hand.  Sets log_active_index
    to the newest entry so the UIList auto-scrolls to it.
    """
    try:
        entries = settings.log_entries
        # Trim from the front when we hit the cap (keep the newest _CP3D_LOG_MAX)
        while len(entries) >= _CP3D_LOG_MAX:
            entries.remove(0)
        it = entries.add()
        it.level = level
        it.message = str(message)
        settings.log_active_index = len(entries) - 1
    except Exception:
        # Logging must never break the operation it is reporting on
        pass


# ── Matte link ─────────────────────────────────────────────────────────────
# One placeholder slot can be linked to MANY matte objects.  Each link records
# the slot letter (A–J) and the matte object's name.  During that slot's Crop
# render every linked matte is made renderable and acts as a holdout, masking
# out the placeholder; all unlinked mattes are hidden.  See the crop pass in
# operators.py and setup_matte_holdout_objects() in utils.py.

class CP3D_MatteLink(PropertyGroup):
    """A single placeholder-slot → matte-object link."""
    slot: StringProperty(default="")   # 'A'..'J'
    name: StringProperty(default="")   # matte object name (from 'Matte' sub-col)


# ── Property groups ───────────────────────────────────────────────────────────

class CollectionRenderItem(PropertyGroup):
    """Per-collection render configuration stored in the collection list.

    One instance of this group exists for each row in the CP3D list panel.
    It carries all the settings that can differ between collections: which
    output passes to render, what objects to use for each crop slot, shadow
    and holdout sub-collection names, resolution override, etc.
    """

    collection:     PointerProperty(name="Collection", type=bpy.types.Collection)
    custom_name:    StringProperty(name="Custom Name", default="")
    resolution_x:   IntProperty(name="Res X", default=1920, min=1, max=16384)
    resolution_y:   IntProperty(name="Res Y", default=1080, min=1, max=16384)
    enabled:        BoolProperty(name="Enabled", default=True)

    # ── Selection (used for bulk output toggle propagation) ───────────────────
    # When `selected` is True for an item, any output toggle (render_raw, etc.)
    # changed on *any other* item will be copied here too via _propagate_to_selected.
    # This is purely a UI multi-edit mechanism — it has no effect on rendering.
    selected:       BoolProperty(name="Selected", default=False)
    # Set to True after a successful render batch — used by the UI to highlight
    # this collection's name in green.  Cleared on the next user interaction.
    was_rendered:   BoolProperty(name="Was Rendered", default=False)

    # ── Custom compositor node tree (per-collection) ───────────────────────────
    # When assigned, this node tree is used as the active compositor during the
    # Render and Render ISO passes.  The Highlight / Shadow passes re-use it too,
    # routing the render through its "Highlight" / "Shadow" node respectively.
    compositor_node_tree: PointerProperty(
        name="Compositor",
        type=bpy.types.NodeTree,
        poll=_poll_cus_compositor,
        description="Compositor node tree for the Render / Render ISO passes "
                    "(any compositor group except reserved 'PASS …' ones)",
        update=_upd_item_comp,
    )

    # ── Custom World (per-collection) ──────────────────────────────────────────
    # When assigned, this World overrides the scene World during the Render and
    # Render ISO passes (and is inherited by the Highlight / Shadow re-saves of
    # the Render output).  Leave empty to use the scene's current World.
    # Mirrors the per-collection Compositor selector above.
    world: PointerProperty(
        name="World",
        type=bpy.types.World,
        description="World used for the Render / Render ISO passes "
                    "(overrides the scene World). Leave empty to use the scene World",
        update=_upd_item_world,
    )

    # ── Render output toggles (propagate to selected items) ───────────────────
    render_raw: BoolProperty(
        name="Render", default=False,
        description="Render the beauty pass with the assigned Compositor and World (saved as _render)",
        update=_upd_render_raw,
    )
    render_crop:    BoolProperty(name="Crop",       default=True,  update=_upd_render_crop)

    # ── Convert passes (post-process _render.png — no Cycles re-render) ───────
    # These run after the Render pass (or standalone) and route the already-
    # written _render.png through the Convert_Highlight / Convert_Shadow
    # compositor node groups, saving the result as _highlight.png / _shadow.png.
    convert_highlight: BoolProperty(
        name="→ Highlight", default=False,
        description="Post-process _render.png through Convert_Highlight node tree "
                    "(saved as _highlight). Requires _render.png to exist on disk",
        update=_upd_convert_hl,
    )
    convert_shadow: BoolProperty(
        name="→ Shadow", default=False,
        description="Post-process _render.png through Convert_Shadow node tree "
                    "(saved as _shadow). Requires _render.png to exist on disk",
        update=_upd_convert_shadow,
    )

    # ── Placeholder material assignment ─────────────────────────────────────────
    # When True, the Import Placeholders operator assigns Placeholder_mat to
    # imported objects (Diffuse front / Transparent back via Backfacing node).
    assign_placeholder_mat: BoolProperty(
        name="Assign Mat",
        description="Assign Placeholder_mat to imported placeholder objects",
        default=True,
    )

    # ── Crop / Placeholder settings (per-collection) ──────────────────────────
    # crop_slot_count controls how many placeholder rows the panel shows (1–10).
    # Slots beyond the active count are disabled and their names cleared when
    # the count is reduced via the Remove Slot button.
    crop_slot_count: IntProperty(
        name="Slot Count", default=1, min=1, max=10,
        description="How many placeholder object slots are active",
    )
    # Each placeholder slot carries: an enabled flag, the assigned object name,
    # and integer Width / Height values (written into the GLB-folder CSV and
    # used to label each exported placeholder).
    crop_a_enabled: BoolProperty(default=True)
    crop_a_name:    StringProperty(default="")
    crop_a_width:   IntProperty(name="Width",  default=0, min=0, max=100000)
    crop_a_height:  IntProperty(name="Height", default=0, min=0, max=100000)
    crop_b_enabled: BoolProperty(default=True)
    crop_b_name:    StringProperty(default="")
    crop_b_width:   IntProperty(name="Width",  default=0, min=0, max=100000)
    crop_b_height:  IntProperty(name="Height", default=0, min=0, max=100000)
    crop_c_enabled: BoolProperty(default=True)
    crop_c_name:    StringProperty(default="")
    crop_c_width:   IntProperty(name="Width",  default=0, min=0, max=100000)
    crop_c_height:  IntProperty(name="Height", default=0, min=0, max=100000)
    crop_d_enabled: BoolProperty(default=True)
    crop_d_name:    StringProperty(default="")
    crop_d_width:   IntProperty(name="Width",  default=0, min=0, max=100000)
    crop_d_height:  IntProperty(name="Height", default=0, min=0, max=100000)
    crop_e_enabled: BoolProperty(default=True)
    crop_e_name:    StringProperty(default="")
    crop_e_width:   IntProperty(name="Width",  default=0, min=0, max=100000)
    crop_e_height:  IntProperty(name="Height", default=0, min=0, max=100000)
    crop_f_enabled: BoolProperty(default=True)
    crop_f_name:    StringProperty(default="")
    crop_f_width:   IntProperty(name="Width",  default=0, min=0, max=100000)
    crop_f_height:  IntProperty(name="Height", default=0, min=0, max=100000)
    crop_g_enabled: BoolProperty(default=True)
    crop_g_name:    StringProperty(default="")
    crop_g_width:   IntProperty(name="Width",  default=0, min=0, max=100000)
    crop_g_height:  IntProperty(name="Height", default=0, min=0, max=100000)
    crop_h_enabled: BoolProperty(default=True)
    crop_h_name:    StringProperty(default="")
    crop_h_width:   IntProperty(name="Width",  default=0, min=0, max=100000)
    crop_h_height:  IntProperty(name="Height", default=0, min=0, max=100000)
    crop_i_enabled: BoolProperty(default=True)
    crop_i_name:    StringProperty(default="")
    crop_i_width:   IntProperty(name="Width",  default=0, min=0, max=100000)
    crop_i_height:  IntProperty(name="Height", default=0, min=0, max=100000)
    crop_j_enabled: BoolProperty(default=True)
    crop_j_name:    StringProperty(default="")
    crop_j_width:   IntProperty(name="Width",  default=0, min=0, max=100000)
    crop_j_height:  IntProperty(name="Height", default=0, min=0, max=100000)

    # ── Per-slot matte links (one slot → many mattes) ───────────────────────────
    # matte_links holds CP3D_MatteLink entries; each carries a slot letter and a
    # matte object name.  A slot can have several linked mattes — during that
    # slot's Crop render every linked matte becomes a holdout that masks out the
    # placeholder, and all unlinked mattes are hidden.  No links → no holdout.
    matte_links: CollectionProperty(type=CP3D_MatteLink)

    # Legacy single-matte fields (pre-1.0.46).  Kept so existing assignments
    # still drive the holdout; new links use matte_links above.  Not shown in UI.
    crop_a_matte: StringProperty(name="Matte", default="")
    crop_b_matte: StringProperty(name="Matte", default="")
    crop_c_matte: StringProperty(name="Matte", default="")
    crop_d_matte: StringProperty(name="Matte", default="")
    crop_e_matte: StringProperty(name="Matte", default="")
    crop_f_matte: StringProperty(name="Matte", default="")
    crop_g_matte: StringProperty(name="Matte", default="")
    crop_h_matte: StringProperty(name="Matte", default="")
    crop_i_matte: StringProperty(name="Matte", default="")
    crop_j_matte: StringProperty(name="Matte", default="")


class CollectionRenderSettings(PropertyGroup):
    """Scene-level settings shared across all collections.

    Attached to bpy.types.Scene as `scene.collection_render_settings` in
    __init__.py.  Acts as the root container for:
      - The list of CollectionRenderItems (one per collection to render).
      - The currently active list index (which row is highlighted).
      - A compositor on/off toggle that drives the live preview node graph.
      - Shared colour-correction parameters (desaturation, black/white point)
        that are baked into the compositor node graph for highlight and shadow
        outputs — these apply globally, not per-collection.
      - crop_source: whether Cryptomatte masks are keyed by object name or
        material name (affects which Cryptomatte layer the nodes read from).
    """

    collections:    CollectionProperty(type=CollectionRenderItem)
    active_index:   IntProperty(default=0, update=_upd_active_index)
    # ── Render-done feedback (transient — cleared on next user click) ─────────
    render_done:    BoolProperty(default=False)
    render_message: StringProperty(default="")

    # ── Live Update preview (World / Compositor) ──────────────────────────────
    # When ON, the scene World + compositor mirror the active collection's, so
    # the viewport previews them.  Originals are saved here and restored on OFF.
    live_update: BoolProperty(
        name="Live Update", default=False,
        description="Preview the active collection's World and Compositor in the "
                    "viewport by applying them to the scene (restored when off)",
        update=_upd_live_update,
    )
    live_saved_world: PointerProperty(type=bpy.types.World)
    live_saved_comp:  PointerProperty(type=bpy.types.NodeTree)

    # ── Show isolate preview (viewport) ───────────────────────────────────────
    # When ON, every collection is shown except the active collection's
    # placeholder & matte sub-collections.  show_backup stores the pre-toggle
    # exclude states (JSON) for restore on OFF.
    show_isolate: BoolProperty(
        name="Show", default=False,
        description="Show only the active (highlighted) collection in the "
                    "viewport, minus its placeholder and matte sub-collections",
        update=_upd_show_isolate,
    )
    show_backup: StringProperty(default="")
    show_saved_camera: PointerProperty(type=bpy.types.Object)

    # ── Log window (collapsible, between render buttons and the list) ─────────
    # log_entries is filled by log_add(); CP3D_UL_log_list draws it.  log_show
    # toggles the roll-out/collapse; log_active_index tracks the newest row.
    log_entries:      CollectionProperty(type=CP3D_LogItem)
    log_active_index: IntProperty(default=0)
    log_show:         BoolProperty(
        name="Log", default=True,
        description="Show the log window (errors, checks, render and export reports)",
    )

    # ── Live render progress (transient — only set during batch render) ───────
    # CP3D_PT_main_panel reads these to draw two progress bars at the top
    # of the sidebar while rendering.  Set to False/0/"" when not rendering.
    render_in_progress:   BoolProperty(default=False)
    progress_col_idx:     IntProperty(default=0)     # 1-based current collection
    progress_col_total:   IntProperty(default=0)     # total collections enabled
    progress_col_name:    StringProperty(default="")
    progress_pass_idx:    IntProperty(default=0)     # 1-based current pass
    progress_pass_total:  IntProperty(default=0)     # passes for current collection
    progress_pass_name:   StringProperty(default="")
    # ── Cancel request flag (set by Cancel button during batch render) ────────
    # The render loop checks this between passes and breaks out cleanly when
    # True.  The current in-flight render cannot be interrupted — the flag
    # only stops additional passes from starting.
    cancel_requested:     BoolProperty(default=False)

    # ── Width / Height clipboard (scene-level → works across collections) ─────
    # Copy stores a slot's Width / Height here; Paste writes them into another
    # slot — in the same or any other collection.  dim_clip_set gates Paste so
    # nothing is pasted before a Copy has happened.
    dim_clip_width:  IntProperty(default=0, min=0, max=100000)
    dim_clip_height: IntProperty(default=0, min=0, max=100000)
    dim_clip_set:    BoolProperty(default=False)

    # ── Cryptomatte source ────────────────────────────────────────────────────
    # OBJECT keys the Cryptomatte mask by object name (most common).
    # MATERIAL keys it by material name (useful if multiple objects share a mat).
    crop_source: EnumProperty(
        items=[('OBJECT', "Object", ""), ('MATERIAL', "Material", "")],
        default='OBJECT',
    )

    # ── Convert intensity (scene-global — drives the Convert group Value nodes) ─
    # One value per Convert group, shared by every collection's Convert pass.
    # The update callbacks push the value live into the 'Convert_highlight_Value'
    # / 'Convert_Shadow_Value' nodes; convert_render_image re-applies it at
    # convert time so the written PNG always matches the slider.  Slider range is
    # 0–1 (the natural factor range, matching the 0.5 default) with headroom to
    # type up to 2.0 for stronger conversions.
    convert_highlight_value: FloatProperty(
        name="Highlight Intensity", default=0.5,
        min=0.0, max=2.0, soft_min=0.0, soft_max=1.0,
        description="Intensity of the Convert Highlight conversion — drives the "
                    "'Convert_highlight_Value' node inside the Convert_Highlight group",
        update=_upd_convert_highlight_value,
    )
    convert_shadow_value: FloatProperty(
        name="Shadow Intensity", default=0.5,
        min=0.0, max=2.0, soft_min=0.0, soft_max=1.0,
        description="Intensity of the Convert Shadow conversion — drives the "
                    "'Convert_Shadow_Value' node inside the Convert_Shadow group",
        update=_upd_convert_shadow_value,
    )
