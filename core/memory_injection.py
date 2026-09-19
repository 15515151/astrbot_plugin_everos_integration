"""RAG layer: inject the current speaker's EverOS memories into the prompt.

On every LLM request the plugin searches EverOS for the sender's own memories
and appends them to the system prompt, so the model starts each turn already
knowing what it has remembered about that person, instead of having to call a
tool for it.

Retrieval is keyed on the caller-supplied user_id -- the plugin passes
event.get_sender_id() -- never on a model-supplied id, so a speaker can only
ever see their own memories. Best-effort: any failure simply means no injection.
"""

from __future__ import annotations

from typing import Any

import httpx

MARKER = "【EverOS 长期记忆（仅供你参考）】"


async def fetch_memories(
    base_url: str,
    *,
    user_id: str,
    app_id: str,
    project_id: str,
    query: str,
    top_k: int = 5,
    timeout: float = 6.0,
) -> list[dict[str, Any]]:
    """Search one owner's memories; returns [] on any failure."""
    if not user_id or not (query or "").strip():
        return []
    payload = {
        "query": query,
        "user_id": user_id,
        "app_id": app_id,
        "project_id": project_id,
        "method": "hybrid",
        "top_k": max(1, int(top_k)),
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            resp = await client.post(
                base_url.rstrip("/") + "/api/v2/memory/search", json=payload
            )
            resp.raise_for_status()
            data = resp.json().get("data", {}) or {}
    except Exception:
        return []
    items = list(data.get("episodes", []) or []) + list(data.get("profiles", []) or [])
    return [item for item in items if isinstance(item, dict)]


def _item_text(item: dict[str, Any]) -> str:
    for key in ("episode", "summary", "content"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            inner = value.get("content") or value.get("summary")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    profile_data = item.get("profile_data")
    if isinstance(profile_data, dict):
        summary = profile_data.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
    subject = item.get("subject")
    return subject.strip() if isinstance(subject, str) else ""


def build_block(items: list[dict[str, Any]], *, max_chars: int = 1500) -> str:
    """Render retrieved memories into a system-prompt block (empty if none)."""
    lines: list[str] = []
    seen: set[str] = set()
    used = 0
    for item in items:
        text = _item_text(item)
        if not text:
            continue
        key = text[:80]
        if key in seen:
            continue
        seen.add(key)
        kind = item.get("memory_type") or (
            "profile" if "profile_data" in item else "episode"
        )
        chunk = "- [" + str(kind) + "] " + text
        if used + len(chunk) > max_chars:
            break
        lines.append(chunk)
        used += len(chunk)
    if not lines:
        return ""
    header = (
        MARKER
        + "\n以下是系统检索到的、关于当前用户的历史记忆（不是本轮用户说的话）："
    )
    return header + "\n" + "\n".join(lines) + "\n（与当前问题无关时可忽略。）"
