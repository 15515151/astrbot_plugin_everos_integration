"""Read EverOS memory across every owner (user / agent) in a memory root.

POST /api/v2/memory/get (and /search) require a concrete user_id XOR agent_id
and expose no "list owners" endpoint, so a dashboard that wants to show every
memory has to discover the ids itself.

The EverOS memory root is md-first, so the ids are on disk:

    <root>/<app_dir>/<project_dir>/users/<user_id>/...
    <root>/<app_dir>/<project_dir>/agents/<agent_id>/...

app_dir / project_dir are the raw ids, except the reserved "default" which
materialises as default_app / default_project (see
everos.core.persistence.memory_root; the mapping is inverted here).

This module walks that layout, then reads each owner through the HTTP API.
It depends only on httpx -- no everos install is required in AstrBot venv.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

_DEFAULT_APP_DIR = "default_app"
_DEFAULT_PROJECT_DIR = "default_project"

USER_TYPES: tuple[str, ...] = ("episode", "profile")
AGENT_TYPES: tuple[str, ...] = ("agent_case", "agent_skill")
ALL_TYPES: tuple[str, ...] = USER_TYPES + AGENT_TYPES

_OWNER_TYPE: dict[str, str] = {
    "episode": "user",
    "profile": "user",
    "agent_case": "agent",
    "agent_skill": "agent",
}
_RESULT_KEY: dict[str, str] = {
    "episode": "episodes",
    "profile": "profiles",
    "agent_case": "agent_cases",
    "agent_skill": "agent_skills",
}

_PAGE_SIZE = 100
_MAX_PAGES = 100  # 10k rows per owner, guards against a runaway loop


def _app_id_from_dir(name: str) -> str:
    return "default" if name == _DEFAULT_APP_DIR else name


def _project_id_from_dir(name: str) -> str:
    return "default" if name == _DEFAULT_PROJECT_DIR else name


def discover_owners(data_dir: str | None) -> list[dict[str, str]]:
    """Return every app_id/project_id/owner_type/owner_id triple on disk.

    Empty when data_dir is unset or not a directory -- callers fall back to
    their legacy candidate list in that case.
    """
    if not data_dir:
        return []
    root = Path(data_dir).expanduser()
    if not root.is_dir():
        return []

    owners: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def _add(app_id: str, project_id: str, owner_type: str, owner_id: str) -> None:
        key = (app_id, project_id, owner_type, owner_id)
        if key in seen:
            return
        seen.add(key)
        owners.append(
            {
                "app_id": app_id,
                "project_id": project_id,
                "owner_type": owner_type,
                "owner_id": owner_id,
            }
        )

    for app_dir in sorted(root.iterdir()):
        if not app_dir.is_dir() or app_dir.name.startswith("."):
            continue
        for proj_dir in sorted(app_dir.iterdir()):
            if not proj_dir.is_dir() or proj_dir.name.startswith("."):
                continue
            app_id = _app_id_from_dir(app_dir.name)
            project_id = _project_id_from_dir(proj_dir.name)
            users_dir = proj_dir / "users"
            if users_dir.is_dir():
                for entry in sorted(users_dir.iterdir()):
                    if entry.is_dir() and not entry.name.startswith("."):
                        _add(app_id, project_id, "user", entry.name)
            agents_dir = proj_dir / "agents"
            if agents_dir.is_dir():
                for entry in sorted(agents_dir.iterdir()):
                    if entry.is_dir() and not entry.name.startswith("."):
                        _add(app_id, project_id, "agent", entry.name)
    return owners


def _fallback_owners(
    app_id: str,
    project_id: str,
    extra_uids: Iterable[str] = (),
) -> list[dict[str, str]]:
    """Legacy guess-list, used only when the memory root is not reachable.

    Mirrors the pre-fix behaviour (app_id, "default", "webui") so an
    unconfigured deployment degrades instead of going blank.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for uid in [app_id, "default", "webui", *extra_uids]:
        if uid and uid not in seen:
            seen.add(uid)
            out.append(
                {
                    "app_id": app_id,
                    "project_id": project_id,
                    "owner_type": "user",
                    "owner_id": uid,
                }
            )
    return out


