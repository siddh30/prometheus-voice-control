"""Display mirroring through macOS CoreGraphics: no menus, no clicking. Mirror one display to another, or stop mirroring, and read back
which display mirrors which. A mirror set up here lasts for the login session only (it is never written to the display settings)."""
from __future__ import annotations

import ctypes

_U32 = ctypes.c_uint32
_FOR_SESSION = 1                     # kCGConfigureForSession: undone at logout, never made permanent
_cg = None


class DisplayConfigError(Exception):
    """macOS refused a display change."""


def _lib():
    global _cg
    if _cg is None:
        lib = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        lib.CGMainDisplayID.restype = _U32
        for name in ("CGDisplayIsInMirrorSet", "CGDisplayMirrorsDisplay"):
            fn = getattr(lib, name)
            fn.argtypes, fn.restype = [_U32], _U32
        lib.CGGetOnlineDisplayList.argtypes = [_U32, ctypes.POINTER(_U32), ctypes.POINTER(_U32)]
        lib.CGGetOnlineDisplayList.restype = ctypes.c_int32
        lib.CGBeginDisplayConfiguration.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        lib.CGBeginDisplayConfiguration.restype = ctypes.c_int32
        lib.CGConfigureDisplayMirrorOfDisplay.argtypes = [ctypes.c_void_p, _U32, _U32]
        lib.CGConfigureDisplayMirrorOfDisplay.restype = ctypes.c_int32
        lib.CGCompleteDisplayConfiguration.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        lib.CGCompleteDisplayConfiguration.restype = ctypes.c_int32
        lib.CGCancelDisplayConfiguration.argtypes = [ctypes.c_void_p]
        lib.CGCancelDisplayConfiguration.restype = ctypes.c_int32
        _cg = lib
    return _cg


def main_display() -> int:
    """The display with the menu bar: the one a mirror copies."""
    return int(_lib().CGMainDisplayID())


def online_displays() -> list[int]:
    """Every connected display, including one that is mirroring another (macOS hides those from its own screen list)."""
    ids, count = (_U32 * 16)(), _U32()
    if _lib().CGGetOnlineDisplayList(16, ids, ctypes.byref(count)) != 0:
        return []
    return [int(ids[i]) for i in range(count.value)]


def mirror_of(display: int) -> int:
    """The display this one mirrors, or 0 when it mirrors nothing."""
    return int(_lib().CGDisplayMirrorsDisplay(display))


def set_mirror(display: int, master: int) -> None:
    """Make `display` a mirror of `master`; `master` 0 stops it mirroring."""
    lib, config = _lib(), ctypes.c_void_p()
    if lib.CGBeginDisplayConfiguration(ctypes.byref(config)) != 0:
        raise DisplayConfigError("could not start a display change")
    if lib.CGConfigureDisplayMirrorOfDisplay(config, display, master) != 0:
        lib.CGCancelDisplayConfiguration(config)
        raise DisplayConfigError("macOS refused to change that display")
    if lib.CGCompleteDisplayConfiguration(config, _FOR_SESSION) != 0:
        raise DisplayConfigError("macOS did not apply the display change")
