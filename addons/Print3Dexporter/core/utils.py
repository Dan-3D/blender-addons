"""Print3Dexporter — utility / helper functions.

All the heavy lifting that operators call but don't need to own lives here.
Grouped into the following sections:

  Node helpers          — thin wrappers that smooth over B4/B5 node-type
                          differences (math node type, output socket lookup).
  Collection/layer      — find LayerCollections, cameras, lights; set visibility.
  Shadow catcher        — backup/restore is_shadow_catcher states.
  Child LC state        — backup/restore sub-collection exclude states.
  Holdout helpers       — apply/restore layer-holdout on named sub-collections.
  BG + Highlight        — set up the combined Background+GlossDir compositor
                          (single render on B4 via FileOutput; two-pass on B5
                          using persistent_data to avoid re-rendering Cycles).
  Shadow compositor     — wire up the opacity multiply chain for shadow passes.
  GlossDir compositor   — wire GlossDir to output (B5 second-pass helper).
  Node tree helpers     — get_node_tree / get_output_node / get_output_socket
                          with B4/B5 branching.
  Compositor setup/reset— build the full preview compositor graph; reset to
                          a plain RenderLayers→output pass-through.
  Crop compositor       — wire Cryptomatte masks for crop-slot renders.
  Object isolation      — hide_render management for crop and highlight passes.
  World / BG            — build the CP3D_BG_World node tree for composite BG.
"""

import os
import re
import bpy

from .constants import BLENDER_5


# ── node helpers ─────────────────────────────────────────────────────────────

def create_math_node(tree):
    """Create a Math node compatible with the current Blender version.

    Blender 5 uses compositing node groups that share the shader node space,
    so Math lives under 'ShaderNodeMath'.  Blender 4 uses the dedicated
    compositor type 'CompositorNodeMath'.  Both expose the same interface.
    """
    if BLENDER_5:
        # B5: compositor node groups use shader-space node types
        return tree.nodes.new(type='ShaderNodeMath')
    return tree.nodes.new(type='CompositorNodeMath')


def math_out(node):
    """Return the Value output socket of a Math node regardless of version."""
    # B4 calls it 'Value'; B5 may not name it — fall back to index 0
    return node.outputs.get('Value', node.outputs[0])


# ── collection / layer helpers ────────────────────────────────────────────────

def find_layer_collection(layer_col, collection):
    """Recursively search the layer-collection tree for a specific Collection.

    Returns the LayerCollection wrapper (which carries exclude/holdout flags)
    for the given Collection data-block, or None if not found.
    """
    if layer_col.collection == collection:
        return layer_col
    for child in layer_col.children:
        result = find_layer_collection(child, collection)
        if result:
            return result
    return None


def find_camera_in_collection(collection):
    """Return the first camera object found in collection or its sub-collections."""
    for obj in collection.objects:
        if obj.type == 'CAMERA':
            return obj
    for child in collection.children:
        cam = find_camera_in_collection(child)
        if cam:
            return cam
    return None


def get_lights_recursive(collection, lights_set):
    """Accumulate all LIGHT objects from collection and descendants into lights_set."""
    for obj in collection.objects:
        if obj.type == 'LIGHT':
            lights_set.add(obj)
    for child in collection.children:
        get_lights_recursive(child, lights_set)


def get_objects_recursive(collection):
    """Return a flat list of all objects in collection and every descendant."""
    objects = list(collection.objects)
    for child in collection.children:
        objects.extend(get_objects_recursive(child))
    return objects


def setup_collection_visibility(context, target_collection):
    """Exclude all top-level collections except the target.
    Also ensure all child layer collections inside the target are NOT excluded."""
    for col in context.scene.collection.children:
        layer_col = find_layer_collection(context.view_layer.layer_collection, col)
        if layer_col:
            # Exclude every top-level collection that is not the render target
            layer_col.exclude = (col != target_collection)
    target_lc = find_layer_collection(context.view_layer.layer_collection, target_collection)
    if target_lc:
        _enable_all_children(target_lc)


def _enable_all_children(layer_col):
    """Recursively set exclude=False on all child layer collections."""
    for child in layer_col.children:
        child.exclude = False
        _enable_all_children(child)


def setup_lights_for_collection(collection):
    """Show only lights that belong to *collection*; hide all others for rendering.

    This prevents lights from other collections (which may still be linked)
    from contributing to this collection's render.
    """
    col_lights = set()
    get_lights_recursive(collection, col_lights)
    for obj in bpy.data.objects:
        if obj.type == 'LIGHT':
            # hide_render=True silences the light without removing it from the scene
            obj.hide_render = obj not in col_lights


# ── placeholder material helpers ──────────────────────────────────────────────

PLACEHOLDER_MAT_NAME = "Placeholder_mat"


def get_or_create_placeholder_mat():
    """Return the shared Placeholder_mat material, creating it if needed.

    Node graph:
        Geometry[Backfacing] ──► Mix Shader[Fac]
        Diffuse BSDF (white) ──► Mix Shader[Shader 1]  (upper slot)
        Transparent BSDF     ──► Mix Shader[Shader 2]  (lower slot)
        Mix Shader           ──► Material Output[Surface]

    The Backfacing output is 0 for front faces and 1 for back faces,
    so front faces show Diffuse (opaque white) and back faces show
    Transparent — giving a clean silhouette from the camera side while
    letting light pass through the reverse.
    """
    mat = bpy.data.materials.get(PLACEHOLDER_MAT_NAME)
    if mat is not None:
        return mat

    mat = bpy.data.materials.new(name=PLACEHOLDER_MAT_NAME)
    mat.use_nodes = True
    tree = mat.node_tree
    tree.nodes.clear()

    # Geometry node — source of the Backfacing output
    geom = tree.nodes.new(type='ShaderNodeNewGeometry')
    geom.name = "Geometry"
    geom.location = (-400, 300)

    # Diffuse BSDF (white) — upper slot = front faces
    diffuse = tree.nodes.new(type='ShaderNodeBsdfDiffuse')
    diffuse.name = "Diffuse BSDF"
    diffuse.location = (-200, 200)
    diffuse.inputs['Color'].default_value = (1.0, 1.0, 1.0, 1.0)

    # Transparent BSDF — lower slot = back faces
    transparent = tree.nodes.new(type='ShaderNodeBsdfTransparent')
    transparent.name = "Transparent BSDF"
    transparent.location = (-200, 50)
    transparent.inputs['Color'].default_value = (1.0, 1.0, 1.0, 1.0)

    # Mix Shader — blends between Diffuse (front) and Transparent (back)
    mix = tree.nodes.new(type='ShaderNodeMixShader')
    mix.name = "Mix Shader"
    mix.location = (50, 200)

    # Material Output
    output = tree.nodes.new(type='ShaderNodeOutputMaterial')
    output.name = "Material Output"
    output.location = (300, 200)

    # Links
    tree.links.new(geom.outputs['Backfacing'], mix.inputs['Fac'])
    tree.links.new(diffuse.outputs['BSDF'],     mix.inputs[1])   # upper slot
    tree.links.new(transparent.outputs['BSDF'],  mix.inputs[2])   # lower slot
    tree.links.new(mix.outputs['Shader'],         output.inputs['Surface'])

    return mat


