#computer_control.py
import io
import json
import platform
import re
import string
import subprocess
import sys

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}
import time
import random
from pathlib import Path

from core import appidentity, outcome, window_ops
from core.lazyimport import LazyModule, installed

# pyautogui and pyperclip are imported on first USE, not at launch — see
# core/lazyimport.py for why and for what stays the same. The flags still mean
# "is it installed?" and still answer without importing anything.
_PYAUTOGUI = installed("pyautogui")
_PYPERCLIP = installed("pyperclip")


def _configure_pyautogui(module) -> None:
    """Settings that used to sit beside the import, run once when it happens."""
    module.FAILSAFE = True
    module.PAUSE    = 0.05


pyautogui = LazyModule("pyautogui", _configure_pyautogui)
pyperclip = LazyModule("pyperclip")

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_BASE         = _base_dir()
_CONFIG_PATH  = _BASE / "config" / "api_keys.json"
_MEMORY_PATH  = _BASE / "memory" / "long_term.json"

def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _platform_os() -> str:
    return {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}.get(
        platform.system(), "linux"
    )

def _get_os() -> str:
    return _load_config().get("os_system", _platform_os()).lower()


def _get_api_key() -> str:
    return _load_config().get("gemini_api_key", "")

_SAFE_SCREENSHOT_ROOTS = (
    Path.home(),
)

def _safe_screenshot_path(requested: str | None) -> Path:
    fallback = Path.home() / "Desktop" / "jarvis_screenshot.png"
    if not requested:
        return fallback
    try:
        p = Path(requested).expanduser().resolve()
        for root in _SAFE_SCREENSHOT_ROOTS:
            if p.is_relative_to(root.resolve()):
                p.parent.mkdir(parents=True, exist_ok=True)
                return p
    except Exception:
        pass
    return fallback

def _require_pyautogui():
    """Refuse early, with the sentence that says what to install.

    The import itself is the module `__getattr__`'s job; this stays exactly
    what it was — the check that turns a missing optional dependency into an
    instruction rather than a traceback.
    """
    if not _PYAUTOGUI:
        raise RuntimeError("PyAutoGUI not installed. Run: pip install pyautogui")


def _require_pyperclip() -> bool:
    """Is the clipboard usable? Returns a bool rather than raising, because
    every caller already has a hotkey fallback written for the case where it
    is not — turning that into an exception would break the path built to
    survive it."""
    return _PYPERCLIP

_FIRST_NAMES = [
    "Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Drew", "Quinn",
    "Avery", "Blake", "Cameron", "Dakota", "Emerson", "Finley", "Harper",
]
_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Wilson", "Moore", "Taylor", "Anderson", "Thomas", "Jackson",
]
_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "proton.me", "mail.com"]


def _random_data(data_type: str) -> str:
    dt = data_type.lower().strip()

    if dt == "first_name":
        return random.choice(_FIRST_NAMES)

    if dt == "last_name":
        return random.choice(_LAST_NAMES)

    if dt == "name":
        return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"

    if dt == "email":
        first = random.choice(_FIRST_NAMES).lower()
        last  = random.choice(_LAST_NAMES).lower()
        num   = random.randint(10, 999)
        return f"{first}.{last}{num}@{random.choice(_DOMAINS)}"

    if dt == "username":
        return f"{random.choice(_FIRST_NAMES).lower()}{random.randint(100, 9999)}"

    if dt == "password":
        chars = string.ascii_letters + string.digits + "!@#$%"
        raw   = (
            random.choice(string.ascii_uppercase)
            + random.choice(string.digits)
            + random.choice("!@#$%")
            + "".join(random.choices(chars, k=9))
        )
        return "".join(random.sample(raw, len(raw)))

    if dt == "phone":
        return f"+1{random.randint(200,999)}{random.randint(1_000_000, 9_999_999)}"

    if dt == "birthday":
        y = random.randint(1980, 2000)
        m = random.randint(1, 12)
        d = random.randint(1, 28)
        return f"{m:02d}/{d:02d}/{y}"

    if dt == "address":
        num    = random.randint(100, 9999)
        street = random.choice(["Main St", "Oak Ave", "Park Blvd", "Elm St", "Cedar Ln"])
        return f"{num} {street}"

    if dt == "zip_code":
        return str(random.randint(10000, 99999))

    if dt == "city":
        return random.choice(["New York", "Los Angeles", "Chicago", "Houston", "Phoenix"])

    return f"random_{data_type}_{random.randint(1000, 9999)}"

