bl_info = {
    "name": "Bake Animation to Shape Keys",
    "author": "adambb-code",
    "version": (1, 2, 3),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar (N-Panel) > GLB Bake",
    "description": "Bakes complex nested hierarchies and modifiers to shape keys for GLB export.",
    "category": "Import-Export",
}

import bpy
import time as _time
from collections import deque
from mathutils import Vector as _Vector
from mathutils.bvhtree import BVHTree as _BVHTree

# ------------------------------------------------------------------ #
# Module-level helpers — used both during baking (execute) and for the
# live frame-count preview in the panel (draw).  Keeping them here
# avoids duplicating logic and lets draw() call them without operators.
# ------------------------------------------------------------------ #

def _action_fcurves(action):
    """Return every fcurve in an action, handling Blender 5.1 layers API."""
    fc = []
    if hasattr(action, "layers"):
        for layer in action.layers:
            for strip in layer.strips:
                if hasattr(strip, "channelbags"):
                    for bag in strip.channelbags:
                        fc.extend(bag.fcurves)
    elif hasattr(action, "fcurves"):
        fc.extend(action.fcurves)
    return fc


def _lock_custom_normals(mesh, ev_obj):
    """Copy the evaluated per-loop normals (e.g. from Weighted Normal modifier)
    onto *mesh* as persistent custom split normals so they survive modifier removal.
    """
    try:
        cn = [tuple(n.vector) for n in ev_obj.data.corner_normals]
        if cn:
            mesh.normals_split_custom_set(cn)
    except Exception as e:
        print(f"[lock_normals | {ev_obj.name}] EXCEPTION: {e}")


