"""
The four actions the phone deployment can actually perform.

These are deliberately a small subset of the desktop's twenty action modules:
everything here maps to an Android intent the client can execute. The model is
told exactly what each one does — in particular that WhatsApp opens a composer
and YouTube opens search results — so it cannot report a message as sent or a
video as playing when the phone only opened an app.

The client is the executor. The server never runs these; it only validates the
arguments, returns the calls, and takes the results back for the follow-up turn.
"""
from __future__ import annotations

from urllib.parse import urlparse

# Keep this list identical to APP_PACKAGES in android/.../PhoneActionsPlugin.kt.
APP_ALLOWLIST = {
    "chrome", "youtube", "whatsapp", "settings", "downloads", "camera",
    "maps", "gmail", "spotify", "instagram", "telegram", "calculator", "clock",
}

TOOL_SPECS: list[dict] = [
    {
        "name": "open_app",
        "description": (
            "Open an installed app on the user's phone. Allowed: chrome, youtube, "
            "whatsapp, settings, downloads, camera, maps, gmail, spotify, "
            "instagram, telegram, calculator, clock. Android app only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "One of the allowed app names.",
                }
            },
            "required": ["app"],
        },
    },
    {
        "name": "open_url",
        "description": (
            "Open an https web page in the phone's browser. Use for sites that are "
            "not one of the allowed apps."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute https:// URL."}
            },
            "required": ["url"],
        },
    },
    {
        "name": "play_on_youtube",
        "description": (
            "Open YouTube's search results for a song or video. This starts a "
            "search, NOT playback — the user picks a result. Never claim it played."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Title plus artist, or a description of the video.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "compose_whatsapp_message",
        "description": (
            "Open WhatsApp with a message pre-filled. It does NOT send: the client "
            "shows the contact and text for confirmation, then the user sends it. "
            "Only use when the user explicitly asks to message someone."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contact": {
                    "type": "string",
                    "description": "Contact name, or a phone number with country code.",
                },
                "message": {"type": "string", "description": "Message text."},
            },
            "required": ["message"],
        },
    },
]

_BY_NAME = {t["name"]: t for t in TOOL_SPECS}


def describe_capabilities() -> str:
    """Same shape as main.py's _describe_tools, so the prompt reads identically."""
    lines = []
    for spec in TOOL_SPECS:
        desc = " ".join(spec["description"].split())
        lines.append(f"- {spec['name']}: {desc[:150]}")
    return "\n".join(lines)


def describe_limits() -> str:
    return "\n".join([
        "- You act only on the user's phone, through the tools above. You have "
        "no sight, no webcam, no file access, no keyboard or system-settings "
        "control, and you cannot see what any other app is doing after it opens.",
        "- WhatsApp messages are composed, not sent: the user confirms and taps "
        "send. Never say a message was sent — say the composer is open.",
        "- YouTube is opened on a search for the requested title; playback is "
        "the user's next tap. Never claim a video is playing or that you can "
        "pause it, and there are no media controls in this build.",
        "- The phone version does not listen continuously. It hears one request "
        "at a time after the user taps the microphone, and nothing while muted.",
        "- You remember only this conversation. There is no long-term memory in "
        "the phone deployment, and nothing is saved after the session ends.",
        "- Anything not listed above is outside your reach. Say so in one clause "
        "and offer the nearest thing you can actually do.",
    ])


class ToolArgError(ValueError):
    pass


def validate_call(name: str, args: dict) -> dict:
    """Return normalised args or raise ToolArgError with a user-safe message."""
    if name not in _BY_NAME:
        raise ToolArgError(f"unknown tool: {name}")
    args = args or {}

    if name == "open_app":
        app = " ".join(str(args.get("app") or "").lower().split())
        aliases = {
            "google chrome": "chrome", "chrome browser": "chrome",
            "youtube app": "youtube", "whats app": "whatsapp",
            "google maps": "maps", "gmail app": "gmail",
            "settings app": "settings", "my downloads": "downloads",
            "files": "downloads", "downloads folder": "downloads",
            "camera app": "camera", "clock app": "clock",
            "calculator app": "calculator",
        }
        app = aliases.get(app, app)
        if app not in APP_ALLOWLIST:
            raise ToolArgError(
                f"'{app or 'that app'}' is not in the allowed app list")
        return {"app": app}

    if name == "open_url":
        url = str(args.get("url") or "").strip()
        if len(url) > 2048:
            raise ToolArgError("URL is too long")
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            raise ToolArgError("only absolute http(s) URLs are allowed")
        if parsed.scheme.lower() == "http":
            url = "https://" + url[len("http://"):]
        return {"url": url}

    if name == "play_on_youtube":
        query = " ".join(str(args.get("query") or "").split())
        if not query or len(query) > 160:
            raise ToolArgError("a YouTube search needs 1-160 characters")
        return {"query": query}

    if name == "compose_whatsapp_message":
        message = str(args.get("message") or "").strip()
        contact = " ".join(str(args.get("contact") or "").split())
        if not message or len(message) > 1000:
            raise ToolArgError("a WhatsApp message needs 1-1000 characters")
        if len(contact) > 80:
            raise ToolArgError("contact is too long")
        return {"contact": contact, "message": message}

    raise ToolArgError(f"unknown tool: {name}")


def needs_confirmation(name: str) -> bool:
    return name == "compose_whatsapp_message"
