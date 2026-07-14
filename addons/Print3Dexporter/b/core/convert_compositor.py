"""Convert_Highlight and Convert_Shadow compositor node groups.

Loaded from Convert.blend (stored at core/Convert.blend alongside this file).
The .blend file contains two compositor NodeGroup assets:
  Convert_Highlight  — desaturate + screen-blend curve adjustment → _highlight
  Convert_Shadow     — desaturate + shadow-lift curve adjustment  → _shadow

Both expose:
  Input  socket: "Image" (NodeSocketColor)
  Output socket: "Image" (NodeSocketColor)

The Reset helpers remove the existing tree from the scene and reload it fresh
from the .blend file, so any accidental user edits can be undone instantly.
"""

import os
import bpy

HIGHLIGHT_TREE_NAME = "Convert_Highlight"
SHADOW_TREE_NAME    = "Convert_Shadow"

# ── Intensity Value nodes inside the Convert groups ───────────────────────────
# Each Convert group contains a single Value node (default 0.5) that drives how
# strong the conversion is.  The UI sliders write into these via set_convert_value.
# Names as authored in Convert.blend (note the lower-case 'h' in highlight).
HIGHLIGHT_VALUE_NODE = "Convert_highlight_Value"
SHADOW_VALUE_NODE    = "Convert_Shadow_Value"

# Default the Value nodes ship with — also the slider default in the UI.
CONVERT_VALUE_DEFAULT = 0.5

_BLEND_PATH = os.path.join(os.path.dirname(__file__), "Convert.blend")


def _find_existing(name):
    """Return the node group whose name matches *name* (case-insensitive)."""
    lname = name.lower()
    for ng in bpy.data.node_groups:
        if ng.name.lower() == lname:
            return ng
    return None


def _load_tree_from_blend(name):
    """Append a fresh copy of *name* NodeGroup from Convert.blend.

    Tries an exact match first, then a case-insensitive match so minor
    capitalisation differences in the .blend asset don't break the load.
    Returns the loaded NodeGroup, or None on any failure (details printed).
    """
    if not os.path.isfile(_BLEND_PATH):
        print(f"[CP3D] Convert.blend not found at: {_BLEND_PATH}")
        return None
    try:
        with bpy.data.libraries.load(_BLEND_PATH, link=False) as (data_from, data_to):
            available = list(data_from.node_groups)
            # Exact match, then case-insensitive fallback
            actual = name if name in available else next(
                (n for n in available if n.lower() == name.lower()), None
            )
            if actual is None:
                print(
                    f"[CP3D] NodeGroup '{name}' not found in Convert.blend.\n"
                    f"       Available node groups: {available}"
                )
                return None
            data_to.node_groups = [actual]
        loaded = data_to.node_groups[0] if data_to.node_groups else None
        if loaded is None:
            print(f"[CP3D] bpy.data.libraries.load returned None for '{name}'")
            return None
        # Normalise to the canonical name (strips Blender's .001 suffix and
        # fixes any capitalisation difference from the case-insensitive match).
        # If a different node group is already squatting on the canonical name,
        # remove it first so Blender doesn't auto-suffix the rename to ".001".
        if loaded.name != name:
            collision = bpy.data.node_groups.get(name)
            if collision is not None and collision != loaded:
                bpy.data.node_groups.remove(collision)
            loaded.name = name
        return loaded
    except Exception as e:
        print(f"[CP3D] Error loading '{name}' from Convert.blend: {e}")
        return None


# ── Intensity Value-node access ───────────────────────────────────────────────

def _find_value_node(tree, expected_name):
    """Return the intensity Value node inside *tree*.

    Tries an exact name match, then a case-insensitive match, then falls back to
    the first CompositorNodeValue in the group.  Returns None if *tree* is None
    or holds no Value node.
    """
    if tree is None:
        return None
    node = tree.nodes.get(expected_name)
    if node is not None:
        return node
    lname = expected_name.lower()
    for n in tree.nodes:
        if n.name.lower() == lname:
            return n
    for n in tree.nodes:               # last resort: any Value node in the group
        if n.bl_idname == 'CompositorNodeValue':
            return n
    return None


def _value_node_for(mode):
    """Return the Value node for 'highlight' or 'shadow', or None if unavailable."""
    if mode == 'highlight':
        tree = bpy.data.node_groups.get(HIGHLIGHT_TREE_NAME)
        return _find_value_node(tree, HIGHLIGHT_VALUE_NODE)
    tree = bpy.data.node_groups.get(SHADOW_TREE_NAME)
    return _find_value_node(tree, SHADOW_VALUE_NODE)


def get_convert_value(mode):
    """Return the current intensity of the highlight/shadow group, or None."""
    node = _value_node_for(mode)
    if node is None or not node.outputs:
        return None
    return node.outputs[0].default_value


def set_convert_value(mode, value):
    """Write *value* into the highlight/shadow group's intensity Value node.

    Returns True on success, False if the group / Value node isn't present
    (e.g. the Convert trees haven't been set up yet).
    """
    node = _value_node_for(mode)
    if node is None or not node.outputs:
        return False
    node.outputs[0].default_value = float(value)
    return True


# ── public API ────────────────────────────────────────────────────────────────

def ensure_convert_node_trees():
    """Load Convert_Highlight and Convert_Shadow from Convert.blend if absent.

    Safe to call multiple times — existing trees are left untouched.  Uses
    case-insensitive name matching so a "Convert_shadow" already in the scene
    is recognised and (just) renamed to "Convert_Shadow" instead of triggering
    a second append.
    """
    for name in (HIGHLIGHT_TREE_NAME, SHADOW_TREE_NAME):
        existing = _find_existing(name)
        if existing is not None:
            # Already present (possibly with different casing) — normalise name
            if existing.name != name:
                existing.name = name
            continue
        _load_tree_from_blend(name)


def reset_highlight_node_tree():
    """Remove Convert_Highlight from the scene and reload it from Convert.blend."""
    existing = _find_existing(HIGHLIGHT_TREE_NAME)
    if existing is not None:
        bpy.data.node_groups.remove(existing)
    _load_tree_from_blend(HIGHLIGHT_TREE_NAME)


def reset_shadow_node_tree():
    """Remove Convert_Shadow from the scene and reload it from Convert.blend."""
    existing = _find_existing(SHADOW_TREE_NAME)
    if existing is not None:
        bpy.data.node_groups.remove(existing)
    _load_tree_from_blend(SHADOW_TREE_NAME)
