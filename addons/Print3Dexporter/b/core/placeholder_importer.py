"""Placeholder + matte object importers for Print3Dexporter.

This module exposes two parallel importers that share the same walk /
local-copy machinery:

  ``import_placeholders(context, item)``
      Finds meshes with ``placeholder`` in their name, copies them into a
      ``placeholders`` sub-collection of the selected collection, and
      assigns the first three to crop slots A/B/C.  Optionally assigns
      ``Placeholder_mat``.

  ``import_mattes(context, item)``
      Finds meshes with ``matte`` in their name, copies them into a
      ``Matte`` sub-collection of the selected collection.  No slot or
      material assignment — pure import + organise.

Both importers:
  1. Walk the selected collection tree recursively (including library-
     linked sub-collections AND collection-instance Empties — each Empty
     is followed with its own accumulated world transform so duplicates
     are correctly placed).
  2. Create local copies of every match with the world transform baked in.
  3. Find or create the destination sub-collection — Blender's
     auto-numbering suffixes (``placeholders.001``, ``Matte.001``, …) are
     recognised so repeated imports reuse the existing one.
  4. Move the local copies into the destination, cleaning up a temporary
     staging collection at the end.

Called from ``CP3D_OT_import_placeholders`` / ``CP3D_OT_import_mattes``
in ``operators.py``.  Both raise ``RuntimeError`` with a human-readable
message on failure.
"""

import re
import bpy
from mathutils import Matrix

from .utils import assign_placeholder_mat, find_matte_collection, _is_placeholders_col


# ── helpers ───────────────────────────────────────────────────────────────────

def _extract_end_number(name):
    """Return the last numeric substring in *name*, or '0' if none found.

    Examples:
        'Scene_03'       → '03'
        'MyCol_12_final' → '12'
        'NoNumber'       → '0'
    """
    nums = re.findall(r'\d+', name)
    return nums[-1] if nums else '0'


def _is_placeholder(obj):
    """Return True if *obj* is a mesh whose name (or mesh-data name) contains
    'placeholder' (case-insensitive)."""
    if obj.type != 'MESH':
        return False
    if 'placeholder' in obj.name.lower():
        return True
    if obj.data and 'placeholder' in obj.data.name.lower():
        return True
    return False


def _find_placeholder_objects(col, found, parent_matrix=None,
                              visited_children=None, _depth=0):
    """Recursively search *col* for placeholder mesh objects.

    Each entry appended to *found* is a tuple ``(obj, world_matrix)`` where
    *world_matrix* is the object's true scene-space transform — including the
    accumulated transforms of any collection-instance Empties on the path.

    Handles:
      - Direct objects in the collection
      - Objects in child sub-collections (including library-linked)
      - Collection instances (Empty with instance_type='COLLECTION')
      - Multiple Empties referencing the same linked collection (each gets
        its own parent_matrix so placeholders from every instance are found)

    *visited_children* guards against circular parent→child references only.
    Collection instances are NOT guarded — each Empty is a unique entry point.
    *_depth* is a safety limit to prevent runaway recursion (max 20 levels).
    """
    if _depth > 20:
        return
    if visited_children is None:
        visited_children = set()
    if parent_matrix is None:
        parent_matrix = Matrix.Identity(4)

    # Check direct objects in this collection
    for obj in col.objects:
        if _is_placeholder(obj):
            world_mtx = parent_matrix @ obj.matrix_world
            found.append((obj, world_mtx))

        # Collection instance — enter the instanced collection.
        # NOT guarded by visited_children so the same linked collection
        # can be entered multiple times (once per Empty, each with its
        # own accumulated transform).
        if (obj.type == 'EMPTY'
                and obj.instance_type == 'COLLECTION'
                and obj.instance_collection is not None):
            instance_mtx = parent_matrix @ obj.matrix_world
            offset = obj.instance_collection.instance_offset
            offset_mtx = Matrix.Translation(-offset)
            combined = instance_mtx @ offset_mtx
            _find_placeholder_objects(
                obj.instance_collection, found, combined,
                visited_children, _depth + 1
            )

    # Recurse into child sub-collections (guarded to prevent circular refs)
    for child in col.children:
        child_id = id(child)
        if child_id in visited_children:
            continue
        visited_children.add(child_id)
        _find_placeholder_objects(
            child, found, parent_matrix, visited_children, _depth + 1
        )


