"""Image converter — runs a pre-rendered PNG through a Convert compositor group.

Blender 5.1+ only.  Used by the Convert Highlight / Convert Shadow operators to
post-process an already-rendered ``_render.png`` without re-running Cycles.

The Convert_Highlight / Convert_Shadow groups in Convert.blend take their input
from an internal **Render Layers** node (they are authored to run as the scene
compositor during a real render).  To re-use them on a saved PNG we copy the
group and swap that internal Render Layers node for an **Image** node pointing
at the source PNG — so the exact same HueSat / RGB-curve / screen-blend chain
runs, but on our image instead of a live render.

Workflow
--------
1.  Load the source PNG fresh into ``bpy.data.images``.
2.  Copy the convert group; inside the copy, rewire every consumer of the
    internal Render Layers node to read from an Image node (the source PNG).
3.  Nest the rewired copy in a temp compositor:  Group(copy) → GroupOutput.
4.  Trigger a minimal EEVEE render — the compositor's GroupOutput becomes the
    render result, which ``write_still=True`` saves to disk.
5.  Restore every scene setting we touched, swap the user's original compositor
    reference back, and remove the temp node group, the group copy, and image.
"""

import os
import bpy

from .convert_compositor import (
    ensure_convert_node_trees,
    set_convert_value,
    HIGHLIGHT_TREE_NAME,
    SHADOW_TREE_NAME,
)
from .utils import (
    snapshot_datablock_names, purge_leaked_orphans, get_render_output_dir,
)


_TEMP_COMP_NAME = "_CP3D_CONV_TEMP_"


# ── path helpers ──────────────────────────────────────────────────────────────

def _render_out_dir():
    """Return the render output directory (blend_dir/R/<N>) or None if unsaved."""
    return get_render_output_dir()


def find_render_image_path(item):
    """Return the absolute path to *item*'s _render.png if it exists, else None.

    Used by the panel to decide whether the Convert Highlight / Shadow buttons
    should be enabled.
    """
    out_dir = _render_out_dir()
    if not out_dir:
        return None
    name = item.custom_name or (item.collection.name if item.collection else "")
    if not name:
        return None
    safe_name = bpy.path.clean_name(name)
    path = os.path.join(out_dir, f"{safe_name}_render.png")
    return path if os.path.exists(path) else None


# ── main converter ────────────────────────────────────────────────────────────