def assign_placeholder_mat(obj):
    """Replace all material slots on *obj* with Placeholder_mat.
    Returns a list of the original materials for later restore."""
    mat = get_or_create_placeholder_mat()
    original_mats = list(obj.data.materials) if obj.data else []
    if obj.data:
        obj.data.materials.clear()
        obj.data.materials.append(mat)
    return original_mats


def restore_materials(obj, original_mats):
    """Restore *obj*'s material slots from a list saved by assign_placeholder_mat."""
    if obj.data is None:
        return
    obj.data.materials.clear()
    for m in original_mats:
        obj.data.materials.append(m)


def _is_placeholders_col(name):
    """Return True if *name* is ``placeholders`` optionally followed by
    Blender's auto-numbering suffix (``.001``, ``.002``, …).

    Handles the case where multiple collections named "placeholders" exist
    globally (e.g. from importing placeholders into several scene collections)
    and Blender renames subsequent ones to ``placeholders.001`` etc.
    """
    lower = name.lower()
    if lower == 'placeholders':
        return True
    # Blender auto-numbering format: "placeholders.NNN"
    if lower.startswith('placeholders.') and lower[13:].isdigit():
        return True
    return False


def _is_matte_col(name):
    """Return True if *name* is ``Holdout`` (or the legacy ``Matte``)
    optionally followed by Blender's auto-numbering suffix (``.001``, …).

    Holdout objects were called "Matte" before v1.0.68 — old scenes still
    carry ``Matte`` sub-collections, so both names are recognised.  Mirrors
    :func:`_is_placeholders_col`.  Single source of truth — also imported by
    placeholder_importer.py.
    """
    lower = name.lower()
    for base in ('holdout', 'matte'):
        if lower == base:
            return True
        if lower.startswith(base + '.') and lower[len(base) + 1:].isdigit():
            return True
    return False


def find_matte_collection(col):
    """Return the ``Holdout`` (or legacy ``Matte``) sub-collection directly
    under *col*, or None.

    Recognises Blender's auto-numbering (``Holdout.001``, …) via
    :func:`_is_matte_col`.  Used both by the UI (to list imported holdouts)
    and by the crop pass (to drive holdout masking).
    """
    if col is None:
        return None
    for child in col.children:
        if _is_matte_col(child.name):
            return child
    return None


def isolate_placeholders_collection(context, col):
    """Inside *col*, hide all child layer-collections except 'placeholders'.

    Matches ``placeholders``, ``placeholders.001``, etc. so Blender's
    auto-numbering doesn't break crop renders.

    The ``Matte`` sub-collection (if present) is treated specially: instead of
    being hidden it is kept visible and flagged as a **holdout** so the matte
    objects punch transparent holes in the crop silhouette — masking out the
    parts of the placeholder they occlude from camera.  ``hide_render`` on the
    matte meshes is handled separately by :func:`setup_matte_holdout_objects`.

    Returns a backup dict {collection_name: (exclude, holdout)} so the
    caller can restore everything after the render.
    """
    target_lc = find_layer_collection(context.view_layer.layer_collection, col)
    if not target_lc:
        return {}

    backup = {}
    for child_lc in target_lc.children:
        cname = child_lc.collection.name
        backup[cname] = (child_lc.exclude, child_lc.holdout)
        if _is_placeholders_col(cname):
            child_lc.exclude = False
            child_lc.holdout = False
        elif _is_matte_col(cname):
            # Keep mattes visible AND mark them as a holdout so they mask out
            # (cut transparent holes in) the placeholder silhouette.
            child_lc.exclude = False
            child_lc.holdout = True
        else:
            child_lc.exclude = True
    return backup


def restore_placeholders_isolation(context, col, backup):
    """Restore child layer-collection states saved by isolate_placeholders_collection."""
    target_lc = find_layer_collection(context.view_layer.layer_collection, col)
    if not target_lc:
        return
    for child_lc in target_lc.children:
        cname = child_lc.collection.name
        if cname in backup:
            child_lc.exclude, child_lc.holdout = backup[cname]


def setup_matte_holdout_objects(col, only_matte_names=None):
    """Prepare *col*'s ``Matte`` sub-collection for a crop holdout pass.

    A collection-level holdout only masks where its geometry is actually drawn,
    so ``hide_render`` decides which matte meshes contribute to the mask:

      * *only_matte_names* given (str or iterable of names) — make ONLY those
        matte objects renderable (``hide_render=False``) and hide every other
        matte.  This is the per-slot link: placeholder slot X is masked by the
        one-or-many mattes linked to it.
      * *only_matte_names* None / empty — hide ALL matte objects, so no matte
        acts as a holdout for this slot (the placeholder renders un-masked).

    Returns a ``{obj_name: hide_render}`` backup for
    :func:`restore_matte_holdout_objects`.  Returns ``{}`` when *col* has no
    ``Matte`` sub-collection (feature is then a no-op).
    """
    matte_col = find_matte_collection(col)
    if matte_col is None:
        return {}
    # Accept a single name or any iterable of names.
    if isinstance(only_matte_names, str):
        names = {only_matte_names} if only_matte_names else set()
    else:
        names = {n for n in (only_matte_names or []) if n}
    backup = {}
    for obj in matte_col.all_objects:
        backup[obj.name] = obj.hide_render
        if names:
            obj.hide_render = (obj.name not in names)
        else:
            obj.hide_render = True   # no linked matte → no holdout geometry
    return backup


