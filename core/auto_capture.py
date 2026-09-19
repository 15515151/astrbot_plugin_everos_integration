"""Automatic conversation capture (the "conversation track") for the plugin.

Feeds each real conversation turn to EverOS /memory/add and lets EverOS's own
boundary detection extract episodes. It deliberately does NOT flush after every
add: flushing per turn makes every turn its own episode and defeats
summarisation. A background loop flushes a session once it has been idle for
idle_flush_seconds, or once it holds max_pending messages, so short or
single-topic conversations still get extracted eventually.

This module only depends on httpx + stdlib; the EverOS package is not needed.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .memory_reader import flush_session, list_buffered_sessions

_ID_SAFE_RE = re.compile(r"[^a-zA-Z0-9_.@+-]")


def sanitize_id(raw: str, fallback: str = "session") -> str:
    """Make an arbitrary string safe for an EverOS session_id.

    EverOS ids must match ^[a-zA-Z0-9_.@+-]+$ , so AstrBot's
    unified_msg_origin (which contains colons) has to be rewritten before it
    can be used as a stable per-conversation session id.
    """
    cleaned = _ID_SAFE_RE.sub("_", (raw or "").strip())
    return cleaned or fallback


def _age_seconds(ts: str | None) -> float:
    """Seconds since a UTC timestamp string; 0.0 when unknown/unparseable."""
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


class AutoCapture:
    """Record conversation turns and apply the flush fallback.

    Args:
        config: ConfigManager (or any object with .get(key, default)).
        log: logger with .debug/.info/.warning methods.
        interval_seconds: how often the maintenance loop scans the buffer.
    """

    def __init__(self, config: Any, log: Any, *, interval_seconds: float = 30.0):
        self.config = config
        self.log = log
        self.interval_seconds = interval_seconds
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._enabled_override: bool | None = None

    # -- lifecycle ---------------------------------------------------

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0, verify=False)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._maintenance_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- config helpers ----------------------------------------------

    @property
    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return self._enabled_override
        return bool(self.config.get("auto_capture_enabled", False))

    def set_enabled(self, value: bool) -> None:
        """Runtime toggle; not persisted (change the plugin config for that)."""
        self._enabled_override = value

    @property
    def _base_url(self) -> str:
        return str(self.config.get("everos_base_url", "")).rstrip("/")

    def _scope_matches(self, event: Any) -> bool:
        scope = str(self.config.get("auto_capture_scope", "all")).lower()
        is_group = bool(event.get_group_id())
        if scope == "private":
            return not is_group
        if scope == "group":
            return is_group
        return True

    def _session_allowed(self, session_id: str) -> bool:
        raw = str(self.config.get("auto_capture_sessions", "") or "")
        allowed = [s.strip() for s in raw.split(",") if s.strip()]
        return (not allowed) or (session_id in allowed)

    # -- capture -----------------------------------------------------

    async def record_turn(self, event: Any, response: Any) -> None:
        """Feed one conversation turn; returns immediately (fire-and-forget)."""
        if not self.enabled or self._client is None:
            return
        if not self._scope_matches(event):
            return

        user_text = (event.get_message_str() or "").strip()
        reply_text = ""
        if response is not None:
            reply_text = (getattr(response, "completion_text", "") or "").strip()

        min_chars = int(self.config.get("auto_capture_min_chars", 2) or 0)
        if len(user_text) < min_chars:
            return
        # Never capture the plugin's own management commands.
        if user_text.startswith("/") or user_text.startswith("／"):
            return

        session_id = sanitize_id(
            event.unified_msg_origin or event.get_session_id() or "session"
        )
        if not self._session_allowed(session_id):
            return

        sender_id = event.get_sender_id() or event.get_sender_name() or "unknown"
        ts = int(time.time() * 1000)
        messages = [
            {"sender_id": sender_id, "role": "user", "timestamp": ts,
             "content": user_text}
        ]
        mode = str(self.config.get("auto_capture_mode", "both")).lower()
        if mode != "user" and reply_text:
            messages.append({
                "sender_id": event.get_self_id() or "assistant",
                "role": "assistant",
                "timestamp": ts + 1,
                "content": reply_text,
            })

        payload = {
            "session_id": session_id,
            "app_id": self.config.get("app_id", "astrbot"),
            "project_id": self.config.get("project_id", "default"),
            "messages": messages,
        }
        asyncio.create_task(self._post_add(payload))

    async def _post_add(self, payload: dict[str, Any]) -> None:
        try:
            resp = await self._client.post(
                f"{self._base_url}/api/v2/memory/add", json=payload
            )
            resp.raise_for_status()
            status = (resp.json().get("data") or {}).get("status", "?")
            self.log.debug(
                f"[EverOS] auto-capture add session={payload['session_id']} status={status}"
            )
        except Exception as exc:
            self.log.debug(f"[EverOS] auto-capture add failed: {exc}")

    # -- flush fallback ----------------------------------------------

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval_seconds)
                await self.flush_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.warning(f"[EverOS] auto-capture maintenance error: {exc}")

    async def flush_due(self) -> list[dict[str, Any]]:
        """Flush sessions that are idle or over the pending cap. Returns them."""
        if not self.enabled:
            return []
        idle = float(self.config.get("auto_capture_idle_flush_seconds", 300) or 0)
        max_pending = int(self.config.get("auto_capture_max_pending", 80) or 0)
        sessions = list_buffered_sessions(
            self.config.get("everos_data_dir", ""),
            app_id=self.config.get("app_id", "astrbot"),
        )
        flushed: list[dict[str, Any]] = []
        for session in sessions:
            pending = int(session.get("pending") or 0)
            age = _age_seconds(session.get("last_updated"))
            due = (max_pending > 0 and pending >= max_pending) or (
                idle > 0 and age >= idle
            )
            if not due:
                continue
            status = await flush_session(
                self._base_url,
                session["session_id"],
                app_id=session["app_id"],
                project_id=session["project_id"],
            )
            flushed.append({**session, "status": status})
            self.log.info(
                f"[EverOS] auto-flush session={session['session_id']} "
                f"pending={pending} age={int(age)}s status={status}"
            )
        return flushed