def _resolve_owners(
    data_dir: str | None, app_id: str, project_id: str
) -> list[dict[str, str]]:
    owners = discover_owners(data_dir)
    if not owners:
        return _fallback_owners(app_id, project_id)
    # Stay inside this plugin's app space: the configured app_id plus any
    # isolation-persona variants (app_id + "_<persona>"). Other apps that
    # happen to share the same memory root are not this bot's memories.
    scoped = [
        owner
        for owner in owners
        if owner["app_id"] == app_id or owner["app_id"].startswith(f"{app_id}_")
    ]
    return scoped or owners


def _owner_payload(owner: dict[str, str]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "app_id": owner["app_id"],
        "project_id": owner["project_id"],
    }
    if owner["owner_type"] == "user":
        payload["user_id"] = owner["owner_id"]
    else:
        payload["agent_id"] = owner["owner_id"]
    return payload


def _tag(item: dict[str, Any], owner: dict[str, str], memory_type: str) -> None:
    item["memory_type"] = item.get("memory_type") or memory_type
    item.setdefault("app_id", owner["app_id"])
    item.setdefault("project_id", owner["project_id"])
    if owner["owner_type"] == "user":
        item.setdefault("user_id", owner["owner_id"])
    else:
        item.setdefault("agent_id", owner["owner_id"])


async def _get_owner(
    client: httpx.AsyncClient,
    base_url: str,
    owner: dict[str, str],
    memory_type: str,
) -> list[dict[str, Any]]:
    key = _RESULT_KEY[memory_type]
    base = {"memory_type": memory_type, **_owner_payload(owner)}
    items: list[dict[str, Any]] = []
    for page in range(1, _MAX_PAGES + 1):
        resp = await client.post(
            f"{base_url}/api/v2/memory/get",
            json={**base, "page": page, "page_size": _PAGE_SIZE},
        )
        resp.raise_for_status()
        data = resp.json().get("data", {}) or {}
        batch = data.get(key, []) or []
        items.extend(batch)
        if len(batch) < _PAGE_SIZE:
            break
    return items


async def fetch_all(
    base_url: str,
    data_dir: str | None,
    memory_types: Iterable[str] = ALL_TYPES,
    *,
    app_id: str = "astrbot",
    project_id: str = "default",
) -> list[dict[str, Any]]:
    """Fetch every row of the requested types, across every discovered owner."""
    owners = _resolve_owners(data_dir, app_id, project_id)
    base_url = base_url.rstrip("/")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        for mtype in memory_types:
            if mtype not in _OWNER_TYPE:
                continue
            owner_type = _OWNER_TYPE[mtype]
            for owner in owners:
                if owner["owner_type"] != owner_type:
                    continue
                try:
                    batch = await _get_owner(client, base_url, owner, mtype)
                except Exception:
                    continue
                for item in batch:
                    if not isinstance(item, dict):
                        continue
                    mid = item.get("id") or ""
                    if mid and mid in seen:
                        continue
                    if mid:
                        seen.add(mid)
                    _tag(item, owner, mtype)
                    items.append(item)
    return items


