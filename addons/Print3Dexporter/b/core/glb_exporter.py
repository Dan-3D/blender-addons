"""GLB exporter for Print3Dexporter.

Export workflow
---------------
For each placeholder object found in the collection:
1.  Create a temporary collection "TEMP" inside the selected collection.
2.  Duplicate the collection's camera and the single placeholder into TEMP.
3.  Apply transforms on the placeholder mesh to bake world-space positions.
4.  Create an Empty called "lockdown" in TEMP and parent every duplicated
    object under it.
5.  Export all objects in TEMP as a GLB file named after the placeholder suffix.
6.  Delete TEMP and all its contents — the scene is exactly as before.
7.  Repeat for each remaining placeholder.

Output path
-----------
The .blend file is expected to live inside a "BL" (or similar) sub-folder of
the project root.  Each GLB is written one level *above* that folder, inside a
sub-directory named after the collection with any trailing ``_<N>`` index
suffix stripped, then under a ``GLB`` sub-folder:

  <project_root>/<collection_base_name>/GLB/<safe_name>_<letter>.glb

Example:
  blend file  : project/BL/scene.blend
  collection  : canva_print_3D_flags_ca_us_2026-05_16
  slot A      : placeholder_a
  GLB output  : project/canva_print_3D_flags_ca_us_2026-05/GLB/<name>_a.glb

A ``<collection_name>.csv`` is also written / updated in the GLB folder, with
one line per exported placeholder:  ``<name>_<letter> (<width>, <height>)``.

Returns a list of exported filepath strings.
Raises RuntimeError with a human-readable message on failure.
"""

import os
import re
import bpy

from .utils import find_camera_in_collection, get_objects_recursive


# ── helpers ───────────────────────────────────────────────────────────────────

def _strip_index_suffix(name):
    """Strip a trailing ``_<integer>`` index suffix from *name*.

    Used to derive the project sub-folder from a versioned collection name:
      'canva_print_3D_flags_ca_us_2026-05_16'  →  'canva_print_3D_flags_ca_us_2026-05'
      'my_collection'                           →  'my_collection'
    """
    return re.sub(r'_\d+$', '', name)

def _find_layer_collection(layer_col, collection):
    """Recursively find the LayerCollection wrapping *collection*."""
    if layer_col.collection == collection:
        return layer_col
    for child in layer_col.children:
        result = _find_layer_collection(child, collection)
        if result:
            return result
    return None


def _ensure_temp_col_visible(context, temp_col):
    """Make sure the temp collection is not excluded from the view layer."""
    lc = _find_layer_collection(context.view_layer.layer_collection, temp_col)
    if lc:
        lc.exclude = False
        lc.hide_viewport = False


def _is_placeholder(obj):
    """Return True if *obj* is a mesh whose object name or mesh-data name
    contains 'placeholder' (case-insensitive).  Consistent with the importer."""
    if obj.type != 'MESH':
        return False
    if 'placeholder' in obj.name.lower():
        return True
    if obj.data and 'placeholder' in obj.data.name.lower():
        return True
    return False


def _gather_placeholders(collection):
    """Return all placeholder mesh objects from *collection* and all its
    descendants.  Checks both object name and mesh-data name."""
    found = []
    for obj in get_objects_recursive(collection):
        if _is_placeholder(obj):
            found.append(obj)
    return found


def _extract_placeholder_suffix(obj):
    """Extract the letter suffix (a-j) from a placeholder object or data name.

    Matches the last ``_<letter>`` segment before any Blender auto-number
    suffix (.001, .002, …).  Returns the lowercase letter or None.

    Examples:
      'placeholder_a'      → 'a'
      'Placeholder_B.001'  → 'b'
      'my_placeholder_c'   → 'c'
    """
    for name in (obj.name, obj.data.name if obj.data else ''):
        m = re.search(r'_([a-j])(?:\.\d+)?$', name.lower())
        if m:
            return m.group(1)
    return None


def _duplicate_object(obj, temp_col):
    """Create a local duplicate of *obj* (handles both local and linked objects).
    The duplicate is linked into *temp_col* and returned.
    Hide flags are cleared so the duplicate is always visible and selectable."""
    dup = obj.copy()
    if obj.data:
        dup.data = obj.data.copy()
    # Clear hide flags so the duplicate is visible and selectable for export
    dup.hide_viewport = False
    dup.hide_render = False
    dup.hide_select = False
    temp_col.objects.link(dup)
    return dup