def exclude_helper_collections_for_render(context, col):
    """Exclude the ``placeholders`` and ``Matte`` sub-collections of *col*.

    Used by the beauty **Render** pass so only the setup / object / background
    geometry is rendered — placeholder and matte helper objects are kept out of
    the image.  Also reused after a batch render to hide those helper
    collections in the viewport.  Sets ``exclude=True`` on the matching child
    layer-collections.  During a render pass the caller restores them via
    :func:`restore_child_lc_states`; the post-render call leaves them hidden.
    """
    target_lc = find_layer_collection(context.view_layer.layer_collection, col)
    if not target_lc:
        return
    for child_lc in target_lc.children:
        cname = child_lc.collection.name
        if _is_placeholders_col(cname) or _is_matte_col(cname):
            child_lc.exclude = True


# ── Leaked-datablock cleanup ──────────────────────────────────────────────────
# Rendering can leave behind orphaned data-blocks (e.g. a duplicated World named
# "World.001", or a "Convert_Shadow.001" node group from a node-tree copy).
# These helpers snapshot the world / node-group names before a render and remove
# any NEW, zero-user (orphaned) ones afterwards — so the data-block counts stay
# exactly what they were before the render, without ever touching anything that
# existed beforehand or is still referenced.

def snapshot_datablock_names():
    """Record current world + node-group names for post-render orphan cleanup."""
    return {
        'worlds': {w.name for w in bpy.data.worlds},
        'node_groups': {ng.name for ng in bpy.data.node_groups},
    }


def purge_leaked_orphans(snapshot):
    """Remove worlds / node groups that appeared after *snapshot* and have no
    users.  Returns ``(worlds_removed, node_groups_removed)``.

    Only zero-user blocks whose name is NOT in the snapshot are removed, so
    pre-existing data and anything still in use are always preserved.
    """
    removed_w = removed_g = 0
    for w in list(bpy.data.worlds):
        if w.name not in snapshot['worlds'] and w.users == 0:
            try:
                bpy.data.worlds.remove(w)
                removed_w += 1
            except Exception:
                pass
    for ng in list(bpy.data.node_groups):
        if ng.name not in snapshot['node_groups'] and ng.users == 0:
            try:
                bpy.data.node_groups.remove(ng)
                removed_g += 1
            except Exception:
                pass
    return removed_w, removed_g


# ── Render output directory ───────────────────────────────────────────────────
# Renders are written to  <blend_dir>/R/<N>/  where <N> is the trailing number
# in the .blend file name (e.g. "scene_01.blend" → R/01/).  When the file name
# has no trailing number the renders go straight into  <blend_dir>/R/ .

def blend_trailing_number():
    """Return the trailing digit-run of the .blend file name (e.g. '01'), or ''.

    Keeps the exact digits (leading zeros preserved) so the sub-folder matches
    the file suffix.  Returns '' when the blend isn't saved or has no trailing
    number.
    """
    if not bpy.data.filepath:
        return ""
    stem = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
    m = re.search(r'(\d+)$', stem)
    return m.group(1) if m else ""


def get_render_output_dir():
    """Return the absolute render output directory, or None if blend is unsaved.

    ``<blend_dir>/R/<N>/`` when the file name ends in a number, else
    ``<blend_dir>/R/``.  Does not create the directory — callers os.makedirs it.
    """
    if not bpy.data.filepath:
        return None
    base = os.path.join(os.path.dirname(bpy.data.filepath), "R")
    num = blend_trailing_number()
    return os.path.join(base, num) if num else base


# ── Viewport isolate helpers (for the 'Show' toggle) ──────────────────────────
# Capture / restore the exclude state of every layer-collection so the 'Show'
# solo preview can be turned on and off non-destructively.

def capture_all_lc_excludes(context):
    """Return {collection_name: exclude} for every layer-collection in the view."""
    result = {}

    def _walk(lc):
        result[lc.collection.name] = lc.exclude
        for c in lc.children:
            _walk(c)

    for c in context.view_layer.layer_collection.children:
        _walk(c)
    return result


def apply_all_lc_excludes(context, states):
    """Restore exclude states saved by :func:`capture_all_lc_excludes`."""
    def _walk(lc):
        if lc.collection.name in states:
            lc.exclude = states[lc.collection.name]
        for c in lc.children:
            _walk(c)

    for c in context.view_layer.layer_collection.children:
        _walk(c)


def isolate_collection_except_helpers(context, col):
    """Show ONLY *col* in the viewport, minus its placeholder/matte helpers.

    Excludes every top-level collection except *col* (and un-excludes *col*'s
    own children), then hides *col*'s ``placeholders`` and ``Matte``
    sub-collections — i.e. solo the active collection the way the beauty Render
    pass sees it.  Used by the 'Show' toggle.
    """
    if col is None:
        return
    setup_collection_visibility(context, col)   # solo *col*, show its children
    exclude_helper_collections_for_render(context, col)   # hide its helpers


def _pick_target_view3d(context):
    """Pick the ONE 3D viewport that should follow the collection camera.

    Selection rule (first match wins):
      1. A viewport already in CAMERA view — so once one viewport is showing the
         camera, subsequent collection switches keep updating that same one and
         leave the user's other viewports (modelling angles, etc.) alone.
      2. Otherwise a viewport in free PERSP(ective) view.
      3. Otherwise the largest 3D viewport (fallback — "some rule").

    The active window is searched first, then any other window.  Returns the
    (area, space) to use, or (None, None) if there is no 3D viewport at all.
    """
    wm = context.window_manager
    if wm is None:
        return None, None
    windows = []
    if context.window is not None:
        windows.append(context.window)
    windows.extend(w for w in wm.windows if w not in windows)

    camera_view = persp_view = largest = None
    largest_size = -1
    for window in windows:
        for area in window.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            space = area.spaces.active
            r3d = getattr(space, 'region_3d', None)
            if r3d is None:
                continue
            if camera_view is None and r3d.view_perspective == 'CAMERA':
                camera_view = (area, space)
            if persp_view is None and r3d.view_perspective == 'PERSP':
                persp_view = (area, space)
            size = area.width * area.height
            if size > largest_size:
                largest, largest_size = (area, space), size
    return camera_view or persp_view or largest or (None, None)