def _user_profile() -> dict:
    """Read identity fields from long-term memory."""
    try:
        if _MEMORY_PATH.exists():
            data     = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
            identity = data.get("identity", {})
            return {k: v.get("value", "") for k, v in identity.items()}
    except Exception:
        pass
    return {}

def _type(text: str, interval: float = 0.03) -> str:
    _require_pyautogui()
    time.sleep(0.3)
    pyautogui.typewrite(text, interval=interval)
    return f"Typed: {text[:60]}{'…' if len(text) > 60 else ''}"


def _smart_type(text: str, clear_first: bool = True) -> str:
    _require_pyautogui()
    if clear_first:
        _clear_field()
        time.sleep(0.1)

    if len(text) > 20 and _require_pyperclip():
        pyperclip.copy(text)
        time.sleep(0.1)
        paste_key = "command" if _get_os() == "mac" else "ctrl"
        pyautogui.hotkey(paste_key, "v")
        return f"Smart-typed (clipboard): {text[:60]}{'…' if len(text) > 60 else ''}"

    pyautogui.typewrite(text, interval=0.04)
    return f"Smart-typed: {text[:60]}{'…' if len(text) > 60 else ''}"


def _click(x=None, y=None, button: str = "left", clicks: int = 1) -> str:
    _require_pyautogui()
    if x is not None and y is not None:
        pyautogui.click(x, y, button=button, clicks=clicks)
        return f"{'Double-c' if clicks == 2 else 'C'}licked ({x}, {y}) [{button}]"
    pyautogui.click(button=button, clicks=clicks)
    return f"Clicked at current position [{button}]"


def _hotkey(*keys) -> str:
    _require_pyautogui()
    pyautogui.hotkey(*keys)
    return f"Hotkey: {'+'.join(keys)}"


def _press(key: str) -> str:
    _require_pyautogui()
    pyautogui.press(key)
    return f"Pressed: {key}"


def _scroll(direction: str = "down", amount: int = 3) -> str:
    _require_pyautogui()
    vertical   = direction in ("up", "down")
    clicks     = amount if direction in ("up", "right") else -amount
    pyautogui.scroll(clicks) if vertical else pyautogui.hscroll(clicks)
    return f"Scrolled {direction} ×{amount}"


def _move(x: int, y: int, duration: float = 0.3) -> str:
    _require_pyautogui()
    pyautogui.moveTo(x, y, duration=duration)
    return f"Mouse → ({x}, {y})"


def _drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> str:
    _require_pyautogui()
    pyautogui.moveTo(x1, y1, duration=0.2)
    pyautogui.dragTo(x2, y2, duration=duration, button="left")
    return f"Dragged ({x1},{y1}) → ({x2},{y2})"


def _clipboard_get() -> str:
    if _require_pyperclip():
        return pyperclip.paste()
    _hotkey("ctrl", "c")
    time.sleep(0.2)
    return "(copied — pyperclip unavailable for read)"


def _clipboard_paste(text: str) -> str:
    if _require_pyperclip():
        pyperclip.copy(text)
        time.sleep(0.1)
        _require_pyautogui()
        paste_key = "command" if _get_os() == "mac" else "ctrl"
        pyautogui.hotkey(paste_key, "v")
        return f"Pasted: {text[:60]}{'…' if len(text) > 60 else ''}"
    return "pyperclip not available"


def _screenshot(save_path: str | None = None) -> str:
    _require_pyautogui()
    path = _safe_screenshot_path(save_path)
    img  = pyautogui.screenshot()
    img.save(str(path))
    return f"Screenshot saved: {path}"


def _clear_field() -> str:
    _require_pyautogui()
    select_key = "command" if _get_os() == "mac" else "ctrl"
    pyautogui.hotkey(select_key, "a")
    time.sleep(0.1)
    pyautogui.press("delete")
    return "Field cleared"