# ── export list + CSV helpers ───────────────────────────────────────────────

def _build_export_list(item, col):
    """Return a list of (letter, obj, width, height) tuples to export.

    Primary (slot-driven): iterate crop slots a–j; each enabled slot with an
    assigned, existing object is exported under its slot letter and carries that
    slot's Width / Height.  This guarantees the ``_a`` / ``_b`` … appendix.

    Fallback (no slots configured): gather every placeholder by name and use the
    letter parsed from its name, with Width / Height defaulting to 0.
    """
    export_list = []
    for letter in 'abcdefghij':
        if not getattr(item, f"crop_{letter}_enabled", False):
            continue
        obj_name = getattr(item, f"crop_{letter}_name", "")
        if not obj_name:
            continue
        obj = bpy.data.objects.get(obj_name)
        if obj is None:
            continue
        width  = getattr(item, f"crop_{letter}_width", 0.0)
        height = getattr(item, f"crop_{letter}_height", 0.0)
        export_list.append((letter, obj, width, height))
    if export_list:
        return export_list
    # Fallback: name-based gather
    for obj in _gather_placeholders(col):
        letter = _extract_placeholder_suffix(obj)
        if letter:
            export_list.append((letter, obj, 0.0, 0.0))
    return export_list


def count_export_glbs(item):
    """Return how many GLB files :func:`export_crop_glb` would write for *item*.

    Mirrors the export's own :func:`_build_export_list` logic so the panel can
    show the count on the Export GLB button (one GLB per exportable slot, or the
    name-based placeholder fallback).  Returns 0 for an item with no collection.
    """
    col = item.collection
    if col is None:
        return 0
    try:
        return len(_build_export_list(item, col))
    except Exception:
        return 0


def _update_placeholder_csv(csv_path, entries):
    """Merge *entries* into the CSV file at *csv_path*.

    Each CSV line has the form ``<identifier> (<width>, <height>)`` with integer
    width / height.  Existing rows are keyed by identifier: a matching identifier
    has its values replaced, a new identifier is appended, and any other rows are
    left untouched.  Returns the CSV path.

    *entries* is a list of (identifier:str, width, height).
    """
    rows = {}   # identifier -> (width_str, height_str); dict preserves order
    if os.path.exists(csv_path):
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    m = re.match(r'^(.+?)\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)\s*$', line)
                    if m:
                        rows[m.group(1)] = (m.group(2).strip(), m.group(3).strip())
        except Exception as e:
            print(f"[CP3D] Could not read existing CSV ({csv_path}): {e}")
    # Replace / add the rows for this export (values written as integers)
    for ident, width, height in entries:
        rows[ident] = (str(int(width)), str(int(height)))
    with open(csv_path, 'w', encoding='utf-8') as f:
        for ident, (w, h) in rows.items():
            f.write(f"{ident} ({w}, {h})\n")
    return csv_path


# ── main export function ──────────────────────────────────────────────────────