def set_active_camera_view(context, cam):
    """Make *cam* the scene camera and switch ONE 3D viewport to camera view.

    No-op when *cam* is None.  Used by the 'Show' / 'Live' toggle so soloing a
    collection also looks through that collection's camera — but only in a
    single viewport (see :func:`_pick_target_view3d` for which one), leaving the
    user's other viewports untouched.
    """
    if cam is None:
        return
    context.scene.camera = cam
    area, space = _pick_target_view3d(context)
    if area is None:
        return
    space.region_3d.view_perspective = 'CAMERA'
    area.tag_redraw()   # ensure the viewport refreshes to the new camera


def restore_matte_holdout_objects(backup):
    """Restore ``hide_render`` states saved by :func:`setup_matte_holdout_objects`."""
    for obj_name, state in backup.items():
        obj = bpy.data.objects.get(obj_name)
        if obj is not None:
            obj.hide_render = state


# ── child layer-collection state helpers ─────────────────────────────────────
# setup_collection_visibility() forces exclude=False on all children of the
# target collection so sub-collections are visible during the render.  These
# backup/restore helpers undo that change after each pass.

def backup_child_lc_states(context, target_collection):
    """Backup exclude states of all child layer collections."""
    backup = {}
    target_lc = find_layer_collection(context.view_layer.layer_collection, target_collection)
    if target_lc:
        _backup_children(target_lc, backup)
    return backup


def _backup_children(layer_col, backup):
    """Recursively record child.exclude into backup dict keyed by collection name."""
    for child in layer_col.children:
        backup[child.collection.name] = child.exclude
        _backup_children(child, backup)


def restore_child_lc_states(context, target_collection, backup):
    """Restore exclude states of child layer collections."""
    target_lc = find_layer_collection(context.view_layer.layer_collection, target_collection)
    if target_lc:
        _restore_children(target_lc, backup)


def _restore_children(layer_col, backup):
    """Recursively apply saved exclude values back to child layer collections."""
    for child in layer_col.children:
        if child.collection.name in backup:
            child.exclude = backup[child.collection.name]
        _restore_children(child, backup)


# ── Cryptomatte-masked compositor (Shadow Inside) ────────────────────────────

def _build_cryptomatte_mask(tree, context, item):
    """Create CryptomatteV2 nodes for all enabled placeholder slots in *tree*,
    wire them to the existing RenderLayers node, and return the combined mask
    output socket (union of all mattes via MAXIMUM).

    The RenderLayers node must already exist in *tree* with name "Render Layers".
    Returns the mask output socket, or None if no placeholders are configured.
    """
    settings = context.scene.collection_render_settings
    rl = tree.nodes.get("Render Layers")
    if not rl:
        return None

    vl_name = context.view_layer.name
    crypto_layer = (f"{vl_name}.CryptoObject"
                    if settings.crop_source == 'OBJECT'
                    else f"{vl_name}.CryptoMaterial")

    _SLOT_LETTERS = 'ABCDEFGHIJ'
    crop_names   = {sl: getattr(item, f"crop_{sl.lower()}_name",    "") for sl in _SLOT_LETTERS}
    enabled_flags = {sl: getattr(item, f"crop_{sl.lower()}_enabled", False) for sl in _SLOT_LETTERS}

    # Create CryptomatteV2 nodes for enabled slots that have a name assigned
    _slot_y = {sl: -500 - i * 200 for i, sl in enumerate(_SLOT_LETTERS)}
    for slot, y_offset in [(sl, _slot_y[sl]) for sl in _SLOT_LETTERS]:
        if not enabled_flags[slot] or not crop_names[slot]:
            continue
        crypto = tree.nodes.new(type='CompositorNodeCryptomatteV2')
        crypto.name = f"Cryptomatte {slot}"
        crypto.location = (-200, y_offset)
        crypto.source = 'RENDER'
        crypto.layer_name = crypto_layer
        crypto.matte_id = crop_names[slot]
        tree.links.new(rl.outputs['Image'], crypto.inputs['Image'])

    # Combine all enabled mattes into a single mask
    enabled_crops = get_enabled_crop_nodes(item, tree)
    if not enabled_crops:
        return None
    return build_combined_mask(tree, item, enabled_crops, prefix="MASK")


def setup_shadow_inside_compositor(context, item, opacity=1.0):
    """Compositor for Shadow Inside: desaturated render masked by placeholder Cryptomatte.

    Graph:
      RenderLayers[Image] → HueSat(Saturation=0) → Math(MULTIPLY, opacity) → SetAlpha → output
                                                                                  ↑ Alpha
      CryptomatteV2(combined placeholder mask) ───────────────────────────────────┘

    The render is fully desaturated (greyscale), then darkened by opacity,
    then masked so only the placeholder object area is visible (rest is transparent).
    """
    tree = get_node_tree(context)
    if tree is None:
        return
    tree.nodes.clear()

    rl = tree.nodes.new(type='CompositorNodeRLayers')
    rl.name = "Render Layers"
    rl.location = (-400, 300)

    output = get_output_node(tree)
    output.location = (700, 300)
    out_socket = get_output_socket(output)
    if not out_socket:
        return

    # Desaturate the render to greyscale
    hue_sat = tree.nodes.new(type='CompositorNodeHueSat')
    hue_sat.name = "Desaturate"
    hue_sat.location = (-150, 300)
    hue_sat.inputs['Saturation'].default_value = 0.0   # fully desaturated
    tree.links.new(rl.outputs['Image'], hue_sat.inputs['Image'])

    image_out = hue_sat.outputs['Image']

    # Apply darkness control (opacity)
    if opacity < 1.0:
        darken = create_math_node(tree)
        darken.name = "SI Darken"
        darken.location = (50, 300)
        darken.operation = 'MULTIPLY'
        darken.inputs[1].default_value = opacity
        tree.links.new(image_out, darken.inputs[0])
        image_out = math_out(darken)

    # Build combined Cryptomatte mask from placeholder objects
    mask_socket = _build_cryptomatte_mask(tree, context, item)

    if mask_socket:
        set_alpha = tree.nodes.new(type='CompositorNodeSetAlpha')
        set_alpha.name = "SI SetAlpha"
        set_alpha.location = (350, 300)
        if hasattr(set_alpha, 'mode'):
            set_alpha.mode = 'REPLACE_ALPHA'
        tree.links.new(image_out, set_alpha.inputs['Image'])
        tree.links.new(mask_socket, set_alpha.inputs['Alpha'])
        tree.links.new(set_alpha.outputs['Image'], out_socket)
    else:
        # No placeholders — just output desaturated image
        tree.links.new(image_out, out_socket)


