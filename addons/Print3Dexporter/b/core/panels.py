"""Print3Dexporter — UI panels and UIList.

Defines all Panel subclasses that appear in the View3D sidebar under the
'CP3D' tab, plus the UILists (collection rows + log rows).

Panel layout (top → bottom in the CP3D tab):
  CP3D_PT_manual                   — TOP-LEVEL collapsible "How to use" manual,
                                     ordered ABOVE the main panel (bl_order 0) so
                                     it sits above the Render + Export GLB button.
                                     Collapsed-friendly: click the header to roll
                                     it out, click again to collapse back.
  CP3D_PT_main_panel               — root panel (bl_order 1): Render / Export GLB
                                     / Render + Export buttons + live progress
                                     bars, a collapsible Log window, the
                                     collection list, and per-item Compositor /
                                     World selectors and output toggles
  CP3D_PT_convert_trees            — sub-panel: Setup Compositor + Reset
                                     Highlight / Shadow + intensity sliders
  CP3D_PT_crop_settings            — sub-panel: placeholder object slots
                                     (each with an optional linked matte)

bl_order values control the vertical order of the sub-panels under the main
panel:
  20 = Convert Node Trees
  30 = Placeholder Objects
"""

import bpy

from bpy.types import Panel, UIList

from .constants import VERSION_STRING
from .utils import find_camera_in_collection, find_matte_collection
from .convert_compositor import HIGHLIGHT_TREE_NAME, SHADOW_TREE_NAME
from .converter import find_render_image_path
from .icons import get_canva_icon_id
from .glb_exporter import count_export_glbs


# ── Progress bar helper ───────────────────────────────────────────────────────
# Wraps UILayout.progress (Blender 4.0+) with a text-bar fallback for any
# build where the widget is unavailable.  Produces a full-width progress bar
# with a centered label showing the numerical count.

def _draw_progress_bar(layout, factor, label):
    """Draw a progress bar in *layout* filled by *factor* (0..1) with *label*."""
    factor = max(0.0, min(1.0, factor))
    row = layout.row(align=True)
    try:
        row.progress(factor=factor, type='BAR', text=label)
    except (AttributeError, TypeError):
        # Older Blender — simulate a progress bar with Unicode block chars
        width = 24
        filled = int(round(factor * width))
        bar = '█' * filled + '░' * (width - filled)
        row.label(text=f"{bar}  {label}")


def _draw_dotted_divider(layout):
    """Draw a horizontal divider between placeholder slots.

    Prefers Blender's native line separator (``separator(type='LINE')``); falls
    back to a row of dots for builds where the typed separator isn't available.
    """
    try:
        layout.separator(type='LINE')
    except TypeError:
        r = layout.row()
        r.enabled = False
        r.label(text="·" * 48)


# ── Collection UIList ─────────────────────────────────────────────────────────

class CP3D_UL_collection_list(UIList):
    """One row per CollectionRenderItem in the list panel.

    Draws: [enabled toggle] [collection name/icon] [selected checkbox]

    The 'selected' checkbox (right side) is used both for bulk editing of output
    toggles (render_raw, render_crop, etc.) and to pick which collections the
    Export Crop GLB button processes.  See _propagate_to_selected() in
    properties.py.
    """
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            r = layout.row(align=True)
            r.prop(item, "enabled", text="")
            if item.collection:
                # Collections that were just rendered or exported are marked
                # GREEN via the green collection-colour icon (COLLECTION_COLOR_04).
                # Blender's Python UI exposes no green text — alert is red-only —
                # so a green vector icon is how we show the "done" state.
                col_icon = ('COLLECTION_COLOR_04' if item.was_rendered
                            else 'OUTLINER_COLLECTION')
                r.label(text=item.collection.name, icon=col_icon)
            else:
                r.label(text="(None)", icon='ERROR')
            # Checkbox to mark item as selected for bulk Render Output editing.
            # Shows a filled or empty checkbox icon; emboss=False keeps it subtle.
            sel_icon = 'CHECKBOX_HLT' if item.selected else 'CHECKBOX_DEHLT'
            r.prop(item, "selected", text="", toggle=True, icon=sel_icon, emboss=False)


