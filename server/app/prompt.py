"""
The desktop system prompt, rendered for the phone.

core/prompt.txt is used as-is apart from one sentence: the desktop build says it
is a "native desktop application on this machine", which would be false on a
phone. Everything else — voice, judgement, acknowledgement, language rules —
is the same text the working assistant already uses. The capability and limit
blocks are derived from the phone's actual tool list, the same way main.py
derives them from the desktops.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .config import PROMPT_FILE, settings
from .tools import describe_capabilities, describe_limits

_FALLBACK = (
    "You are {assistant_name}, a personal AI assistant on the user's Android "
    "phone. Be concise, direct, and use the provided tools to complete tasks. "
    "Never report a result you did not get."
)

_PLATFORM = """[PLATFORM]
You are running in RAVAN's Android app; the user is holding the phone. A tool
you call is executed by the app itself and its result comes back to you in the
same turn, so call the tool first and describe what actually happened after the
result arrives. The phone's screen, other apps and system settings are outside
your control. Handle one action at a time. If an app is not installed, say so
and offer the browser where one exists. Never claim a message was sent or a
video is playing when the app only opened a composer or a search."""


def _render(template: str, values: dict) -> str:
    out = template or ""
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def _local_now(local_time: str | None) -> str:
    if local_time and len(local_time) <= 80:
        return local_time.strip()
    return datetime.now(timezone.utc).strftime("%A, %B %d, %Y — %I:%M %p UTC")


def build_system_prompt(local_time: str | None = None,
                        platform: str = "Android") -> str:
    try:
        template = PROMPT_FILE.read_text(encoding="utf-8")
    except Exception:
        template = _FALLBACK

    # Only this phrase changes; the rest of the prompt stays the desktop text.
    template = template.replace(
        "running as a native desktop\napplication on this machine",
        "running as a native Android\napplication on the user's phone",
    )

    prompt = _render(template, {
        "assistant_name": settings.assistant_name,
        "platform": platform,
        "capabilities": describe_capabilities(),
        "limits": describe_limits(),
    })

    user = settings.user_name
    address = (
        f"ADDRESS: Always call the user '{user}'." if user else
        "ADDRESS: Address the user with the ordinary respectful form for a "
        "superior in the language you are currently speaking — \"sir\" in "
        "English, its everyday equivalent in any other language. Never an "
        "archaic or aristocratic form, and never the form from a different "
        "language than the one you are speaking in this sentence."
    )

    header = (
        f"[CURRENT DATE & TIME]\nRight now it is: {_local_now(local_time)}\n"
        "Use this to calculate exact times. The phone's clock is authoritative "
        "if the user corrects it.\n\n"
        f"[IDENTITY]\nYour name is {settings.assistant_name}. Always refer to "
        f"yourself as {settings.assistant_name}.\n{address}\n\n"
    )
    return header + _PLATFORM + "\n\n" + prompt