# ── compositor node tree helpers ──────────────────────────────────────────────

def get_node_tree(context):
    """Return the active compositor node tree, creating it if necessary.

    Blender 4: the compositor tree is stored directly on the scene as
               scene.node_tree (a CompositorNodeTree).
    Blender 5: the compositor was decoupled into a NodeGroup asset stored in
               scene.compositing_node_group.  We create the node group if it
               doesn't exist yet, then return it.
    """
    scene = context.scene
    if BLENDER_5:
        if scene.compositing_node_group is None:
            # Create a new compositor node group and link it to the scene
            tree = bpy.data.node_groups.new("Compositing", 'CompositorNodeTree')
            scene.compositing_node_group = tree
        return scene.compositing_node_group
    else:
        scene.use_nodes = True   # activates the node tree on the scene
        return scene.node_tree


def get_output_node(tree):
    """Return (or create) the compositor output node for the current version.

    Blender 4: the output node is CompositorNodeComposite.
    Blender 5: the output node is NodeGroupOutput.  The node group interface
               must also declare the 'Image' output socket, otherwise nothing
               gets piped to the final render result.
    """
    if BLENDER_5:
        output = tree.nodes.get("Group Output")
        if not output:
            output = tree.nodes.new(type='NodeGroupOutput')
            output.name = "Group Output"
            output.location = (700, 300)
            if len(tree.interface.items_tree) == 0:
                # The group needs at least one output socket declared on its
                # interface for the compositor to know what to display/save.
                tree.interface.new_socket(
                    name="Image", in_out='OUTPUT', socket_type='NodeSocketColor'
                )
        return output
    else:
        output = tree.nodes.get("Composite")
        if not output:
            output = tree.nodes.new(type='CompositorNodeComposite')
            output.name = "Composite"
            output.location = (700, 300)
        return output


def get_output_socket(output_node):
    """Return the input socket of the output node that receives the image data.

    Blender 4: CompositorNodeComposite has a named 'Image' input socket.
    Blender 5: NodeGroupOutput exposes its inputs as a positional list;
               the first input (index 0) corresponds to the 'Image' socket
               declared in the node group interface.
    """
    if BLENDER_5:
        return output_node.inputs[0] if len(output_node.inputs) > 0 else None
    return output_node.inputs.get('Image')


# ── compositor backup / restore ──────────────────────────────────────────────
# B5: save/restore is a simple REFERENCE SWAP — we never touch the user's
#     NodeGroup contents.  Zero-copy, zero data-loss.
# B4: scene.node_tree can't be swapped by reference (it's owned by the scene)
#     so we fall back to _copy_nodes as before.

def backup_compositor(context):
    """Save the current compositor state so it can be restored after rendering.

    B5: just saves the compositing_node_group reference — no copying.
    B4: copies nodes into a temp group (lossy fallback).
    """
    scene = context.scene

    if BLENDER_5:
        # Save the reference — the NodeGroup itself is NEVER modified
        return {'b5': True,
                'original_group': scene.compositing_node_group}
    else:
        if not scene.use_nodes or scene.node_tree is None:
            return {'b5': False, 'had_tree': False, 'use_nodes': scene.use_nodes}
        src = scene.node_tree
        backup_tree = bpy.data.node_groups.new("_CP3D_COMP_BACKUP_", 'CompositorNodeTree')
        _copy_nodes(src, backup_tree)
        return {'b5': False, 'had_tree': True, 'backup_tree': backup_tree,
                'use_nodes': scene.use_nodes}


def restore_compositor(context, backup):
    """Restore the compositor state saved by backup_compositor().

    B5: swaps the compositing_node_group reference back to the original.
        Any temporary NodeGroups created during rendering are cleaned up.
    B4: copies nodes back from the backup group.
    """
    if backup is None:
        return
    scene = context.scene

    if backup.get('b5'):
        original = backup.get('original_group')
        # If the current group is a temp one we created, remove it
        current = scene.compositing_node_group
        if current is not None and current != original:
            if current.name.startswith("_CP3D_"):
                scene.compositing_node_group = None
                bpy.data.node_groups.remove(current)
        # Swap back to the original reference
        scene.compositing_node_group = original
    else:
        scene.use_nodes = backup['use_nodes']
        if not backup.get('had_tree'):
            return
        tree = scene.node_tree
        if tree is None:
            return
        tree.nodes.clear()
        _copy_nodes(backup['backup_tree'], tree)
        bpy.data.node_groups.remove(backup['backup_tree'])


def swap_compositor(context, node_tree):
    """B5: set compositing_node_group to *node_tree* directly (zero-copy).
    B4: copy nodes from *node_tree* into scene.node_tree.
    Returns the previous compositing_node_group reference (B5) or None (B4).
    """
    scene = context.scene
    if BLENDER_5:
        prev = scene.compositing_node_group
        scene.compositing_node_group = node_tree
        return prev
    else:
        scene.use_nodes = True
        if node_tree and scene.node_tree:
            scene.node_tree.nodes.clear()
            _copy_nodes(node_tree, scene.node_tree)
        return None


