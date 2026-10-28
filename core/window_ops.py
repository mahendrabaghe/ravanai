"""Native window actions: switch, minimize, restore, and read the foreground.

These are deliberately not keystrokes. Sending Alt+Tab or Alt+F4 into whatever
happens to be focused is how an assistant closes itself, or the wrong document.
The window manager is asked directly which windows exist, and a handle is
activated, minimised or restored by name — the same operations the taskbar
performs.

Windows is the supported platform here because the automation target is
Windows. Elsewhere the functions answer `failed` with a sentence rather than
pretending.
"""
from __future__ import annotations

import ctypes
import platform
import time
from dataclasses import dataclass

_OS = platform.system()

DONE = "done"
FAILED = "failed"
ASK = "ask"

SW_MINIMIZE = 6
SW_SHOW = 5
SW_RESTORE = 9
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

_SHELL_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "Windows.UI.Core.CoreWindow", "ApplicationManager_DesktopShellWindow",
}


@dataclass(frozen=True)
class Window:
    hwnd: int
    title: str
    pid: int
    cls: str
    minimized: bool
    exstyle: int


_USER32 = None
_KERNEL32 = None


def _user32():
    """One configured user32. HWND argtypes matter: without them ctypes
    passes handles as 32-bit ints and truncates them on 64-bit Windows."""
    global _USER32
    if _USER32 is not None:
        return _USER32
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.IsWindowVisible.argtypes = [ctypes.c_void_p]
    u.IsWindowVisible.restype = ctypes.c_bool
    u.IsIconic.argtypes = [ctypes.c_void_p]
    u.IsIconic.restype = ctypes.c_bool
    u.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    u.GetWindowTextLengthW.restype = ctypes.c_int
    u.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                 ctypes.c_int]
    u.GetWindowTextW.restype = ctypes.c_int
    u.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                ctypes.c_int]
    u.GetClassNameW.restype = ctypes.c_int
    u.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    u.GetWindowThreadProcessId.restype = ctypes.c_ulong
    u.GetForegroundWindow.restype = ctypes.c_void_p
    u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u.GetWindowLongW.restype = ctypes.c_long
    u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u.ShowWindow.restype = ctypes.c_bool
    u.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong,
                                    ctypes.c_bool]
    u.AttachThreadInput.restype = ctypes.c_bool
    u.BringWindowToTop.argtypes = [ctypes.c_void_p]
    u.BringWindowToTop.restype = ctypes.c_bool
    u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    u.SetForegroundWindow.restype = ctypes.c_bool
    u.SwitchToThisWindow.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    u.SwitchToThisWindow.restype = None
    u.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte,
                              ctypes.c_ulong, ctypes.c_size_t]
    u.keybd_event.restype = None
    _USER32 = u
    return u


def _kernel32():
    global _KERNEL32
    if _KERNEL32 is not None:
        return _KERNEL32
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentThreadId.restype = ctypes.c_ulong
    _KERNEL32 = k
    return k


def _own_pids() -> set[int]:
    try:
        from core import appidentity
        return appidentity.own_pids()
    except Exception:
        return set()


def _text(hwnd, user32) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value.strip()


def _class(hwnd, user32) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid(hwnd, user32) -> int:
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _info(hwnd, user32) -> Window:
    return Window(
        hwnd=int(hwnd),
        title=_text(hwnd, user32),
        pid=_pid(hwnd, user32),
        cls=_class(hwnd, user32),
        minimized=bool(user32.IsIconic(hwnd)),
        exstyle=int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE)),
    )