def convert_render_image(context, item, name, mode):
    """Post-process *_render.png through Convert_Highlight or Convert_Shadow.

    Parameters
    ----------
    context : bpy.types.Context
    item    : CollectionRenderItem
    name    : str  — pre-computed safe output name (e.g. "collection_01")
    mode    : 'highlight' | 'shadow'

    Returns
    -------
    str — absolute path of the written .png file.

    Raises
    ------
    RuntimeError on missing source file, missing node tree, or unsaved blend.
    """
    if not bpy.data.filepath:
        raise RuntimeError("Save the .blend file before converting")

    out_dir = get_render_output_dir()
    source_path = os.path.join(out_dir, f"{name}_render.png")
    if not os.path.exists(source_path):
        raise RuntimeError(
            f"Source render not found: {source_path}\n"
            "Run the Render pass first or check the output folder."
        )

    # Ensure the convert node trees exist (idempotent)
    ensure_convert_node_trees()
    tree_name    = HIGHLIGHT_TREE_NAME if mode == 'highlight' else SHADOW_TREE_NAME
    convert_tree = bpy.data.node_groups.get(tree_name)
    if not convert_tree:
        raise RuntimeError(
            f"Convert node tree '{tree_name}' not found. "
            "Use 'Setup Compositor' to load it from Convert.blend."
        )

    # Apply the user-set intensity into the group's Value node BEFORE we copy the
    # tree below, so the throwaway work copy (and thus the written PNG) reflects
    # the current slider value.  No-ops gracefully if the Value node is absent.
    settings = context.scene.collection_render_settings
    set_convert_value(
        mode,
        settings.convert_highlight_value if mode == 'highlight'
        else settings.convert_shadow_value,
    )

    os.makedirs(out_dir, exist_ok=True)
    output_path = os.path.join(out_dir, f"{name}_{mode}.png")

    # Snapshot world / node-group names now (the base Convert trees already
    # exist, so they're protected) — any orphan left by the tree copy below is
    # removed in the finally block.
    data_snapshot = snapshot_datablock_names()

    # ── load source image (force fresh) ───────────────────────────────────────
    img_name = os.path.basename(source_path)
    existing = bpy.data.images.get(img_name)
    if existing is not None:
        try:
            bpy.data.images.remove(existing, do_unlink=True)
        except Exception:
            pass
    img = bpy.data.images.load(source_path, check_existing=False)
    img.source = 'FILE'

    img_w = img.size[0] if img.size[0] > 0 else 1920
    img_h = img.size[1] if img.size[1] > 0 else 1080

    scene = context.scene

    # ── snapshot every scene setting we are about to mutate ───────────────────
    orig = {
        'engine':            scene.render.engine,
        'filepath':          scene.render.filepath,
        'res_x':             scene.render.resolution_x,
        'res_y':             scene.render.resolution_y,
        'res_pct':           scene.render.resolution_percentage,
        'film_transparent':  scene.render.film_transparent,
        'format':            scene.render.image_settings.file_format,
        'color_mode':        scene.render.image_settings.color_mode,
        'color_depth':       scene.render.image_settings.color_depth,
        'persistent_data':   scene.render.use_persistent_data,
        'use_compositing':   scene.render.use_compositing,
        'compositor_group':  scene.compositing_node_group,
    }

    try:
        # ── copy the convert group and swap internal Render Layers → Image ────
        # The group reads from an internal Render Layers node, so feeding the
        # group's external "Image" input does nothing.  Work on a throwaway copy
        # so the user's saved asset is never modified.
        work_tree = convert_tree.copy()
        work_tree.name = "_CP3D_CONV_WORK_"

        src_img_node = work_tree.nodes.new(type='CompositorNodeImage')
        src_img_node.name     = "_CP3D_SRC_IMG_"
        src_img_node.image    = img
        src_img_node.location = (-900, 0)
        # Set the source image's colorspace to the working/linear space so the
        # node chain receives the same data it would during a live render.
        for _cs in ('Linear Rec.709', 'Linear'):
            try:
                img.colorspace_settings.name = _cs
                break
            except TypeError:
                continue

        # Rewire: every link originating from an internal Render Layers output is
        # reconnected to the matching-named output of our Image node (Image→Image,
        # Alpha→Alpha).  Covers groups with one or more Render Layers nodes.
        for rl in [n for n in work_tree.nodes
                   if n.bl_idname == 'CompositorNodeRLayers']:
            for rl_out in rl.outputs:
                targets = [l.to_socket for l in work_tree.links
                           if l.from_socket == rl_out]
                if not targets:
                    continue
                img_out = src_img_node.outputs.get(rl_out.name)
                if img_out is None:
                    # Fall back to Image output for the primary colour socket
                    img_out = (src_img_node.outputs[0]
                               if rl_out.name == 'Image' else None)
                if img_out is None:
                    continue
                for to_socket in targets:
                    work_tree.links.new(img_out, to_socket)

        # ── configure render settings for a compositor-only EEVEE pass ────────
        # 100% scale is mandatory — a 50% preview scale would halve the output.
        scene.render.resolution_x = img_w
        scene.render.resolution_y = img_h
        scene.render.resolution_percentage = 100
        scene.render.engine = 'BLENDER_EEVEE'
        scene.render.use_persistent_data = False
        scene.render.film_transparent = True
        scene.render.image_settings.file_format = 'PNG'
        scene.render.image_settings.color_mode  = 'RGBA'
        scene.render.image_settings.color_depth = '8'
        scene.render.filepath = output_path
        scene.render.use_compositing = True
        if hasattr(scene, 'use_gpu_compositor'):
            scene.use_gpu_compositor = True

        # ── build a fresh temp compositor NodeGroup ───────────────────────────
        # Graph:  Group(rewired convert copy) → GroupOutput → render result
        # Remove any leftover temp from a previous failed run
        old_temp = bpy.data.node_groups.get(_TEMP_COMP_NAME)
        if old_temp is not None:
            bpy.data.node_groups.remove(old_temp)

        tree = bpy.data.node_groups.new(_TEMP_COMP_NAME, 'CompositorNodeTree')
        scene.compositing_node_group = tree

        # Declare the Image OUTPUT socket BEFORE creating NodeGroupOutput so the
        # node auto-populates its single input from the interface.
        tree.interface.new_socket(
            name="Image", in_out='OUTPUT', socket_type='NodeSocketColor'
        )

        # The rewired convert group (reads our source image internally)
        grp_node = tree.nodes.new(type='CompositorNodeGroup')
        grp_node.name      = "_CP3D_CONV_GRP_"
        grp_node.node_tree = work_tree
        grp_node.location  = (-100, 0)

        # GroupOutput — its Image input becomes the render result
        go_node = tree.nodes.new(type='NodeGroupOutput')
        go_node.name     = "_CP3D_CONV_OUT_"
        go_node.location = (250, 0)

        if not grp_node.outputs:
            raise RuntimeError(
                f"Convert node tree '{tree_name}' has no output socket — "
                "check Convert.blend (group needs an 'Image' OUTPUT)."
            )
        if not go_node.inputs:
            raise RuntimeError(
                "Temp NodeGroupOutput has no inputs — interface socket "
                "did not propagate"
            )

        tree.links.new(grp_node.outputs[0], go_node.inputs[0])

        # Force the depsgraph to acknowledge the new node graph before render.
        # Without this the compositor sometimes uses a stale evaluated graph.
        context.evaluated_depsgraph_get().update()

        # ── execute compositor via minimal render ─────────────────────────────
        # write_still=True saves scene.render.filepath using the render result,
        # which the compositor's GroupOutput "Image" socket now provides.
        bpy.ops.render.render(write_still=True)

        if not os.path.exists(output_path):
            raise RuntimeError(
                f"Render produced no output file at: {output_path}"
            )

    finally:
        # ── restore the user's compositor ─────────────────────────────────────
        scene.compositing_node_group = orig['compositor_group']
        temp = bpy.data.node_groups.get(_TEMP_COMP_NAME)
        if temp is not None:
            try:
                bpy.data.node_groups.remove(temp)
            except Exception:
                pass
        # Remove the throwaway rewired group copy
        work = bpy.data.node_groups.get("_CP3D_CONV_WORK_")
        if work is not None:
            try:
                bpy.data.node_groups.remove(work)
            except Exception:
                pass

        # ── restore every render setting we touched ───────────────────────────
        scene.render.engine                     = orig['engine']
        scene.render.filepath                   = orig['filepath']
        scene.render.resolution_x               = orig['res_x']
        scene.render.resolution_y               = orig['res_y']
        scene.render.resolution_percentage      = orig['res_pct']
        scene.render.film_transparent           = orig['film_transparent']
        scene.render.image_settings.file_format = orig['format']
        scene.render.image_settings.color_mode  = orig['color_mode']
        scene.render.image_settings.color_depth = orig['color_depth']
        scene.render.use_persistent_data        = orig['persistent_data']
        scene.render.use_compositing            = orig['use_compositing']

        try:
            bpy.data.images.remove(img, do_unlink=True)
        except Exception:
            pass

        # Remove any worlds / node groups the convert render leaked (e.g. a
        # duplicated 'Convert_Shadow.001') so the data-block count is unchanged.
        try:
            purge_leaked_orphans(data_snapshot)
        except Exception:
            pass

    return output_path
