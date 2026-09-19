"""配置管理器 — 默认值 + 类型安全访问。"""

from __future__ import annotations

from typing import Any

_DEFAULTS: dict[str, Any] = {
    "everos_base_url": "http://127.0.0.1:8000",
    "enable_tools": True,
    # 是否允许 LLM 工具 everos_recall 用 user_id="*" 查询所有用户的记忆。
    # 关闭时工具描述里不会暴露 "*"，模型也无法触发全量查询。
    "allow_query_all_memories": False,
    "enable_webui": True,
    "app_id": "astrbot",
    "project_id": "default",
    "standalone_webui_enabled": True,
    "standalone_webui_host": "0.0.0.0",
    "standalone_webui_port": 18766,
    "isolation_personas": "",
    # Host path of the EverOS memory root (the everos-data bind mount).
    # The WebUI walks it to discover every real user_id/agent_id, because
    # EverOS has no "list owners" API.
    "everos_data_dir": "/opt/EverOS/everos-data",
    # ── 自动对话记忆（对话轨）──────────────────────────────────
    # 把每轮真实对话 /add 给 EverOS，由 EverOS 的边界检测决定何时抽取；
    # 空闲或条数超限时由后台循环兜底 flush。
    "auto_capture_enabled": False,
    "auto_capture_mode": "both",  # both / user
    "auto_capture_scope": "all",  # all / private / group
    # true=按人隔离会话，同一群里每个人与机器人的对话分别缓冲/提炼；
    # false=按整个群会话一起缓冲/提炼。
    "auto_capture_per_user": True,
    "auto_capture_sessions": "",  # 可选白名单(unified_msg_origin, 逗号分隔)
    "auto_capture_idle_flush_seconds": 300,
    "auto_capture_max_pending": 80,
    # 空闲提炼前单会话至少需要的用户轮数（1 轮 = 1 条用户消息）。
    # 达到就提炼；空闲超过阈值仍不足则直接丢弃，不留在待提炼里。
    "auto_capture_min_turns": 1,
    "auto_capture_min_chars": 2,
    # 写入记忆时用于标注机器人发言的名称（人格名 / 自称）。
    # 留空则自动使用当前人格名，仍无法解析时退化为「我」。
    "assistant_display_name": "",
    # ── 记忆自动注入（RAG）────────────────────────────────────
    # 每次 LLM 请求前检索记忆并追加到 system prompt。目标由插件构造：
    # scope=self 只搜当前说话人；scope=all 还搜本应用空间内所有用户。
    "memory_injection_enabled": False,
    "memory_injection_scope": "self",  # self / all
    "memory_injection_top_k": 5,
    "memory_injection_timeout": 6.0,
    "memory_injection_max_chars": 1500,
}


class ConfigManager:
    def __init__(self, raw: dict[str, Any] | None = None):
        self._data = {**_DEFAULTS, **(raw or {})}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    @property
    def everos_base_url(self) -> str:
        return self.get("everos_base_url")

    @property
    def enable_tools(self) -> bool:
        return self.get("enable_tools")

    @property
    def enable_webui(self) -> bool:
        return self.get("enable_webui")

    @property
    def app_id(self) -> str:
        return self.get("app_id")

    @property
    def project_id(self) -> str:
        return self.get("project_id")

    @property
    def everos_data_dir(self) -> str:
        """Host path of the EverOS memory root, used to discover owners."""
        return self.get("everos_data_dir", "")

    # ─── 记忆隔离 ──────────────────────────────────────────────

    @property
    def isolation_personas(self) -> list[str]:
        """获取隔离白名单人格列表。"""
        raw = self.get("isolation_personas", "")
        if not raw or not raw.strip():
            return []
        return [p.strip() for p in raw.split(",") if p.strip()]

    def is_isolated(self, persona_name: str | None) -> bool:
        """判断指定人格是否在隔离白名单中。"""
        if not persona_name:
            return False
        return persona_name in self.isolation_personas

    def get_app_id_for(self, persona_name: str | None) -> str:
        """获取指定人格应使用的 app_id。

        在隔离白名单中的人格 → 使用独立的 app_id（默认 app_id + 人格名）
        不在白名单中的人格  → 使用全局默认 app_id
        """
        base = self.app_id
        if persona_name and self.is_isolated(persona_name):
            return f"{base}_{persona_name}"
        return base