def export_crop_glb(context, item):
    """Run the full placeholder-based GLB export for *item*.

    Each placeholder is exported as a separate GLB named by its slot letter:
      slot A  →  <safe_name>_a.glb   (object inside also named <safe_name>_a)
      slot B  →  <safe_name>_b.glb
    A ``placeholders.csv`` listing every exported placeholder with its Width /
    Height is written / updated in the GLB folder.

    Parameters
    ----------
    context : bpy.types.Context
    item    : CollectionRenderItem

    Returns
    -------
    list[str] — absolute paths to all exported .glb files.
    """
    scene = context.scene
    col = item.collection

    if not col:
        raise RuntimeError("No collection assigned to this item")

    if not bpy.data.filepath:
        raise RuntimeError("Save the .blend file before exporting")

    # ── build output directory ────────────────────────────────────────────────
    blend_dir   = os.path.dirname(bpy.data.filepath)
    project_dir = os.path.dirname(blend_dir)
    name        = item.custom_name or col.name
    safe_name   = bpy.path.clean_name(name)
    folder_base = bpy.path.clean_name(_strip_index_suffix(col.name))
    out_dir     = os.path.join(project_dir, folder_base, "GLB")
    os.makedirs(out_dir, exist_ok=True)

    # ── find source objects (before we touch anything) ────────────────────────
    cam_obj = find_camera_in_collection(col)
    export_list = _build_export_list(item, col)

    if not export_list:
        raise RuntimeError(
            "No placeholder objects to export — assign placeholder objects to "
            f"the crop slots, or add 'placeholder_<letter>' objects to '{col.name}'"
        )

    exported_paths = []
    csv_entries = []

    for letter, ph_obj, width, height in export_list:
        identifier = f"{safe_name}_{letter}"
        filename   = f"{identifier}.glb"
        filepath   = os.path.join(out_dir, filename)

        # ── create TEMP collection inside the selected collection ─────────────
        temp_col = bpy.data.collections.new("TEMP")
        col.children.link(temp_col)
        _ensure_temp_col_visible(context, temp_col)

        created_objects = []

        try:
            # ── duplicate camera into TEMP ────────────────────────────────────
            dup_cam = None
            if cam_obj:
                dup_cam = _duplicate_object(cam_obj, temp_col)
                created_objects.append(dup_cam)

            # ── duplicate this single placeholder into TEMP ───────────────────
            dup_ph = _duplicate_object(ph_obj, temp_col)
            created_objects.append(dup_ph)
            # Name the exported object with the letter appendix (e.g. <name>_a)
            dup_ph.name = identifier
            if dup_ph.data:
                dup_ph.data.name = identifier

            # ── apply transforms to the placeholder mesh ──────────────────────
            context.view_layer.update()
            bpy.ops.object.select_all(action='DESELECT')
            if dup_ph.type == 'MESH':
                try:
                    dup_ph.select_set(True)
                except RuntimeError:
                    pass
                context.view_layer.objects.active = dup_ph
                bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
            bpy.ops.object.select_all(action='DESELECT')

            # ── create "lockdown" empty and parent everything under it ────────
            lockdown = bpy.data.objects.new("lockdown", None)
            lockdown.empty_display_type = 'PLAIN_AXES'
            temp_col.objects.link(lockdown)
            created_objects.append(lockdown)

            dup_ph.parent = lockdown
            dup_ph.matrix_parent_inverse = lockdown.matrix_world.inverted()
            if dup_cam:
                dup_cam.parent = lockdown
                dup_cam.matrix_parent_inverse = lockdown.matrix_world.inverted()

            # ── backup selection state ────────────────────────────────────────
            prev_selected = [o for o in scene.objects if o.select_get()]
            prev_active = context.view_layer.objects.active

            try:
                context.view_layer.update()
                bpy.ops.object.select_all(action='DESELECT')
                for obj in created_objects:
                    try:
                        obj.select_set(True)
                    except RuntimeError:
                        pass
                context.view_layer.objects.active = lockdown

                bpy.ops.export_scene.gltf(
                    filepath=filepath,
                    use_selection=True,
                    # Export ONLY the active scene.  Without this the glTF
                    # exporter writes EVERY Blender scene as a separate glTF
                    # scene (e.g. a "Thumbnail" scene), which the importer then
                    # recreates as extra "Scene.0xx" / "Thumbnail" collections
                    # on every import.  Limiting to the active scene keeps the
                    # GLB to just the selected placeholder + camera.
                    use_active_scene=True,
                    export_format='GLB',
                    export_apply=True,
                    export_cameras=True,
                    export_lights=False,
                    export_materials='NONE',
                )

            finally:
                # ── restore selection ─────────────────────────────────────────
                bpy.ops.object.select_all(action='DESELECT')
                for obj in prev_selected:
                    try:
                        obj.select_set(True)
                    except Exception:
                        pass
                if prev_active:
                    try:
                        context.view_layer.objects.active = prev_active
                    except Exception:
                        pass

        finally:
            # ── delete TEMP collection with hierarchy ─────────────────────────
            for obj in reversed(created_objects):
                try:
                    bpy.data.objects.remove(obj, do_unlink=True)
                except Exception:
                    pass
            try:
                bpy.data.collections.remove(temp_col)
            except Exception:
                pass

        exported_paths.append(filepath)
        csv_entries.append((identifier, width, height))

    # ── write / update the <collection_name>.csv in the GLB folder ────────────
    if csv_entries:
        try:
            csv_path = os.path.join(out_dir, f"{folder_base}.csv")
            _update_placeholder_csv(csv_path, csv_entries)
            print(f"[CP3D] Updated CSV: {csv_path}")
        except Exception as e:
            print(f"[CP3D] Could not write {safe_name}.csv: {e}")

    return exported_paths