def _mesh_from_object(obj, context):
    """Get an evaluated mesh from any object type, including GeoNodes that output
    curve or instance geometry.

    Fast path: new_from_object on the evaluated object — works for regular meshes
    and GeoNodes that output mesh geometry directly.

    Fallback (0 vertices + NODES modifier): create a *temporary copy* of the object,
    append a Realize Instances modifier to the COPY (never to obj), evaluate it, then
    immediately remove the copy.  This avoids invalidating obj's depsgraph, which was
    the root cause of empty shape key coordinates when the original object was modified.
    """
    dg   = context.evaluated_depsgraph_get()
    ev   = obj.evaluated_get(dg)
    mesh = bpy.data.meshes.new_from_object(ev)
    if len(mesh.vertices) > 0 or not any(m.type == 'NODES' for m in obj.modifiers):
        _lock_custom_normals(mesh, ev)
        return mesh
    bpy.data.meshes.remove(mesh)

    # Build (or reuse) a one-node group: GroupInput → RealizeInstances → GroupOutput.
    _NG = "_bake_realize_instances"
    ng  = bpy.data.node_groups.get(_NG)
    if ng is None:
        ng = bpy.data.node_groups.new(_NG, 'GeometryNodeTree')
        ng.interface.new_socket('Geometry', in_out='INPUT',  socket_type='NodeSocketGeometry')
        ng.interface.new_socket('Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
        _n_in  = ng.nodes.new('NodeGroupInput')
        _n_ri  = ng.nodes.new('GeometryNodeRealizeInstances')
        _n_out = ng.nodes.new('NodeGroupOutput')
        ng.links.new(_n_in.outputs[0], _n_ri.inputs[0])
        ng.links.new(_n_ri.outputs[0], _n_out.inputs[0])

    # Use a temporary COPY of obj so obj's depsgraph is never invalidated.
    tmp_obj = obj.copy()
    context.scene.collection.objects.link(tmp_obj)
    try:
        tmp_mod            = tmp_obj.modifiers.new("_bake_realize", 'NODES')
        tmp_mod.node_group = ng
        dg2  = context.evaluated_depsgraph_get()
        ev2  = tmp_obj.evaluated_get(dg2)
        mesh = bpy.data.meshes.new_from_object(ev2)
        _lock_custom_normals(mesh, ev2)
    finally:
        bpy.data.objects.remove(tmp_obj, do_unlink=True)
    return mesh


def _patch_glb_scale_step(glb_path):
    """Patch scale samplers for topology-split segment nodes (_Seg) to STEP interpolation.

    The Blender glTF exporter merges all animation channels into a shared input
    accessor and forces LINEAR on every sampler, ignoring CONSTANT fcurve settings.
    This post-process step reads the exported GLB, finds scale samplers whose target
    node name contains '_Seg', changes their interpolation to STEP, and rewrites the
    file in-place.  Returns (patched_count, message).
    """
    import struct, json as _json

    with open(glb_path, 'rb') as _f:
        _data = bytearray(_f.read())

    if len(_data) < 20 or struct.unpack_from('<I', _data, 0)[0] != 0x46546C67:
        return 0, "Not a valid GLB file"

    _chunk0_len  = struct.unpack_from('<I', _data, 12)[0]
    _chunk0_type = struct.unpack_from('<I', _data, 16)[0]
    if _chunk0_type != 0x4E4F534A:          # 'JSON'
        return 0, "First GLB chunk is not JSON"

    _gltf = _json.loads(bytes(_data[20:20 + _chunk0_len]).decode('utf-8'))

    _seg_nodes = {
        i for i, n in enumerate(_gltf.get('nodes', []))
        if '_Seg' in n.get('name', '')
    }
    if not _seg_nodes:
        return 0, "No topology-split segment nodes (_Seg) found in this GLB"

    _patched = 0
    for _anim in _gltf.get('animations', []):
        _samplers = _anim.get('samplers', [])
        for _ch in _anim.get('channels', []):
            _t = _ch.get('target', {})
            if _t.get('node') in _seg_nodes and _t.get('path') == 'scale':
                _s = _samplers[_ch['sampler']]
                if _s.get('interpolation') != 'STEP':
                    _s['interpolation'] = 'STEP'
                    _patched += 1

    if _patched == 0:
        return 0, "No scale samplers needed patching (already STEP or none found)"

    # Re-serialise JSON; pad to 4-byte boundary with spaces (valid JSON whitespace).
    _new_json = _json.dumps(_gltf, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    _new_json += b' ' * ((-len(_new_json)) % 4)

    _rest     = bytes(_data[20 + _chunk0_len:])          # BIN chunk — unchanged
    _new_total = 12 + 8 + len(_new_json) + len(_rest)

    _out = bytearray()
    _out += struct.pack('<III', 0x46546C67, 2, _new_total)
    _out += struct.pack('<II',  len(_new_json), 0x4E4F534A)
    _out += _new_json
    _out += _rest

    with open(glb_path, 'wb') as _f:
        _f.write(_out)

    return _patched, f"Patched {_patched} scale sampler(s) to STEP — overwrite written to {glb_path}"


def _merge_glb_animations(glb_path):
    """Merge all animation entries in a GLB into a single 'Scene' animation.

    The Blender glTF exporter in SCENE mode still produces one animation entry
    per object action when objects have separate actions.  This post-process step
    reads the exported GLB, combines every animations[*].channels and
    animations[*].samplers list into one animation named 'Scene', adjusts sampler
    indices accordingly, and rewrites the file in-place.
    Returns (merged_count, message).
    """
    import struct, json as _json

    with open(glb_path, 'rb') as _f:
        _data = bytearray(_f.read())

    if len(_data) < 20 or struct.unpack_from('<I', _data, 0)[0] != 0x46546C67:
        return 0, "Not a valid GLB file"

    _chunk0_len  = struct.unpack_from('<I', _data, 12)[0]
    _chunk0_type = struct.unpack_from('<I', _data, 16)[0]
    if _chunk0_type != 0x4E4F534A:          # 'JSON'
        return 0, "First GLB chunk is not JSON"

    _gltf = _json.loads(bytes(_data[20:20 + _chunk0_len]).decode('utf-8'))

    _anims = _gltf.get('animations', [])
    if len(_anims) <= 1:
        return 0, "Nothing to merge (0 or 1 animation entries)"

    # Build merged animation from all entries
    _merged_samplers = []
    _merged_channels = []

    for _anim in _anims:
        _sampler_offset = len(_merged_samplers)
        _merged_samplers.extend(_anim.get('samplers', []))
        for _ch in _anim.get('channels', []):
            _ch = dict(_ch)                          # shallow copy — don't mutate original
            _ch['sampler'] = _ch['sampler'] + _sampler_offset
            _merged_channels.append(_ch)

    _merged = {
        'name':     'Scene',
        'samplers': _merged_samplers,
        'channels': _merged_channels,
    }
    _gltf['animations'] = [_merged]

    # Re-serialise JSON; pad to 4-byte boundary with spaces (valid JSON whitespace).
    _new_json = _json.dumps(_gltf, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    _new_json += b' ' * ((-len(_new_json)) % 4)

    _rest      = bytes(_data[20 + _chunk0_len:])     # BIN chunk — unchanged
    _new_total = 12 + 8 + len(_new_json) + len(_rest)

    _out = bytearray()
    _out += struct.pack('<III', 0x46546C67, 2, _new_total)
    _out += struct.pack('<II',  len(_new_json), 0x4E4F534A)
    _out += _new_json
    _out += _rest

    with open(glb_path, 'wb') as _f:
        _f.write(_out)

    _n = len(_anims)
    return _n, f"Merged {_n} animation entries into one 'Scene' animation — written to {glb_path}"


def _tag_redraw_3d():
    """Force a redraw of all View3D areas."""
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _fmt_size(n_keys, n_verts):
    """Rough GLB size estimate: each morph target stores position deltas
    (n_verts × 3 × float32) plus the base mesh overhead (~same again).
    Returns a human-readable string."""
    if n_keys <= 0 or n_verts <= 0:
        return ''
    morph_bytes = n_keys * n_verts * 12      # position deltas per shape key
    base_bytes  = n_verts * 32               # positions + normals + UVs for base mesh
    total_mb    = (morph_bytes + base_bytes) / (1024 * 1024)
    if total_mb < 1.0:
        return f"~{total_mb*1024:.0f} KB"
    return f"~{total_mb:.1f} MB"


def _walk_hierarchy(objs, callback, *, follow_parents=True):
    """Visit every unique object reachable from objs via armature modifiers
    (and optionally the parent chain), calling callback(obj) exactly once each.

    follow_parents=True  — includes the full parent chain (world-transform aware)
    follow_parents=False — armature targets only (deform-only traversal)
    """
    scanned = set()

    def visit(obj):
        if obj is None or obj in scanned:
            return
        scanned.add(obj)
        callback(obj)
        if follow_parents:
            visit(obj.parent)
        for mod in getattr(obj, "modifiers", []):
            if mod.type == 'ARMATURE' and mod.object:
                visit(mod.object)

    for obj in objs:
        visit(obj)


def collect_key_times(objs, rng_start, rng_end):
    """
    Walk the full hierarchy (parent chain + armature targets) to collect
    every keyframe time within [rng_start, rng_end].
    If any keyframe exists before rng_start, rng_start is added as a
    synthetic entry so the opening segment is never silently skipped.
    Also walks constraint targets so objects driven by Copy Transforms,
    Follow Path, etc. contribute their keyframe times correctly.
    """
    times          = set()
    has_key_before = False
    _visited_con   = set()   # prevent infinite loops through constraint targets

    def _collect_action(obj):
        nonlocal has_key_before
        if obj.animation_data and obj.animation_data.action:
            for fc in _action_fcurves(obj.animation_data.action):
                for kp in fc.keyframe_points:
                    t = int(round(kp.co[0]))
                    if rng_start <= t <= rng_end:
                        times.add(t)
                    elif t < rng_start:
                        has_key_before = True

    def _visit(obj):
        _collect_action(obj)
        # Walk constraint targets — their keyframes drive this object's transform.
        for _con in getattr(obj, 'constraints', []):
            _tgt = getattr(_con, 'target', None)
            if _tgt is not None and id(_tgt) not in _visited_con:
                _visited_con.add(id(_tgt))
                _collect_action(_tgt)

    _walk_hierarchy(objs, _visit, follow_parents=True)
    if has_key_before:
        times.add(rng_start)
    return times


def collect_deform_fcurves(objs):
    """
    Collect fcurves that drive actual mesh deformation (own action +
    armature modifier targets).  Does NOT walk the parent chain so that
    parent world-transform animation never contaminates the static check.
    """
    result = []

    def _visit(obj):
        if obj.animation_data and obj.animation_data.action:
            result.extend(_action_fcurves(obj.animation_data.action))

    _walk_hierarchy(objs, _visit, follow_parents=False)
    return result


def build_smart_frames(objs, rng_start, rng_end, step):
    """Auto-range frame list: stops at last keyframe, collapses static holds."""
    key_times = collect_key_times(objs, rng_start, rng_end)
    if not key_times:
        return list(range(rng_start, rng_end + 1, step))

    sorted_keys = sorted(key_times)
    last_key    = sorted_keys[-1]
    EPSILON     = 1e-5
    deform_fc   = collect_deform_fcurves(objs)

    def is_static(t1, t2):
        if not deform_fc:
            return True
        return all(abs(fc.evaluate(t1) - fc.evaluate(t2)) < EPSILON
                   for fc in deform_fc)

    frames = set()
    for i, t1 in enumerate(sorted_keys):
        frames.add(t1)
        if i + 1 >= len(sorted_keys):
            break
        t2 = sorted_keys[i + 1]
        if is_static(t1, t2):
            frames.add(t2)
        else:
            f = t1 + step
            while f < t2:
                frames.add(f)
                f += step
            frames.add(t2)

    return sorted(f for f in frames if rng_start <= f <= last_key)


# ------------------------------------------------------------------ #
# Background estimate — chunked timer approach.
# Phase 2 (mesh subdivision) runs a few queue items per timer tick so
# the UI stays live and the spinner actually animates.
# ------------------------------------------------------------------ #

_PREVIEW = {
    'active':          False,
    'collection':      '',   # name of the temp preview collection
    'original_names':  [],   # original object names (hidden during preview)
    'baked_names':     [],   # preview baked object names
    'hash':            None, # settings hash at bake time — used to detect staleness
    'orig_to_baked':   {},   # original name → primary baked object name
    'solo_hidden':     [],   # names of objects hidden by solo mode (restored on exit)
    'topo_groups':     {},   # orig_name → [seg_obj_name, ...] for topology-split objects
    'output_col_name': '',   # output collection name, used by confirm to restore structure
}

_PREVIEW_REFRESH = {
    'pending':     False,   # True while debounce countdown is running
    'last_change': 0.0,     # time.time() of last detected settings change
    'debounce':    0.8,     # seconds to wait after last change before auto-refreshing
    'seen_hash':   None,    # last hash seen (resets countdown on every change)
}


def _preview_settings_hash(scene):
    """Hash of bake-relevant settings without requiring a context (no selection).
    Selection is fixed for the lifetime of a preview — stored in _PREVIEW."""
    return hash((
        scene.frame_start, scene.frame_end,
        scene.frame_preview_start, scene.frame_preview_end,
        scene.use_preview_range,
        scene.glb_bake_step,
        scene.glb_bake_auto_range,
        scene.glb_bake_adaptive,
        round(scene.glb_bake_curve_sensitivity, 3),
        round(scene.glb_bake_mesh_error, 5),
    ))


def _trigger_preview_refresh():
    """Invoke the baker in preview mode, then restore the previously active preview object."""
    # Remember which original the user was inspecting (via its baked counterpart)
    remembered_orig = None
    active = bpy.context.active_object
    if active and _PREVIEW.get('orig_to_baked'):
        for orig_name, baked_name in _PREVIEW['orig_to_baked'].items():
            if baked_name == active.name:
                remembered_orig = orig_name
                break

    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                with bpy.context.temp_override(window=window, area=area):
                    bpy.ops.object.glb_shapekey_baker('EXEC_DEFAULT', preview_mode=True)

                # Restore active object to the new baked counterpart of the remembered original
                if remembered_orig and _PREVIEW.get('orig_to_baked'):
                    new_baked_name = _PREVIEW['orig_to_baked'].get(remembered_orig)
                    new_obj = bpy.data.objects.get(new_baked_name) if new_baked_name else None
                    if new_obj:
                        view_layer = window.view_layer
                        for obj in list(view_layer.objects.selected):
                            obj.select_set(False)
                        new_obj.select_set(True)
                        view_layer.objects.active = new_obj
                return


def _preview_refresh_tick():
    """Timer: detect settings changes and auto-refresh the preview after debounce."""
    if not _PREVIEW['active']:
        _PREVIEW_REFRESH['pending'] = False
        return None  # unregister timer

    scene = bpy.context.scene
    new_hash = _preview_settings_hash(scene)
    prev_pending = _PREVIEW_REFRESH['pending']

    # Track every individual hash change so we can reset the countdown each tick
    if new_hash != _PREVIEW_REFRESH['seen_hash']:
        _PREVIEW_REFRESH['seen_hash'] = new_hash
        if new_hash != _PREVIEW['hash']:
            # Settings differ from last bake — (re)start debounce countdown
            _PREVIEW_REFRESH['pending']     = True
            _PREVIEW_REFRESH['last_change'] = _time.time()
        else:
            # Settings match last bake — nothing to do
            _PREVIEW_REFRESH['pending'] = False

    if _PREVIEW_REFRESH['pending']:
        if _time.time() - _PREVIEW_REFRESH['last_change'] >= _PREVIEW_REFRESH['debounce']:
            _PREVIEW_REFRESH['pending'] = False
            _trigger_preview_refresh()

    # Only force a panel redraw when the pending state changed — avoids
    # constant forced redraws that make sliders feel sticky.
    if _PREVIEW_REFRESH['pending'] != prev_pending:
        _tag_redraw_3d()

    return 0.15  # check every 150 ms

_EST = {
    'status':     'idle',   # 'idle' | 'running' | 'done'
    'result':     '',       # final display string when done
    'found':      0,        # frames found so far (live counter)
    'spinner':    0,        # ticks since start (for spinner animation)
    'hash':       None,     # settings hash that triggered this run
    # internal
    '_queue':          deque(),  # deque of (fa, fb) pairs still to check
    '_final':          set(),
    '_vcache':         {},  # frame → {obj_name: flat float list}
    '_obj_names':      [],  # names of deform objects to evaluate
    '_step':           1,
    '_merr':           0.02,
    '_t0':             0.0,
    '_deform_objs_cache': [],  # filtered deform objects, rebuilt only on hash change
    '_display':      'Estimated: —',  # last computed label text (shown while debouncing)
    '_display_icon': 'INFO',
    '_total_verts':  0,        # total vertex count across all deform objects (for size estimate)
}
_SPIN = ('|', '/', '-', '\\')

_EST_DEBOUNCE = {
    'pending':      False,
    'last_change':  0.0,
    'debounce':     0.8,    # seconds of stability before starting mesh calculation
    'seen_hash':    None,   # last hash seen — resets countdown on every change
    '_last_sel':    None,   # cached selection tuple — stable fallback when context restricted
    'phase1_ready': False,  # True for one tick so spinner renders before computation runs
}



def _est_poll_tick():
    """Polls for settings changes every 250 ms — completely outside draw().

    Hash = scene settings + selection (with cached fallback).
    When bpy.context is restricted in a timer callback, selected_objects raises;
    we fall back to the last known-good selection so the hash stays stable
    (no flicker) without losing selection awareness.
    """
    # During preview we still want the estimate to update when settings change,
    # but we must not run _est_start (which calls frame_set and would fight the
    # preview's own timeline scrubbing).  The flag is checked further down.

    # While phase-2 mesh calculation is active, _est_tick scrubs the timeline
    # via frame_set().  That can make selected_objects return [] temporarily,
    # causing a spurious hash change that kills the running calculation and
    # restarts it — producing the "runs 13 times" symptom.  Skip polling
    # entirely until the calculation finishes.
    if _EST['status'] == 'running':
        return 0.25

    try:
        scene = bpy.context.scene
    except Exception:
        return 0.25

    # --- selection: try live, fall back to cache to prevent hash flicker ---
    try:
        live_sel = tuple(sorted(o.name for o in bpy.context.selected_objects))
        # Don't accept an empty result while we have a cached selection —
        # frame_set() during mesh eval can temporarily suppress context reads.
        if live_sel or _EST_DEBOUNCE['_last_sel'] is None:
            sel_key = live_sel
            _EST_DEBOUNCE['_last_sel'] = sel_key
        else:
            sel_key = _EST_DEBOUNCE['_last_sel']
    except Exception:
        sel_key = _EST_DEBOUNCE['_last_sel']
        if sel_key is None:
            return 0.25   # no cache yet — wait until context is available

    sh = _preview_settings_hash(scene)
    h = hash((sh, sel_key))

    # Reset debounce countdown on every individual hash change (seen_hash pattern).
    # Immediately flip to 'pending' so draw() can show feedback during the wait.
    if h != _EST_DEBOUNCE['seen_hash']:
        _EST_DEBOUNCE['seen_hash'] = h
        _EST['status'] = 'pending'
        _EST_DEBOUNCE['pending']     = True
        _EST_DEBOUNCE['last_change'] = _time.time()
        _tag_redraw_3d()

    if _EST_DEBOUNCE['pending']:
        if _time.time() - _EST_DEBOUNCE['last_change'] < _EST_DEBOUNCE['debounce']:
            return 0.25  # still within debounce window

        # Debounce elapsed — show spinner for one tick BEFORE computation
        _EST_DEBOUNCE['pending']      = False
        _EST_DEBOUNCE['phase1_ready'] = True
        _EST['status']  = 'phase1'
        _EST['spinner'] = (_EST['spinner'] + 1) % len(_SPIN)
        _tag_redraw_3d()
        return 0.05   # come back quickly to run the actual computation

    if _EST_DEBOUNCE['phase1_ready']:
        _EST_DEBOUNCE['phase1_ready'] = False
        try:
            # During preview use the original objects (they have the animation data).
            # Normal mode uses the cached selection.
            if _PREVIEW['active']:
                sel = [o for n in _PREVIEW['original_names']
                       if (o := bpy.data.objects.get(n))]
            else:
                cached_sel = _EST_DEBOUNCE['_last_sel'] or ()
                sel = [o for n in cached_sel if (o := bpy.data.objects.get(n))]
                try:
                    active = bpy.context.active_object
                    if active and active not in sel:
                        sel.append(active)
                except Exception:
                    pass
                if not sel:
                    sel = list(bpy.context.view_layer.active_layer_collection.collection.all_objects)
            def _est_is_deforming(obj):
                """Broader deform check for estimation: includes fcurve-driven,
                constraint-driven, and cache-driven (Alembic, physics, GeoNodes) objects."""
                if collect_deform_fcurves([obj]):
                    return True
                if obj.type == 'MESH':
                    cache_types = {'MESH_SEQUENCE_CACHE', 'NODES', 'CLOTH',
                                   'SOFT_BODY', 'FLUID', 'DYNAMIC_PAINT', 'EXPLODE'}
                    if any(m.type in cache_types for m in obj.modifiers):
                        return True
                if any(getattr(c, 'target', None) is not None
                       for c in getattr(obj, 'constraints', [])):
                    return True
                return False
            deform_objs = [o for o in gather_all_targets(sel) if _est_is_deforming(o)]
            _EST['_deform_objs_cache'] = deform_objs

            # Vertex count for size estimate — use _mesh_from_object so that
            # GeoNodes instances (e.g. String to Curve) are realized before counting.
            try:
                _total = 0
                for _vo in deform_objs:
                    if _vo.type != 'MESH':
                        continue
                    _vm = _mesh_from_object(_vo, bpy.context)
                    _total += len(_vm.vertices)
                    bpy.data.meshes.remove(_vm)
                _EST['_total_verts'] = _total
            except Exception:
                _EST['_total_verts'] = 0

            rng_s = scene.frame_preview_start if scene.use_preview_range else scene.frame_start
            rng_e = scene.frame_preview_end   if scene.use_preview_range else scene.frame_end
            step  = scene.glb_bake_step

            if not deform_objs:
                _EST['_display']      = 'Estimated: 0 shape keys'
                _EST['_display_icon'] = 'INFO'
                _EST['status']        = 'idle'
            elif scene.glb_bake_adaptive:
                candidates = glb_phase1_estimate(scene, deform_objs, rng_s, rng_e, step)
                sz = _fmt_size(len(candidates), _EST['_total_verts'])
                _EST['_display']      = f'Estimated: \u2265 {len(candidates)} shape keys  {sz}'
                _EST['_display_icon'] = 'INFO'
                _EST['status']        = 'idle'
                if scene.glb_bake_live_calc and not _PREVIEW['active']:
                    _est_start(scene, deform_objs, rng_s, rng_e, step, candidates)
            elif scene.glb_bake_auto_range:
                n  = len(build_smart_frames(deform_objs, rng_s, rng_e, step))
                sz = _fmt_size(n, _EST['_total_verts'])
                _EST['_display']      = f'Estimated: {n} shape keys  {sz}'
                _EST['_display_icon'] = 'INFO'
                _EST['status']        = 'idle'
            else:
                n  = len(range(rng_s, rng_e + 1, step))
                sz = _fmt_size(n, _EST['_total_verts'])
                _EST['_display']      = f'Estimated: {n} shape keys  {sz}'
                _EST['_display_icon'] = 'INFO'
                _EST['status']        = 'idle'
        except Exception:
            _EST['_display']      = 'Estimated: —'
            _EST['_display_icon'] = 'INFO'
            _EST['status']        = 'idle'

        # Redraw to surface the result
        _tag_redraw_3d()

    return 0.25


def _est_cache(f):
    """Evaluate and cache world-space vertex positions for frame f."""
    if f in _EST['_vcache']:
        return
    try:
        bpy.context.scene.frame_set(f)
        dg = bpy.context.evaluated_depsgraph_get()
        _EST['_vcache'][f] = {}
        for name in _EST['_obj_names']:
            orig = bpy.data.objects.get(name)
            if orig is None:
                continue
            ev  = orig.evaluated_get(dg)
            n   = len(ev.data.vertices)
            flat = [0.0] * (n * 3)
            ev.data.vertices.foreach_get("co", flat)
            M   = ev.matrix_world
            buf = [0.0] * (n * 3)
            for i in range(n):
                x, y, z = M @ _Vector((flat[i*3], flat[i*3+1], flat[i*3+2]))
                buf[i*3]   = x
                buf[i*3+1] = y
                buf[i*3+2] = z
            _EST['_vcache'][f][name] = buf
    except Exception:
        _EST['_vcache'][f] = {}


def _est_midpoint_err(fa, fb):
    fm = (fa + fb) // 2
    if fm <= fa or fm >= fb:
        return 0.0
    _est_cache(fa)
    _est_cache(fb)
    _est_cache(fm)
    t       = (fm - fa) / (fb - fa)
    max_err = 0.0
    for name in _EST['_obj_names']:
        ca = _EST['_vcache'][fa].get(name)
        cb = _EST['_vcache'][fb].get(name)
        cm = _EST['_vcache'][fm].get(name)
        if not ca or not cb or not cm or len(ca) != len(cm):
            continue
        for i in range(0, len(ca), 3):
            ex = ca[i]     + t * (cb[i]     - ca[i])     - cm[i]
            ey = ca[i + 1] + t * (cb[i + 1] - ca[i + 1]) - cm[i + 1]
            ez = ca[i + 2] + t * (cb[i + 2] - ca[i + 2]) - cm[i + 2]
            err = (ex * ex + ey * ey + ez * ez) ** 0.5
            if err > max_err:
                max_err = err
    return max_err


def _est_tick():
    """Timer callback — processes a few queue items, animates spinner, forces redraw."""
    if _PREVIEW['active']:
        return None
    if _EST['status'] != 'running':
        return None

    ITEMS_PER_TICK = 3
    for _ in range(ITEMS_PER_TICK):
        if not _EST['_queue']:
            break
        fa, fb = _EST['_queue'].popleft()
        if fb - fa <= _EST['_step']:
            continue
        if _est_midpoint_err(fa, fb) > _EST['_merr']:
            fm = (fa + fb) // 2
            _EST['_final'].add(fm)
            _EST['_queue'].append((fa, fm))
            _EST['_queue'].append((fm, fb))

    _EST['found']   = len(_EST['_final'])
    _EST['spinner'] = (_EST['spinner'] + 1) % len(_SPIN)

    if not _EST['_queue']:
        sz             = _fmt_size(_EST['found'], _EST['_total_verts'])
        _EST['result'] = f"{_EST['found']} shape keys  {sz}"
        _EST['status'] = 'done'

    _tag_redraw_3d()

    return 0.05 if _EST['status'] == 'running' else None




def _est_start(scene, objs, rng_s, rng_e, step, candidates=None):
    """Kick off a new background estimate for the given objects and range.
    Pass pre-computed candidates to avoid calling glb_phase1_estimate twice."""
    if candidates is None:
        candidates = glb_phase1_estimate(scene, objs, rng_s, rng_e, step)
    deform_names = [o.name for o in objs if collect_deform_fcurves([o])]

    _EST.update({
        'status':     'running',
        'result':     '',
        'found':      len(candidates),
        'spinner':    0,
        '_queue':     deque((candidates[i], candidates[i + 1])
                            for i in range(len(candidates) - 1)),
        '_final':     set(candidates),
        '_vcache':    {},
        '_obj_names': deform_names,
        '_step':      step,
        '_merr':      scene.glb_bake_mesh_error,
        '_t0':        _time.time(),
    })

    if not bpy.app.timers.is_registered(_est_tick):
        bpy.app.timers.register(_est_tick, first_interval=0.05)


def gather_all_targets(initial_objs):
    """Return initial_objs plus every descendant, mirroring execute()'s hierarchy walk."""
    all_targets = set(initial_objs)

    def gather_children(obj):
        for child in obj.children:
            all_targets.add(child)
            gather_children(child)

    for obj in initial_objs:
        gather_children(obj)
    return list(all_targets)


def find_animation_start(objs):
    """Return the minimum keyframe time across all objects (no lower bound).
    Used to extend baking into negative frame numbers when auto range is on."""
    min_t = None

    def _visit(obj):
        nonlocal min_t
        if obj.animation_data and obj.animation_data.action:
            for fc in _action_fcurves(obj.animation_data.action):
                for kp in fc.keyframe_points:
                    t = int(round(kp.co[0]))
                    if min_t is None or t < min_t:
                        min_t = t

    _walk_hierarchy(objs, _visit, follow_parents=True)
    return min_t


def find_animation_end(objs):
    """Return the maximum keyframe time across all objects (no upper bound).
    Used to extend baking beyond the preview range end when auto range is on."""
    max_t = None

    def _visit(obj):
        nonlocal max_t
        if obj.animation_data and obj.animation_data.action:
            for fc in _action_fcurves(obj.animation_data.action):
                for kp in fc.keyframe_points:
                    t = int(round(kp.co[0]))
                    if max_t is None or t > max_t:
                        max_t = t

    _walk_hierarchy(objs, _visit, follow_parents=True)
    return max_t


def glb_phase1_estimate(scene, objs, rng_start, rng_end, step):
    """
    Fast frame-count estimate for adaptive mode (Phase 1 only — no mesh eval).
    Returns the candidate frame list produced by the fcurve curvature filter.
    Phase 2 (mesh subdivision) may add more frames on top of this.
    """
    key_times   = collect_key_times(objs, rng_start, rng_end)
    last_key    = max(key_times) if key_times else rng_end
    sorted_keys = sorted(key_times) if key_times else [rng_start, rng_end]
    all_fc      = collect_deform_fcurves(objs)
    c_thresh    = 10 ** ((1.0 - scene.glb_bake_curve_sensitivity) * 4 - 4)

    def max_curvature(f):
        if not all_fc:
            return float('inf')
        return max(abs(fc.evaluate(f + 1) - 2.0 * fc.evaluate(f) + fc.evaluate(f - 1))
                   for fc in all_fc)

    candidates = set(sorted_keys)
    for i in range(len(sorted_keys) - 1):
        t1, t2 = sorted_keys[i], sorted_keys[i + 1]
        for f in range(t1 + step, t2, step):
            if max_curvature(f) > c_thresh:
                candidates.add(f)

    return sorted(f for f in candidates if rng_start <= f <= last_key)



class OBJECT_OT_glb_shapekey_baker(bpy.types.Operator):
    bl_idname = "object.glb_shapekey_baker"
    bl_label = "Bake Hierarchy to GLB"
    bl_description = "Bakes selected objects or active collection to shape keys"
    bl_options = {'REGISTER', 'UNDO'}

    preview_mode: bpy.props.BoolProperty(name="Preview Mode", default=False, options={'SKIP_SAVE'})

    def invoke(self, context, event):
        if not self.preview_mode and context.scene.glb_bake_delete_originals:
            return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context):
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        scene = context.scene
        FRAME_STEP = scene.glb_bake_step
        if scene.use_preview_range:
            start = scene.frame_preview_start
            end   = scene.frame_preview_end
        else:
            start = scene.frame_start
            end   = scene.frame_end
        
        initial_targets = set()

        # 1. SMART TARGETING
        # When refreshing a preview, always use the objects from the original
        # preview run — they are currently hidden so select_get() would miss them.
        if self.preview_mode and _PREVIEW['active'] and _PREVIEW['original_names']:
            for name in _PREVIEW['original_names']:
                obj = bpy.data.objects.get(name)
                if obj:
                    initial_targets.add(obj)
            if not initial_targets:
                self.report({'ERROR'}, "Original preview objects no longer exist.")
                return {'CANCELLED'}
        else:
            # Normal selection logic.
            # select_get() returns False for objects with hide_viewport=True, so a
            # hidden object clicked in the Outliner (making it active) would be missed.
            # We also check context.active_object: if it wasn't captured by select_get()
            # it means it's a hidden-but-intentionally-chosen object — include it.
            explicitly_selected = [obj for obj in context.view_layer.objects if obj.select_get()]
            # view_layer.objects.active works for hidden objects too;
            # context.active_object can return None when hide_viewport=True.
            active = context.view_layer.objects.active
            if active is not None and active not in explicitly_selected:
                explicitly_selected = list(explicitly_selected) + [active]
            if explicitly_selected:
                self.report({'INFO'}, "Objects selected. Using viewport selection.")
                for obj in explicitly_selected:
                    initial_targets.add(obj)
            else:
                # Nothing selected and no active — fall back to active collection.
                # all_objects is used (not objects) so hidden objects are included.
                active_col = context.view_layer.active_layer_collection.collection
                self.report({'INFO'}, f"No selection. Using Active Collection: '{active_col.name}'")
                for obj in active_col.all_objects:
                    initial_targets.add(obj)
                
        if not initial_targets:
            self.report({'ERROR'}, "Nothing to bake! Select an object or a collection.")
            return {'CANCELLED'}
            
        # 2. Gather Hierarchy
        all_targets = set(initial_targets)
        def gather_children(obj):
            for child in obj.children:
                all_targets.add(child)
                gather_children(child) 
                
        for obj in initial_targets:
            gather_children(obj)
            
        valid_types = {'MESH', 'CURVE', 'FONT', 'SURFACE', 'META'}
        orig_objs = [o for o in all_targets if o.type in valid_types]
        
        if not orig_objs:
            self.report({'ERROR'}, "No valid geometry found to bake.")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Auto-Gathered {len(orig_objs)} objects. Starting Bake...")

        # 3. Setup Duplication
        # Set the scene to the start frame before duplicating so that
        # bpy.ops.object.convert applies modifiers (SubDiv, GeoNodes, etc.)
        # at the correct pose — not wherever the user's playhead happens to be.
        scene.frame_set(start)

        for i, obj in enumerate(orig_objs):
            obj["_bake_id"] = i

        try:
            bpy.ops.object.select_all(action='DESELECT')
            for obj in orig_objs:
                obj.hide_set(False)
                obj.hide_viewport = False
                obj.select_set(True)

            context.view_layer.objects.active = orig_objs[0]

            bpy.ops.object.duplicate(linked=False)
            # Bug 1 fix: snapshot the list immediately to avoid live-reference issues
            baked_objs = list(context.selected_objects)

        except Exception as e:
            # Bug 6 fix: clean up _bake_id from originals if duplication fails
            for obj in orig_objs:
                if "_bake_id" in obj:
                    del obj["_bake_id"]
            self.report({'ERROR'}, f"Duplication failed: {e}")
            return {'CANCELLED'}

        output_col_name = scene.glb_bake_output_name.strip() or "GLB_Baked_Export"
        if output_col_name not in bpy.data.collections:
            output_col = bpy.data.collections.new(output_col_name)
            scene.collection.children.link(output_col)
        else:
            output_col = bpy.data.collections[output_col_name]

        # 4. Map & Clean Duplicates
        bake_map = {}
        try:
            for baked in baked_objs:
                if "_bake_id" in baked:
                    idx = baked["_bake_id"]
                    orig = orig_objs[idx]
                    bake_map[orig] = baked

                    del baked["_bake_id"]
                    del orig["_bake_id"]

                    for col in list(baked.users_collection):
                        col.objects.unlink(baked)
                    output_col.objects.link(baked)

                    baked.name = orig.name + "_GLB_Baked"

                    # Evaluate the *original* object to get the correct mesh.
                    # Using new_from_object on the original is reliable for all
                    # modifier types including Geometry Nodes with curve/point
                    # output — bpy.ops.object.convert can silently produce empty
                    # geometry for those in Blender 5.1.
                    _init_mesh = _mesh_from_object(orig, context)

                    # If the baked copy is not a MESH (e.g. Curve, Text, Meta)
                    # we still need to change its type before assigning mesh data.
                    if baked.type != 'MESH':
                        bpy.ops.object.select_all(action='DESELECT')
                        baked.select_set(True)
                        context.view_layer.objects.active = baked
                        bpy.ops.object.convert(target='MESH')

                    # Replace the baked object's mesh data with the evaluated result.
                    _old_data  = baked.data
                    baked.data = _init_mesh
                    if _old_data.users == 0:
                        bpy.data.meshes.remove(_old_data)

                    for mod in list(baked.modifiers):
                        baked.modifiers.remove(mod)
                    for const in list(baked.constraints):
                        baked.constraints.remove(const)
                    baked.animation_data_clear()

                    # Bug 2 fix: clear any inherited shape keys before adding a fresh Basis
                    if baked.data.shape_keys:
                        baked.shape_key_clear()
                    baked.shape_key_add(name="Basis")

                    # Flatten hierarchy: clear parent while preserving world transform.
                    # Required for two reasons:
                    # (a) deform objects encode full world-space motion in shape keys —
                    #     an animated baked parent would double-transform the mesh.
                    # (b) transform keyframes on non-deform objects are set via
                    #     matrix_world; if the parent isn't at the right position yet
                    #     when the child is processed, Blender computes wrong locals → jitter.
                    _baked_world = baked.matrix_world.copy()
                    baked.parent = None
                    baked.matrix_world = _baked_world

        except Exception as e:
            # Bug 6 fix: clean up any remaining _bake_id on originals if mapping fails
            for obj in orig_objs:
                if "_bake_id" in obj:
                    del obj["_bake_id"]
            self.report({'ERROR'}, f"Setup failed: {e}")
            return {'CANCELLED'}

        # Bug 5 fix: report error if nothing was mapped
        if not bake_map:
            self.report({'ERROR'}, "Failed to map any objects for baking. Duplicate may not have preserved custom properties.")
            return {'CANCELLED'}

        # 5. Build frame list
        # _action_fcurves, collect_key_times, collect_deform_fcurves,
        # build_smart_frames, and glb_phase1_estimate are all module-level
        # functions so draw() can also call them for the live frame preview.

        # Precompute which originals actually need shape keys.
        # Primary: fcurve-based — catches armature deformation, shape key animation,
        # and directly-keyed modifier properties.
        deform_objs   = {orig for orig in bake_map if collect_deform_fcurves([orig])}
        # Objects that need transform keyframes: any animation anywhere in their
        # hierarchy (own action, parent chain, armature target).
        animated_objs = {orig for orig in bake_map if collect_key_times([orig], start, end)}

        # Secondary: evaluated-mesh comparison — catches implicit deformers that have
        # no fcurves on the object itself: Geometry Nodes driven by Scene Time,
        # physics caches, drivers sourced from other objects, etc.
        # Sample 5 evenly-spaced frames and compare world-space vertex positions.
        _span = max(end - start, 1)
        _sample_frames = sorted({
            start,
            start + _span // 4,
            (start + end) // 2,
            start + 3 * _span // 4,
            end,
        })
        _sample_verts   = {}   # orig → (n_verts, flat_coords) at first sample
        _sample_mats    = {}   # orig → matrix_world at first sample
        for _sf in _sample_frames:
            scene.frame_set(_sf)
            _dg = context.evaluated_depsgraph_get()
            for orig in list(bake_map.keys()):
                ev = orig.evaluated_get(_dg)
                # --- mesh deformation check (local-space vertices) ---
                if orig not in deform_objs and orig.type == 'MESH':
                    # GeoNodes may output curve/point geometry — .data.vertices
                    # is empty in that case.  Use new_from_object instead so the
                    # result is always a proper mesh regardless of output type.
                    if any(m.type == 'NODES' for m in orig.modifiers):
                        _samp_tmp = _mesh_from_object(orig, context)
                        n   = len(_samp_tmp.vertices)
                        buf = [0.0] * (n * 3)
                        _samp_tmp.vertices.foreach_get("co", buf)
                        bpy.data.meshes.remove(_samp_tmp)
                    else:
                        n   = len(ev.data.vertices)
                        buf = [0.0] * (n * 3)
                        ev.data.vertices.foreach_get("co", buf)
                    if orig not in _sample_verts:
                        _sample_verts[orig] = (n, buf)
                    else:
                        n_ref, buf_ref = _sample_verts[orig]
                        if n != n_ref or any(
                                abs(buf[i] - buf_ref[i]) > 1e-5
                                for i in range(n_ref * 3)):
                            deform_objs.add(orig)
                # --- world-matrix change check (constraint-driven transforms) ---
                if orig not in animated_objs and getattr(orig, 'constraints', None):
                    M = ev.matrix_world
                    if orig not in _sample_mats:
                        _sample_mats[orig] = M.copy()
                    else:
                        M_ref = _sample_mats[orig]
                        if any(abs(M[r][c] - M_ref[r][c]) > 1e-5
                               for r in range(4) for c in range(4)):
                            animated_objs.add(orig)
        del _sample_verts, _sample_mats

        # ------------------------------------------------------------------ #
        # Adaptive density: two-factor frame list builder.
        # Phase 1 (fast)  — fcurve second-derivative (curvature) pre-filter.
        # Phase 2 (exact) — recursive mesh midpoint subdivision: evaluates the
        #                    actual deformed mesh at a candidate midpoint and
        #                    compares it to the linearly-interpolated pose; if
        #                    the max vertex error exceeds the threshold the
        #                    midpoint is kept and both halves are recursed.
        # ------------------------------------------------------------------ #
        def build_adaptive_frames(rng_start, rng_end, step):
            key_times = collect_key_times(list(all_targets), rng_start, rng_end)

            if scene.glb_bake_auto_range and key_times:
                # Stop at the last keyframe, same as Auto Range behaviour.
                last_key    = max(key_times)
                sorted_keys = sorted(key_times)
            else:
                # Respect the manual range: run all the way to rng_end.
                # Add rng_start/rng_end as anchors so Phase 1 curvature check
                # covers the full range even when keyframes are sparse.
                last_key    = rng_end
                sorted_keys = sorted(key_times | {rng_start, rng_end})

            all_fc   = collect_deform_fcurves(list(bake_map.keys()))
            # Map curve_sensitivity [0..1] → curvature threshold on a log scale.
            # 0 → 1e-4 (very sensitive, many keys)  1 → 1.0 (loose, few keys)
            c_thresh = 10 ** ((1.0 - scene.glb_bake_curve_sensitivity) * 4 - 4)

            def max_curvature(f):
                if not all_fc:
                    return 0.0   # no fcurves — skip Phase 1, let Phase 1b/2 handle it
                return max(
                    abs(fc.evaluate(f + 1) - 2.0 * fc.evaluate(f) + fc.evaluate(f - 1))
                    for fc in all_fc
                )

            # --- Phase 1: curvature pre-filter ---
            candidates = set(sorted_keys)
            for i in range(len(sorted_keys) - 1):
                t1, t2 = sorted_keys[i], sorted_keys[i + 1]
                for f in range(t1 + step, t2, step):
                    if max_curvature(f) > c_thresh:
                        candidates.add(f)

            sorted_cands = sorted(candidates)

            # --- Phase 2: mesh midpoint subdivision ---
            vert_cache = {}   # frame → {orig: flat float list of world-space coords}

            def cache_verts(f):
                if f in vert_cache:
                    return
                scene.frame_set(f)
                dg = context.evaluated_depsgraph_get()
                vert_cache[f] = {}
                for orig in deform_objs:
                    if orig not in bake_map:
                        continue
                    ev  = orig.evaluated_get(dg)
                    M   = ev.matrix_world.copy()
                    tmp = _mesh_from_object(orig, context)
                    buf = []
                    for v in tmp.vertices:
                        wco = M @ v.co
                        buf += [wco.x, wco.y, wco.z]
                    bpy.data.meshes.remove(tmp)
                    vert_cache[f][orig] = buf

            def midpoint_error(fa, fb):
                fm = (fa + fb) // 2
                if fm <= fa or fm >= fb:
                    return 0.0
                cache_verts(fa)
                cache_verts(fb)
                cache_verts(fm)
                t       = (fm - fa) / (fb - fa)
                max_err = 0.0
                for orig in deform_objs:
                    ca = vert_cache[fa].get(orig)
                    cb = vert_cache[fb].get(orig)
                    cm = vert_cache[fm].get(orig)
                    if not ca or not cb or not cm or len(ca) != len(cb) or len(ca) != len(cm):
                        continue
                    for i in range(0, len(ca), 3):
                        ex = ca[i]     + t * (cb[i]     - ca[i])     - cm[i]
                        ey = ca[i + 1] + t * (cb[i + 1] - ca[i + 1]) - cm[i + 1]
                        ez = ca[i + 2] + t * (cb[i + 2] - ca[i + 2]) - cm[i + 2]
                        err = (ex * ex + ey * ey + ez * ez) ** 0.5
                        if err > max_err:
                            max_err = err
                return max_err

            def max_displacement(fa, fb):
                """Max Euclidean distance any vertex travels from fa to fb.
                Uses the same vert_cache as midpoint_error (no extra eval)."""
                cache_verts(fa)
                cache_verts(fb)
                max_d = 0.0
                for orig in deform_objs:
                    ca = vert_cache[fa].get(orig)
                    cb = vert_cache[fb].get(orig)
                    if not ca or not cb or len(ca) != len(cb):
                        continue
                    for i in range(0, len(ca), 3):
                        dx = cb[i]   - ca[i]
                        dy = cb[i+1] - ca[i+1]
                        dz = cb[i+2] - ca[i+2]
                        d  = (dx*dx + dy*dy + dz*dz) ** 0.5
                        if d > max_d:
                            max_d = d
                return max_d

            def effective_error(fa, fb):
                """Error metric used by Phase 1b and Phase 2.
                For fcurve-based objects: absolute midpoint error (existing behaviour).
                For cache-driven objects (Alembic, physics, GeoNodes):
                  normalise by the interval's max vertex displacement so the
                  threshold represents non-linearity as a fraction of motion
                  rather than an absolute world-space distance.  This makes
                  threshold 0.1 mean 'allow up to 10% curvature' regardless
                  of how fast the cloth is moving, giving a smooth and
                  intuitive relationship between the slider and frame count."""
                err = midpoint_error(fa, fb)
                if all_fc:
                    return err          # fcurve objects: keep absolute semantics
                disp = max_displacement(fa, fb)
                return err / max(1.0, disp)   # cache objects: fraction of motion

            # --- Phase 1b: coarse mesh scan for Alembic / no-fcurve objects ---
            # When Phase 1 produced no fcurve candidates, seed Phase 2 with a
            # coarse vertex-velocity pass (~30 evenly-spaced mesh evaluations).
            # High-error coarse intervals get their midpoint added as a candidate
            # so Phase 2 subdivides them further.  Low-error intervals only get
            # their endpoints — Phase 2 evaluates once, confirms error is within
            # threshold, and stops, producing very few keys for slow sections.
            if not all_fc and len(sorted_cands) <= 2:
                _n_frames    = max(last_key - rng_start, 1)
                _coarse_step = max(step, _n_frames // 30)
                _cframes     = sorted(
                    {rng_start} |
                    set(range(rng_start + _coarse_step, last_key, _coarse_step)) |
                    {last_key}
                )
                for _ci in range(len(_cframes) - 1):
                    _fa, _fb = _cframes[_ci], _cframes[_ci + 1]
                    candidates.add(_fa)
                    candidates.add(_fb)
                    if midpoint_error(_fa, _fb) > scene.glb_bake_mesh_error:
                        candidates.add((_fa + _fb) // 2)
                sorted_cands = sorted(candidates)

            final = set(sorted_cands)

            def subdivide(fa, fb):
                if fb - fa <= step:
                    return
                err = midpoint_error(fa, fb)
                if err > scene.glb_bake_mesh_error:
                    fm = (fa + fb) // 2
                    final.add(fm)
                    subdivide(fa, fm)
                    subdivide(fm, fb)

            for i in range(len(sorted_cands) - 1):
                subdivide(sorted_cands[i], sorted_cands[i + 1])

            return sorted(f for f in final if rng_start <= f <= last_key)

        # --- Choose frame list strategy ---
        # When auto range or adaptive is active, extend start backwards into
        # negative frame numbers if keyframes exist before scene.frame_start.
        if scene.glb_bake_adaptive or scene.glb_bake_auto_range:
            anim_start = find_animation_start(list(all_targets))
            if anim_start is not None and anim_start < start:
                start = anim_start
            anim_end = find_animation_end(list(all_targets))
            if anim_end is not None and anim_end > end:
                end = anim_end

        # Build topo helpers early — needed both for the adaptive topology scan
        # below and for the segment-setup loop later.
        _has_nodes_cache = {o: any(m.type == 'NODES' for m in o.modifiers)
                            for o in deform_objs}

        def _topo_key(orig, mesh):
            """Topology fingerprint: (n_verts, n_polys, n_edges) + connectivity hash
            for NODES objects so that same-count but different-connectivity topologies
            (e.g. '6' vs '9', or any two different GeoNodes string outputs) are
            treated as distinct segments."""
            nv  = len(mesh.vertices)
            np_ = len(mesh.polygons)
            ne  = len(mesh.edges)
            if not _has_nodes_cache.get(orig) or nv == 0:
                return (nv, np_, ne)
            n_loops = len(mesh.loops)
            lv_buf  = [0] * n_loops
            mesh.loops.foreach_get("vertex_index", lv_buf)
            return (nv, np_, ne, hash(tuple(lv_buf)))

        if scene.glb_bake_adaptive:
            bake_frames = build_adaptive_frames(start, end, FRAME_STEP)
            if not bake_frames:
                bake_frames = list(range(start, end + 1, FRAME_STEP))

            # Topology-change補正: for GeoNodes objects, adaptive may have missed
            # frames where the string/counter switches to a different character.
            # Scan all frames at FRAME_STEP and add any frame where the topo_key
            # changes from the previous frame so every topology boundary is present.
            _topo_nodes_objs = [o for o in deform_objs
                                if any(m.type == 'NODES' for m in o.modifiers)]
            if _topo_nodes_objs:
                _extra = set()
                for _tno in _topo_nodes_objs:
                    _prev_tk = None
                    for _tf in range(start, end + 1, FRAME_STEP):
                        scene.frame_set(_tf)
                        _tk_mesh = _mesh_from_object(_tno, context)
                        _tk = _topo_key(_tno, _tk_mesh)
                        bpy.data.meshes.remove(_tk_mesh)
                        if _prev_tk is not None and _tk != _prev_tk:
                            _extra.add(_tf)
                        _prev_tk = _tk
                if _extra:
                    bake_frames = sorted(set(bake_frames) | _extra)
                    self.report({'INFO'},
                        f"Topology scan added {len(_extra)} boundary frame(s) for GeoNodes objects.")

            self.report({'INFO'}, f"Adaptive: {len(bake_frames)} frames (last key at {bake_frames[-1]}).")
        elif scene.glb_bake_auto_range:
            bake_frames = build_smart_frames(list(all_targets), start, end, FRAME_STEP)
            if not bake_frames:
                bake_frames = list(range(start, end + 1, FRAME_STEP))
            self.report({'INFO'}, f"Auto Range: {len(bake_frames)} frames (last key at {bake_frames[-1]}).")
        else:
            bake_frames = list(range(start, end + 1, FRAME_STEP))

        # 6. Topology-segment setup + M_ref (Approach 2).
        #
        # For objects with constant vertex count throughout the animation, this is
        # the standard single-baked-object path.
        #
        # For objects whose vertex count changes mid-animation (animated SubDiv
        # level, Geometry Nodes driven by Scene Time, etc.) we:
        #   • Scan every bake frame to find contiguous same-count segments.
        #   • Duplicate the original at the first frame of each extra segment,
        #     apply modifiers there, and set up a fresh baked object.
        #   • Bake shape keys into the correct segment object per frame.
        #   • Animate hide_render / hide_viewport so only the right object is
        #     visible at each point in time.
        #
        # bake_segs[orig] = [(seg_frames, seg_baked, seg_M_ref), ...]

        def _setup_seg(orig, seg_frames, seg_baked):
            """Find first invertible frame, lock world matrix, rebuild Basis. Returns M."""
            ref_f = seg_frames[0]
            for cand in seg_frames:
                scene.frame_set(cand)
                ev_c = orig.evaluated_get(context.evaluated_depsgraph_get())
                if abs(ev_c.matrix_world.determinant()) > 1e-6:
                    ref_f = cand
                    break
            scene.frame_set(ref_f)
            ev_r  = orig.evaluated_get(context.evaluated_depsgraph_get())
            seg_M = ev_r.matrix_world.copy()
            seg_baked.matrix_world = seg_M
            tmp = _mesh_from_object(orig, context)

            # Always replace the mesh data block so the Basis mesh is consistent
            # with ref_f (vertex positions AND any auto-smooth/sharp data from the
            # evaluated object at that frame).
            if seg_baked.data.shape_keys:
                seg_baked.shape_key_clear()
            old_data = seg_baked.data
            seg_baked.data = tmp
            if old_data.users == 0:
                bpy.data.meshes.remove(old_data)
            seg_baked.shape_key_add(name="Basis")
            return seg_M

        bake_segs       = {}   # orig → [(seg_frames, seg_baked, seg_M), ...]
        extra_baked_objs = []  # segment duplicates created below (for post-processing)
        M_ref           = {}   # orig → primary segment M (kept for non-segment path)

        for orig in deform_objs:
            primary_baked = bake_map[orig]

            # Scan all bake_frames for this object to find topology segments.
            # Uses a multi-tier topology key: (n_verts, n_polys, n_edges) catches
            # most changes; a coarse spatial hash catches same-count cases like
            # different digit shapes in a GeoNodes number counter.
            seg_groups = []          # [(topo_key, [frame, ...]), ...]
            cur_k, cur_frames = None, []
            for f in bake_frames:
                scene.frame_set(f)
                dg = context.evaluated_depsgraph_get()
                if _has_nodes_cache.get(orig):
                    _seg_tmp = _mesh_from_object(orig, context)
                    k = _topo_key(orig, _seg_tmp)
                    bpy.data.meshes.remove(_seg_tmp)
                else:
                    _ev_seg = orig.evaluated_get(dg)
                    _seg_tmp = bpy.data.meshes.new_from_object(_ev_seg)
                    k = _topo_key(orig, _seg_tmp)
                    bpy.data.meshes.remove(_seg_tmp)
                if cur_k is None or k == cur_k:
                    cur_k = k
                    cur_frames.append(f)
                else:
                    seg_groups.append((cur_k[0], cur_frames))
                    cur_k, cur_frames = k, [f]
            if cur_frames:
                seg_groups.append((cur_k[0] if cur_k is not None else 0, cur_frames))

            if len(seg_groups) == 1:
                seg_M = _setup_seg(orig, bake_frames, primary_baked)
                M_ref[orig]      = seg_M
                bake_segs[orig]  = [(bake_frames, primary_baked, seg_M)]
            else:
                self.report({'INFO'},
                    f"'{orig.name}': {len(seg_groups)} topology segments detected — "
                    f"splitting into separate objects with scale visibility animation.")

                # Create a sub-collection named after the original to hold all
                # segments together, nested inside the main output collection.
                sub_col_name = orig.name
                if sub_col_name in bpy.data.collections:
                    sub_col = bpy.data.collections[sub_col_name]
                else:
                    sub_col = bpy.data.collections.new(sub_col_name)
                if sub_col.name not in [c.name for c in output_col.children]:
                    output_col.children.link(sub_col)

                # Move the already-created primary baked object into the sub-collection
                # and rename it to Seg1 so all segments follow a consistent scheme.
                primary_baked.name = f"{orig.name}_Seg1"
                for col in list(primary_baked.users_collection):
                    col.objects.unlink(primary_baked)
                sub_col.objects.link(primary_baked)

                segs_out = []
                for seg_idx, (_, seg_flist) in enumerate(seg_groups):
                    if seg_idx == 0:
                        seg_baked = primary_baked
                    else:
                        # Duplicate original at this segment's first frame.
                        scene.frame_set(seg_flist[0])
                        bpy.ops.object.select_all(action='DESELECT')
                        orig_obj = bpy.data.objects.get(orig.name)
                        if orig_obj:
                            orig_obj.hide_set(False)
                            orig_obj.select_set(True)
                            context.view_layer.objects.active = orig_obj
                        bpy.ops.object.duplicate(linked=False)
                        seg_baked = context.selected_objects[0]

                        bpy.ops.object.select_all(action='DESELECT')
                        seg_baked.select_set(True)
                        context.view_layer.objects.active = seg_baked
                        bpy.ops.object.convert(target='MESH')

                        for mod in list(seg_baked.modifiers):
                            seg_baked.modifiers.remove(mod)
                        for con in list(seg_baked.constraints):
                            seg_baked.constraints.remove(con)
                        seg_baked.animation_data_clear()
                        if seg_baked.data.shape_keys:
                            seg_baked.shape_key_clear()
                        seg_baked.shape_key_add(name="Basis")
                        _w = seg_baked.matrix_world.copy()
                        seg_baked.parent = None
                        seg_baked.matrix_world = _w
                        seg_baked.name = f"{orig.name}_Seg{seg_idx + 1}"
                        for col in list(seg_baked.users_collection):
                            col.objects.unlink(seg_baked)
                        sub_col.objects.link(seg_baked)
                        extra_baked_objs.append(seg_baked)

                    seg_M = _setup_seg(orig, seg_flist, seg_baked)
                    if seg_idx == 0:
                        M_ref[orig] = seg_M
                    segs_out.append((seg_flist, seg_baked, seg_M))
                bake_segs[orig] = segs_out

        # Fast per-frame lookup: (orig, frame) → (seg_baked, seg_M)
        _frame_seg = {}
        for orig, segs in bake_segs.items():
            for seg_flist, seg_baked, seg_M in segs:
                for f in seg_flist:
                    _frame_seg[(id(orig), f)] = (seg_baked, seg_M)

        # Bridge-frame lookup: (id(orig), first_f) → prev_last_f.
        # At the first frame of every non-first segment the shape key content
        # is baked from the PREVIOUS segment's last evaluated frame (projected
        # onto the new segment's basis via BVH).  This makes the new segment
        # appear identical to the old one the instant the scale switch fires,
        # then morph naturally toward its own geometry from the next keyframe.
        _seg_bridge = {}
        for _bo, _segs in bake_segs.items():
            for _si, (_sfl, _, _) in enumerate(_segs):
                if _si > 0:
                    _seg_bridge[(id(_bo), _sfl[0])] = _segs[_si - 1][0][-1]

        # 7. The Bake Loop
        for i, f in enumerate(bake_frames):
            scene.frame_set(f)
            depsgraph = context.evaluated_depsgraph_get()

            prev_bake = bake_frames[i - 1] if i > 0 else bake_frames[0] - 1
            next_bake = bake_frames[i + 1] if i + 1 < len(bake_frames) else None

            for orig, primary_baked in bake_map.items():
                eval_orig = orig.evaluated_get(depsgraph)

                if orig in deform_objs:
                    entry = _frame_seg.get((id(orig), f))
                    if entry is None:
                        continue
                    baked, seg_M = entry

                    M_world   = eval_orig.matrix_world.copy()
                    temp_mesh = _mesh_from_object(orig, context)
                    M_ref_inv = seg_M.inverted_safe()

                    # Determine source mesh and projection mode.
                    # Bridge frames: project the previous segment's last-frame
                    # geometry onto this segment's basis so the instant scale
                    # switch is visually seamless — the new segment appears
                    # identical to the old one, then morphs to its own shape.
                    _bridge_key = (id(orig), f)
                    if _bridge_key in _seg_bridge:
                        _prev_lf     = _seg_bridge[_bridge_key]
                        scene.frame_set(_prev_lf)
                        _bdg_dg      = context.evaluated_depsgraph_get()
                        _bdg_eval    = orig.evaluated_get(_bdg_dg)
                        _src_M_world = _bdg_eval.matrix_world.copy()
                        _src_mesh    = _mesh_from_object(orig, context)
                        scene.frame_set(f)
                        depsgraph    = context.evaluated_depsgraph_get()
                        _use_bvh     = True
                        _free_src    = True
                    else:
                        _src_mesh    = temp_mesh
                        _src_M_world = M_world
                        _use_bvh     = any(m.type == 'REMESH' for m in orig.modifiers)
                        _free_src    = False

                    _vert_ok = (len(temp_mesh.vertices) == len(baked.data.vertices)
                                or _use_bvh)

                    if _vert_ok:
                        prefix    = scene.glb_bake_shape_key_prefix
                        shape_key = baked.shape_key_add(name=f"{prefix}{f:03d}")

                        if _use_bvh:
                            # BVH surface projection: for remesh objects (unstable
                            # vertex ordering) and bridge frames (cross-topology).
                            _n_v     = len(baked.data.vertices)
                            _basis_d = baked.data.shape_keys.key_blocks["Basis"].data
                            _bvh_vs  = [(_src_M_world @ v.co)[:]
                                        for v in _src_mesh.vertices]
                            _bvh_ps  = [tuple(p.vertices)
                                        for p in _src_mesh.polygons]
                            _bvh     = _BVHTree.FromPolygons(_bvh_vs, _bvh_ps)
                            coords   = [0.0] * (_n_v * 3)
                            for _i in range(_n_v):
                                _bc      = _basis_d[_i].co
                                _world_q = seg_M @ _Vector((_bc[0], _bc[1], _bc[2]))
                                _hit     = _bvh.find_nearest(_world_q)
                                _world_p = _hit[0] if _hit[0] is not None else _world_q
                                _co      = M_ref_inv @ _world_p
                                coords[_i*3]   = _co[0]
                                coords[_i*3+1] = _co[1]
                                coords[_i*3+2] = _co[2]
                        else:
                            # Direct vertex-index mapping for regular animation.
                            coords = []
                            for v in _src_mesh.vertices:
                                coords.extend(M_ref_inv @ (_src_M_world @ v.co))

                        shape_key.data.foreach_set("co", coords)

                    if _free_src:
                        bpy.data.meshes.remove(_src_mesh)

                    if _vert_ok:
                        shape_key.value = 0.0
                        shape_key.keyframe_insert(data_path='value', frame=prev_bake)
                        shape_key.value = 1.0
                        shape_key.keyframe_insert(data_path='value', frame=f)
                        if next_bake is not None:
                            # Skip the weight=0 keyframe when next_bake belongs to
                            # a different topology segment object.  Without this,
                            # the viewer linearly morphs the last shape key from 1→0
                            # during the 1-frame STEP scale-switch window, snapping
                            # the mesh back toward the Basis (first-frame) pose —
                            # visible as a stutter.  CONSTANT fcurve extrapolation
                            # (set after _set_linear) holds the weight at 1 through
                            # the transition; the STEP scale hides the object before
                            # any wrong state becomes visible.
                            _next_entry = _frame_seg.get((id(orig), next_bake))
                            _cross_seg  = (_next_entry is not None
                                           and _next_entry[0] is not baked)
                            if not _cross_seg:
                                shape_key.value = 0.0
                                shape_key.keyframe_insert(data_path='value',
                                                          frame=next_bake)

                    bpy.data.meshes.remove(temp_mesh)

                else:
                    if orig in animated_objs:
                        baked = primary_baked
                        baked.matrix_world = eval_orig.matrix_world.copy()
                        baked.keyframe_insert(data_path="location", frame=f)
                        if baked.rotation_mode == 'QUATERNION':
                            baked.keyframe_insert(data_path="rotation_quaternion", frame=f)
                        elif baked.rotation_mode == 'AXIS_ANGLE':
                            baked.keyframe_insert(data_path="rotation_axis_angle", frame=f)
                        else:
                            baked.keyframe_insert(data_path="rotation_euler", frame=f)
                        baked.keyframe_insert(data_path="scale", frame=f)

        # 8. Scale keyframes for multi-segment objects.
        #
        # Use integer frame boundaries so the glTF exporter outputs a STEP
        # sampler (interpolation="STEP") — instant switching, zero blending.
        # Keyframes are placed at integer frames; CONSTANT interpolation is
        # restored on scale fcurves after _set_linear runs below.

        ANIM_F0 = bake_frames[0]

        def _kf_scale(obj, frame, s):
            obj.scale = (s, s, s)
            obj.keyframe_insert(data_path="scale", frame=frame)

        for orig, segs in bake_segs.items():
            if len(segs) <= 1:
                continue
            n_segs = len(segs)
            for seg_idx, (seg_flist, seg_baked, _) in enumerate(segs):
                first_f, last_f = seg_flist[0], seg_flist[-1]
                is_last = (seg_idx == n_segs - 1)
                # Use the next segment's first baked frame as the switch point.
                # With step > 1, last_f + 1 is not a baked frame — the next segment
                # starts at last_f + FRAME_STEP.  Placing "off" at last_f + 1 creates
                # a gap of (FRAME_STEP - 1) frames where both objects are invisible,
                # causing flicker.  Placing "off" at exactly the next segment's first_f
                # means the STEP switch is simultaneous on both objects.
                next_first_f = segs[seg_idx + 1][0][0] if not is_last else None

                if seg_idx == 0:
                    _kf_scale(seg_baked, ANIM_F0,     1.0)  # on from start
                    _kf_scale(seg_baked, last_f,      1.0)  # still on at end of segment
                    _kf_scale(seg_baked, next_first_f, 0.0)  # off exactly when next seg begins
                    seg_baked.scale = (1.0, 1.0, 1.0)
                else:
                    _kf_scale(seg_baked, ANIM_F0,  0.0)  # off from start
                    _kf_scale(seg_baked, first_f,  1.0)  # on at segment start
                    if not is_last:
                        _kf_scale(seg_baked, last_f,       1.0)  # still on at end
                        _kf_scale(seg_baked, next_first_f, 0.0)  # off exactly when next seg begins
                    seg_baked.scale = (0.0, 0.0, 0.0)

        # 6. Linear Interpolation (Blender 5.1 Compliant)
        def _set_linear(action):
            for fc in _action_fcurves(action):
                for kp in fc.keyframe_points:
                    kp.interpolation = 'LINEAR'

        for baked in list(bake_map.values()) + extra_baked_objs:
            if baked.animation_data and baked.animation_data.action:
                _set_linear(baked.animation_data.action)
            if baked.data.shape_keys and baked.data.shape_keys.animation_data:
                _set_linear(baked.data.shape_keys.animation_data.action)

        # Clamp shape key value fcurve extrapolation so weights stay at 0 before the
        # first keyframe.  Without this, fcurves extrapolate linearly outside the
        # keyframe range: Frame_002's first keyframe is at bake_frames[0], so at
        # bake_frames[0]-1 (the unified GLB timeline start) its slope projects to -1,
        # deforming the mesh backwards.  GLB viewers start playback at t=0 (the first
        # keyframe in the shared input accessor), so this shows as a "stutter" at the
        # very beginning that is invisible in Blender (which starts at ANIM_F0).
        for _baked in list(bake_map.values()) + extra_baked_objs:
            _sk_anim = (
                _baked.data.shape_keys.animation_data
                if _baked.data.shape_keys and _baked.data.shape_keys.animation_data
                else None
            )
            if _sk_anim and _sk_anim.action:
                for _fc in _action_fcurves(_sk_anim.action):
                    if _fc.data_path.endswith('.value'):
                        _fc.extrapolation = 'CONSTANT'

        # Restore CONSTANT interpolation on scale fcurves for multi-segment objects.
        # This must run AFTER _set_linear so the glTF exporter sees CONSTANT and
        # exports "interpolation":"STEP" — instant switching with no blending.
        for orig, segs in bake_segs.items():
            if len(segs) <= 1:
                continue
            for _, seg_baked, _ in segs:
                if not (seg_baked.animation_data and seg_baked.animation_data.action):
                    continue
                for fc in _action_fcurves(seg_baked.animation_data.action):
                    if fc.data_path == 'scale':
                        for kp in fc.keyframe_points:
                            kp.interpolation = 'CONSTANT'

        # 7. Zero-Delta Shape Key Purge
        eps = scene.glb_bake_mesh_error
        purged_total = 0
        for baked in list(bake_map.values()) + extra_baked_objs:
            mesh = baked.data
            if not (mesh.shape_keys and len(mesh.shape_keys.key_blocks) > 1):
                continue
            basis_coords = [0.0] * (len(mesh.vertices) * 3)
            mesh.shape_keys.key_blocks[0].data.foreach_get("co", basis_coords)
            to_remove = []
            for kb in list(mesh.shape_keys.key_blocks)[1:]:
                sk_coords = [0.0] * (len(mesh.vertices) * 3)
                kb.data.foreach_get("co", sk_coords)
                max_delta = 0.0
                n = len(mesh.vertices)
                for i in range(n):
                    dx = sk_coords[i*3]   - basis_coords[i*3]
                    dy = sk_coords[i*3+1] - basis_coords[i*3+1]
                    dz = sk_coords[i*3+2] - basis_coords[i*3+2]
                    d = (dx*dx + dy*dy + dz*dz) ** 0.5
                    if d > max_delta:
                        max_delta = d
                if max_delta < eps:
                    to_remove.append(kb.name)
            sk_action = (
                mesh.shape_keys.animation_data.action
                if mesh.shape_keys.animation_data and mesh.shape_keys.animation_data.action
                else None
            )
            for kb_name in to_remove:
                # Remove fcurves first so dope sheet has no dangling references.
                if sk_action:
                    dp = f'key_blocks["{kb_name}"].value'
                    if hasattr(sk_action, 'layers'):
                        for layer in sk_action.layers:
                            for strip in layer.strips:
                                if hasattr(strip, 'channelbags'):
                                    for bag in strip.channelbags:
                                        dead = [fc for fc in bag.fcurves if fc.data_path == dp]
                                        for fc in dead:
                                            bag.fcurves.remove(fc)
                    elif hasattr(sk_action, 'fcurves'):
                        dead = [fc for fc in sk_action.fcurves if fc.data_path == dp]
                        for fc in dead:
                            sk_action.fcurves.remove(fc)
                baked.shape_key_remove(mesh.shape_keys.key_blocks[kb_name])
            purged_total += len(to_remove)
        if purged_total:
            self.report({'INFO'}, f"Purged {purged_total} zero-delta shape key(s).")

        # ── Preview mode: quarantine baked objects, hide originals ──────────
        if self.preview_mode:
            PREV_COL = "[PREVIEW] GLB Bake"

            # If refreshing an existing preview, unhide originals before
            # removing them so nothing ends up permanently hidden.
            if _PREVIEW['active']:
                for name in _PREVIEW['original_names']:
                    obj = bpy.data.objects.get(name)
                    if obj:
                        obj.hide_set(False)

            # Remove any leftover preview baked objects
            if PREV_COL in bpy.data.collections:
                old = bpy.data.collections[PREV_COL]
                for o in list(old.objects):
                    bpy.data.objects.remove(o, do_unlink=True)
                bpy.data.collections.remove(old)

            prev_col = bpy.data.collections.new(PREV_COL)
            scene.collection.children.link(prev_col)

            orig_names  = []
            baked_names = []
            for orig, baked in bake_map.items():
                # Move baked into the preview collection
                for col in list(baked.users_collection):
                    col.objects.unlink(baked)
                prev_col.objects.link(baked)
                baked_names.append(baked.name)

                # Hide the original — re-fetch by name in case the RNA ref was
                # invalidated during the bake loop
                orig_live = bpy.data.objects.get(orig.name)
                if orig_live:
                    orig_live.hide_set(True)
                    orig_names.append(orig_live.name)

            # Also move extra topology-segment objects into the preview collection.
            for seg_obj in extra_baked_objs:
                live = bpy.data.objects.get(seg_obj.name)
                if live:
                    for col in list(live.users_collection):
                        col.objects.unlink(live)
                    prev_col.objects.link(live)
                    baked_names.append(live.name)

            # Remove sub-collections that were created for topology splits —
            # they are now empty and only clutter the outliner during preview.
            for orig in bake_segs:
                sub = bpy.data.collections.get(orig.name)
                if sub and not sub.objects and sub.name in [c.name for c in output_col.children]:
                    output_col.children.unlink(sub)
                    bpy.data.collections.remove(sub)

            # Solo: hide every visible object that isn't an original or a baked preview
            solo_hidden = []
            if scene.glb_bake_preview_solo:
                exempt = set(orig_names) | set(baked_names)
                for obj in context.view_layer.objects:
                    if obj.name not in exempt and not obj.hide_get():
                        obj.hide_set(True)
                        solo_hidden.append(obj.name)

            # For topology-split originals, remember which segment objects belong
            # together so confirm can recreate the sub-collection structure.
            topo_groups = {
                orig.name: [seg_baked.name for (_, seg_baked, _) in segs]
                for orig, segs in bake_segs.items()
                if len(segs) > 1
            }

            _PREVIEW.update({
                'active':         True,
                'collection':     PREV_COL,
                'original_names': orig_names,
                'baked_names':    baked_names,
                'hash':           _preview_settings_hash(scene),
                'orig_to_baked':  {o.name: bake_map[o].name for o in bake_map},
                'solo_hidden':    solo_hidden,
                'topo_groups':    topo_groups,
                'output_col_name': output_col_name,
            })
            _PREVIEW_REFRESH['pending'] = False
            if not bpy.app.timers.is_registered(_preview_refresh_tick):
                bpy.app.timers.register(_preview_refresh_tick, first_interval=0.15)
            self.report({'INFO'},
                f"Preview ready — {len(baked_names)} objects in '[PREVIEW] GLB Bake'. "
                "Check viewport & dope sheet, then Confirm or Cancel.")
            return {'FINISHED'}

        if scene.glb_bake_hide_originals:
            for orig in bake_map:
                orig.hide_set(True)

        self.report({'INFO'}, f"SUCCESS! Baked {len(bake_map)} objects to '{output_col_name}'.")
        return {'FINISHED'}


# --- PREVIEW CONFIRM / CANCEL ---
class OBJECT_OT_glb_preview_confirm(bpy.types.Operator):
    bl_idname  = "object.glb_preview_confirm"
    bl_label   = "Confirm Bake"
    bl_description = "Keep the preview result and delete the original objects"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not _PREVIEW['active']:
            self.report({'WARNING'}, "No active preview to confirm.")
            return {'CANCELLED'}

        # Move baked objects out of the preview collection into the output collection,
        # recreating sub-collections for topology-split objects.
        out_name = _PREVIEW.get('output_col_name') or \
                   context.scene.glb_bake_output_name.strip() or "GLB_Baked_Export"
        if out_name not in bpy.data.collections:
            out_col = bpy.data.collections.new(out_name)
            context.scene.collection.children.link(out_col)
        else:
            out_col = bpy.data.collections[out_name]

        # Build reverse map: segment object name → sub-collection name
        topo_groups  = _PREVIEW.get('topo_groups', {})
        obj_to_subcol = {}
        for orig_name, seg_names in topo_groups.items():
            for seg_name in seg_names:
                obj_to_subcol[seg_name] = orig_name

        col = bpy.data.collections.get(_PREVIEW['collection'])
        if col:
            for obj in list(col.objects):
                col.objects.unlink(obj)
                subcol_name = obj_to_subcol.get(obj.name)
                if subcol_name:
                    if subcol_name not in bpy.data.collections:
                        sub_col = bpy.data.collections.new(subcol_name)
                        out_col.children.link(sub_col)
                    else:
                        sub_col = bpy.data.collections[subcol_name]
                        if sub_col.name not in [c.name for c in out_col.children]:
                            out_col.children.link(sub_col)
                    sub_col.objects.link(obj)
                else:
                    out_col.objects.link(obj)
            bpy.data.collections.remove(col)

        # Optionally delete the originals
        if context.scene.glb_bake_delete_originals:
            for name in _PREVIEW['original_names']:
                obj = bpy.data.objects.get(name)
                if obj:
                    bpy.data.objects.remove(obj, do_unlink=True)
        else:
            for name in _PREVIEW['original_names']:
                obj = bpy.data.objects.get(name)
                if obj:
                    obj.hide_set(context.scene.glb_bake_hide_originals)

        for name in _PREVIEW['solo_hidden']:
            obj = bpy.data.objects.get(name)
            if obj:
                obj.hide_set(False)

        _PREVIEW.update({'active': False, 'collection': '',
                         'original_names': [], 'baked_names': [], 'solo_hidden': [],
                         'topo_groups': {}, 'output_col_name': ''})
        self.report({'INFO'}, "Bake confirmed and committed to scene.")
        return {'FINISHED'}


class OBJECT_OT_glb_preview_cancel(bpy.types.Operator):
    bl_idname  = "object.glb_preview_cancel"
    bl_label   = "Cancel Preview"
    bl_description = "Discard the preview result and restore original objects"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not _PREVIEW['active']:
            self.report({'WARNING'}, "No active preview to cancel.")
            return {'CANCELLED'}

        # Delete all preview baked objects
        col = bpy.data.collections.get(_PREVIEW['collection'])
        if col:
            for obj in list(col.objects):
                bpy.data.objects.remove(obj, do_unlink=True)
            bpy.data.collections.remove(col)

        # Restore original visibility
        for name in _PREVIEW['original_names']:
            obj = bpy.data.objects.get(name)
            if obj:
                obj.hide_set(False)

        for name in _PREVIEW['solo_hidden']:
            obj = bpy.data.objects.get(name)
            if obj:
                obj.hide_set(False)

        _PREVIEW.update({'active': False, 'collection': '',
                         'original_names': [], 'baked_names': [], 'solo_hidden': [],
                         'topo_groups': {}, 'output_col_name': ''})
        self.report({'INFO'}, "Preview cancelled. Originals restored.")
        return {'FINISHED'}


# --- UI PANEL ---
class VIEW3D_PT_glb_baker(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'GLB Bake'
    bl_label = 'GLB Shape Key Baker'

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # Frame Range
        range_row = layout.row(align=True)
        range_row.prop(scene, "use_preview_range", text="", toggle=True, icon='PREVIEW_RANGE')
        range_row.prop(scene, "glb_bake_auto_range", text="Auto Range", toggle=True, icon='KEYFRAME_HLT')

        sub = layout.row(align=True)
        sub.enabled = not scene.glb_bake_auto_range
        if scene.use_preview_range:
            sub.prop(scene, "frame_preview_start", text="Start")
            sub.prop(scene, "frame_preview_end",   text="End")
        else:
            sub.prop(scene, "frame_start", text="Start")
            sub.prop(scene, "frame_end",   text="End")

        layout.separator(factor=0.5)

        # Frame Step
        layout.prop(scene, "glb_bake_step")

        layout.separator(factor=0.1)

        # Adaptive Density
        layout.prop(scene, "glb_bake_adaptive", toggle=True, icon='SHADERFX')
        if scene.glb_bake_adaptive:
            col = layout.column(align=True)
            col.prop(scene, "glb_bake_curve_sensitivity", slider=True)
            col.prop(scene, "glb_bake_mesh_error")

        layout.separator()

        # Live frame-count estimate — shown in both normal and preview mode.
        try:
            sp = _SPIN[_EST['spinner']]
            if _EST['status'] == 'pending':
                layout.label(text="...", icon='SORTTIME')
            elif _EST['status'] == 'phase1':
                layout.label(text=f"Analysing...  {sp}", icon='TIME')
            elif scene.glb_bake_adaptive and scene.glb_bake_live_calc and not _PREVIEW['active']:
                if _EST['status'] == 'running':
                    layout.label(
                        text=f"Calculating...  {_EST['found']} shape keys  {sp}",
                        icon='TIME'
                    )
                elif _EST['status'] == 'done':
                    layout.label(text=f"Calculated: {_EST['result']}", icon='CHECKMARK')
                else:
                    layout.label(text=_EST['_display'], icon=_EST['_display_icon'])
            else:
                layout.label(text=_EST['_display'], icon=_EST['_display_icon'])
        except Exception:
            layout.label(text="Estimated: —", icon='INFO')

        layout.separator()

        # ── Preview active: status + Confirm/Cancel ──
        if _PREVIEW['active']:
            box = layout.box()
            if _PREVIEW_REFRESH['pending']:
                status_row = box.row()
                status_row.label(text="Refreshing preview...", icon='FILE_REFRESH')
            else:
                status_row = box.row()
                status_row.label(text="Preview active — check viewport & dope sheet", icon='HIDE_OFF')
            btn_row = box.row(align=True)
            btn_row.scale_y = 1.8
            btn_row.operator("object.glb_preview_confirm", text="Confirm Bake", icon='CHECKMARK')
            btn_row.operator("object.glb_preview_cancel",  text="Cancel",       icon='X')
            return

        # ── Normal: Preview + Bake buttons ────────────────────────────────
        big_row = layout.row(align=True)
        big_row.scale_y = 2.0
        prev_op = big_row.operator("object.glb_shapekey_baker",
                                   text="Preview", icon='HIDE_OFF')
        prev_op.preview_mode = True
        bake_op = big_row.operator("object.glb_shapekey_baker",
                                   text="Bake Animation", icon='RENDER_ANIMATION')
        bake_op.preview_mode = False

        layout.separator()

        # Settings + Info — collapsible, small
 
        # layout.operator("object.glb_reload_script", icon='FILE_REFRESH')


        wm  = context.window_manager
        box = layout.box()
        hint_row = box.row()
        hint_row.scale_y = 0.7
        icon = 'TRIA_DOWN' if wm.glb_bake_show_hints else 'TRIA_RIGHT'
        hint_row.prop(wm, "glb_bake_show_hints", text="Settings + Info", icon=icon, emboss=False)
        if wm.glb_bake_show_hints:
            col = box.column()
            col.prop(scene, "glb_bake_live_calc")
            col.prop(scene, "glb_bake_hide_originals")
            col.prop(scene, "glb_bake_delete_originals")
            col.prop(scene, "glb_bake_preview_solo")
            col.prop(scene, "glb_bake_output_name")
            col.prop(scene, "glb_bake_shape_key_prefix")
            col.separator()
            col.operator("object.glb_export_baked", icon='EXPORT',
                         text="Export Scene as GLB")
            col.separator()
            info = col.column()
            info.scale_y = 0.65
            info.label(text="1. Select objects or a collection.")
            info.label(text="2. Set frame range and step size.")
            info.label(text="3. Click Preview or Bake.")
            info.separator()



# --- GLB EXPORT (scene → GLB, no sampling, scale STEP auto-patched) ---
class OBJECT_OT_glb_export(bpy.types.Operator):
    """Export the scene as a GLB file without animation sampling.  Scale animation
    for topology-split segment objects (_Seg) is automatically fixed to STEP
    interpolation so GLB viewers show instant switching without blending."""
    bl_idname      = "object.glb_export_baked"
    bl_label       = "Export Scene as GLB"
    bl_description = (
        "Export the scene as a GLB file (no animation sampling — keyframes only). "
        "Scale STEP interpolation for topology-split segments is patched automatically."
    )

    filepath:    bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default="*.glb", options={'HIDDEN'})

    def invoke(self, context, event):
        import os
        col_name  = context.scene.glb_bake_output_name.strip() or "GLB_Baked_Export"
        blend_dir = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else ""
        self.filepath = os.path.join(blend_dir, col_name + ".glb") if blend_dir else col_name + ".glb"
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        import os
        path = bpy.path.abspath(self.filepath)
        if not path.lower().endswith(".glb"):
            path += ".glb"

        # Auto-select every object in the baked output collection (including
        # sub-collections for topology-split segments) so use_selection=True
        # captures all segments, not just whatever happens to be selected.
        def _col_objects_recursive(col):
            objs = list(col.objects)
            for child in col.children:
                objs.extend(_col_objects_recursive(child))
            return objs

        out_col_name = context.scene.glb_bake_output_name.strip() or "GLB_Baked_Export"
        out_col      = bpy.data.collections.get(out_col_name)

        prev_selected = [o for o in context.view_layer.objects if o.select_get()]
        prev_active   = context.view_layer.objects.active

        if out_col:
            for o in context.view_layer.objects:
                o.select_set(False)
            first_vl = None
            for o in _col_objects_recursive(out_col):
                vl_obj = context.view_layer.objects.get(o.name)
                if vl_obj:
                    vl_obj.select_set(True)
                    if first_vl is None:
                        first_vl = vl_obj
            if first_vl:
                context.view_layer.objects.active = first_vl

        # All the settings we want to pass to the glTF exporter.
        # Names and valid enum values differ between Blender versions, so we
        # query the operator's RNA properties at runtime and only pass the ones
        # that exist in this build — avoids TypeError on unknown kwargs.
        desired = {
            'filepath':               path,
            'export_format':          'GLB',
            'use_selection':          True,
            'export_animations':      True,
            'export_force_sampling':  True,
            'export_frame_range':     True,
            'export_anim_slide_to_zero': True,
            'export_morph':           True,
            'export_morph_normal':    True,
            'export_apply':           False,
        }

        # Discover valid property names AND valid enum values for
        # export_animation_mode so we can pick the right merge mode.
        try:
            _rna      = bpy.ops.export_scene.gltf.get_rna_type()
            valid_ids = {p.identifier for p in _rna.properties}
            kwargs    = {k: v for k, v in desired.items() if k in valid_ids}

            _anim_prop = _rna.properties.get('export_animation_mode')
            if _anim_prop:
                _valid_modes = [e.identifier for e in _anim_prop.enum_items]
                print(f"[GLB Baker] export_animation_mode valid values: {_valid_modes}")
                # Pick first mode that merges all objects into one animation.
                for _m in ('SCENE', 'BROADCAST', 'NLA_TRACKS', 'ACTIONS'):
                    if _m in _valid_modes:
                        kwargs['export_animation_mode'] = _m
                        print(f"[GLB Baker] Using animation mode: {_m}")
                        break
            else:
                print("[GLB Baker] export_animation_mode property NOT found in this Blender build")

            # Keep per-sampler accessors only if the param exists.
            if 'export_optimize_animation_size' in valid_ids:
                kwargs['export_optimize_animation_size'] = False
        except Exception as e:
            print(f"[GLB Baker] RNA introspection failed: {e}")
            kwargs = desired.copy()

        try:
            result = bpy.ops.export_scene.gltf(**kwargs)
        except Exception as e:
            # Restore selection before returning
            for o in context.view_layer.objects:
                o.select_set(False)
            for o in prev_selected:
                vl_obj = context.view_layer.objects.get(o.name)
                if vl_obj:
                    vl_obj.select_set(True)
            context.view_layer.objects.active = prev_active
            self.report({'ERROR'}, f"GLB export failed: {e}")
            return {'CANCELLED'}

        # Restore original selection
        for o in context.view_layer.objects:
            o.select_set(False)
        for o in prev_selected:
            vl_obj = context.view_layer.objects.get(o.name)
            if vl_obj:
                vl_obj.select_set(True)
        context.view_layer.objects.active = prev_active

        if 'FINISHED' not in result:
            self.report({'ERROR'}, "GLB export cancelled or failed.")
            return {'CANCELLED'}

        if not os.path.isfile(path):
            self.report({'ERROR'}, f"GLB export ran but file not found at: {path}")
            return {'CANCELLED'}

        # Merge multiple animation entries into one 'Scene' animation.
        try:
            _merge_glb_animations(path)
        except Exception as e:
            self.report({'WARNING'}, f"Exported but animation merge failed: {e}")
            return {'FINISHED'}

        # Patch scale samplers to STEP — guaranteed fix regardless of exporter version.
        try:
            count, _ = _patch_glb_scale_step(path)
        except Exception as e:
            self.report({'WARNING'}, f"Exported but scale-STEP patch failed: {e}")
            return {'FINISHED'}

        name = os.path.basename(path)
        if count > 0:
            self.report({'INFO'}, f"Exported '{name}' — {count} scale sampler(s) set to STEP.")
        else:
            self.report({'INFO'}, f"Exported '{name}'.")
        return {'FINISHED'}


# --- RELOAD
class OBJECT_OT_glb_reload(bpy.types.Operator):
    bl_idname  = "object.glb_reload_script"
    bl_label   = "Reload Script"
    bl_description = "Unregister, reload, and re-register this addon (for development)"

    def execute(self, context):
        import importlib, sys
        mod = sys.modules.get(__name__)
        if mod is None:
            self.report({'WARNING'}, "Module not found in sys.modules.")
            return {'CANCELLED'}

        # Defer the actual reload so this execute() call has fully returned
        # before the operator class gets unregistered (otherwise Blender crashes).
        def do_reload():
            try:
                mod.unregister()
                importlib.reload(mod)
                mod.register()
            except Exception as e:
                print(f"[GLB Baker] Reload failed: {e}")
            return None  # run once

        bpy.app.timers.register(do_reload, first_interval=0.0)
        return {'FINISHED'}


# --- REGISTRATION ---
classes = (
    OBJECT_OT_glb_shapekey_baker,
    OBJECT_OT_glb_preview_confirm,
    OBJECT_OT_glb_reload,
    OBJECT_OT_glb_preview_cancel,
    OBJECT_OT_glb_export,
    VIEW3D_PT_glb_baker,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    if not bpy.app.timers.is_registered(_est_poll_tick):
        bpy.app.timers.register(_est_poll_tick, first_interval=0.2)
        
    bpy.types.Scene.glb_bake_step = bpy.props.IntProperty(
        name="Frame Step",
        description="Bake every Nth frame (1 = smooth, 2+ = optimized file size)",
        default=1,
        min=1
    )
    bpy.types.Scene.glb_bake_auto_range = bpy.props.BoolProperty(
        name="Auto Range",
        description=(
            "Only bake frames where keyframes exist. "
            "Stops at the last keyframe and collapses static holds to just two shape keys."
        ),
        default=False
    )
    bpy.types.Scene.glb_bake_adaptive = bpy.props.BoolProperty(
        name="Adaptive Density",
        description=(
            "Automatically vary keyframe density: dense where the animation curves "
            "are highly curved or the mesh deforms rapidly, sparse where motion is linear. "
            "Supersedes Auto Range when enabled."
        ),
        default=False
    )
    bpy.types.Scene.glb_bake_curve_sensitivity = bpy.props.FloatProperty(
        name="Keyframe Density",
        description=(
            "Controls how many keyframes are added in curved sections of the animation. "
            "0 = minimal keyframes (only at sharp extremes), "
            "1 = dense keyframes (captures every subtle curve change)"
        ),
        default=0.5,
        min=0.0,
        max=1.0,
        subtype='FACTOR'
    )
    bpy.types.Scene.glb_bake_mesh_error = bpy.props.FloatProperty(
        name="Max Mesh Error",
        description=(
            "Maximum allowed vertex position error (world units) between the actual "
            "deformed mesh and a linear interpolation between two baked frames. "
            "Lower = more keyframes, higher = fewer keyframes."
        ),
        default=0.02,
        min=0.0001,
        max=2.0,
        step=1,
        precision=4,
        subtype='DISTANCE'
    )
    bpy.types.Scene.glb_bake_live_calc = bpy.props.BoolProperty(
        name="Live Mesh Calculation",
        description=(
            "Automatically calculate the exact shape key count by evaluating mesh "
            "deformation in the background. Causes the timeline to scrub while "
            "calculating — disable if this is distracting."
        ),
        default=True
    )
    bpy.types.Scene.glb_bake_hide_originals = bpy.props.BoolProperty(
        name="Hide Originals After Baking",
        description="Hide the original objects in the viewport after baking completes.",
        default=True
    )
    bpy.types.Scene.glb_bake_delete_originals = bpy.props.BoolProperty(
        name="Delete Originals After Baking",
        description="When confirming a preview bake, delete the original objects. "
                    "Off by default — originals are kept and made visible again.",
        default=False
    )
    bpy.types.Scene.glb_bake_preview_solo = bpy.props.BoolProperty(
        name="Solo Preview Objects",
        description="Hide all other scene objects while preview is active, "
                    "showing only the baked preview. Restored on Confirm or Cancel.",
        default=False
    )
    bpy.types.Scene.glb_bake_output_name = bpy.props.StringProperty(
        name="Baked Collection Name",
        description="Name for the output collection that holds the baked objects.",
        default="GLB_Baked_Export"
    )
    bpy.types.Scene.glb_bake_shape_key_prefix = bpy.props.StringProperty(
        name="Shape Key Prefix",
        description=(
            "Prefix for baked shape key names (e.g. 'Frame_' → Frame_001, Frame_002). "
            "Game engines like Three.js and Babylon.js use shape key names in code — "
            "set a meaningful prefix to simplify your WebGL integration."
        ),
        default="Frame_"
    )
    bpy.types.WindowManager.glb_bake_show_hints = bpy.props.BoolProperty(
        name="Show hints",
        default=False
    )



def unregister():
    for cls in classes:
        bpy.utils.unregister_class(cls)
        
    del bpy.types.Scene.glb_bake_step
    del bpy.types.Scene.glb_bake_auto_range
    del bpy.types.Scene.glb_bake_adaptive
    del bpy.types.Scene.glb_bake_curve_sensitivity
    del bpy.types.Scene.glb_bake_mesh_error
    del bpy.types.Scene.glb_bake_live_calc
    del bpy.types.Scene.glb_bake_hide_originals
    del bpy.types.Scene.glb_bake_delete_originals
    del bpy.types.Scene.glb_bake_preview_solo
    del bpy.types.Scene.glb_bake_output_name
    del bpy.types.Scene.glb_bake_shape_key_prefix
    if bpy.app.timers.is_registered(_est_tick):
        bpy.app.timers.unregister(_est_tick)
    if bpy.app.timers.is_registered(_est_poll_tick):
        bpy.app.timers.unregister(_est_poll_tick)
    if bpy.app.timers.is_registered(_preview_refresh_tick):
        bpy.app.timers.unregister(_preview_refresh_tick)
    del bpy.types.WindowManager.glb_bake_show_hints

if __name__ == "__main__":
    register()