def create_temp_compositor(context):
    """Create a temporary NodeGroup for add-on-built passes (Crop, Shadow, etc).

    B5: creates a new NodeGroup and sets it as compositing_node_group.
    B4: just clears scene.node_tree.

    Returns the temp group (B5) or None (B4).  The caller builds nodes in
    get_node_tree(context) which now points to the temp group.
    """
    scene = context.scene
    if BLENDER_5:
        temp = bpy.data.node_groups.new("_CP3D_TEMP_COMP_", 'CompositorNodeTree')
        scene.compositing_node_group = temp
        return temp
    else:
        scene.use_nodes = True
        if scene.node_tree:
            scene.node_tree.nodes.clear()
        return None


def remove_temp_compositor(context, temp_group):
    """Remove a temporary NodeGroup created by create_temp_compositor().
    Does NOT restore the original — use restore_compositor() for that."""
    if temp_group is not None and BLENDER_5:
        scene = context.scene
        if scene.compositing_node_group == temp_group:
            scene.compositing_node_group = None
        try:
            bpy.data.node_groups.remove(temp_group)
        except Exception:
            pass


def copy_compositor_with_node_routed(node_tree, node_name):
    """Return a temp COPY of *node_tree* whose node named *node_name* is wired
    straight to the compositor output — or None if it can't be built.

    Used by the Highlight / Shadow passes (mirrors the Render ISO "Curves
    Mockup" trick): the beauty render is routed through a single named node
    (typically an RGB-curve adjustment) instead of the tree's normal output,
    so the pass re-saves the Render image with that one adjustment applied.

    Returns None when *node_tree* is None or contains no node named
    *node_name* — the caller treats that as "skip this pass with an error".
    The returned copy is a throwaway NodeGroup (name prefixed "_CP3D_…_TEMP_")
    that the render loop removes via remove_temp_compositor() afterwards.
    """
    if node_tree is None or node_tree.nodes.get(node_name) is None:
        return None
    temp = node_tree.copy()
    temp.name = f"_CP3D_{node_name}_TEMP_"
    target = temp.nodes.get(node_name)
    # Find the tree's output node (B4: Composite, B5: Group Output)
    output_node = None
    for nd in temp.nodes:
        if nd.bl_idname in ('NodeGroupOutput', 'CompositorNodeComposite'):
            output_node = nd
            break
    if target and output_node and target.outputs:
        out_socket = output_node.inputs[0] if output_node.inputs else None
        if out_socket:
            # Drop whatever currently feeds the output, then route the named node
            for link in list(temp.links):
                if link.to_node == output_node:
                    temp.links.remove(link)
            temp.links.new(target.outputs[0], out_socket)
    return temp


def _copy_nodes(src_tree, dst_tree):
    """Deep-copy all nodes and links from *src_tree* into *dst_tree*.
    dst_tree.nodes must already be empty (or the caller accepts duplicates).

    Handles special node types:
      - Convert Colorspace: copies from_color_space / to_color_space
      - RGB Curves (CurveRGB): copies every curve's control points
      - Any node with a 'mapping' attribute (CurveRGB, CurveVec, etc.)
    """
    node_map = {}   # src_node.name → dst_node
    for sn in src_tree.nodes:
        dn = dst_tree.nodes.new(type=sn.bl_idname)
        dn.name = sn.name
        dn.label = sn.label
        dn.location = sn.location
        dn.width = sn.width
        dn.height = sn.height
        dn.mute = sn.mute
        dn.hide = sn.hide

        # Copy input default values
        for i, si in enumerate(sn.inputs):
            if i < len(dn.inputs) and hasattr(si, 'default_value'):
                try:
                    dn.inputs[i].default_value = si.default_value
                except Exception:
                    pass

        # Copy common settable attributes
        for attr in ('operation', 'mode', 'blend_type', 'use_clamp',
                      'source', 'layer_name', 'matte_id', 'image',
                      'color_space', 'interpolation', 'projection',
                      'extension', 'premul', 'use_alpha',
                      'from_color_space', 'to_color_space',
                      'use_premultiply', 'use_straight_alpha',
                      'distance', 'falloff', 'edge',
                      'fac', 'factor'):
            if hasattr(sn, attr):
                try:
                    setattr(dn, attr, getattr(sn, attr))
                except Exception:
                    pass

        # ── RGB Curves / CurveVec: deep-copy the mapping (curve points) ───────
        if hasattr(sn, 'mapping') and hasattr(dn, 'mapping'):
            _copy_curve_mapping(sn.mapping, dn.mapping)

        node_map[sn.name] = dn

    # Recreate links
    for link in src_tree.links:
        from_node = node_map.get(link.from_node.name)
        to_node   = node_map.get(link.to_node.name)
        if from_node and to_node:
            fi = _find_socket_index(link.from_node.outputs, link.from_socket)
            ti = _find_socket_index(link.to_node.inputs,    link.to_socket)
            if fi < len(from_node.outputs) and ti < len(to_node.inputs):
                try:
                    dst_tree.links.new(from_node.outputs[fi], to_node.inputs[ti])
                except Exception:
                    pass


def _copy_curve_mapping(src_mapping, dst_mapping):
    """Deep-copy a CurveMapping (used by RGB Curves, Vector Curves, etc.).

    Copies clip bounds, every curve's control points (location + handle type),
    and calls update() so Blender recalculates the internal LUT.
    """
    # Copy clipping bounds
    try:
        dst_mapping.use_clip = src_mapping.use_clip
        dst_mapping.clip_min_x = src_mapping.clip_min_x
        dst_mapping.clip_min_y = src_mapping.clip_min_y
        dst_mapping.clip_max_x = src_mapping.clip_max_x
        dst_mapping.clip_max_y = src_mapping.clip_max_y
    except Exception:
        pass

    # Copy each curve (typically 4: C, R, G, B)
    for ci in range(min(len(src_mapping.curves), len(dst_mapping.curves))):
        src_curve = src_mapping.curves[ci]
        dst_curve = dst_mapping.curves[ci]

        src_pts = src_curve.points
        dst_pts = dst_curve.points

        # Ensure dst has enough points (it starts with 2; add extras if needed)
        while len(dst_pts) < len(src_pts):
            dst_pts.new(0.5, 0.5)   # position will be overwritten below
        # Remove excess points from the end (keep at least 2)
        while len(dst_pts) > len(src_pts) and len(dst_pts) > 2:
            dst_pts.remove(dst_pts[-1])

        # Copy point locations and handle types
        for pi in range(min(len(src_pts), len(dst_pts))):
            dst_pts[pi].location = src_pts[pi].location
            try:
                dst_pts[pi].handle_type = src_pts[pi].handle_type
            except Exception:
                pass

    # Recalculate the internal lookup table
    try:
        dst_mapping.update()
    except Exception:
        pass


