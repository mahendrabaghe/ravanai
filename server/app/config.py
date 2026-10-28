"""
Server configuration, read once from the environment.

No secrets are ever written to disk or logged. On the desktop the Gemini key
lives in config/api_keys.json; in the cloud deployment it must come from the
host's environment (Render -> Environment), never from a committed file.
"""
from __future__ import annotations

import os
from pathlib import Path

VERSION = "1.0.0"

# Repo root: server/app/config.py -> server/app -> server -> repo root
ROOT = Path(__file__).resolve().parents[2]

# Where the assistant's own prompt lives. Reused verbatim from the desktop app.
PROMPT_FILE = ROOT / "core" / "prompt.txt"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


class Settings:
    # Live vs offline contract tests. Mock mode answers deterministically and
    # makes no network calls, which is what the automated tests use.
    mock: bool = _env("RAVAN_MOCK", "0").lower() in ("1", "true", "yes", "on")
    mock_delay: float = _env_float("RAVAN_MOCK_DELAY", 0.15)

    gemini_api_key: str = _env("GEMINI_API_KEY")
    # Explicit REST model. The desktop's own ladder (core/gemini.py) still sits
    # behind this choice, so a quota error falls through to the next rung.
    model: str = _env("GEMINI_MODEL", "gemini-2.5-flash")
    chat_timeout_s: float = _env_float("CHAT_TIMEOUT_S", 35.0)

    assistant_name: str = _env("ASSISTANT_NAME", "RAVAN")
    user_name: str = _env("USER_NAME", "")

    # Optional bearer token. Strongly recommended on the public internet; when
    # empty the API is open and /health reports auth=false so it is visible.
    api_token: str = _env("RAVAN_API_TOKEN")

    # Comma-separated list. "*" is allowed because auth is a bearer header, not
    # a cookie, but pinning the Pages origin is the better deployment.
    allowed_origins: list[str] = [
        o.strip() for o in _env("ALLOWED_ORIGINS", "*").split(",") if o.strip()
    ]

    rate_limit_per_min: int = _env_int("RATE_LIMIT_PER_MIN", 30)
    session_ttl_s: int = _env_int("SESSION_TTL_S", 30 * 60)
    max_sessions: int = _env_int("MAX_SESSIONS", 200)
    max_history: int = _env_int("MAX_HISTORY", 24)
    max_text: int = _env_int("MAX_TEXT_CHARS", 2000)

    @property
    def ai_configured(self) -> bool:
        return bool(self.mock or self.gemini_api_key)


settings = Settings()

if not settings.mock and not settings.gemini_api_key:
    print("[RAVAN] GEMINI_API_KEY is not set — /api/chat will return 503. "
          "Set it in the host environment, or run with RAVAN_MOCK=1 for tests.")
