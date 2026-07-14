"""Print3Dexporter — custom icon loading.

Blender can only display custom icons through a *preview collection*
(``bpy.utils.previews``), and that loader accepts **raster** images only —
SVG is not supported.  ``Canva_ico.svg`` therefore cannot be handed to Blender
directly; we ship a 32x32 PNG rasterised from it (``Canva_logo.png``) alongside
it in this ``core`` folder and load that instead.

Public API:
  register_icons()      — create the preview collection and load the PNG
  unregister_icons()    — free the preview collection
  get_canva_icon_id()   — the loaded icon's id for ``icon_value=``; 0 if absent

``0`` is Blender's "no icon" sentinel, so any draw code can pass the result of
``get_canva_icon_id()`` straight to ``icon_value=`` and degrade gracefully when
the PNG is missing or failed to load.
"""

import os
import bpy
import bpy.utils.previews

# The raster icon that stands in for Canva_ico.svg (Blender can't load SVG).
# Lives next to this module in the 'core' folder.
_ICON_FILE = "Canva_logo.png"
_ICON_KEY = "canva"

# Module-level preview collection.  Created in register_icons(), freed in
# unregister_icons().  None while unloaded.
_pcoll = None


def register_icons():
    """Create the preview collection and load the Canva PNG into it (idempotent)."""
    global _pcoll
    if _pcoll is not None:
        return
    _pcoll = bpy.utils.previews.new()
    path = os.path.join(os.path.dirname(__file__), _ICON_FILE)
    try:
        # 'IMAGE' = load a raster image file (PNG/JPG/…) as a preview thumbnail.
        _pcoll.load(_ICON_KEY, path, 'IMAGE')
    except Exception as e:
        print(f"[CP3D] Could not load custom icon '{path}': {e}")


def unregister_icons():
    """Free the preview collection (safe to call even if never registered)."""
    global _pcoll
    if _pcoll is not None:
        try:
            bpy.utils.previews.remove(_pcoll)
        except Exception:
            pass
        _pcoll = None


def get_canva_icon_id():
    """Return the Canva icon id for ``icon_value=``, or 0 (no icon) if absent."""
    if _pcoll is not None and _ICON_KEY in _pcoll:
        return _pcoll[_ICON_KEY].icon_id
    return 0
