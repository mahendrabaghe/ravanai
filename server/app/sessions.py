"""
Conversation state, in memory only.

There is no database and no persistence on purpose: the phone keeps the visible
transcript, the server keeps the model's working context for the length of a
session, and both forget when the session expires or the service restarts.
Nothing here writes to disk, and user text is never logged.

Two safety properties live here:

* a per-session lock, so two requests cannot race into two assistant replies;
* a request-id cache, so a client retrying after a dropped connection gets the
  same answer instead of making the model answer twice.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .config import settings


@dataclass
class Session:
    id: str
    history: list = field(default_factory=list)      # google-genai Content objects
    pending: list[dict] = field(default_factory=list)  # function calls awaiting results
    replies: dict[str, tuple[float, dict]] = field(default_factory=dict)
    created: float = field(default_factory=time.time)
    used: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def touch(self) -> None:
        self.used = time.time()

    def remember_reply(self, request_id: str, payload: dict) -> None:
        self.replies[request_id] = (time.time(), payload)
        if len(self.replies) > 20:
            oldest = min(self.replies, key=lambda k: self.replies[k][0])
            self.replies.pop(oldest, None)

    def cached(self, request_id: str) -> dict | None:
        hit = self.replies.get(request_id)
        if not hit:
            return None
        when, payload = hit
        if time.time() - when > 300:
            self.replies.pop(request_id, None)
            return None
        return payload

    def trim(self) -> None:
        if len(self.history) > settings.max_history:
            self.history = self.history[-settings.max_history:]


class SessionStore:
    def __init__(self) -> None:
        self._items: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    async def get(self, session_id: str) -> Session:
        now = time.time()
        async with self._lock:
            dead = [k for k, s in self._items.items()
                    if now - s.used > settings.session_ttl_s]
            for k in dead:
                self._items.pop(k, None)
            if len(self._items) >= settings.max_sessions:
                oldest = min(self._items, key=lambda k: self._items[k].used)
                self._items.pop(oldest, None)
            item = self._items.get(session_id)
            if item is None:
                item = Session(id=session_id)
                self._items[session_id] = item
            item.touch()
            return item

    async def count(self) -> int:
        async with self._lock:
            return len(self._items)


store = SessionStore()