# ── Log UIList ────────────────────────────────────────────────────────────────

class CP3D_UL_log_list(UIList):
    """One row per CP3D_LogItem — a level icon plus the message text.

    Drives the collapsible Log window in the main panel.  The icon is chosen
    from the entry's level so errors / warnings / successes are scannable.

    Colour convention (used by Check Setup and other validators):
      OK / SUCCESS   — green strip-color icon, no tint
      WARN           — yellow strip-color icon, no tint
      FAIL / ERROR   — red strip-color icon + row.alert (Blender red tint)
    """
    # Map each log level to a Blender icon.  Unknown levels fall back to 'DOT'.
    # STRIP_COLOR_* are the reliably-colored icons Blender exposes (Blender 5.1
    # renamed the old SEQUENCE_COLOR_* set), so we use them for the
    # traffic-light OK / WARN / FAIL trio.
    _ICONS = {
        'INFO':    'INFO',
        'CHECK':   'CHECKMARK',
        'RENDER':  'RENDER_STILL',
        'EXPORT':  'EXPORT',
        'WARNING': 'ERROR',              # yellow warning triangle
        'WARN':    'STRIP_COLOR_03',     # yellow strip-color icon
        'ERROR':   'CANCEL',             # red error circle
        'FAIL':    'STRIP_COLOR_01',     # red strip-color icon
        'SUCCESS': 'STRIP_COLOR_04',     # green strip-color icon
        'OK':      'STRIP_COLOR_04',     # green strip-color icon
        'DIVIDER': 'BLANK1',             # blank icon — a plain separator row
    }

    # Levels that get Blender's red-alert row tint (in addition to their icon).
    _ALERT_LEVELS = {'ERROR', 'FAIL'}

    # Separator packing (collection-name, status) into one RESULT message.
    # Must match the value written by CP3D_OT_check_setup in operators.py.
    _RESULT_SEP = '\x1f'

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            if item.muted:
                # Greyed-out: a previous report superseded by a newer run.
                row.active = False

            if item.level == 'RESULT':
                # Overall per-collection result.  The collection NAME is drawn
                # with a light-blue accent icon and its own emphasised sub-row.
                # (Blender N-panel labels cannot be recoloured or bolded, so a
                # light-blue strip-colour icon is the closest "highlight" the
                # UI API allows — the name text itself stays theme-coloured.)
                name, _, status = item.message.partition(self._RESULT_SEP)
                name_row = row.row(align=True)
                name_row.label(text=name, icon='STRIP_COLOR_05')  # light blue
                if status:
                    row.label(text=status)
                return

            if not item.muted:
                row.alert = (item.level in self._ALERT_LEVELS)   # tint reds red
            row.label(text=item.message,
                      icon=self._ICONS.get(item.level, 'DOT'))
        elif self.layout_type == 'GRID':
            layout.label(text="", icon=self._ICONS.get(item.level, 'DOT'))


# ── Main panel ────────────────────────────────────────────────────────────────