def _enumerate() -> list[Window]:
    """Top-level windows in Z-order, top first. Never raises."""
    if _OS != "Windows":
        return []
    try:
        user32 = _user32()
        found: list[Window] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

        def _collect(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                found.append(_info(hwnd, user32))
            return True

        user32.EnumWindows(WNDENUMPROC(_collect), 0)
        return found
    except Exception as exc:
        print(f"[window_ops] enumeration failed: {exc}")
        return []


def _eligible(window: Window, own: set[int], include_own: bool,
              allow_minimized: bool) -> bool:
    if not window.title:
        return False
    if window.cls in _SHELL_CLASSES:
        return False
    # Palettes, the assistant's own orb, tray flyouts: never switch targets.
    if window.exstyle & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE):
        return False
    if window.pid in own and not include_own:
        return False
    if window.minimized and not allow_minimized:
        return False
    return True


def find_windows(fragment: str = "", include_own: bool = False,
                 allow_minimized: bool = True) -> list[Window]:
    """Visible windows whose title contains `fragment` (case-insensitive)."""
    own = _own_pids()
    needle = (fragment or "").strip().lower()
    result = []
    for window in _enumerate():
        if not _eligible(window, own, include_own, allow_minimized):
            continue
        if needle and needle not in window.title.lower():
            continue
        result.append(window)
    return result


def _activate(hwnd: int) -> bool:
    """Restore and foreground one window. -> Windows accepted the change.

    SetForegroundWindow is refused when the calling process is not already in
    front — Windows calls that focus stealing, and it said no on this machine
    with Settings open. Attaching to the foreground thread is the documented
    workaround; a synthetic Alt tap is the second half, because Windows lets a
    process that has just received a user keystroke take the foreground.
    SwitchToThisWindow is the last resort for the cases that still refuse.
    """
    try:
        user32 = _user32()
        kernel32 = _kernel32()
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)

        foreground = user32.GetForegroundWindow()
        current = kernel32.GetCurrentThreadId()
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        fg_thread = (user32.GetWindowThreadProcessId(foreground, None)
                     if foreground else 0)

        attached: list[int] = []
        for tid in {target_thread, fg_thread}:
            if tid and tid != current and user32.AttachThreadInput(
                    current, tid, True):
                attached.append(tid)
        try:
            user32.BringWindowToTop(hwnd)
            # A real Alt press counts as user input for the duration of the
            # next SetForegroundWindow call; the flag is set and cleared
            # together, so no key is left down.
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            user32.SetForegroundWindow(hwnd)
        finally:
            for tid in attached:
                user32.AttachThreadInput(current, tid, False)

        time.sleep(0.18)
        if int(user32.GetForegroundWindow() or 0) == int(hwnd):
            return True
        try:
            user32.SwitchToThisWindow(hwnd, True)
            time.sleep(0.18)
        except Exception:
            pass
        return int(user32.GetForegroundWindow() or 0) == int(hwnd)
    except Exception as exc:
        print(f"[window_ops] activate failed: {exc}")
        return False


def focus(target: str) -> tuple[str, str, list[str]]:
    """Bring the best window whose title contains `target` to the front."""
    target = (target or "").strip()
    if not target:
        return (FAILED, "no window name was given", [])
    if _OS != "Windows":
        return (FAILED, "window focusing needs Windows on this machine", [])

    windows = find_windows(target, allow_minimized=True)
    if not windows:
        return (FAILED, f"no open window matches '{target}'", [])
    window = windows[0]
    if _activate(window.hwnd):
        return (DONE, window.title, [window.title])
    return (FAILED, f"Windows would not bring '{window.title}' to the front",
            [window.title])


def switch_previous() -> tuple[str, str, list[str]]:
    """Bring the next window down the Z-order to the front (one Alt+Tab)."""
    if _OS != "Windows":
        return (FAILED, "window switching needs Windows on this machine", [])
    windows = find_windows(allow_minimized=False)
    if not windows:
        return (FAILED, "there is no other open window to switch to", [])

    try:
        foreground = int(_user32().GetForegroundWindow() or 0)
    except Exception:
        foreground = 0
    index = next((i for i, w in enumerate(windows) if w.hwnd == foreground), -1)
    target = windows[(index + 1) % len(windows)] if index >= 0 else windows[0]

    if _activate(target.hwnd):
        return (DONE, target.title, [target.title])
    return (FAILED, f"Windows would not bring '{target.title}' to the front",
            [target.title])


