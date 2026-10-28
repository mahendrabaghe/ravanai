"""
One assistant turn, on the server.

Reused unchanged from the desktop:
  * core/prompt.txt (with only the "desktop" sentence adapted for the phone);
  * core/gemini.py's client, timeout handling and model ladder;
  * core/viseme.py's text-to-mouth sequence, which the client animates while
    the phone's TTS speaks.

New here: the phone's function-call protocol. The model asks for an Android
action; this server validates the arguments and hands the call to the client.
The client executes it (intent), posts the result, and the model answers with
what actually happened. No tool is executed on the server.
"""
from __future__ import annotations

import asyncio
import time

from .config import settings
from .prompt import build_system_prompt
from .sessions import Session
from .tools import TOOL_SPECS, ToolArgError, needs_confirmation, validate_call

from core.viseme import text_to_visemes  # pure stdlib, safe on the server


class AIUnavailable(RuntimeError):
    """The model could not answer (no key, quota, timeout, outage)."""


class AIRejected(RuntimeError):
    """The invocation itself was invalid (bad tool results, unknown session)."""


def _types():
    from google.genai import types
    return types


def _visemes(text: str) -> list[list]:
    try:
        pairs = text_to_visemes(text or "")
    except Exception:
        return []
    return [[v, round(float(w), 3)] for v, w in pairs][:400]


def _config(local_time: str | None):
    types = _types()
    return types.GenerateContentConfig(
        system_instruction=build_system_prompt(local_time=local_time),
        temperature=0.65,
        max_output_tokens=700,
        tools=[types.Tool(function_declarations=TOOL_SPECS)],
    )


def _reply_text(resp) -> str:
    try:
        return (resp.text or "").strip()
    except Exception:
        return ""


def _extract(resp):
    """(text, calls) from a model response. Invalid calls are dropped."""
    text = _reply_text(resp)
    calls = []
    for i, fc in enumerate(getattr(resp, "function_calls", None) or []):
        name = getattr(fc, "name", "") or ""
        raw = dict(getattr(fc, "args", None) or {})
        try:
            args = validate_call(name, raw)
        except ToolArgError:
            continue
        calls.append({
            "name": name,
            "args": args,
            "id": getattr(fc, "id", None) or f"call-{i+1}",
            "confirm": needs_confirmation(name),
        })
    return text, calls


async def _generate(session: Session, contents, local_time: str | None):
    from core import gemini as g

    if settings.mock:
        await asyncio.sleep(settings.mock_delay)
        return _mock_response(session, contents)

    if not settings.gemini_api_key:
        raise AIUnavailable(
            "The server has no Gemini API key configured. Set GEMINI_API_KEY "
            "in the backend environment.")

    config = _config(local_time)
    timeout_ms = int(settings.chat_timeout_s * 1000)
    try:
        resp = await asyncio.to_thread(
            g.call, contents, settings.model, config, timeout_ms,
            settings.gemini_api_key,
        )
    except Exception as exc:
        print(f"[RAVAN] model call failed: {type(exc).__name__}")
        raise AIUnavailable("The assistant model could not be reached.") from exc
    if resp is None:
        raise AIUnavailable(
            "The assistant model is unavailable right now (quota or outage). "
            "Try again shortly.")
    return resp