def _focus_window(title: str) -> str:
    """Bring a named window to the front.

    Windows goes through core/window_ops.py, which asks the window manager
    directly. There is no command string built out of the user's words: the
    old version spliced the title into a PowerShell one-liner, so a title
    containing a quote and a semicolon was a command.
    """
    title = (title or "").strip()
    if not title:
        return outcome.failed("focus_window", "no window name was given")

    if _get_os() == "windows":
        status, detail, _ = window_ops.focus(title)
        if status == window_ops.DONE:
            return outcome.done("focus_window", window=detail, verified=True)
        return outcome.failed("focus_window", detail, window=title)

    # macOS: the name is passed as an ARGUMENT to AppleScript, not spliced
    # into its source, so a quote in a window title stays a quote.
    if _get_os() == "mac":
        script = (
            'on run argv\n'
            'tell application "System Events" to set frontmost of '
            '(first process whose name contains (item 1 of argv)) to true\n'
            'end run'
        )
        try:
            res = subprocess.run(
                ["osascript", "-e", script, title],
                capture_output=True, timeout=5,
            )
        except Exception as exc:
            return outcome.failed("focus_window", f"osascript failed: {exc}",
                                  window=title)
        if res.returncode == 0:
            return outcome.done("focus_window", window=title, verified=True)
        return outcome.failed(
            "focus_window", f"no application matches '{title}'", window=title)

    if _get_os() == "linux":
        for argv in (["wmctrl", "-a", title],
                     ["xdotool", "search", "--name", title, "windowactivate"]):
            try:
                res = subprocess.run(argv, capture_output=True, timeout=5)
            except FileNotFoundError:
                continue
            except Exception as exc:
                return outcome.failed(
                    "focus_window", f"{argv[0]} failed: {exc}", window=title)
            if res.returncode == 0:
                return outcome.done("focus_window", window=title, verified=True)
        return outcome.failed(
            "focus_window",
            "no window matched - wmctrl or xdotool is required on Linux",
            window=title)

    return outcome.failed("focus_window", f"unsupported OS '{_get_os()}'",
                          window=title)