def _find_socket_index(socket_collection, socket):
    """Return the integer index of *socket* within *socket_collection*."""
    for i, s in enumerate(socket_collection):
        if s == socket:
            return i
    return 0


# ── compositor setup ──────────────────────────────────────────────────────────

def setup_crop_alpha_compositor(context, item):
    """Set up compositor for the crop pass: white image masked by render alpha.

    On Blender 5 the compositor NodeGroup output does NOT reliably carry the
    alpha channel through a plain RenderLayers[Image] → NodeGroupOutput
    passthrough.  Every other pass (Shadow, Highlight, Shadow Inside) works
    because they wire alpha explicitly through a SetAlpha node.

    This function does the same for Crop:

      RGB(white) ──────────────→ SetAlpha(REPLACE_ALPHA) → output
      RenderLayers[Alpha] ──→ (opt. DilateErode) ───↗ (Alpha input)

    The render alpha comes from Film Transparent + the isolated placeholder
    object (which has an opaque material on front faces).  The white RGB node
    ensures the crop output is a clean white silhouette with the placeholder's
    outline as the alpha mask — independent of scene lighting.
    """
    tree = get_node_tree(context)
    if tree is None:
        return
    tree.nodes.clear()

    rl = tree.nodes.new(type='CompositorNodeRLayers')
    rl.name = "Render Layers"
    rl.location = (0, 300)

    output = get_output_node(tree)
    output.location = (600, 300)
    out_socket = get_output_socket(output)
    if not out_socket:
        return

    # Solid white colour — the alpha channel carries all the shape information
    rgb = tree.nodes.new(type='CompositorNodeRGB')
    rgb.name = "Crop White"
    rgb.location = (200, 400)
    rgb.outputs[0].default_value = (1, 1, 1, 1)

    # SetAlpha replaces the RGB node's alpha (1.0) with the render's alpha,
    # so the final image is white where the placeholder is and transparent elsewhere.
    set_alpha = tree.nodes.new(type='CompositorNodeSetAlpha')
    set_alpha.name = "Crop SetAlpha"
    set_alpha.location = (400, 300)
    if hasattr(set_alpha, 'mode'):
        set_alpha.mode = 'REPLACE_ALPHA'

    alpha_source = rl.outputs['Alpha']

    tree.links.new(rgb.outputs[0], set_alpha.inputs['Image'])
    tree.links.new(alpha_source, set_alpha.inputs['Alpha'])
    tree.links.new(set_alpha.outputs['Image'], out_socket)


def get_enabled_crop_nodes(item, tree):
    """Return Cryptomatte nodes that are enabled and have an object assigned."""
    crops = []
    for sl in 'ABCDEFGHIJ':
        sl_l = sl.lower()
        if getattr(item, f"crop_{sl_l}_enabled", False) and getattr(item, f"crop_{sl_l}_name", ""):
            c = tree.nodes.get(f"Cryptomatte {sl}")
            if c:
                crops.append(c)
    return crops


def build_combined_mask(tree, item, enabled_crops, prefix="HL"):
    """Combine multiple Cryptomatte matte outputs with MAXIMUM.

    When more than one crop slot is active (e.g. Crop A + Crop B), we need a
    single mask that covers the union of both objects.  We chain MAXIMUM Math
    nodes: max(A, B) then max(result, C) — the pixel-wise maximum of two 0–1
    matte values gives the union without brightening overlap areas above 1.

    Returns the combined mask socket (the output socket to connect to SetAlpha),
    or None if enabled_crops is empty.
    """
    if not enabled_crops:
        return None
    # Remove any stale nodes from a previous wiring to avoid duplicates
    for nn in [f"{prefix}_Crop_SetAlpha", f"{prefix}_Crop_Math1",
               f"{prefix}_Crop_Math2"]:
        old = tree.nodes.get(nn)
        if old:
            tree.nodes.remove(old)
    if len(enabled_crops) == 1:
        # Single crop — no merging needed
        combined_mask = enabled_crops[0].outputs['Matte']
    else:
        # Chain MAXIMUM nodes: result accumulates as we add each additional crop
        prev = enabled_crops[0].outputs['Matte']
        for i in range(1, len(enabled_crops)):
            m = create_math_node(tree)
            m.name = f"{prefix}_Crop_Math{i}"
            m.location = (200 + i * 150, -500)
            m.operation = 'MAXIMUM'
            tree.links.new(prev, m.inputs[0])
            tree.links.new(enabled_crops[i].outputs['Matte'], m.inputs[1])
            prev = math_out(m)   # carry forward the merged result
        combined_mask = prev
    return combined_mask


# ── object render-isolation helpers ───────────────────────────────────────────

def isolate_object_for_render(col, target_obj_name):
    """Hide every object in *col* (recursively) for rendering except
    the one named *target_obj_name*.

    Returns a dict ``{obj_name: original_hide_render}`` so the caller can
    restore with :func:`restore_render_isolation`.
    """
    backup = {}
    for obj in get_objects_recursive(col):
        backup[obj.name] = obj.hide_render
        obj.hide_render = (obj.name != target_obj_name)
    return backup


def restore_render_isolation(backup):
    """Restore ``hide_render`` states saved by :func:`isolate_object_for_render`."""
    for obj_name, state in backup.items():
        obj = bpy.data.objects.get(obj_name)
        if obj is not None:
            obj.hide_render = state


# ── Per-pass EXR outputs via CompositorNodeOutputFile ────────────────────────
# Blender 5's multi-layer EXR support is unreliable at the Python API level
# (the ``OPEN_EXR_MULTILAYER`` enum was removed and layer_slots don't always
# produce a true multi-layer file).  The robust alternative is to write each
# render pass as its own single-layer .exr file — done with
# ``CompositorNodeOutputFile`` configured with ``file_slots`` (one per pass).
# Each slot writes a separate .exr named ``<stem>_<pass_suffix><frame>.exr``,
# which :func:`finalize_separate_exr_outputs` then renames to the clean
# ``<stem>_<pass_suffix>.exr`` form.