def foreground_title() -> tuple[bool, str]:
    if _OS != "Windows":
        return (False, "")
    try:
        user32 = _user32()
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return (False, "")
        return (True, _text(hwnd, user32))
    except Exception:
        return (False, "")


def foreground_window() -> Window | None:
    """The focused top-level window, or None. Used to decide whether an action
    is safe to aim at whatever is in front."""
    if _OS != "Windows":
        return None
    try:
        user32 = _user32()
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return None
        info = _info(hwnd, user32)
        return info if info.title else None
    except Exception:
        return None


def foreground_is_own() -> bool:
    if _OS != "Windows":
        return False
    try:
        user32 = _user32()
        hwnd = int(user32.GetForegroundWindow() or 0)
        return bool(hwnd) and _pid(hwnd, user32) in _own_pids()
    except Exception:
        return False


def minimize_foreground() -> tuple[str, str, list[str]]:
    if _OS != "Windows":
        return (FAILED, "minimising windows needs Windows on this machine", [])
    try:
        user32 = _user32()
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return (FAILED, "there is no foreground window to minimise", [])
        info = _info(hwnd, user32)
        if not info.title or info.cls in _SHELL_CLASSES:
            return (FAILED, "the foreground window is not an application", [])
        if info.pid in _own_pids():
            return (FAILED,
                    "the assistant's own window is in front - name the "
                    "application you want minimised", [info.title])
        user32.ShowWindow(hwnd, SW_MINIMIZE)
        time.sleep(0.15)
        ok = bool(user32.IsIconic(hwnd))
        return ((DONE, info.title, [info.title]) if ok else
                (FAILED, f"Windows did not minimise '{info.title}'", [info.title]))
    except Exception as exc:
        return (FAILED, f"minimising failed: {exc}", [])


def minimize(target: str) -> tuple[str, str, list[str]]:
    target = (target or "").strip()
    if not target:
        return minimize_foreground()
    if _OS != "Windows":
        return (FAILED, "minimising windows needs Windows on this machine", [])

    windows = find_windows(target, allow_minimized=True)
    if not windows:
        return (FAILED, f"no visible window matches '{target}'", [])

    try:
        user32 = _user32()
        titles = []
        for window in windows:
            titles.append(window.title)
            if not window.minimized:
                user32.ShowWindow(window.hwnd, SW_MINIMIZE)
        time.sleep(0.25)
        remaining = [w.title for w in find_windows(target, allow_minimized=True)
                     if not w.minimized]
        if remaining:
            return (FAILED,
                    f"{len(remaining)} window(s) would not minimise", remaining)
        return (DONE, ", ".join(titles[:3]), titles)
    except Exception as exc:
        return (FAILED, f"minimising '{target}' failed: {exc}", [])


def restore(target: str = "") -> tuple[str, str, list[str]]:
    if _OS != "Windows":
        return (FAILED, "restoring windows needs Windows on this machine", [])

    if target:
        windows = find_windows(target, allow_minimized=True)
        if not windows:
            return (FAILED, f"no window matches '{target}'", [])
        window = next((w for w in windows if w.minimized), windows[0])
        if _activate(window.hwnd):
            return (DONE, window.title, [window.title])
        return (FAILED, f"Windows would not restore '{window.title}'",
                [window.title])

    windows = [w for w in find_windows(allow_minimized=True) if w.minimized]
    if not windows:
        return (FAILED, "no minimised window is available to restore", [])
    window = windows[0]
    if _activate(window.hwnd):
        return (DONE, window.title, [window.title])
    return (FAILED, f"Windows would not restore '{window.title}'", [window.title])


def state_of_own(title_fragment: str) -> tuple[bool, bool]:
    """(found, minimized) for one of the assistant's own windows."""
    windows = find_windows(title_fragment, include_own=True,
                           allow_minimized=True)
    if not windows:
        return (False, False)
    return (True, windows[0].minimized)