async def count_by_type(
    base_url: str,
    data_dir: str | None,
    *,
    app_id: str = "astrbot",
    project_id: str = "default",
) -> dict[str, int]:
    """Count rows per memory type without downloading them all."""
    owners = _resolve_owners(data_dir, app_id, project_id)
    base_url = base_url.rstrip("/")
    counts: dict[str, int] = {mtype: 0 for mtype in ALL_TYPES}

    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        for mtype in ALL_TYPES:
            owner_type = _OWNER_TYPE[mtype]
            for owner in owners:
                if owner["owner_type"] != owner_type:
                    continue
                try:
                    resp = await client.post(
                        f"{base_url}/api/v2/memory/get",
                        json={
                            "memory_type": mtype,
                            **_owner_payload(owner),
                            "page": 1,
                            "page_size": 1,
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json().get("data", {}) or {}
                    counts[mtype] += int(data.get("total_count") or 0)
                except Exception:
                    continue
    return counts


async def search(
    base_url: str,
    data_dir: str | None,
    query: str,
    top_k: int = 10,
    *,
    app_id: str = "astrbot",
    project_id: str = "default",
) -> list[dict[str, Any]]:
    """Search every discovered owner and merge the ranked results."""
    query = (query or "").strip()
    if not query:
        return []
    owners = _resolve_owners(data_dir, app_id, project_id)
    base_url = base_url.rstrip("/")
    top_k = int(top_k)
    per_owner = max(1, top_k // max(1, len(owners)))
    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
        for owner in owners:
            try:
                resp = await client.post(
                    f"{base_url}/api/v2/memory/search",
                    json={
                        "query": query,
                        **_owner_payload(owner),
                        "top_k": per_owner,
                    },
                )
                resp.raise_for_status()
                data = resp.json().get("data", {}) or {}
            except Exception:
                continue
            for key in ("episodes", "profiles", "agent_cases", "agent_skills"):
                for item in data.get(key, []) or []:
                    if not isinstance(item, dict):
                        continue
                    mid = item.get("id") or ""
                    if mid and mid in seen:
                        continue
                    if mid:
                        seen.add(mid)
                    _tag(item, owner, key.rstrip("s"))
                    results.append(item)

    results.sort(key=lambda it: it.get("score") or 0, reverse=True)
    return results[:top_k]


def list_buffered_sessions(
    data_dir: str | None,
    *,
    app_id: str = "astrbot",
) -> list[dict[str, Any]]:
    """Return sessions that still have messages waiting in EverOS buffer.

    EverOS exposes no "list sessions" endpoint, so the only way to know which
    session_id a manual flush should target is the unprocessed_buffer table of
    the system SQLite. Read-only and best-effort: returns [] when the database
    is missing or unreadable.

    Each item is {app_id, project_id, session_id, pending}.
    """
    if not data_dir:
        return []
    db_path = Path(data_dir).expanduser() / ".index" / "sqlite" / "system.db"
    if not db_path.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute(
            "SELECT app_id, project_id, session_id, COUNT(*) "
            "FROM unprocessed_buffer "
            "GROUP BY app_id, project_id, session_id"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()

    out: list[dict[str, Any]] = []
    for db_app, db_project, session_id, pending in rows:
        # Same app-space filter used for reads: the configured app_id plus
        # its isolation-persona variants.
        if app_id and not (
            db_app == app_id or str(db_app).startswith(f"{app_id}_")
        ):
            continue
        out.append(
            {
                "app_id": db_app,
                "project_id": db_project,
                "session_id": session_id,
                "pending": int(pending),
            }
        )
    return out


async def flush_session(
    base_url: str,
    session_id: str,
    *,
    app_id: str = "astrbot",
    project_id: str = "default",
) -> str:
    """Flush one session; return EverOS's data.status string."""
    base_url = base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
        resp = await client.post(
            f"{base_url}/api/v2/memory/flush",
            json={
                "session_id": session_id,
                "app_id": app_id,
                "project_id": project_id,
            },
        )
        resp.raise_for_status()
        return (resp.json().get("data") or {}).get("status", "unknown")


async def flush_buffered(
    base_url: str,
    data_dir: str | None,
    *,
    app_id: str = "astrbot",
    project_id: str = "default",
) -> list[dict[str, Any]]:
    """Flush every session currently holding buffered messages.

    Returns one {app_id, project_id, session_id, pending, status} entry per
    flushed session; an empty list means the buffer was already empty.
    """
    sessions = list_buffered_sessions(data_dir, app_id=app_id)
    results: list[dict[str, Any]] = []
    for session in sessions:
        try:
            status = await flush_session(
                base_url,
                session["session_id"],
                app_id=session["app_id"],
                project_id=session["project_id"],
            )
        except Exception as exc:  # surface per-session failure, keep going
            status = f"error: {exc}"
        results.append({**session, "status": status})
    return results
