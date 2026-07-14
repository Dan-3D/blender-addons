"""Print3Dexporter — shared constants.

Single source of truth for values that are referenced from multiple modules.
Everything that might need to change in one place (version number, Blender
compatibility gate, named data-block identifiers) lives here.

Contents:
  BLENDER_5        — True when running on Blender 5+; gates API differences
  VERSION          — add-on version tuple (kept in sync with bl_info)
  VERSION_STRING   — human-readable version string e.g. "1.0.3"
"""

import bpy

# ── Blender version compatibility flag ───────────────────────────────────────
# Blender 5 changed the compositor API significantly:
#   - scene.node_tree was removed; compositing now lives in
#     scene.compositing_node_group (a separate NodeGroup asset).
#   - The output node is NodeGroupOutput instead of CompositorNodeComposite.
#   - CompositorNodeOutputFile is unreliable inside compositing_node_group,
#     so the highlight pass uses a second render call instead (see utils.py).
# Every place that needs to branch on B4 vs B5 behaviour imports this flag.
BLENDER_5 = bpy.app.version >= (5, 0, 0)
print(f"[CP3D] Blender {bpy.app.version}, B5={BLENDER_5}")

# ── Add-on version ────────────────────────────────────────────────────────────
# Keep in sync with bl_info in __init__.py.
VERSION = (1, 0, 71)
VERSION_STRING = ".".join(str(v) for v in VERSION)