class CP3D_PT_main_panel(Panel):
    """Root panel — compositor toggle, collection list, per-item output controls."""
    # bl_label is left EMPTY so the header is drawn entirely by draw_header()
    # with the custom Canva icon in front of the title.  (Blender draws the
    # expand-triangle, then draw_header() content, then bl_label — a non-empty
    # bl_label here would print the title a second time after our custom one.)
    bl_label = ""
    bl_idname = "CP3D_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'CP3D'
    bl_order = 1   # below the top-level Manual panel (bl_order 0)

    def draw_header(self, context):
        # Draw the title ourselves with the custom Canva icon in front:
        #   [Canva icon] Print 3D Exporter
        # icon_value=0 (no custom icon loaded) degrades to text only.
        layout = self.layout
        icon_id = get_canva_icon_id()
        if icon_id:
            layout.label(text="Print 3D Exporter", icon_value=icon_id)
        else:
            layout.label(text="Print 3D Exporter")

    def draw(self, context):
        layout = self.layout
        settings = context.scene.collection_render_settings

        # ── Version info row (top — small, informational) ─────────────────────
        box = layout.box()
        box.label(
            text=f"v{VERSION_STRING}  |  Blender {bpy.app.version_string}",
            icon='INFO',
        )

        # ── TOP ACTION BAR ────────────────────────────────────────────────────
        # Dominant row: Render + GLB (left) and Check Setup (right), then the
        # two individual actions (Render | Export Crop GLB) on one line, then
        # the live progress box.

        en = sum(1 for i in settings.collections if i.enabled and i.collection)

        # How many GLB files the Export GLB button will write — mirrors the
        # export operator's target selection (every checkmark-selected
        # collection, else the active one) and counts one GLB per exportable
        # placeholder slot in each.  Shown in () on the button like Render (N).
        _glb_targets = [i for i in settings.collections if i.selected and i.collection]
        if not _glb_targets and 0 <= settings.active_index < len(settings.collections):
            _act = settings.collections[settings.active_index]
            if _act.collection:
                _glb_targets = [_act]
        glb_n = sum(count_export_glbs(i) for i in _glb_targets)

        # Dominant primary action — Render + GLB (one click does both), with
        # Check Setup as a co-primary button on the same row so setup can be
        # validated before committing to a full render.
        row = layout.row(align=True)
        row.scale_y = 2.2   # tallest buttons — the main actions
        if settings.render_done:
            row.alert = True   # highlighted finish-state look
            row.operator("cp3d.render_and_export",
                         text="Render + GLB", icon='CHECKMARK')
        else:
            row.operator("cp3d.render_and_export",
                         text="Render + GLB", icon='RENDER_RESULT')
        row.operator("cp3d.check_setup",
                     text="Check Setup", icon='VIEWZOOM')

        # Compact report — same small-text treatment as the matte list.
        # Splits on the "  |  " separator the combined operator uses so each
        # half (render result, export result) sits on its own line and the
        # full text is always readable without clipping.
        if settings.render_done and settings.render_message:
            info = layout.column(align=True)
            info.scale_y = 0.65   # small, secondary text
            for part in settings.render_message.split("  |  "):
                info.label(text=part, icon='CHECKMARK')

        # Secondary actions on one line — Render (N) | Export GLB (N)
        row = layout.row(align=True)
        row.scale_y = 1.3
        row.operator("cp3d.render_all", text=f"Render ({en})", icon='RENDER_STILL')
        row.operator("cp3d.export_glb", text=f"Export GLB ({glb_n})", icon='EXPORT')

        # Live render progress — two bars + numerical counters.  Only shown
        # while render_in_progress is True (cleared by operator finally block).
        # Panel redraws between each render (bpy.ops.render.render blocks, but
        # Python runs between passes and updates these properties + tag_redraw).
        if settings.render_in_progress:
            prog_box = layout.box()
            prog_box.label(text="Rendering...", icon='RENDER_ANIMATION')

            # Collection-level progress
            sub = prog_box.box()
            sub.label(text=f"Collection:  {settings.progress_col_name}",
                      icon='OUTLINER_COLLECTION')
            col_total = max(settings.progress_col_total, 1)
            col_frac = settings.progress_col_idx / col_total
            _draw_progress_bar(
                sub, col_frac,
                f"Collections {settings.progress_col_idx}/{settings.progress_col_total}"
            )

            # Pass-level progress within the current collection
            sub = prog_box.box()
            sub.label(text=f"Pass:  {settings.progress_pass_name}",
                      icon='RENDER_STILL')
            pass_total = max(settings.progress_pass_total, 1)
            pass_frac = settings.progress_pass_idx / pass_total
            _draw_progress_bar(
                sub, pass_frac,
                f"Passes {settings.progress_pass_idx}/{settings.progress_pass_total}"
            )

            # ── Cancel button ─────────────────────────────────────────────────
            # Cooperative cancel: sets a flag that the render loop checks
            # between passes.  The current render call cannot be interrupted,
            # but no further passes will start.
            cancel_row = prog_box.row()
            cancel_row.scale_y = 1.3
            cancel_row.alert = True
            if settings.cancel_requested:
                cancel_row.enabled = False
                cancel_row.operator("cp3d.cancel_render",
                                    text="Cancelling…", icon='X')
            else:
                cancel_row.operator("cp3d.cancel_render",
                                    text="Cancel Render", icon='CANCEL')

        layout.separator()

        # ── Log window (rollable) ─────────────────────────────────────────────
        # Sits between the render buttons and the collection list.  The header
        # toggles log_show (roll out / collapse back); the trash icon clears it.
        # When open, a scrollable UIList shows the newest reports at the bottom.
        log_box = layout.box()
        hdr = log_box.row(align=True)
        hdr.prop(settings, "log_show", text="Log",
                 icon='TRIA_DOWN' if settings.log_show else 'TRIA_RIGHT',
                 emboss=False)
        n = len(settings.log_entries)
        hdr.label(text=f"({n})" if n else "")
        clr = hdr.row(align=True)
        clr.enabled = n > 0
        clr.operator("cp3d.clear_log", text="", icon='TRASH')
        if settings.log_show:
            if n:
                # Size the list to the actual number of entries so a 1-line
                # report doesn't leave a tall empty box — but cap it at 6 rows
                # so a long log stays scrollable rather than filling the panel.
                dyn_rows = min(max(n, 1), 6)
                log_box.template_list(
                    "CP3D_UL_log_list", "",
                    settings, "log_entries",
                    settings, "log_active_index",
                    rows=dyn_rows, maxrows=6,
                )
            else:
                empty = log_box.row()
                empty.enabled = False
                empty.label(text="No log entries yet", icon='INFO')

        layout.separator()
        # ── Collection list + add/remove buttons ──────────────────────────────
        row = layout.row()
        row.template_list(
            "CP3D_UL_collection_list", "",
            settings, "collections",
            settings, "active_index",
            rows=4,
        )
        col = row.column(align=True)
        col.operator("cp3d.add_active_collection", icon='ADD', text="")
        col.operator("cp3d.remove_collection", icon='REMOVE', text="")
        col.separator()
        col.operator("cp3d.clear_collections", icon='X', text="")
        col.separator()
        col.operator("cp3d.move_collection_up", icon='TRIA_UP', text="")
        col.operator("cp3d.move_collection_down", icon='TRIA_DOWN', text="")
        col.separator()
        # Show: solo-preview toggle (blue when on) — shows all collections except
        # the active collection's placeholder + matte sub-collections.
        col.prop(settings, "show_isolate", text="", icon='HIDE_OFF', toggle=True)

        # ── Per-collection settings (only shown when a valid item is active) ──
        if 0 <= settings.active_index < len(settings.collections):
            item = settings.collections[settings.active_index]
            box = layout.box()
            # Header row: "Collection Settings" (left) + camera (right) on one line
            hdr = box.row()
            hdr.label(text="Collection Settings", icon='SETTINGS')
            if item.collection:
                cam = find_camera_in_collection(item.collection)
                hdr.label(text=(f"Camera: {cam.name}" if cam else "No camera!"),
                          icon=('CAMERA_DATA' if cam else 'ERROR'))
                box.prop(item, "custom_name", text="Output Name")
                box.separator()
                # Per-collection Compositor selector (used by Render / Render ISO,
                # and routed-through by the Highlight / Shadow re-saves).
                box.prop(item, "compositor_node_tree", text="Compositor")
                # Missing compositor is a hard requirement for the Render and
                # Render ISO passes.
                if item.compositor_node_tree is None:
                    err_row = box.row()
                    err_row.alert = True
                    err_row.label(
                        text="No Compositor NodeTree assigned!",
                        icon='ERROR',
                    )
                # Per-collection World selector — overrides the scene World for
                # the Render / Render ISO passes.  Empty = use the scene World.
                box.prop(item, "world", text="World")

                # Live Update (blue when on) — applies this collection's World +
                # Compositor to the scene so the viewport previews them; restores
                # the scene's originals when switched off.
                box.prop(settings, "live_update", text="Live Update",
                         icon='SHADING_RENDERED', toggle=True)

                # ── Render Outputs (own boxed area, separate from settings) ───
                obox = layout.box()
                obox.label(text="Render Outputs", icon='OUTPUT')
                row = obox.row(align=True)
                row.prop(item, "render_raw", toggle=True, text="Render")
                row.prop(item, "render_crop", toggle=True)

                # ── Convert passes (post-process _render.png) ─────────────────
                obox.separator()
                obox.label(text="Convert (from _render.png):", icon='IMAGE_DATA')

                # Batch-render checkboxes
                row = obox.row(align=True)
                row.prop(item, "convert_highlight", toggle=True)
                row.prop(item, "convert_shadow",    toggle=True)

                # "Convert" buttons — both on one line, only active when
                # _render.png exists.
                has_render = (not settings.render_in_progress
                              and find_render_image_path(item) is not None)
                row = obox.row(align=True)
                row.enabled = has_render
                row.operator("cp3d.convert_highlight",
                             text="Convert Highlight", icon='IMAGE_DATA')
                row.operator("cp3d.convert_shadow",
                             text="Convert Shadow", icon='IMAGE_DATA')
                if not has_render and not settings.render_in_progress:
                    hint = obox.row()
                    hint.label(text="No _render.png found — render first",
                               icon='INFO')