import os as _os  # local alias so we don't shadow callers using `os`


def add_separate_pass_exr_outputs(tree, base_dir, filename_stem, pass_specs):
    """Inject a CompositorNodeOutputFile that writes one EXR per render pass.

    Creates (or reuses) a RenderLayers node in *tree* and adds one file_slot
    per entry in *pass_specs*, wiring the corresponding RenderLayers output
    socket into that slot.  The render engine then writes one .exr per slot
    during the main render call — no re-render, no save_render tricks.

    Parameters
    ----------
    tree          : bpy.types.CompositorNodeTree (scene.node_tree OR a
                    ``compositing_node_group`` on Blender 5)
    base_dir      : str  — output directory for all EXR files
    filename_stem : str  — shared prefix, e.g. ``"Scene_01_background"``.
                    Each slot writes ``<filename_stem>_<suffix>_<frame>.exr``.
    pass_specs    : list[tuple[str, str]] — (rl_socket_name, file_suffix)
                    e.g. ``[('IndexMA', 'IndexMA'), ('IndexOB', 'IndexOB')]``.
                    Entries whose socket isn't enabled on the RenderLayers
                    node are silently skipped — protects against passes the
                    user hasn't enabled in view-layer settings.

    Returns
    -------
    tuple
      (fo_node, slot_records)
         fo_node       — CompositorNodeOutputFile to remove after render.
         slot_records  — list of (file_suffix, slot_path_prefix) usable by
                         :func:`finalize_separate_exr_outputs` to rename
                         Blender's frame-numbered output files.
    """
    # Find or create a dedicated RenderLayers node for the EXR outputs.
    # Creating our own avoids collisions with a user CUS compositor that
    # may have its RL node set to a different view layer.
    rl = tree.nodes.new(type='CompositorNodeRLayers')
    rl.name = "_CP3D_RL_EXR_"
    rl.location = (-600, -400)

    # Create the File Output node
    fo = tree.nodes.new(type='CompositorNodeOutputFile')
    fo.name = "_CP3D_EXR_FO_"
    fo.label = "CP3D Per-Pass EXR"
    fo.location = (400, -400)
    fo.base_path = base_dir

    # Single-layer EXR per file — OPEN_EXR is universally available on both
    # Blender 4 and 5.  DWAA codec, 16-bit half float.
    fo.format.file_format = 'OPEN_EXR'
    fo.format.color_depth = '16'
    if hasattr(fo.format, 'exr_codec'):
        fo.format.exr_codec = 'DWAA'

    # Clear default slots so numbering starts cleanly
    try:
        fo.file_slots.clear()
    except AttributeError:
        while len(fo.file_slots):
            fo.file_slots.remove(fo.file_slots[0])
    try:
        fo.layer_slots.clear()
    except AttributeError:
        while len(fo.layer_slots):
            fo.layer_slots.remove(fo.layer_slots[0])

    slot_records = []
    for socket_name, file_suffix in pass_specs:
        # Skip passes that aren't enabled on the view layer — those sockets
        # either don't exist or carry no data
        out_sock = rl.outputs.get(socket_name)
        if out_sock is None:
            continue
        try:
            if not out_sock.enabled:
                continue
        except AttributeError:
            pass
        # Trailing underscore so Blender's frame-number suffix doesn't merge
        # with the pass suffix when we look for the written file.
        slot_path = f"{filename_stem}_{file_suffix}_"
        fo.file_slots.new(slot_path)
        # The new slot appended an input at the end — wire the pass in
        tree.links.new(out_sock, fo.inputs[-1])
        slot_records.append((file_suffix, slot_path))

    return fo, slot_records


def finalize_separate_exr_outputs(context, base_dir, slot_records, target_stem):
    """Rename each slot's frame-numbered EXR to a clean ``<stem>_<suffix>.exr``.

    Blender's File Output node writes ``<base_dir>/<slot_path><frame>.exr``.
    This function finds each of those files (probing common frame-padding
    widths) and renames them to ``<target_stem>_<file_suffix>.exr``.

    Parameters
    ----------
    context       : bpy.types.Context
    base_dir      : str  — directory used as the File Output node's base_path
    slot_records  : list[tuple[str, str]] — (file_suffix, slot_path) from
                    :func:`add_separate_pass_exr_outputs`
    target_stem   : str  — absolute path stem, e.g.
                    ``"D:/…/Render/Scene_01/Scene_01_background"``.
                    Each pass gets renamed to
                    ``"{target_stem}_{file_suffix}.exr"``.

    Returns
    -------
    list[str]  — list of final absolute paths that now exist on disk.
    """
    frame = context.scene.frame_current
    renamed_paths = []

    for file_suffix, slot_path in slot_records:
        # Locate Blender's written file (try common frame-padding widths)
        written_path = None
        for pad in (4, 0, 3, 5, 6):
            if pad == 0:
                cand = _os.path.join(base_dir, f"{slot_path}{frame}.exr")
            else:
                cand = _os.path.join(base_dir, f"{slot_path}{frame:0{pad}d}.exr")
            if _os.path.exists(cand):
                written_path = cand
                break
        if written_path is None:
            continue

        final_path = f"{target_stem}_{file_suffix}.exr"
        if written_path != final_path:
            try:
                if _os.path.exists(final_path):
                    _os.remove(final_path)
            except Exception:
                pass
            try:
                _os.rename(written_path, final_path)
            except Exception as e:
                print(f"[CP3D] Failed to rename EXR '{written_path}' → "
                      f"'{final_path}': {e}")
                final_path = written_path
        renamed_paths.append(final_path)

    return renamed_paths


# (The "world / background helpers" section was removed in v1.0.24 along with
#  the Background pass and "Render on Background" mode.  Per-collection World
#  selection now uses an existing World data-block chosen in the panel — see
#  CollectionRenderItem.world — so no World shader is built by the add-on.)