def _mock_response(session: Session, contents):
    """Deterministic replies for tests and UI demos. No network, no key."""
    last = contents[-1] if contents else None
    text = ""
    results = []
    if last is not None:
        for part in getattr(last, "parts", []) or []:
            if getattr(part, "text", None):
                text += part.text
            fr = getattr(part, "function_response", None)
            if fr is not None:
                results.append(fr)
    text = (text or "").strip()
    low = text.lower()

    class _R:
        pass

    r = _R()
    r.text = ""
    r.function_calls = []
    r.candidates = []

    def call(name, args):
        class _FC:
            pass
        fc = _FC()
        fc.name, fc.args, fc.id = name, args, f"mock-{int(time.time()*1000)}"
        r.function_calls.append(fc)

    if results:
        fr = results[-1]
        res = {}
        try:
            res = dict(getattr(fr, "response", None) or {}).get("result", {}) or {}
        except Exception:
            pass
        status = res.get("detail") or ("done" if res.get("ok") else "not completed")
        status = str(status).strip().rstrip(".")
        r.text = f"Done. {str(fr.name).replace('_', ' ')}: {status}."
    elif "whatsapp" in low:
        call("compose_whatsapp_message", {
            "contact": "",
            "message": "Hello, I will call you later.",
        })
        r.text = "I have the WhatsApp message ready for you to confirm."
    elif "play " in low and ("youtube" in low or "song" in low or "music" in low):
        query = text
        for lead in ("open youtube and play ", "play on youtube ",
                     "play ", "search youtube for "):
            if lead in low:
                query = text[low.index(lead) + len(lead):]
                break
        call("play_on_youtube", {"query": query.strip(" .") or "music"})
        r.text = "Opening YouTube search for that."
    elif low.startswith("open ") and ("http://" in low or "https://" in low):
        url = text[low.index("http"):].split()[0]
        call("open_url", {"url": url})
        r.text = "Opening that link."
    elif low.startswith("open "):
        app = low.replace("open ", "", 1).split()[0].strip(".")
        call("open_app", {"app": app})
        r.text = f"Opening {app}."
    else:
        r.text = (
            "Mock mode is on, so I am not calling the live model. The mobile "
            "interface, voice pipeline and Android actions are testable; set "
            "GEMINI_API_KEY on the backend for real answers."
        )
    return r


def _append_model_content(session: Session, resp) -> None:
    try:
        content = resp.candidates[0].content
        if content is not None:
            session.history.append(content)
            return
    except Exception:
        pass
    # Fallback: reconstruct a model turn so the continuation is not orphaned.
    types = _types()
    parts = []
    text = _reply_text(resp)
    if text:
        parts.append(types.Part(text=text))
    for fc in getattr(resp, "function_calls", None) or []:
        parts.append(types.Part(function_call=types.FunctionCall(
            name=fc.name, args=dict(fc.args or {}))))
    if parts:
        session.history.append(types.Content(role="model", parts=parts))


async def run_user_turn(session: Session, text: str,
                        local_time: str | None = None) -> dict:
    types = _types()
    session.history.append(types.Content(
        role="user", parts=[types.Part(text=text)]))
    session.pending = []

    resp = await _generate(session, session.history, local_time)
    reply, calls = _extract(resp)
    _append_model_content(session, resp)
    session.trim()

    confirmation = None
    if calls:
        session.pending = calls
        for c in calls:
            if c["confirm"]:
                confirmation = {
                    "kind": "whatsapp",
                    "call_id": c["id"],
                    "contact": c["args"].get("contact", ""),
                    "message": c["args"].get("message", ""),
                }
                break

    return {
        "reply": reply,
        "calls": calls,
        "confirmation": confirmation,
        "visemes": _visemes(reply),
        "mode": "mock" if settings.mock else "live",
    }


async def run_tool_continuation(session: Session, results: list[dict],
                                local_time: str | None = None) -> dict:
    types = _types()
    pending = {c["id"]: c for c in session.pending}
    if not pending:
        raise AIRejected("There are no pending actions for this session.")

    parts = []
    for item in results:
        name = str(item.get("name") or "")
        call_id = str(item.get("call_id") or "")
        call = pending.get(call_id) or next(
            (c for c in pending.values() if c["name"] == name), None)
        if call is None:
            continue
        out = item.get("result")
        if not isinstance(out, dict):
            out = {"ok": False, "detail": str(out)[:300]}
        out = {k: (str(v)[:500] if not isinstance(v, (int, float, bool, type(None)))
                   else v) for k, v in out.items()}
        parts.append(types.Part(function_response=types.FunctionResponse(
            name=call["name"],
            response={"result": out},
            id=call["id"],
        )))
    if not parts:
        raise AIRejected("None of those results matched a pending action.")

    session.history.append(types.Content(role="user", parts=parts))
    resp = await _generate(session, session.history, local_time)
    reply, calls = _extract(resp)
    _append_model_content(session, resp)
    session.pending = calls
    session.trim()

    if not reply and not calls:
        reply = "Done."

    return {
        "reply": reply,
        "calls": calls,
        "confirmation": None,
        "visemes": _visemes(reply),
        "mode": "mock" if settings.mock else "live",
    }
