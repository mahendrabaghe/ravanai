"""
Contract tests for the cloud API, in mock mode: no network, no API key.

Run:
    RAVAN_MOCK=1 python -m unittest discover -s server/tests -v

They use FastAPI's TestClient, so they exercise the real middleware, auth,
rate limiter, session store and mock model code — not a copy of it. What they
cannot cover is the phone itself: intents, the overlay, and on-device STT/TTS
are verified on a device, not here.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from uuid import uuid4

# Must be set before server.app.config is imported: it reads the environment
# once, at import time.
os.environ["RAVAN_MOCK"] = "1"
os.environ["RAVAN_MOCK_DELAY"] = "0.01"
os.environ.pop("RAVAN_API_TOKEN", None)
os.environ.pop("GEMINI_API_KEY", None)

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from server.app import main as srv  # noqa: E402
from server.app.config import settings  # noqa: E402
from server.app.sessions import store  # noqa: E402


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(srv.app)

    def setUp(self) -> None:
        # Rate limiting is per client address and the test client always looks
        # like the same caller, so start each test from a clean window.
        srv.limiter._hits.clear()
        self.session_id = uuid4().hex

    def tearDown(self) -> None:
        srv.limiter._hits.clear()

    def chat(self, *, text=None, tool_results=None, session_id=None,
             request_id=None, headers=None):
        body = {
            "session_id": session_id or self.session_id,
            "request_id": request_id or uuid4().hex,
        }
        if text is not None:
            body["text"] = text
        if tool_results is not None:
            body["tool_results"] = tool_results
        return self.client.post("/api/chat", json=body, headers=headers or {})

    def ask(self, text, *, session_id=None):
        response = self.chat(text=text, session_id=session_id)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class HealthTests(ApiTestCase):
    def test_health_is_public_and_reports_mock_mode(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["service"], "ravan-ai")
        self.assertEqual(data["mode"], "mock")
        self.assertTrue(data["ai_configured"])
        self.assertFalse(data["auth_required"])
        # The endpoint must never leak the key, even in live mode.
        self.assertNotIn("key", " ".join(data.keys()).lower())


class ChatTests(ApiTestCase):
    def test_plain_turn_returns_reply_and_visemes(self):
        data = self.ask("hello there")
        self.assertTrue(data["reply"])
        self.assertEqual(data["mode"], "mock")
        self.assertIsNone(data["confirmation"])
        self.assertIsInstance(data["visemes"], list)
        for viseme, weight in data["visemes"][:5]:
            self.assertIsInstance(viseme, str)
            self.assertIsInstance(weight, float)
            self.assertGreaterEqual(weight, 0.0)

    def test_visemes_match_reply_text(self):
        data = self.ask("tell me something")
        self.assertTrue(data["reply"])
        self.assertGreater(len(data["visemes"]), 0)

    def test_open_app_returns_an_unconfirmed_call(self):
        data = self.ask("open youtube")
        self.assertEqual(len(data["calls"]), 1)
        call = data["calls"][0]
        self.assertEqual(call["name"], "open_app")
        self.assertEqual(call["args"], {"app": "youtube"})
        self.assertFalse(call["confirm"])
        self.assertIsNone(data["confirmation"])

    def test_open_url_is_normalised_to_https(self):
        data = self.ask("open http://example.com/page")
        self.assertEqual(data["calls"][0]["name"], "open_url")
        self.assertEqual(data["calls"][0]["args"]["url"],
                         "https://example.com/page")

    def test_local_time_is_accepted(self):
        response = self.client.post("/api/chat", json={
            "session_id": uuid4().hex,
            "request_id": uuid4().hex,
            "text": "hello",
            "local_time": "2026-10-28T18:00:00+03:00",
        })
        self.assertEqual(response.status_code, 200)


class ToolContinuationTests(ApiTestCase):
    def test_open_app_result_is_answered_with_the_outcome(self):
        first = self.ask("open youtube")
        call = first["calls"][0]

        response = self.chat(session_id=self.session_id, tool_results=[{
            "name": call["name"],
            "call_id": call["id"],
            "result": {"ok": True, "detail": "Opened YouTube."},
        }])
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertIn("Done.", data["reply"])
        self.assertIn("Opened YouTube", data["reply"])
        # The mock once appended the detail's own period to its own.
        self.assertFalse(data["reply"].endswith(".."))
        self.assertEqual(data["calls"], [])
        self.assertIsNone(data["confirmation"])

    def test_continuation_without_pending_calls_is_rejected(self):
        response = self.chat(
            session_id=uuid4().hex,
            tool_results=[{"name": "open_app", "call_id": "nope",
                           "result": {"ok": True}}],
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("pending", response.json()["detail"].lower())

    def test_unmatched_result_is_rejected(self):
        self.ask("open youtube")
        response = self.chat(session_id=self.session_id, tool_results=[{
            "name": "compose_whatsapp_message",
            "call_id": "not-a-real-id",
            "result": {"ok": True},
        }])
        self.assertEqual(response.status_code, 400)


class WhatsAppTests(ApiTestCase):
    def test_message_request_requires_confirmation(self):
        data = self.ask("send a whatsapp message to mom saying I am late")
        self.assertEqual(len(data["calls"]), 1)
        call = data["calls"][0]
        self.assertEqual(call["name"], "compose_whatsapp_message")
        self.assertTrue(call["confirm"])
        confirmation = data["confirmation"]
        self.assertIsNotNone(confirmation)
        self.assertEqual(confirmation["kind"], "whatsapp")
        self.assertEqual(confirmation["call_id"], call["id"])
        self.assertTrue(confirmation["message"])

    def test_confirmed_result_reports_composer_not_delivery(self):
        first = self.ask("send a whatsapp message saying hello")
        call = first["calls"][0]
        response = self.chat(session_id=self.session_id, tool_results=[{
            "name": call["name"],
            "call_id": call["id"],
            "result": {
                "ok": True,
                "detail": "WhatsApp is open with the message. "
                          "Nothing has been sent.",
            },
        }])
        self.assertEqual(response.status_code, 200, response.text)
        reply = response.json()["reply"].lower()
        self.assertIn("nothing has been sent", reply)
        self.assertNotIn("message sent", reply)

    def test_cancelled_result_is_reported_honestly(self):
        first = self.ask("send a whatsapp message saying hello")
        call = first["calls"][0]
        response = self.chat(session_id=self.session_id, tool_results=[{
            "name": call["name"],
            "call_id": call["id"],
            "result": {"ok": False,
                       "detail": "The user cancelled. Nothing was sent."},
        }])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("cancelled", response.json()["reply"].lower())


class ValidationTests(ApiTestCase):
    def test_text_and_results_together_is_rejected(self):
        response = self.chat(text="hi", tool_results=[{"name": "open_app"}])
        self.assertEqual(response.status_code, 400)

    def test_empty_request_is_rejected(self):
        response = self.chat()
        self.assertEqual(response.status_code, 400)

    def test_overlong_text_is_rejected(self):
        response = self.chat(text="x" * (settings.max_text + 100))
        self.assertEqual(response.status_code, 400)

    def test_short_session_id_is_rejected(self):
        response = self.chat(text="hi", session_id="abc")
        self.assertEqual(response.status_code, 422)


class DedupeTests(ApiTestCase):
    def test_repeated_request_id_returns_the_cached_reply(self):
        request_id = uuid4().hex
        first = self.chat(text="hello", request_id=request_id)
        second = self.chat(text="hello", request_id=request_id)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())

    def test_cache_is_per_session(self):
        request_id = uuid4().hex
        first = self.chat(text="hello", request_id=request_id)
        second = self.chat(text="hello", request_id=request_id,
                           session_id=uuid4().hex)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        # Same request id in a different session is a different request.
        self.assertEqual(first.json()["mode"], second.json()["mode"])


class ConcurrencyTests(ApiTestCase):
    def test_second_in_flight_request_gets_409(self):
        sid = uuid4().hex
        with TestClient(srv.app) as client:
            session = client.portal.call(store.get, sid)
            client.portal.call(session.lock.acquire)
            try:
                response = client.post("/api/chat", json={
                    "session_id": sid,
                    "request_id": uuid4().hex,
                    "text": "hello",
                })
                self.assertEqual(response.status_code, 409)
                self.assertIn("still answering",
                              response.json()["detail"].lower())
            finally:
                client.portal.call(session.lock.release)


class SecurityTests(ApiTestCase):
    def test_bearer_token_is_enforced_when_configured(self):
        original = settings.api_token
        settings.api_token = "unit-test-token"
        try:
            self.assertEqual(self.chat(text="hello").status_code, 401)
            self.assertEqual(self.chat(
                text="hello", headers={"Authorization": "Bearer wrong"}
            ).status_code, 401)
            ok = self.chat(
                text="hello",
                headers={"Authorization": "Bearer unit-test-token"},
            )
            self.assertEqual(ok.status_code, 200, ok.text)
            # Health stays public so a monitor can see the service is up.
            self.assertEqual(self.client.get("/health").status_code, 200)
        finally:
            settings.api_token = original

    def test_rate_limit_returns_429_with_retry_after(self):
        original = srv.limiter.limit
        srv.limiter.limit = 2
        try:
            self.assertEqual(self.chat(text="one").status_code, 200)
            self.assertEqual(self.chat(text="two").status_code, 200)
            third = self.chat(text="three")
            self.assertEqual(third.status_code, 429)
            self.assertIn("Retry-After", third.headers)
        finally:
            srv.limiter.limit = original

    def test_api_responses_are_not_cacheable(self):
        response = self.chat(text="hello")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_invalid_json_body_is_rejected(self):
        response = self.client.post(
            "/api/chat",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 422)


class StaticTests(ApiTestCase):
    def test_web_build_is_served_with_revalidation(self):
        response = self.client.get("/app/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers.get("content-type", ""))
        self.assertEqual(response.headers.get("Cache-Control"), "no-cache")
        self.assertIn("RAVAN", response.text.upper())


if __name__ == "__main__":
    unittest.main(verbosity=2)
