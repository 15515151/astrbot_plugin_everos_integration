"""RAG layer: inject EverOS memories into the prompt before each LLM request.

On every LLM request the plugin searches EverOS and appends the results to the
system prompt, so the model starts each turn already knowing relevant memories
instead of having to call a tool for them.

Who gets searched is decided by the caller (main.py): scope "self" searches only
the current speaker; scope "all" also searches every user in the plugin's app
space, so the assistant can answer about other people. The caller builds the
target list itself -- a model-supplied owner id is never trusted.

Best-effort: any failure or timeout simply means no injection.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .memory_reader import discover_owners

MARKER = "【EverOS 长期记忆（仅供你参考）】"

_MAX_TARGETS = 30  # cap owners searched per request
_CONCURRENCY = 8  # in-flight searches


def discover_user_targets(
    data_dir: str | None,
    app_id: str,
) -> list[tuple[str, str, str]]:
    """Every (app_id, project_id, user_id) inside this plugin's app space."""
    targets: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for owner in discover_owners(data_dir):
        if owner.get("owner_type") != "user":
            continue
        owner_app = owner.get("app_id") or ""
        if app_id and not (
            owner_app == app_id or owner_app.startswith(app_id + "_")
        ):
            continue
        key = (
            owner_app,
            owner.get("project_id") or "default",
            owner.get("owner_id") or "",
        )
        if key[2] and key not in seen:
            seen.add(key)
            targets.append(key)
    return targets


async def _search_one(
    client: httpx.AsyncClient,
    base_url: str,
    semaphore: asyncio.Semaphore,
    target: tuple[str, str, str],
    query: str,
    per_target: int,
) -> list[dict[str, Any]]:
    app_id, project_id, user_id = target
    payload = {
        "query": query,
        "user_id": user_id,
        "app_id": app_id,
        "project_id": project_id,
        "method": "hybrid",
        "top_k": per_target,
    }
    try:
        async with semaphore:
            resp = await client.post(
                base_url.rstrip("/") + "/api/v2/memory/search", json=payload
            )
            resp.raise_for_status()
            data = resp.json().get("data", {}) or {}
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for key, kind in (("episodes", "episode"), ("profiles", "profile")):
        for item in data.get(key, []) or []:
            if isinstance(item, dict):
                item.setdefault("memory_type", kind)
                item.setdefault("user_id", user_id)
                out.append(item)
    return out


async def fetch_memories(
    base_url: str,
    targets: list[tuple[str, str, str]],
    *,
    query: str,
    top_k: int = 5,
    timeout: float = 6.0,
) -> list[dict[str, Any]]:
    """Search every target in parallel and merge, best score first."""
    query = (query or "").strip()
    if not query:
        return []

    ordered: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for target in targets:
        if not target or not target[2]:
            continue
        key = (target[0], target[1], target[2])
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)
        if len(ordered) >= _MAX_TARGETS:
            break
    if not ordered:
        return []

    top_k = max(1, int(top_k))
    per_target = max(2, top_k)
    semaphore = asyncio.Semaphore(_CONCURRENCY)
    async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
        batches = await asyncio.gather(
            *(
                _search_one(client, base_url, semaphore, target, query, per_target)
                for target in ordered
            )
        )

    results: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for batch in batches:
        for item in batch:
            mid = item.get("id") or ""
            if mid and mid in seen_ids:
                continue
            if mid:
                seen_ids.add(mid)
            results.append(item)
    results.sort(key=lambda it: it.get("score") or 0, reverse=True)
    return results[:top_k]


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
        owner = item.get("user_id") or item.get("agent_id")
        tag = str(kind) + (("/" + str(owner)) if owner else "")
        chunk = "- [" + tag + "] " + text
        if used + len(chunk) > max_chars:
            break
        lines.append(chunk)
        used += len(chunk)
    if not lines:
        return ""
    header = (
        MARKER
        + "\n以下是系统检索到的、可能相关的历史记忆（可能来自其他用户，"
        + "不是本轮用户说的话）："
    )
    return header + "\n" + "\n".join(lines) + "\n（与当前问题无关时可忽略。）"