# ── Sub-panels (ordered by bl_order) ─────────────────────────────────────────
# Top-level panels in the CP3D tab order by bl_order:
#   0  = Manual (How to use)      ← above the main panel / Render button
#   1  = Print3Dexporter main panel
# Sub-panels under the main panel order by their own bl_order:
#   20 = Convert Node Trees
#   30 = Placeholder Objects


# ── Manual content ────────────────────────────────────────────────────────────
# The built-in "How to use" text, stored as plain data so it is trivial to edit.
# Each top-level entry is (section_title, [lines]).  A line beginning with a
# bullet "•" is drawn slightly indented; an empty string "" inserts a small gap.
# Blender UI labels do not word-wrap, so each line is kept short by hand.
_MANUAL_SECTIONS = [
    ("What it does", [
        "Batch-renders Blender collections into",
        "layered PNGs (beauty, crop silhouettes,",
        "highlight / shadow) and exports a GLB",
        "per placeholder for the Print 3D pipeline.",
    ]),
    ("Quick start", [
        "1. Save the .blend first — outputs go to",
        "   an  R/  folder next to it.",
        "2. Pick collection(s) in the Outliner,",
        "   click  +  to add them to the list.",
        "3. Select a row, set Output Name,",
        "   Compositor, World and tick the",
        "   Render Outputs you want.",
        "4. For crops: open Placeholder Objects,",
        "   Import Placeholders, assign slots",
        "   A–J and set Width / Height.",
        "5. Click  Check Setup  to validate all",
        "   listed collections (green = OK).",
        "6. Click  Render + GLB  (does both),",
        "   or use the two buttons below it",
        "   separately.",
    ]),
    ("Collection list", [
        "•  +  /  -   add / remove rows",
        "•  X   clear the whole list",
        "•  up / down   reorder",
        "•  Show (eye, blue=on) — solo the",
        "   active (highlighted) collection,",
        "   minus its placeholders & mattes,",
        "   and look through its camera.",
        "•  left toggle = include in render",
        "•  right checkbox = select for",
        "   bulk-edit and GLB export",
        "•  green name = just rendered / exported",
    ]),
    ("Log window", [
        "Between the buttons and the list.",
        "•  Click the 'Log' header to roll it",
        "   out / collapse it back.",
        "•  Shows checks, errors, render and",
        "   export reports (newest at bottom).",
        "•  Trash icon clears it.",
    ]),
    ("Collection Settings", [
        "Shown for the highlighted row:",
        "•  Camera — auto-found in collection",
        "•  Output Name — file-name stem",
        "•  Compositor — node tree for Render",
        "•  World — overrides scene World",
        "•  Live Update (blue=on) — preview this",
        "   collection's World + Compositor in",
        "   the viewport (restored when off).",
        "•  Render Outputs: Render / Crop",
        "•  Convert: → Highlight / → Shadow",
        "   (post-process _render.png on disk)",
        "Note: Render = setup / object / bg",
        "only — placeholders & mattes hidden.",
    ]),
    ("Convert Node Trees", [
        "•  Setup Compositor — load the",
        "   Convert_Highlight / _Shadow groups",
        "   from Convert.blend.",
        "•  Reset Highlight / Shadow — rebuild",
        "   a group back to defaults.",
        "•  Conversion Intensity sliders —",
        "   set Highlight / Shadow strength",
        "   (0.5 default). Shared by all",
        "   collections; applied on Convert.",
    ]),
    ("Placeholder Objects", [
        "•  Import Placeholders — pull in meshes",
        "   named 'placeholder*' as crop slots.",
        "   (modifiers are kept, not applied).",
        "   Runs for all checkmarked collections.",
        "•  Reload Placeholders — re-read the",
        "   existing placeholders sub-collection",
        "   and resync slots (imports nothing).",
        "•  Import Mattes — pull in 'matte*'",
        "   meshes into the Matte sub-collection.",
        "•  Per slot: pick by selection or by",
        "   viewport eyedropper, set Width /",
        "   Height,  + / -  to add / remove.",
        "•  Matte (per slot): Add Matte to link",
        "   one or more mattes — each becomes a",
        "   holdout that masks out the placeholder",
        "   on its Crop pass; other mattes hidden.",
        "•  Render hides placeholders & mattes",
        "   in the viewport when it finishes.",
    ]),
    ("Outputs", [
        "Renders go to  R/<N>/  where <N> is the",
        "trailing number in the .blend name",
        "(scene_01.blend → R/01/), else R/.",
        "•  <name>_render.png   beauty",
        "•  <name>_crop_a..j.png   silhouettes",
        "•  <name>_highlight.png / _shadow.png",
        "•  <name>_IndexMA.exr   material index",
        "•  GLB: <project>/<base>/GLB/",
        "   <name>_<letter>.glb  (+ size CSV)",
    ]),
    ("Tips", [
        "•  Each collection needs a camera.",
        "•  Render needs a Compositor assigned.",
        "•  Convert needs _render.png to exist",
        "   first — render before converting.",
        "•  Tick several checkboxes, then change",
        "   one toggle to apply it to them all.",
    ]),
]