def _close_app(app_name: str, confirmed_index: int | None = None) -> str:
    """Close the application the user named — that one, and never this one.

    THE BUG THIS REPLACES
        `computer_settings.close_app` was two lines:

            if _OS == "Darwin": pyautogui.hotkey("command", "q")
            else:               pyautogui.hotkey("alt", "f4")

        No target. It sent Alt+F4 to whatever window had focus, and while
        somebody is talking to the assistant the window with focus is very
        often the assistant — which is why people reported that asking it to
        close an app closed JARVIS itself. It also returned nothing at all, so
        the model announced a success it had no information about.

        The keystroke is not fixable by aiming it better. "The focused window"
        is the wrong idea: the user named an application, so the application is
        what should be found and asked to close.

    HOW THIS ONE BEHAVES
        * The name is matched against what is actually RUNNING — no list of
          applications, so anything on any machine in any country works.
        * The assistant's own process tree is never a candidate. When the only
          thing matching is the assistant, that is said out loud rather than
          obeyed.
        * Two equally good matches is a question, not a coin toss. Closing the
          wrong window loses somebody's unsaved work.
        * It asks politely — WM_CLOSE on Windows, SIGTERM elsewhere — so an
          application with unsaved changes gets to say so, and it NEVER
          escalates to a kill.
        * It then checks whether the process is gone, and answers `done` only
          if it is.
    """
    app_name = (app_name or "").strip()
    if not app_name:
        return outcome.failed("close_app", "no application name was given")

    if not appidentity._PSUTIL:
        return outcome.failed(
            "close_app",
            "psutil is not installed, so running applications cannot be found "
            "- run: pip install psutil",
            app=app_name,
        )

    matches = appidentity.running(app_name)

    if not matches:
        # Nothing, or nothing that is not us. Telling those two apart matters:
        # "it is not running" and "the only thing by that name is me" need
        # completely different sentences.
        including_self = appidentity.running(app_name, include_self=True)
        if any(m.is_self for m in including_self):
            return outcome.failed(
                "close_app",
                "the only thing matching that name is the assistant itself, "
                "and it will not close itself - if the user really wants the "
                "assistant closed they should close its window themselves",
                app=app_name,
            )
        return outcome.failed(
            "close_app",
            "no running application matches that name - it may already be "
            "closed, or be running under a different name",
            app=app_name,
        )

    # Group by process NAME, because twenty `opera.exe` are one application
    # with nineteen helpers, not twenty applications — and closing "Opera"
    # means all of them. `matches` is already best-first, so insertion order
    # here ranks the groups.
    by_name: dict[str, list] = {}
    for match in matches:
        by_name.setdefault(match.label, []).append(match)

    ranked = list(by_name.items())          # [(name, [matches...]), ...]
    target_name, group = ranked[0]

    # Two DIFFERENT applications that answer equally well is a question, not a
    # coin toss. Only when they tie: "discord" beating "discordcanary" on an
    # exact match is a clear winner and interrupting the user to confirm it
    # would be the assistant refusing to do an unambiguous thing.
    if len(ranked) > 1 and not confirmed_index:
        best_score = group[0].score
        tied = [name for name, ms in ranked if ms[0].score == best_score]
        if len(tied) > 1:
            return outcome.ask(
                "close_app",
                f"more than one application matches '{app_name}' equally well: "
                f"{', '.join(tied[:4])} - which one should be closed?",
                app=app_name, candidates=len(tied),
            )
        print(f"[ComputerControl] close_app: '{app_name}' -> {target_name} "
              f"(also matched: {', '.join(n for n, _ in ranked[1:5])})")

    closed, still_open, reason = 0, 0, ""
    for match in group:
        gone, detail = appidentity.request_close(match)
        if gone:
            closed += 1
        else:
            still_open += 1
            reason = reason or detail

    if closed and not still_open:
        return outcome.done(
            "close_app", app=app_name, closed=target_name,
            processes=closed, verified=True,
        )

    if closed and still_open:
        # Partly gone. Common and benign for multi-process applications: the
        # window is shut and a background helper is taking its time. Said as
        # `unverified` because that is exactly what it is.
        return outcome.unverified(
            "close_app",
            f"{closed} of {closed + still_open} processes closed; the rest are "
            f"still running - {reason}",
            app=app_name, closed=target_name,
        )

    return outcome.failed(
        "close_app", reason or "the application did not close",
        app=app_name, matched=target_name,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Window actions — the active context decides what a verb means
# ─────────────────────────────────────────────────────────────────────────────
# Ctrl+Z is a documented undo in these and nothing else. Sending it to an
# unknown window is the "random shortcut" the automation rules forbid.
_UNDO_EXES = {
    "notepad.exe", "wordpad.exe", "winword.exe", "excel.exe", "powerpnt.exe",
    "notepad++.exe", "sublime_text.exe", "code.exe", "devenv.exe",
    "pycharm64.exe", "idea64.exe", "wps.exe",
}
_BROWSER_EXES = {
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "vivaldi.exe", "arc.exe",
}


def _foreground_process() -> tuple[str, str]:
    """(process exe name, window title) for the focused window, or ('', '')."""
    window = window_ops.foreground_window()
    if window is None:
        return ("", "")
    try:
        import psutil
        name = psutil.Process(window.pid).name().lower()
    except Exception:
        name = ""
    return (name, window.title)


def _is_ravan(name: str) -> bool:
    return (name or "").strip().lower() in {
        "ravan", "raavan", "jarvis", "assistant", "the assistant",
        "you", "yourself", "this app", "the app",
    }


def _wait_own_minimized(want: bool, timeout: float = 1.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found, minimized = window_ops.state_of_own("")
        if found and minimized == want:
            return True
        time.sleep(0.08)
    return False


def _wait_own_visible(timeout: float = 1.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found, minimized = window_ops.state_of_own("")
        if found and not minimized:
            return True
        time.sleep(0.08)
    return False


def _minimize_window(player, target: str) -> str:
    """Minimise RAVAN by default; a named application when one is given."""
    target = (target or "").strip()
    if not target or _is_ravan(target):
        if player is None or not hasattr(player, "minimize_main"):
            return outcome.failed(
                "minimize_window",
                "the assistant's own window controls are unavailable")
        player.minimize_main()
        if _wait_own_minimized(True):
            return outcome.done("minimize_window", window="RAVAN",
                                verified=True)
        return outcome.unverified(
            "minimize_window",
            "asked RAVAN to minimise, but Windows did not report it minimised")

    status, detail, _ = window_ops.minimize(target)
    if status == window_ops.DONE:
        return outcome.done("minimize_window", window=detail, verified=True)
    return outcome.failed("minimize_window", detail, window=target)


def _restore_window(player, target: str) -> str:
    """Bring RAVAN back by default; a named application when one is given."""
    target = (target or "").strip()
    if not target or _is_ravan(target):
        if player is None or not hasattr(player, "show_main"):
            return outcome.failed(
                "restore_window",
                "the assistant's own window controls are unavailable")
        player.show_main()
        if _wait_own_visible():
            return outcome.done("restore_window", window="RAVAN", verified=True)
        return outcome.unverified(
            "restore_window",
            "asked RAVAN to show its window, but no window appeared on screen")

    status, detail, _ = window_ops.restore(target)
    if status == window_ops.DONE:
        return outcome.done("restore_window", window=detail, verified=True)
    return outcome.failed("restore_window", detail, window=target)


def _switch_window() -> str:
    status, detail, _ = window_ops.switch_previous()
    if status == window_ops.DONE:
        return outcome.done("switch_window", window=detail, verified=True)
    return outcome.failed("switch_window", detail)


def _browser_back() -> str:
    """Alt+Left — but only when a browser is actually in front."""
    exe, title = _foreground_process()
    if exe not in _BROWSER_EXES:
        return outcome.ask(
            "go_back",
            "the window in front is not a web browser - should I go back in "
            "a browser, or did you mean something else?")
    try:
        _require_pyautogui()
        pyautogui.hotkey("alt", "left")
    except Exception as exc:
        return outcome.failed("go_back", f"the Back shortcut failed: {exc}",
                              window=title)
    return outcome.unverified(
        "go_back",
        "sent the browser's Back shortcut; the page itself cannot be read "
        "from here, so the change is not confirmed",
        window=title)


def _undo() -> str:
    """Ctrl+Z, only in an editor where that is the documented undo."""
    exe, title = _foreground_process()
    if window_ops.foreground_is_own():
        return outcome.ask(
            "undo", "the assistant's own window is in front - which "
                    "application should the undo be applied to?")
    if exe not in _UNDO_EXES:
        return outcome.ask(
            "undo",
            f"'{exe or title or 'the window in front'}' is not an editor this "
            f"tool can safely send Undo to - which application should the "
            f"undo be applied to?")
    try:
        _require_pyautogui()
        pyautogui.hotkey("ctrl", "z")
    except Exception as exc:
        return outcome.failed("undo", f"the Undo shortcut failed: {exc}",
                              window=title)
    return outcome.unverified(
        "undo",
        "sent the standard Undo shortcut; the application's document cannot "
        "be inspected from here, so the change is not confirmed",
        window=title)


def _screen_find(description: str) -> tuple[int, int] | None:
    api_key = _get_api_key()
    if not api_key:
        print("[ComputerControl] ⚠️ No API key for screen_find")
        return None

    try:
        from google import genai
        from google.genai import types as gtypes

        _require_pyautogui()
        w, h  = pyautogui.size()
        img   = pyautogui.screenshot()
        buf   = io.BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        prompt = (
            f"This is a screenshot of a {w}×{h} pixel screen. "
            f"Locate the UI element described as: '{description}'. "
            f"Reply with ONLY the center coordinates as: x,y "
            f"If the element is not visible, reply: NOT_FOUND"
        )

        from core import gemini
        response = gemini.call(
            [gtypes.Part.from_bytes(data=image_bytes, mime_type="image/png"), prompt],
            tier=gemini.FAST, timeout_ms=20_000,
        )
        if response is None:
            return None

        text = (response.text or "").strip()
        if "NOT_FOUND" in text.upper():
            return None

        match = re.search(r"(\d+)\s*,\s*(\d+)", text)
        if match:
            return int(match.group(1)), int(match.group(2))

    except Exception as e:
        print(f"[ComputerControl] ⚠️ screen_find failed: {e}")

    return None

def computer_control(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Dispatch table for all computer control actions.

    parameters keys (all optional unless noted):
      action        : (required) one of the actions listed below
      text          : text to type or paste
      x, y          : screen coordinates
      button        : 'left' | 'right' (default: left)
      keys          : hotkey string, e.g. 'ctrl+c'
      key           : single key name, e.g. 'enter'
      direction     : 'up' | 'down' | 'left' | 'right'
      amount        : scroll amount (default: 3)
      seconds       : wait duration
      title         : window title fragment for focus_window
      description   : natural-language element description for screen_find/click
      type          : data type for random_data
      field         : memory field name for user_data
      clear_first   : bool, clear field before typing (default: true)
      path          : save path for screenshot (must be inside home dir)

    Actions:
      type          — type text at cursor
      smart_type    — clear field + type (clipboard-backed)
      click         — left click
      double_click  — double left click
      right_click   — right click
      move          — move mouse
      drag          — click-drag between two points
      hotkey        — key combination
      press         — single key
      scroll        — scroll the wheel
      copy          — read clipboard
      paste         — write + paste clipboard
      screenshot    — capture screen (safe path only)
      wait          — sleep N seconds
      clear_field   — select-all + delete
      focus_window  — bring window to foreground
      switch_window — bring the previous window to the front
      minimize_window — minimise RAVAN, or the application named in 'app_name'
      restore_window  — restore RAVAN, or the application named in 'app_name'
      go_back       — browser Back, only when a browser is in front
      undo          — Ctrl+Z in a recognised editor; asks otherwise
      screen_find   — AI element finder (returns x,y)
      screen_click  — AI element finder + click
      random_data   — generate fake form data
      user_data     — pull real data from memory
    """
    params = parameters or {}
    action = params.get("action", "").lower().strip()

    if not action:
        return "No action specified for computer_control."

    if player:
        # Terminal, not the HUD: the activity log is for the conversation and
        # for problems, not a receipt for every keystroke. See
        # core/outcome.is_problem().
        print(f"[ComputerControl] {action}")

    print(f"[ComputerControl] ▶ {action}  {params}")

    try:

        if action == "type":
            return _type(params.get("text", ""))

        if action == "smart_type":
            return _smart_type(
                params.get("text", ""),
                clear_first=params.get("clear_first", True),
            )

        if action in ("click", "left_click"):
            return _click(params.get("x"), params.get("y"), "left", 1)

        if action == "double_click":
            return _click(params.get("x"), params.get("y"), "left", 2)

        if action == "right_click":
            return _click(params.get("x"), params.get("y"), "right", 1)

        if action == "move":
            return _move(int(params.get("x", 0)), int(params.get("y", 0)))

        if action == "drag":
            return _drag(
                int(params.get("x1", 0)), int(params.get("y1", 0)),
                int(params.get("x2", 0)), int(params.get("y2", 0)),
            )

        if action == "hotkey":
            raw  = params.get("keys", "")
            keys = [k.strip() for k in raw.split("+")] if isinstance(raw, str) else raw
            return _hotkey(*keys)

        if action == "press":
            return _press(params.get("key", "enter"))

        if action == "scroll":
            return _scroll(
                direction=params.get("direction", "down"),
                amount=int(params.get("amount", 3)),
            )

        if action == "copy":
            return _clipboard_get()

        if action == "paste":
            return _clipboard_paste(params.get("text", ""))

        if action == "screenshot":
            return _screenshot(params.get("path"))

        if action == "screen_find":
            coords = _screen_find(params.get("description", ""))
            return f"{coords[0]},{coords[1]}" if coords else "NOT_FOUND"

        if action == "screen_click":
            desc   = params.get("description", "")
            coords = _screen_find(desc)
            if coords:
                time.sleep(0.2)
                _click(x=coords[0], y=coords[1])
                return f"Clicked '{desc}' at {coords}"
            return f"Element not found on screen: '{desc}'"

        if action == "wait":
            secs = float(params.get("seconds", 1.0))
            secs = min(secs, 30.0)
            time.sleep(secs)
            return f"Waited {secs}s"

        if action == "clear_field":
            return _clear_field()

        if action == "focus_window":
            return _focus_window(params.get("title", ""))

        if action in ("switch_window", "switch_app", "previous_window"):
            return _switch_window()

        if action in ("minimize_window", "minimize", "minimise"):
            return _minimize_window(
                player, params.get("app_name") or params.get("title") or "")

        if action in ("restore_window", "restore", "unminimize"):
            return _restore_window(
                player, params.get("app_name") or params.get("title") or "")

        if action in ("go_back", "browser_back", "back"):
            return _browser_back()

        if action in ("undo", "undo_last"):
            return _undo()

        if action in ("close_app", "close_application", "quit_app"):
            return _close_app(
                params.get("app_name") or params.get("text") or "",
                confirmed_index=params.get("confirmed"),
            )

        if action == "random_data":
            dt     = params.get("type", "name")
            result = _random_data(dt)
            print(f"[ComputerControl] 🎲 random {dt} → {result}")
            return result

        if action == "user_data":
            field   = params.get("field", "name")
            profile = _user_profile()
            value   = profile.get(field, "")
            if not value:
                value = _random_data(field)
                print(f"[ComputerControl] ⚠️ No '{field}' in memory, using random: {value}")
            return value

        return f"Unknown action: '{action}'"

    except Exception as e:
        print(f"[ComputerControl] ❌ {action}: {e}")
        return f"computer_control '{action}' failed: {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "computer_control",
    "description": (
        "Direct computer control: type, click, hotkeys, scroll, move mouse, "
        "screenshots, find elements on screen, switch/minimise/restore "
        "windows, and CLOSE an application. "
        "Use action='close_app' with 'app_name' whenever the user asks to "
        "close, quit or exit a program - 'close Spotify', 'quit Chrome'. "
        "Always name the application in 'app_name'; there is no action that "
        "closes 'whatever is in front', because that is usually the assistant. "
        "To OPEN an application use the open_app tool instead. "
        "WINDOW VERBS, interpreted from what the user said: 'switch window' / "
        "'go to the previous app' -> action='switch_window'. 'minimize' / "
        "'minimise yourself' -> action='minimize_window' with no app_name "
        "(minimises RAVAN); 'minimize Chrome' -> app_name='Chrome'. "
        "'restore' / 'bring RAVAN back' -> action='restore_window' with no "
        "app_name; 'restore Chrome' -> app_name='Chrome'. "
        "'go back' while browsing -> action='go_back' (only sends Back when a "
        "browser is in front, otherwise it asks). 'undo that' -> "
        "action='undo' (only sends Ctrl+Z in a recognised editor, otherwise it "
        "asks). Never send a random shortcut instead: if the tool answers "
        "'ask', put its question to the user and wait. "
        "Read the status the tool returns: 'done' means it was confirmed, "
        "'unverified' means it was requested and could NOT be confirmed - say "
        "so rather than claiming success - and 'ask' means nothing was done "
        "and the question must go to the user first."
    ),
    # Used in place of `description` only when a small model is on a tool
    # budget - Gemini still gets the full text. See providers/schema.py.
    "brief": (
        "Controls the computer: type, click, hotkeys, scroll, screenshots. "
        "close_app closes a named app; switch_window, minimize_window, "
        "restore_window, go_back and undo act on the active context. Use "
        "open_app to open an application."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | switch_window | minimize_window | restore_window | go_back | undo | close_app | screen_find | screen_click | random_data | user_data"
            },
            "app_name": {
                "type": "STRING",
                "description": (
                    "For close_app: the application to close, named as the "
                    "user said it. For minimize_window and restore_window: the "
                    "application to act on; leave empty to act on RAVAN "
                    "itself. Any application running on the machine works - "
                    "there is no fixed list. Never leave this empty for "
                    "close_app."
                )
            },
            "confirmed": {
                "type": "BOOLEAN",
                "description": (
                    "For close_app: set true only after the tool asked which "
                    "application was meant AND the user answered. Pass their "
                    "answer in 'app_name'."
                )
            },
            "text": {
                "type": "STRING",
                "description": "Text to type or paste"
            },
            "x": {
                "type": "INTEGER",
                "description": "X coordinate"
            },
            "y": {
                "type": "INTEGER",
                "description": "Y coordinate"
            },
            "keys": {
                "type": "STRING",
                "description": "Key combination e.g. 'ctrl+c'"
            },
            "key": {
                "type": "STRING",
                "description": "Single key e.g. 'enter'"
            },
            "direction": {
                "type": "STRING",
                "description": "up | down | left | right"
            },
            "amount": {
                "type": "INTEGER",
                "description": "Scroll amount (default: 3)"
            },
            "seconds": {
                "type": "NUMBER",
                "description": "Seconds to wait"
            },
            "title": {
                "type": "STRING",
                "description": "Window title for focus_window"
            },
            "description": {
                "type": "STRING",
                "description": "Element description for screen_find/screen_click"
            },
            "type": {
                "type": "STRING",
                "description": "Data type for random_data"
            },
            "field": {
                "type": "STRING",
                "description": "Field for user_data: name|email|city"
            },
            "clear_first": {
                "type": "BOOLEAN",
                "description": "Clear field before typing (default: true)"
            },
            "path": {
                "type": "STRING",
                "description": "Save path for screenshot"
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": computer_control,
}
