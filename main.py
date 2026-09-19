"""
EverOS for AstrBot — 为 AstrBot 接入 GitHub 上的 EverOS 自进化记忆引擎。

让 AstrBot 的 Agent 能直接使用 EverOS 的记忆写入/检索能力，
通过 Plugin Pages 管理面板监控服务状态。

功能：
- 连接 EverOS REST API（独立容器部署）
- 注册 LLM 工具：everos_memorize / everos_recall
- Plugin Page 管理面板：状态监控 + 记忆统计 + 快速测试
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from quart import jsonify, request

from .core.auto_capture import AutoCapture
from .core.config_manager import ConfigManager
from .core.everos_client import EverOSClient
from .core.memory_reader import (
    count_by_type,
    fetch_all,
    flush_buffered,
    flush_session,
    list_buffered_messages,
    list_buffered_sessions,
)
from .core.memory_injection import (
    MARKER,
    build_block,
    discover_user_targets,
    fetch_memories,
)
from .core.memory_reader import search as read_search
from .core.standalone_server import StandaloneServer
from .tools.everos_tools import EverOSLearnTool, EverOSMemorizeTool, EverOSRecallTool

PLUGIN_NAME = "astrbot_plugin_everos_integration"


def _normalize_item(item: dict, mtype: str = "episode") -> dict:
    """统一记忆条目的字段名（将 EverOS 各类型字段映射为 content）。"""
    if not item.get("content"):
        if mtype == "episode":
            item["content"] = (
                item.get("episode")  # 完整内容优先
                or item.get("summary")
                or item.get("subject")
                or json.dumps(item, ensure_ascii=False)[:200]
            )
        elif "profile_data" in item:
            pd = item["profile_data"]
            if isinstance(pd, dict):
                item["content"] = pd.get("summary", json.dumps(pd, ensure_ascii=False)[:200])
            else:
                item["content"] = str(pd)[:200]
        else:
            item["content"] = json.dumps(item, ensure_ascii=False)[:200]
    return item


@register(
    PLUGIN_NAME,
    "白芷 & Masumeiki",
    "为 AstrBot 集成 EverOS 自进化记忆引擎，让 Agent 拥有长期记忆与自我学习能力",
    "1.1.0",
    "https://github.com/Masumeiki/astrbot_plugin_everos_integration",
)
class EverOSIntegrationPlugin(Star):
    """EverOS Integration 插件主类。"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.context = context
        self.config = ConfigManager(config or {})
        self.data_dir = str(StarTools.get_data_dir())

        # 运行时状态
        self._client: EverOSClient | None = None
        self._tools_registered = False
        self._healthy = False
        self._standalone_server: StandaloneServer | None = None
        self._auto_capture: AutoCapture | None = None

        # 注册 Web API
        self._register_web_apis()

        # 异步启动初始化
        self._bg_task = asyncio.create_task(self._initialize())

    # ─── Web API 路由 ──────────────────────────────────────────────

    def _register_web_apis(self) -> None:
        """注册 Plugin Page 后端 API。"""
        try:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/status",
                self.api_status,
                ["GET"],
                "EverOS 服务状态与统计",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/memories",
                self.api_memories,
                ["GET"],
                "获取 EverOS 记忆列表",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/test-memorize",
                self.api_test_memorize,
                ["POST"],
                "测试记忆写入 EverOS",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/memorize",
                self.api_memorize,
                ["POST"],
                "写入单条记忆到 EverOS",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/memories-by-type",
                self.api_memories_by_type,
                ["POST"],
                "按类型获取记忆",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/search",
                self.api_search,
                ["POST"],
                "语义检索记忆",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/flush",
                self.api_flush,
                ["POST"],
                "触发记忆提炼",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/pending",
                self.api_pending,
                ["GET"],
                "待提炼消息（缓冲区）",
            )
            logger.info("📊 EverOS Web API 已注册（全功能）")
        except Exception as e:
            logger.warning(f"Web API 注册失败: {e}")

    async def api_status(self):
        """GET /api/plug/everos_integration/status"""
        if self._client is None:
            return jsonify({"healthy": False, "error": "client not initialized"})

        healthy = await self._client.is_healthy()
        stats = {}
        latency = None
        if healthy:
            try:
                t0 = time.monotonic()
                # 从记忆根目录自动发现所有 user_id / agent_id 再统计
                stats = await count_by_type(
                    self.config.everos_base_url,
                    self.config.everos_data_dir,
                    app_id=self.config.app_id,
                    project_id=self.config.project_id,
                )
                latency = int((time.monotonic() - t0) * 1000)
            except Exception as e:
                stats = {"error": str(e)}

        return jsonify({
            "healthy": healthy,
            "latency": latency,
            "base_url": self.config.everos_base_url,
            "app_id": self.config.app_id,
            "project_id": self.config.project_id,
            "stats": stats,
        })

    async def api_memories(self):
        """GET /api/plug/everos_integration/memories

        获取最近记忆（从所有类型中取最新 10 条）。
        """
        if self._client is None:
            return jsonify({"ok": False, "error": "client not initialized", "data": {"items": []}})

        try:
            items = await fetch_all(
                self.config.everos_base_url,
                self.config.everos_data_dir,
                app_id=self.config.app_id,
                project_id=self.config.project_id,
            )
            all_items = [
                _normalize_item(dict(item), item.get("memory_type", "episode"))
                for item in items
            ]

            # 按时间倒序，取前 10
            def _sort_key(item):
                ts = item.get("timestamp") or item.get("created_at") or 0
                if isinstance(ts, str):
                    try:
                        from datetime import datetime
                        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        return 0
                return ts

            if all_items:
                all_items.sort(key=_sort_key, reverse=True)
                all_items = all_items[:10]

            return jsonify({"ok": True, "data": {"items": all_items}})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e), "data": {"items": []}})

    async def api_test_memorize(self):
        """POST /api/plug/everos_integration/test-memorize"""
        if self._client is None:
            return jsonify({"ok": False, "error": "client not initialized"})

        try:
            body = await request.get_json()
        except Exception:
            body = {}

        content = body.get("content", "AstrBot EverOS Integration 测试消息")
        user_id = body.get("user_id", "test")

        try:
            ts = int(time.time() * 1000)
            await self._client.memory_add(
                session_id=f"webui-test-{ts}",
                messages=[{
                    "sender_id": user_id,
                    "role": "user",
                    "timestamp": ts,
                    "content": content,
                }],
                app_id=self.config.app_id,
                project_id=self.config.project_id,
            )
            await self._client.memory_flush(
                session_id=f"webui-test-{ts}",
                app_id=self.config.app_id,
                project_id=self.config.project_id,
            )
            return jsonify({"ok": True, "message": f"已写入并提取: {content}"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    async def api_memorize(self):
        """POST /api/plug/everos_integration/memorize

        简化写入接口，供 Dashboard 使用。
        """
        if self._client is None:
            return jsonify({"ok": False, "error": "client not initialized"})

        try:
            body = await request.get_json()
        except Exception:
            body = {}

        content = body.get("content", "").strip()
        if not content:
            return jsonify({"ok": False, "error": "内容为空"})

        user_id = body.get("user_id", "webui")
        ts = int(time.time() * 1000)

        try:
            await self._client.memory_add(
                session_id=f"webui-{user_id}-{ts}",
                messages=[{
                    "sender_id": user_id,
                    "role": "user",
                    "timestamp": ts,
                    "content": content,
                }],
                app_id=self.config.app_id,
                project_id=self.config.project_id,
            )
            await self._client.memory_flush(
                session_id=f"webui-{user_id}-{ts}",
                app_id=self.config.app_id,
                project_id=self.config.project_id,
            )
            return jsonify({"ok": True, "status": "ok", "message": "记忆已写入"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    async def api_memories_by_type(self):
        """POST /api/plug/everos_integration/memories-by-type

        按类型获取记忆列表。
        """
        if self._client is None:
            return jsonify({"ok": False, "error": "client not initialized", "data": {"items": []}})

        try:
            body = await request.get_json()
        except Exception:
            body = {}

        memory_type = body.get("memory_type", "episode")

        try:
            items = await fetch_all(
                self.config.everos_base_url,
                self.config.everos_data_dir,
                [memory_type],
                app_id=self.config.app_id,
                project_id=self.config.project_id,
            )
            all_items = [
                _normalize_item(dict(item), memory_type) for item in items
            ]
            return jsonify({"ok": True, "data": {"items": all_items}})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e), "data": {"items": []}})

    async def api_search(self):
        """POST /api/plug/everos_integration/search

        语义检索记忆。
        """
        if self._client is None:
            return jsonify({"ok": False, "error": "client not initialized", "results": []})

        try:
            body = await request.get_json()
        except Exception:
            body = {}

        query = body.get("query", "").strip()
        if not query:
            return jsonify({"ok": False, "error": "查询为空", "results": []})

        top_k = min(body.get("top_k", 10), 50)

        try:
            items = await read_search(
                self.config.everos_base_url,
                self.config.everos_data_dir,
                query,
                top_k=top_k,
                app_id=self.config.app_id,
                project_id=self.config.project_id,
            )
            all_items = [
                _normalize_item(dict(item), item.get("memory_type", "episode"))
                for item in items
            ]
            return jsonify({"ok": True, "data": {"items": all_items}})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e), "results": []})

    async def api_pending(self):
        """GET /api/plug/everos_integration/pending"""
        if self._client is None:
            return jsonify({
                "ok": False,
                "error": "client not initialized",
                "data": {"sessions": [], "messages": []},
            })
        try:
            sessions = list_buffered_sessions(
                self.config.everos_data_dir, app_id=self.config.app_id
            )
            messages = list_buffered_messages(
                self.config.everos_data_dir, app_id=self.config.app_id
            )
            return jsonify({
                "ok": True,
                "data": {"sessions": sessions, "messages": messages},
            })
        except Exception as e:
            return jsonify({
                "ok": False,
                "error": str(e),
                "data": {"sessions": [], "messages": []},
            })

    async def api_flush(self):
        """POST /api/plug/everos_integration/flush"""
        if self._client is None:
            return jsonify({"ok": False, "error": "client not initialized"})

        try:
            body = await request.get_json()
        except Exception:
            body = {}

        session_id = (body.get("session_id") or "").strip()
        try:
            if session_id:
                status = await flush_session(
                    self.config.everos_base_url,
                    session_id,
                    app_id=self.config.app_id,
                    project_id=self.config.project_id,
                )
                return jsonify({
                    "ok": True,
                    "status": status,
                    "flushed": 1,
                    "message": f"会话 {session_id} → {status}",
                })
            results = await flush_buffered(
                self.config.everos_base_url,
                self.config.everos_data_dir,
                app_id=self.config.app_id,
                project_id=self.config.project_id,
            )
            if not results:
                return jsonify({
                    "ok": True,
                    "status": "no_pending",
                    "flushed": 0,
                    "message": "缓冲区为空：当前没有待提炼的消息",
                })
            return jsonify({
                "ok": True,
                "status": "ok",
                "flushed": len(results),
                "sessions": results,
                "message": f"已提炼 {len(results)} 个会话",
            })
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    # ─── 初始化 ────────────────────────────────────────────────────

    async def _initialize(self) -> None:
        """异步初始化：连接 EverOS + 注册工具。"""
        try:
            self._client = EverOSClient(
                base_url=self.config.everos_base_url,
            )
            self._healthy = await self._client.is_healthy()

            if self._healthy:
                logger.info(f"✅ EverOS 连接成功: {self.config.everos_base_url}")
            else:
                logger.warning(
                    f"⚠️ EverOS 连接失败: {self.config.everos_base_url}，"
                    f"插件以降级模式运行"
                )

            if self.config.enable_tools and self._healthy:
                self._register_tools()

        except Exception as e:
            logger.error(f"EverOS Integration 初始化失败: {e}", exc_info=True)

        # 启动自动对话记忆（对话轨）：只 /add，不逐条 flush，交给边界检测 + 后台兜底
        try:
            self._auto_capture = AutoCapture(self.config, logger)
            await self._auto_capture.start()
            logger.info(
                f"[EverOS] 自动对话记忆: "
                f"{'已开启' if self._auto_capture.enabled else '未开启'}"
                f"（scope={self.config.get('auto_capture_scope', 'all')}, "
                f"mode={self.config.get('auto_capture_mode', 'both')}）"
            )
        except Exception as e:
            logger.warning(f"[EverOS] 自动对话记忆启动失败: {e}（不影响插件主体功能）")

        # 启动独立 WebUI 服务器（下载即用，访问 http://IP:18766）
        try:
            self._standalone_server = StandaloneServer(self)
            asyncio.create_task(self._standalone_server.start())
        except Exception as e:
            logger.warning(f"[EverOS] 独立 WebUI 启动失败: {e}（不影响插件主体功能）")

    def _register_tools(self) -> None:
        """注册 LLM 工具。"""
        if self._tools_registered or self._client is None:
            return

        tools = [
            EverOSLearnTool(self._client, self.config),
            EverOSMemorizeTool(self._client, self.config),
            EverOSRecallTool(self._client, self.config),
        ]
        try:
            self.context.add_llm_tools(*tools)
            self._tools_registered = True
            logger.info("🔧 LLM 工具已注册: everos_learn, everos_memorize, everos_recall")
        except Exception as e:
            logger.error(f"LLM 工具注册失败: {e}", exc_info=True)

    # ─── 记忆自动注入（RAG）──────────────────────────────────────────

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """每次 LLM 请求前，检索记忆并注入 system prompt。

        目标列表由插件自己构造：默认只搜当前说话人；配置
        memory_injection_scope=all 时会额外搜索本应用空间内的所有用户，
        因此 AI 也能在对话中引用别人的记忆。失败/超时静默跳过。
        """
        if not self.config.get("memory_injection_enabled", False):
            return
        if self._client is None:
            return
        if MARKER in (getattr(req, "system_prompt", "") or ""):
            return
        sender_id = event.get_sender_id()
        if not sender_id:
            return
        app_id = self.config.app_id
        project_id = self.config.project_id
        targets = [(app_id, project_id, sender_id)]
        if str(self.config.get("memory_injection_scope", "self")).lower() == "all":
            targets += discover_user_targets(self.config.everos_data_dir, app_id)
        try:
            items = await fetch_memories(
                self.config.everos_base_url,
                targets,
                query=event.get_message_str() or "",
                top_k=int(self.config.get("memory_injection_top_k", 5)),
                timeout=float(self.config.get("memory_injection_timeout", 6.0)),
            )
            block = build_block(
                items,
                max_chars=int(self.config.get("memory_injection_max_chars", 1500)),
            )
            if not block:
                return
            base = getattr(req, "system_prompt", "") or ""
            req.system_prompt = (base + "\n" + block) if base else block
            logger.debug(f"[EverOS] memory injection: {len(items)} for {sender_id}")
        except Exception as e:
            logger.debug(f"[EverOS] memory injection skipped: {e}")

    # ─── 自动对话记忆（对话轨）────────────────────────────────────────

    @filter.on_llm_response()
    async def on_llm_response(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        """每轮 LLM 回复结束后，把这轮真实对话喂给 EverOS。

        只 /add 不 flush：由 EverOS 边界检测决定何时抽取，空闲/超限时由
        AutoCapture 的后台循环兜底。异常只记 debug，绝不影响正常聊天。
        """
        if self._auto_capture is None:
            return
        try:
            await self._auto_capture.record_turn(event, response)
        except Exception as e:
            logger.debug(f"[EverOS] auto-capture skipped: {e}")

    # ─── 命令组 ──────────────────────────────────────────────────────

    @filter.command_group("everos")
    def everos(self):
        """EverOS 记忆管理命令组"""
        pass

    @permission_type(PermissionType.ADMIN)
    @everos.command("status", priority=10)
    async def cmd_everos_status(self, event: AstrMessageEvent):
        """/everos status — 查看 EverOS 连接状态"""
        if self._healthy:
            yield event.plain_result(
                f"🧠 **EverOS Integration** v1.1.0\n"
                f"✅ 服务在线: {self.config.everos_base_url}\n"
                f"📱 App: `{self.config.app_id}`\n"
                f"📦 Project: `{self.config.project_id}`\n"
                f"🔧 LLM 工具: {'已注册' if self._tools_registered else '未注册'}"
            )
        else:
            yield event.plain_result(
                f"🧠 **EverOS Integration** v1.1.0\n"
                f"❌ 服务离线: {self.config.everos_base_url}\n"
                f"\n请确认 EverOS 容器是否正在运行。"
            )

    @permission_type(PermissionType.ADMIN)
    @everos.command("memorize")
    async def cmd_everos_memorize(
        self, event: AstrMessageEvent, content: str
    ):
        """/everos memorize <内容> — 手动存储一条记忆到 User Track"""
        if not self._client:
            yield event.plain_result("❌ EverOS 客户端未初始化")
            return

        tool = EverOSMemorizeTool(self._client, self.config)
        result = await tool(content=content)
        yield event.plain_result(result)

    @permission_type(PermissionType.ADMIN)
    @everos.command("learn")
    async def cmd_everos_learn(
        self, event: AstrMessageEvent, content: str
    ):
        """/everos learn <内容> — 手动存储一条技能/规则到 Agent Track"""
        if not self._client:
            yield event.plain_result("❌ EverOS 客户端未初始化")
            return

        tool = EverOSLearnTool(self._client, self.config)
        result = await tool(content=content)
        yield event.plain_result(result)

    @permission_type(PermissionType.ADMIN)
    @everos.command("flush")
    async def cmd_everos_flush(
        self, event: AstrMessageEvent, session_id: str = ""
    ):
        """/everos flush [会话ID] — 立即触发记忆提炼

        不带参数时自动发现缓冲区中所有待提炼的会话；带参数时只提炼该会话。
        """
        if not self._client:
            yield event.plain_result("❌ EverOS 客户端未初始化")
            return

        session_id = (session_id or "").strip()
        try:
            if session_id:
                status = await flush_session(
                    self.config.everos_base_url,
                    session_id,
                    app_id=self.config.app_id,
                    project_id=self.config.project_id,
                )
                yield event.plain_result(
                    f"✅ 会话 {session_id} 提炼完成（状态: {status}）"
                )
                return

            before_stats = await self._get_memory_stats()
            results = await flush_buffered(
                self.config.everos_base_url,
                self.config.everos_data_dir,
                app_id=self.config.app_id,
                project_id=self.config.project_id,
            )
            if not results:
                yield event.plain_result(
                    "⏳ 缓冲区为空：当前没有待提炼的消息。\n"
                    "（插件的每次写入都会立即提炼，所以正常情况下这里就是空的；"
                    "如果以后开启了对话缓存，这条命令会把它提炼出来。）"
                )
                return

            lines = [f"✅ 已触发 {len(results)} 个会话的提炼："]
            for item in results:
                lines.append(
                    f"  • {item.get('session_id')}"
                    f"（{item.get('pending', '?')} 条）→ {item.get('status')}"
                )
            after_stats = await self._get_memory_stats()
            diff = after_stats.get("episode", 0) - before_stats.get("episode", 0)
            if diff > 0:
                lines.append(
                    f"\n📈 Episode: {before_stats.get('episode', 0)}"
                    f" → {after_stats.get('episode', 0)} (+{diff})"
                )
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"❌ 触发失败: {e}")

    async def _get_memory_stats(self) -> dict[str, int]:
        """查询当前记忆库各类型的数量。"""
        try:
            return await count_by_type(
                self.config.everos_base_url,
                self.config.everos_data_dir,
                app_id=self.config.app_id,
                project_id=self.config.project_id,
            )
        except Exception:
            return {"episode": 0, "profile": 0, "agent_case": 0, "agent_skill": 0}

    @permission_type(PermissionType.ADMIN)
    @everos.command("search")
    async def cmd_everos_search(
        self, event: AstrMessageEvent, query: str
    ):
        """/everos search <关键词> — 搜索 EverOS 记忆"""
        if not self._client:
            yield event.plain_result("❌ EverOS 客户端未初始化")
            return

        tool = EverOSRecallTool(self._client, self.config)
        result = await tool(query=query)
        yield event.plain_result(result)

    @permission_type(PermissionType.ADMIN)
    @everos.command("remove")
    async def cmd_everos_remove(
        self, event: AstrMessageEvent, memory_id: str
    ):
        """/everos remove <记忆ID> — 删除指定记忆"""
        if not self._client:
            yield event.plain_result("❌ EverOS 客户端未初始化")
            return

        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as c:
                resp = await c.post(
                    f"http://127.0.0.1:18766/api/everos/forget",
                    json={"id": memory_id, "memory_type": "episode"},
                )
                result = resp.json()
                if result.get("ok"):
                    yield event.plain_result(f"✅ 已删除记忆: {memory_id}")
                else:
                    yield event.plain_result(f"❌ 删除失败: {result.get('error', '未知错误')}")
        except Exception as e:
            yield event.plain_result(f"❌ 删除失败: {e}")

    @permission_type(PermissionType.ADMIN)
    @everos.command("capture")
    async def cmd_everos_capture(self, event: AstrMessageEvent, state: str = ""):
        """/everos capture [on|off] — 查看/切换自动对话记忆"""
        if self._auto_capture is None:
            yield event.plain_result("❌ 自动对话记忆未初始化")
            return

        arg = (state or "").strip().lower()
        if arg in ("on", "1", "true", "开", "开启"):
            self._auto_capture.set_enabled(True)
            yield event.plain_result(
                "✅ 自动对话记忆：已开启（仅本次运行有效；要持久化请改插件配置 "
                "auto_capture_enabled）"
            )
        elif arg in ("off", "0", "false", "关", "关闭"):
            self._auto_capture.set_enabled(False)
            yield event.plain_result("🛑 自动对话记忆：已关闭（仅本次运行有效）")
        else:
            state_txt = "开启" if self._auto_capture.enabled else "关闭"
            yield event.plain_result(
                f"📥 自动对话记忆当前：{state_txt}\n"
                f"  范围：{self.config.get('auto_capture_scope', 'all')}  "
                f"记录：{self.config.get('auto_capture_mode', 'both')}\n"
                f"  空闲提炼：{self.config.get('auto_capture_idle_flush_seconds', 300)}s  "
                f"条数上限：{self.config.get('auto_capture_max_pending', 80)}\n"
                "用法：/everos capture on|off"
            )

    @permission_type(PermissionType.ADMIN)
    @everos.command("help")
    async def cmd_everos_help(self, event: AstrMessageEvent):
        """/everos help — 显示帮助信息"""
        yield event.plain_result(
            "🧠 **EverOS 命令帮助**\n\n"
            "/everos status         — 查看连接状态\n"
            "/everos memorize <内容> — 手动存储记忆（User Track）\n"
            "/everos learn <内容>    — 手动存储技能（Agent Track）\n"
            "/everos flush [会话ID]  — 立即触发记忆提炼（不带参数=提炼所有待处理会话）\n"
            "/everos search <关键词> — 搜索记忆\n"
            "/everos capture [on|off] — 查看/切换自动对话记忆\n"
            "/everos remove <记忆ID> — 删除指定记忆\n"
            "/everos help           — 显示此帮助"
        )

    # ─── 生命周期 ──────────────────────────────────────────────────

    async def terminate(self) -> None:
        """插件卸载时关闭 HTTP 客户端、自动捕获和独立 WebUI。"""
        if self._auto_capture:
            await self._auto_capture.stop()
        if self._client:
            await self._client.close()
        if self._standalone_server:
            await self._standalone_server.stop()
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
        logger.info("EverOS Integration 已关闭")