class CP3D_PT_manual(Panel):
    """Top-level collapsible manual — 'How to use' reference for the add-on.

    A TOP-LEVEL panel (no bl_parent_id) ordered ABOVE the main panel via
    bl_order 0, so it sits above the Render + Export GLB button.  Collapsed by
    default (DEFAULT_CLOSED): click the header to roll the manual out, click
    again to collapse it back — the closest native equivalent of a dockable
    side panel that Blender's Python API allows (add-on panels can only live in
    the 3D view's right-hand N-panel, not the reserved left toolbar region).
    """
    bl_label = "Manual — How to use"
    bl_idname = "CP3D_PT_manual"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'CP3D'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 0   # top-level: above the Print3Dexporter main panel (bl_order 1)

    def draw(self, context):
        layout = self.layout
        for title, lines in _MANUAL_SECTIONS:
            box = layout.box()
            box.label(text=title, icon='HELP')
            col = box.column(align=True)
            col.scale_y = 0.7   # compact, secondary text — fits more on screen
            for line in lines:
                if line == "":
                    col.separator(factor=0.5)
                else:
                    col.label(text=line)


class CP3D_PT_convert_trees(Panel):
    """Collapsible panel: Setup Compositor + Reset Highlight / Shadow node trees.

    Manages the Convert_Highlight / Convert_Shadow compositor node groups loaded
    from Convert.blend (used by the Convert passes).  These are scene-global
    (not per-collection), so the panel needs no active item.
    """
    bl_label = "Convert Node Trees"
    bl_idname = "CP3D_PT_convert_trees"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'CP3D'
    bl_parent_id = "CP3D_PT_main_panel"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 20   # appears above Placeholder Objects

    def draw(self, context):
        layout = self.layout
        settings = context.scene.collection_render_settings
        layout.operator("cp3d.setup_convert_trees",
                        text="Setup Compositor", icon='ADD')
        row = layout.row(align=True)
        hl_ok = bpy.data.node_groups.get(HIGHLIGHT_TREE_NAME) is not None
        sh_ok = bpy.data.node_groups.get(SHADOW_TREE_NAME)    is not None
        btn_hl = row.row(align=True)
        btn_hl.alert = not hl_ok
        btn_hl.operator("cp3d.reset_highlight",
                        text="Reset Highlight", icon='FILE_REFRESH')
        btn_sh = row.row(align=True)
        btn_sh.alert = not sh_ok
        btn_sh.operator("cp3d.reset_shadow",
                        text="Reset Shadow", icon='FILE_REFRESH')

        # ── Conversion intensity sliders ──────────────────────────────────────
        # Drive the 'Convert_highlight_Value' / 'Convert_Shadow_Value' nodes
        # inside the appended Convert groups (default 0.5).  Scene-global — one
        # value per group, shared by every collection's Convert pass.  Greyed
        # out for a group that isn't loaded yet (its slider would have no effect).
        box = layout.box()
        box.label(text="Conversion Intensity", icon='IMAGE_DATA')
        # Both sliders on one line — Highlight on the left, Shadow on the right.
        # Two columns inside one row so each keeps its own enabled state.
        row = box.row(align=True)
        hl = row.column(align=True)
        hl.enabled = hl_ok
        hl.prop(settings, "convert_highlight_value", text="Highlight", slider=True)
        sh = row.column(align=True)
        sh.enabled = sh_ok
        sh.prop(settings, "convert_shadow_value", text="Shadow", slider=True)


