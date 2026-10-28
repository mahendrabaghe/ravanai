"""Resolve the user's real folders through the operating system.

Downloads is not always `%USERPROFILE%\\Downloads` — OneDrive redirects it,
enterprise machines move it, and a second drive is common. On Windows the
shell's known-folder API is authoritative; the environment is only a fallback
for the odd machine where that call fails. Nothing here guesses a school or
company machine's layout.
"""
from __future__ import annotations

import ctypes
import os
import platform
import uuid
from pathlib import Path

_OS = platform.system()

# FOLDERID_* GUIDs from the Windows SDK. Only the folders people actually ask
# for are listed; an unknown name is not guessed.
_FOLDER_IDS = {
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "desktop":   "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "pictures":  "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "music":     "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "videos":    "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "home":      "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",
}

_FOLDER_NAMES = {
    "downloads": "Downloads",
    "documents": "Documents",
    "desktop":   "Desktop",
    "pictures":  "Pictures",
    "music":     "Music",
    "videos":    "Videos",
    "home":      "Home",
}

_ALIASES = {
    "download": "downloads", "downloads folder": "downloads",
    "download folder": "downloads",
    "doc": "documents", "docs": "documents", "document": "documents",
    "documents folder": "documents", "document folder": "documents",
    "desktop folder": "desktop",
    "photo": "pictures", "photos": "pictures", "pic": "pictures",
    "pics": "pictures", "pictures folder": "pictures",
    "picture folder": "pictures",
    "song": "music", "songs": "music", "music folder": "music",
    "movie": "videos", "movies": "videos", "video": "videos",
    "videos folder": "videos", "video folder": "videos",
    "home folder": "home", "user folder": "home", "user profile": "home",
    "profile folder": "home", "my profile": "home",
}

_PREFIXES = ("open ", "go to ", "show ", "take me to ", "my ", "the ")


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _shell_known_folder(key: str) -> Path | None:
    if _OS != "Windows":
        return None
    folder_id = _FOLDER_IDS.get(key)
    if not folder_id:
        return None
    try:
        guid = _GUID.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
        path_ptr = ctypes.c_wchar_p()
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        result = shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(path_ptr))
        if result != 0 or not path_ptr.value:
            return None
        value = path_ptr.value
        try:
            ctypes.WinDLL("ole32").CoTaskMemFree(path_ptr)
        except Exception:
            pass
        path = Path(value)
        return path if path.is_dir() else None
    except Exception as exc:
        print(f"[knownfolders] {key} via shell API failed: {exc}")
        return None


def _env_folder(key: str) -> Path | None:
    """Fallback only: the usual locations, including a redirected OneDrive."""
    names = {
        "downloads": "Downloads",
        "documents": "Documents",
        "desktop": "Desktop",
        "pictures": "Pictures",
        "music": "Music",
        "videos": "Videos",
    }
    roots: list[Path] = []
    if _OS == "Windows":
        profile = os.environ.get("USERPROFILE")
        if profile:
            roots.append(Path(profile))
        onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
        if onedrive:
            roots.append(Path(onedrive))
    else:
        roots.append(Path.home())

    if key == "home":
        return roots[0] if roots and roots[0].is_dir() else None

    name = names.get(key)
    if not name:
        return None
    for root in roots:
        candidate = root / name
        if candidate.is_dir():
            return candidate
    return None


def normalise(spoken: str) -> str | None:
    """Turn 'open my downloads folder' into 'downloads'; else None."""
    text = " ".join(str(spoken or "").lower().replace("\\", " ").split())
    changed = True
    while changed:
        changed = False
        for prefix in _PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                changed = True
    text = " ".join(text.replace(" folder", " ").replace(" directory", " ").split())
    text = text.strip(" .!?,'\"")
    if text in _FOLDER_IDS:
        return text
    return _ALIASES.get(text)


def resolve(spoken: str) -> Path | None:
    """The real folder the user named, or None when it is not a folder."""
    key = normalise(spoken)
    if not key:
        return None
    return _shell_known_folder(key) or _env_folder(key)


def label(spoken: str) -> str:
    key = normalise(spoken)
    return _FOLDER_NAMES.get(key or "", str(spoken or "").strip())


def names() -> tuple[str, ...]:
    return tuple(_FOLDER_NAMES.values())