def _make_local_copy(obj, world_matrix, temp_col):
    """Create a local copy of *obj* and place it at *world_matrix*.

    The copy is linked into *temp_col* as a staging area.  Modifiers on the
    original are PRESERVED on the copy (obj.copy() carries them over) — they are
    neither deleted nor applied, so the imported placeholder / matte keeps its
    modifier stack intact.
    Returns the new local object.
    """
    new_obj = obj.copy()
    if obj.data is not None:
        new_obj.data = obj.data.copy()
    # Apply the accumulated world-space transform so the copy sits at the
    # exact same position/rotation/scale as the original appears in the scene
    new_obj.matrix_world = world_matrix
    temp_col.objects.link(new_obj)
    return new_obj


def _remove_collection_recursive(col):
    """Delete *col*, all its child collections, and every object inside."""
    for child in list(col.children):
        _remove_collection_recursive(child)
    for obj in list(col.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(col)


# ── main import function ──────────────────────────────────────────────────────

def import_placeholders(context, item):
    """Import placeholder objects from the selected collection into crop slots.

    Parameters
    ----------
    context : bpy.types.Context
    item    : CollectionRenderItem — the active entry from the CP3D list.

    Returns
    -------
    int — number of placeholders imported and assigned to crop slots.
    """
    scene = context.scene
    col = item.collection

    if not col:
        raise RuntimeError("No collection assigned to this item")

    # ── 1. Search the ORIGINAL collection tree for placeholder objects ────────
    # Each entry is (obj, world_matrix) — world_matrix includes the
    # accumulated transforms of any collection-instance Empties on the path.
    found_originals = []
    _find_placeholder_objects(col, found_originals)

    if not found_originals:
        raise RuntimeError(
            f"No objects with 'placeholder' in their name found "
            f"in collection '{col.name}' (searched sub-collections "
            f"and collection instances)"
        )

    # ── 2. Create a temporary staging collection for local copies ─────────────
    temp_name = "Temporary_imported"
    temp_col = bpy.data.collections.new(temp_name)
    scene.collection.children.link(temp_col)

    imported_count = 0

    try:
        # ── 3. Create local copies — always copy so the transform is baked ────
        # Even local objects get a copy so we can safely set matrix_world
        # to the accumulated instance transform without modifying the original.
        local_placeholders = []
        for orig_obj, world_mtx in found_originals:
            local_obj = _make_local_copy(orig_obj, world_mtx, temp_col)
            local_placeholders.append(local_obj)

        # ── 4. Create destination sub-collection in the original collection ───
        # Path: <original_col> > placeholders
        dest_name = "placeholders"

        # Find or create the 'placeholders' sub-collection directly under the
        # selected collection (no intermediate 'objects' level).
        # Handles Blender auto-numbering: if a previous import created
        # "placeholders.001", we reuse it instead of creating yet another one.
        placeholders_col = None
        for child in col.children:
            lower = child.name.lower()
            if (lower == dest_name
                    or (lower.startswith(dest_name + '.') and lower[len(dest_name)+1:].isdigit())):
                placeholders_col = child
                break
        if placeholders_col is None:
            placeholders_col = bpy.data.collections.new(dest_name)
            col.children.link(placeholders_col)

        # ── 5. Assign placeholders to crop slots by parsing their names ───────
        # 'placeholder_1' → slot 1 (A), 'placeholder_2' → slot 2 (B), etc.
        # Objects without a number suffix get the next available slot.
        # Supports up to 10 slots (A–J).
        _MAX_SLOTS = 10
        _SLOT_LETTERS = 'abcdefghij'

        slot_map = {}     # {1: obj, ..., 10: obj}
        unassigned = []

        for obj in local_placeholders:
            match = re.search(
                r'placeholder[_\s-]*(\d+)', obj.name, re.IGNORECASE
            )
            if match:
                num = int(match.group(1))
                if 1 <= num <= _MAX_SLOTS and num not in slot_map:
                    slot_map[num] = obj
                else:
                    unassigned.append(obj)
            else:
                unassigned.append(obj)

        # Fill remaining slots with unassigned placeholders (in order)
        for num in range(1, _MAX_SLOTS + 1):
            if num not in slot_map and unassigned:
                slot_map[num] = unassigned.pop(0)

        # ── 6. Move assigned placeholders into the destination collection ─────
        for num, obj in sorted(slot_map.items()):
            # Unlink from all current collections (temp staging + any original)
            for c in list(obj.users_collection):
                try:
                    c.objects.unlink(obj)
                except RuntimeError:
                    pass
            # Link into the destination sub-collection
            placeholders_col.objects.link(obj)

            # Optionally assign Placeholder_mat (if the toggle is on)
            if item.assign_placeholder_mat and obj.type == 'MESH':
                assign_placeholder_mat(obj)

            # Write the object name into the item's crop slot
            sl = _SLOT_LETTERS[num - 1]
            setattr(item, f"crop_{sl}_name", obj.name)
            setattr(item, f"crop_{sl}_enabled", True)

            imported_count += 1

        # Raise crop_slot_count to cover the highest filled slot
        if slot_map:
            max_slot = max(slot_map.keys())
            if max_slot > item.crop_slot_count:
                item.crop_slot_count = min(max_slot, _MAX_SLOTS)

    finally:
        # ── 7. Clean up: delete the temporary staging collection ──────────────
        # Any objects still inside (unassigned leftovers) are removed with it.
        try:
            _remove_collection_recursive(temp_col)
        except Exception:
            pass

    return imported_count


def _assign_objects_to_slots(item, objs):
    """Assign *objs* to crop slots A–J and return how many were assigned.

    Shared by import_placeholders (indirectly) and reload_placeholders.  Objects
    named ``placeholder_<n>`` claim slot <n>; the rest fill the lowest free
    slots in order.  Clears any slot names not covered so stale names drop off.
    """
    _MAX_SLOTS = 10
    _SLOT_LETTERS = 'abcdefghij'

    # Clear all slot names first so removed placeholders don't linger
    for sl in _SLOT_LETTERS:
        setattr(item, f"crop_{sl}_name", "")

    slot_map = {}
    unassigned = []
    for obj in objs:
        match = re.search(r'placeholder[_\s-]*(\d+)', obj.name, re.IGNORECASE)
        if match:
            num = int(match.group(1))
            if 1 <= num <= _MAX_SLOTS and num not in slot_map:
                slot_map[num] = obj
            else:
                unassigned.append(obj)
        else:
            unassigned.append(obj)
    for num in range(1, _MAX_SLOTS + 1):
        if num not in slot_map and unassigned:
            slot_map[num] = unassigned.pop(0)

    count = 0
    for num, obj in sorted(slot_map.items()):
        sl = _SLOT_LETTERS[num - 1]
        setattr(item, f"crop_{sl}_name", obj.name)
        setattr(item, f"crop_{sl}_enabled", True)
        count += 1
    if slot_map:
        item.crop_slot_count = min(max(slot_map.keys()), _MAX_SLOTS)
    return count


def reload_placeholders(context, item):
    """Re-assign crop slots from the placeholders already in the collection.

    Unlike :func:`import_placeholders`, this imports NOTHING — it just reads the
    meshes already present in the collection's existing ``placeholders``
    sub-collection and re-assigns them to crop slots A–J (parsing
    ``placeholder_<n>`` names).  Use it after hand-editing the placeholders
    sub-collection (added / removed / renamed objects) to resync the slots.

    Returns the number of placeholders assigned.  Raises RuntimeError if the
    collection has no ``placeholders`` sub-collection or it is empty.
    """
    col = item.collection
    if not col:
        raise RuntimeError("No collection assigned to this item")

    ph_col = None
    for child in col.children:
        if _is_placeholders_col(child.name):
            ph_col = child
            break
    if ph_col is None:
        raise RuntimeError(
            f"No 'placeholders' sub-collection in '{col.name}' — "
            "use Import Placeholders first"
        )

    objs = [o for o in ph_col.all_objects if o.type == 'MESH']
    if not objs:
        raise RuntimeError(
            f"'placeholders' sub-collection in '{col.name}' has no mesh objects"
        )

    count = _assign_objects_to_slots(item, objs)

    # Re-apply Placeholder_mat if the toggle is on (matches import behaviour)
    if item.assign_placeholder_mat:
        for obj in objs:
            if obj.type == 'MESH':
                assign_placeholder_mat(obj)

    return count


# ──────────────────────────────────────────────────────────────────────────────
# Matte importer — same workflow as placeholders, but for objects with
# ``matte`` in their name.  Destination sub-collection is named ``Matte``.
# Mattes are not assigned to crop slots and do not get a material auto-assigned;
# the import simply collects, transforms and re-parents them.
# ──────────────────────────────────────────────────────────────────────────────


def _is_matte(obj):
    """Return True if *obj* is a mesh whose name (or mesh-data name) contains
    'matte' (case-insensitive)."""
    if obj.type != 'MESH':
        return False
    if 'matte' in obj.name.lower():
        return True
    if obj.data and 'matte' in obj.data.name.lower():
        return True
    return False


def _find_matte_objects(col, found, parent_matrix=None,
                        visited_children=None, _depth=0):
    """Recursively search *col* for matte mesh objects.

    Identical structure to :func:`_find_placeholder_objects` — walks the
    collection tree, follows collection-instance Empties (multi-entry), and
    appends ``(obj, world_matrix)`` tuples for every match.  See that
    function's docstring for the full recursion semantics.
    """
    if _depth > 20:
        return
    if visited_children is None:
        visited_children = set()
    if parent_matrix is None:
        parent_matrix = Matrix.Identity(4)

    for obj in col.objects:
        if _is_matte(obj):
            world_mtx = parent_matrix @ obj.matrix_world
            found.append((obj, world_mtx))

        if (obj.type == 'EMPTY'
                and obj.instance_type == 'COLLECTION'
                and obj.instance_collection is not None):
            instance_mtx = parent_matrix @ obj.matrix_world
            offset = obj.instance_collection.instance_offset
            offset_mtx = Matrix.Translation(-offset)
            combined = instance_mtx @ offset_mtx
            _find_matte_objects(
                obj.instance_collection, found, combined,
                visited_children, _depth + 1
            )

    for child in col.children:
        child_id = id(child)
        if child_id in visited_children:
            continue
        visited_children.add(child_id)
        _find_matte_objects(
            child, found, parent_matrix, visited_children, _depth + 1
        )


def import_mattes(context, item):
    """Import matte objects from the selected collection into a Matte sub-col.

    Mirrors the placeholder import workflow:
      1. Recursively scan the selected collection tree (including library-
         linked sub-collections AND collection-instance Empties) for meshes
         whose name (or mesh-data name) contains ``matte`` (case-insensitive).
      2. Create local copies of every match, baking their accumulated
         world-space transforms so the copies sit exactly where the originals
         appear in the scene.
      3. Find (or create) a ``Matte`` sub-collection directly under the
         selected collection — auto-numbering variants (``Matte.001``, …) are
         recognised so repeated imports reuse the existing one.
      4. Move every local copy into the ``Matte`` sub-collection.

    Unlike placeholders there are no per-object slots and no material is
    auto-assigned — mattes are just collected and organised.

    Parameters
    ----------
    context : bpy.types.Context
    item    : CollectionRenderItem — the active entry from the CP3D list.

    Returns
    -------
    int — number of matte objects imported into the destination collection.
    """
    scene = context.scene
    col = item.collection

    if not col:
        raise RuntimeError("No collection assigned to this item")

    # ── 1. Search the ORIGINAL collection tree for matte objects ──────────────
    found_originals = []
    _find_matte_objects(col, found_originals)

    if not found_originals:
        raise RuntimeError(
            f"No objects with 'matte' in their name found "
            f"in collection '{col.name}' (searched sub-collections "
            f"and collection instances)"
        )

    # ── 2. Temporary staging collection for local copies ──────────────────────
    temp_name = "Temporary_imported_mattes"
    temp_col = bpy.data.collections.new(temp_name)
    scene.collection.children.link(temp_col)

    imported_count = 0

    try:
        # ── 3. Local copies with baked world transforms ───────────────────────
        local_mattes = []
        for orig_obj, world_mtx in found_originals:
            local_obj = _make_local_copy(orig_obj, world_mtx, temp_col)
            local_mattes.append(local_obj)

        # ── 4. Find or create the 'Matte' destination sub-collection ──────────
        dest_name = "Matte"
        matte_col = find_matte_collection(col)
        if matte_col is None:
            matte_col = bpy.data.collections.new(dest_name)
            col.children.link(matte_col)

        # ── 5. Move every matte into the destination sub-collection ───────────
        for obj in local_mattes:
            # Unlink from temp staging (and anywhere else it may have ended up)
            for c in list(obj.users_collection):
                try:
                    c.objects.unlink(obj)
                except RuntimeError:
                    pass
            matte_col.objects.link(obj)
            imported_count += 1

    finally:
        # ── 6. Clean up: delete the temporary staging collection ──────────────
        try:
            _remove_collection_recursive(temp_col)
        except Exception:
            pass

    return imported_count