class CP3D_PT_crop_settings(Panel):
    """Placeholder object slots and their Cryptomatte pickers.

    Each 'slot' corresponds to one placeholder object whose Cryptomatte mask
    will be used to generate a crop PNG.  Up to ten slots (A–J) can be active.
    """
    bl_label = "Placeholder Objects"
    bl_idname = "CP3D_PT_crop_settings"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'CP3D'
    bl_parent_id = "CP3D_PT_main_panel"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 30   # appears third under the main panel

    def draw(self, ctx):
        layout = self.layout
        s = ctx.scene.collection_render_settings
        if not (0 <= s.active_index < len(s.collections)):
            layout.label(text="No collection selected", icon='ERROR')
            return
        item = s.collections[s.active_index]

        # ── Import Placeholders + Assign Mat toggle ─────────────────────────
        # Scans linked sub-collections for objects named 'placeholder*',
        # appends them locally and assigns them to the crop slots.
        # The 'Assign Mat' toggle controls whether Placeholder_mat is applied
        # to the imported objects during the import step.
        row = layout.row(align=True)
        row.operator("cp3d.import_placeholders", icon='IMPORT')
        row.prop(item, "assign_placeholder_mat", toggle=True, text="Assign Mat")

        # Reload Placeholders — re-reads the existing 'placeholders' sub-collection
        # and re-assigns crop slots (imports nothing).  Use after hand-editing.
        layout.operator("cp3d.reload_placeholders", icon='FILE_REFRESH')

        # Import Mattes — parallel workflow to placeholders.  Scans linked
        # sub-collections for meshes with 'matte' in their name and drops
        # local copies into a 'Matte' sub-collection.  No slot assignment.
        # A matte only acts as a holdout when LINKED to a placeholder slot via
        # the per-slot 'Matte' dropdown below (see operators.py crop pass).
        row = layout.row(align=True)
        row.operator("cp3d.import_mattes", icon='MATERIAL')

        # Small descriptive list of the matte objects currently imported into
        # this collection's 'Matte' sub-collection.  Reads the live contents so
        # it always reflects reality (hand-deleted mattes drop off the list).
        matte_col = find_matte_collection(item.collection)
        if matte_col is not None:
            matte_objs = [o for o in matte_col.all_objects if o.type == 'MESH']
            if matte_objs:
                info = layout.column(align=True)
                info.scale_y = 0.65   # compact, secondary text
                info.label(text=f"Mattes ({len(matte_objs)}) — link to a slot below:")
                for o in matte_objs:
                    info.label(text=f"  •  {o.name}")

        # Map slot letter to the property name that stores the object name
        slot_defs = [
            ('A', 'crop_a_name'), ('B', 'crop_b_name'), ('C', 'crop_c_name'),
            ('D', 'crop_d_name'), ('E', 'crop_e_name'), ('F', 'crop_f_name'),
            ('G', 'crop_g_name'), ('H', 'crop_h_name'), ('I', 'crop_i_name'),
            ('J', 'crop_j_name'),
        ]
        box = layout.box()
        # Only draw rows up to crop_slot_count — hidden slots are not rendered
        for i in range(item.crop_slot_count):
            sl, np = slot_defs[i]
            sl_l = sl.lower()
            # Placeholder object name + pickers (selection / eyedropper) + Select
            row = box.row(align=True)
            row.prop(item, np, text=sl)
            op = row.operator("cp3d.pick_cryptomatte_object", text="",
                              icon='RESTRICT_SELECT_OFF')
            op.slot = sl   # tell the operator which slot to write into
            op = row.operator("cp3d.pick_cryptomatte_viewport", text="",
                              icon='EYEDROPPER')
            op.slot = sl
            # Select this slot's placeholder object in the viewport / Outliner.
            # Text label (not another icon) so it reads distinctly from the two
            # icon pickers to its left.
            op = row.operator("cp3d.select_placeholder", text="Select",
                              icon='RESTRICT_SELECT_OFF')
            op.slot = sl
            # Delete this slot's placeholder object from the scene (undoable).
            op = row.operator("cp3d.delete_placeholder", text="", icon='TRASH')
            op.slot = sl

            # ── Linked matte object(s) — shown ABOVE Width / Height ───────────
            # A slot can link several mattes; each becomes a holdout that masks
            # out the placeholder on this slot's Crop render (all other mattes
            # hidden).  Each row searches the 'Matte' sub-collection.
            mcol = box.column(align=True)
            if matte_col is not None:
                for li, link in enumerate(item.matte_links):
                    if link.slot != sl:
                        continue
                    lr = mcol.row(align=True)
                    lr.prop_search(link, "name", matte_col, "all_objects",
                                   text="Matte", icon='MOD_MASK')
                    rm = lr.operator("cp3d.remove_matte_link", text="", icon='X')
                    rm.index = li
                add = mcol.operator("cp3d.add_matte_link",
                                    text="Add Matte", icon='ADD')
                add.slot = sl
            else:
                dis = mcol.row()
                dis.enabled = False
                dis.label(text="Matte: import mattes first", icon='MOD_MASK')

            # Width / Height for this placeholder — written to the GLB-folder CSV
            wh = box.row(align=True)
            wh.prop(item, f"crop_{sl_l}_width",  text="Width")
            wh.prop(item, f"crop_{sl_l}_height", text="Height")

            # Dimension tools: Optimize | Swap | Copy | Paste.  Each carries the
            # slot letter; Copy/Paste use a scene-level clipboard so values move
            # between collections.  Paste is greyed until a Copy has happened.
            tools = box.row(align=True)
            tools.operator("cp3d.optimize_dims", text="Optimize",
                           icon='FULLSCREEN_EXIT').slot = sl
            tools.operator("cp3d.swap_dims", text="Swap",
                           icon='ARROW_LEFTRIGHT').slot = sl
            tools.operator("cp3d.copy_dims", text="Copy",
                           icon='COPYDOWN').slot = sl
            tools.operator("cp3d.paste_dims", text="Paste",
                           icon='PASTEDOWN').slot = sl

            # Dotted divider between placeholder slots (not after the last one)
            if i < item.crop_slot_count - 1:
                _draw_dotted_divider(box)

        # + / - buttons to add or remove slots (clamped to 1–10 by operator poll).
        # The + button is greyed at 10 slots; the - button is greyed at 1 slot.
        row = box.row(align=True)
        row.operator("cp3d.add_crop_slot", icon='ADD', text="")
        row.operator("cp3d.remove_crop_slot", icon='REMOVE', text="")


