# EverOS for AstrBot

**为 AstrBot 接入 EverOS 记忆引擎：长期记忆、自动对话总结、可检索的用户画像与 Agent 技能。**

---

## ✨ 功能

| 功能 | 说明 |
|---|---|
| 🔌 **服务桥接** | 通过 HTTP API 连接独立部署的 EverOS |
| 🔧 **LLM 工具** | `everos_memorize`（用户记忆）/ `everos_learn`（Agent 技能）/ `everos_recall`（检索） |
| 💬 **自动对话记忆** | 每轮对话自动写入 EverOS，由边界检测抽取；空闲或超限自动提炼 |
| 🧠 **记忆自动注入** | 每次对话前按当前说话人检索其记忆并注入提示词，无需模型调用工具；**严格按人隔离** |
| 📥 **待提炼视图** | Dashboard 查看缓冲区中尚未提炼的消息全文，支持一键提炼 |
| 📊 **WebUI 管理面板** | 状态 / 记忆仓库 / 待提炼 / 检索 / 技能库 / 系统 / 设置 |
| 🌐 **独立 WebUI** | 安装即启动，浏览器访问 `http://<IP>:18766/` |
| 🧩 **记忆隔离** | 按人格白名单使用独立 `app_id` |
| 🌏 **语言跟随对话** | 中文对话产出中文记忆（EverOS 默认行为，无需额外配置） |

---

## 📦 一、部署 EverOS

插件需要一个已运行的 EverOS 服务。**EverOS 默认监听 `127.0.0.1:8000`**。

### 方式 A：源码 / uv

```bash
git clone https://github.com/EverMind-AI/EverOS.git
cd EverOS
uv sync
everos init                 # 生成 <root>/everos.toml（默认根目录 ~/.everos）

# 配置模型（二选一）
#   1) 编辑 ~/.everos/everos.toml 的 [llm] 段
#   2) 或使用环境变量：
export EVEROS_LLM__MODEL=gpt-4.1-mini
export EVEROS_LLM__BASE_URL=https://api.openai.com/v1
export EVEROS_LLM__API_KEY=sk-...

everos server start         # 默认 127.0.0.1:8000
curl http://127.0.0.1:8000/health
```

> ⚠️ 两个常见误解：
> 1. `everos init` 生成的是 `everos.toml` / `ome.toml`，**不是 `.env`**；EverOS 也不会自动读取 `.env`。
> 2. 配置项用 `<root>/everos.toml`，或 `EVEROS_<SECTION>__<KEY>` 环境变量（如 `EVEROS_API__HOST=0.0.0.0`、`EVEROS_API__PORT=8000`）。

### 方式 B：Docker

EverOS 仓库自带 `Dockerfile` 与 `docker-compose.yml`：

```bash
git clone https://github.com/EverMind-AI/EverOS.git
cd EverOS
cp .env.example .env        # 填入 EVEROS_LLM__* 等
docker compose up -d --build
curl http://127.0.0.1:8000/health
```

或手动构建运行（`-v` 把记忆目录挂到宿主机，插件需要读它）：

```bash
docker build -t everos:latest .
docker run -d --name everos -p 8000:8000 -v "$PWD/everos-data:/data/everos" -e EVEROS_ROOT=/data/everos -e EVEROS_LLM__MODEL=gpt-4.1-mini -e EVEROS_LLM__BASE_URL=https://api.openai.com/v1 -e EVEROS_LLM__API_KEY=sk-... everos:latest
```

### 与 AstrBot 的连通

| AstrBot 位置 | `everos_base_url` |
|---|---|
| 与 EverOS 同宿主机 | `http://127.0.0.1:8000` |
| AstrBot 在容器、EverOS 在宿主机 | `http://host.docker.internal:8000`（Linux 需 `--add-host=host.docker.internal:host-gateway`，或直接用宿主机内网 IP） |
| 远程服务器 | `http://<服务器IP>:8000` |

---

## 📥 二、安装插件

AstrBot 后台 → 插件市场 → 搜索 `everos` 安装；或把本目录放到 `data/plugins/`。

依赖（插件市场会自动安装，见 `requirements.txt`）：`httpx`、`fastapi`、`uvicorn`。

### 配置连接

AstrBot 后台 → 插件配置：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `everos_base_url` | `http://127.0.0.1:8000` | EverOS 服务地址 |
| `everos_data_dir` | `/opt/EverOS/everos-data` | **宿主机上 EverOS 的记忆根目录**（Docker 部署时是挂载出来的 `everos-data`）。用于自动发现全部记忆、读取待提炼缓冲区。留空则退化为只查 `app_id` / `default` / `webui` 三个猜测值 |
| `app_id` / `project_id` | `astrbot` / `default` | 记忆分区 |
| `isolation_personas` | `""` | 隔离人格白名单（逗号分隔） |

> `everos_data_dir` 必须能被插件进程访问。若 AstrBot 与 EverOS 不在一台机器上，请留空（但记忆列表会退化为猜测的 `user_id`，且待提炼视图不可用）。

---

## ⚙️ 完整配置

| 配置项 | 默认 | 说明 |
|---|---|---|
| `everos_base_url` | `http://127.0.0.1:8000` | EverOS 地址 |
| `everos_data_dir` | `/opt/EverOS/everos-data` | EverOS 记忆根目录（宿主机路径） |
| `enable_tools` | `true` | 注册 LLM 工具 |
| `enable_webui` | `true` | AstrBot 内嵌面板 |
| `standalone_webui_enabled` | `true` | 独立 WebUI |
| `standalone_webui_host` | `0.0.0.0` | 监听地址 |
| `standalone_webui_port` | `18766` | 端口 |
| `app_id` | `astrbot` | 应用标识 |
| `project_id` | `default` | 项目标识 |
| `isolation_personas` | `""` | 隔离人格白名单 |
| `auto_capture_enabled` | `false` | 自动对话记忆（对话轨） |
| `auto_capture_mode` | `both` | `both`=用户消息+机器人回复 / `user`=只记用户消息 |
| `auto_capture_scope` | `all` | `all` / `private`（仅私聊）/ `group`（仅群聊） |
| `auto_capture_sessions` | `""` | 会话白名单（`unified_msg_origin`，逗号分隔） |
| `auto_capture_idle_flush_seconds` | `300` | 空闲多久自动提炼，0=关闭 |
| `auto_capture_max_pending` | `80` | 单会话待提炼条数上限，0=关闭 |
| `auto_capture_min_chars` | `2` | 短于该长度的消息跳过 |
| `memory_injection_enabled` | `false` | 记忆自动注入（RAG） |
| `memory_injection_top_k` | `5` | 每次注入的 episode/profile 条数上限 |
| `memory_injection_timeout` | `6.0` | 注入检索超时（秒），超时就不注入 |
| `memory_injection_max_chars` | `1500` | 注入文本长度上限，避免撑爆上下文 |

---

## 🎮 使用

### 命令（管理员）

- `/everos status` — 查看连接状态
- `/everos memorize <内容>` — 手动写一条用户记忆
- `/everos learn <内容>` — 手动写一条 Agent 技能
- `/everos flush [会话ID]` — 提炼缓冲区；不带参数 = 提炼所有待处理会话
- `/everos search <关键词>` — 检索记忆
- `/everos capture [on|off]` — 查看 / 切换自动对话记忆（运行期，持久化改插件配置）
- `/everos remove <记忆ID>` — 删除一条记忆
- `/everos help` — 帮助

### LLM 工具

- `everos_memorize` — 记录用户偏好 / 事实 / 关键信息
- `everos_learn` — 记录 Agent 自己的工作规范 / 经验（进入 Agent Track，提炼为 Case / Skill）
- `everos_recall` — 检索相关记忆

### 自动对话记忆

开启后，插件的 `on_llm_response` 钩子会把每一轮对话写入 EverOS：

1. 只调用 `/api/v2/memory/add`，**不逐条 flush**，交给 EverOS 的边界检测决定何时抽取；
2. `session_id` 取自会话标识（`unified_msg_origin`，清洗为 EverOS 允许的字符），同一会话稳定复用，跨轮累积；
3. 后台每 30 秒扫描缓冲区：**空闲超过 `auto_capture_idle_flush_seconds`** 或 **单会话超过 `auto_capture_max_pending` 条** 时自动提炼。

> 工具轨（`everos_memorize` / `everos_learn`）每次写入后立即提炼，所以缓冲区通常为空；对话轨产生的待提炼消息，可以在 WebUI 的 **待提炼** 标签页查看全文并一键提炼。

### 记忆自动注入（RAG）

开启后，`on_llm_request` 钩子会在每次请求 LLM 之前，用**当前说话人的 `sender_id`** 检索其记忆（hybrid），并把结果追加到 system prompt。这样模型每轮都自带该用户的背景，不必自己调用 `everos_recall`。

- **检索身份由插件强制绑定**（`event.get_sender_id()`），不接受模型提供的 id —— 一个用户无法通过该通道看到别人的记忆；
- 检索失败或超时（默认 6 秒）会**静默跳过**，不影响对话；
- 注入内容上限默认 1500 字符，避免撑爆上下文；
- 会带来一次检索（embedding + BM25）的延迟，可按需调 `memory_injection_timeout` 或关闭。

### WebUI

- **内嵌**：AstrBot 后台侧边栏 → EverOS Bridge
- **独立（推荐）**：浏览器打开 `http://<服务器IP>:18766/`

页面：总览 / 记忆仓库 / **待提炼** / 检索 / 技能库 / 系统 / 设置。

---

## 🏗 结构

```text
astrbot_plugin_everos_integration/
├── main.py                      # 插件入口：命令组、LLM 工具、Web API、on_llm_response 钩子
├── core/
│   ├── everos_client.py         # HTTP 客户端
│   ├── config_manager.py        # 配置默认值
│   ├── memory_reader.py         # 发现 owner、跨 owner 读取、缓冲区读取、flush
│   ├── auto_capture.py          # 自动对话记忆（对话轨）+ 空闲/超限兜底提炼
│   └── standalone_server.py     # 独立 WebUI（:18766）
├── tools/
│   └── everos_tools.py          # everos_memorize / everos_learn / everos_recall
└── pages/everos-dashboard/
    ├── index.html
    ├── style.css
    ├── app.js                   # 双端统一前端
    └── server.py                # [可选] 手动启动的独立版
```

通信流程：

```text
浏览器 ──:18766──▶ standalone_server.py ──▶ EverOS :8000
AstrBot 后台 ──插件页──▶ register_web_api ──▶ EverOS :8000
每轮对话 ──on_llm_response──▶ EverOS /memory/add（边界检测）
```

---

## 🌏 中文记忆

**无需任何配置。** EverOS 自带的抽取提示词里就有 CRITICAL LANGUAGE RULE：输出语言与对话参与者书写的语言一致。因此中文对话天然产出中文记忆（本项目实测如此）。

早期版本的本文档曾建议去修改 EverOS 容器内的 `prompt_slots/episode_extract.yaml` —— 这**既不必要、又会降低质量**：默认提示词包含更完整的时间解析、细节保留、检索友好等规则。而且 EverOS 1.3.1 的提示词加载器只读包内 bundled 文件、**没有用户级覆盖层**，所以容器里手改的内容重建镜像后会丢失。

<details>
<summary>确实需要自定义提示词时</summary>

把自定义文件烤进镜像。例如在 EverOS 仓库里放一份 `episode_extract.yaml`，在 `Dockerfile` 中覆盖安装后的包内文件：

```dockerfile
COPY your-prompts/episode_extract.yaml /app/src/everos/config/prompt_slots/episode_extract.yaml
```

然后 `docker compose up -d --build`。注意必须保留占位符 `{conversation_start_time}`、`{conversation}`、`{custom_instructions}`。

</details>

---

## 📄 License

Apache 2.0

---

## 📋 更新记录

### 本地分支增强

- **feat: 自动对话记忆（对话轨）** — `on_llm_response` 钩子 + 稳定 session + 空闲/超限兜底提炼 + `auto_capture_*` 配置 + `/everos capture`
- **feat: WebUI 待提炼视图** — 查看缓冲区消息全文，支持单会话 / 全部提炼
- **fix: WebUI 显示全部记忆** — 从记忆根目录发现真实 `user_id` / `agent_id`，不再只查 `app_id` / `default` / `webui`
- **fix: flush 指向真实会话** — `/everos flush` 与 WebUI flush 不再写死 `default_dialog`，改为自动发现缓冲区中待提炼的会话
- **fix: 默认端口** — `everos_base_url` 默认改为 `http://127.0.0.1:8000`（EverOS 默认端口，此前误写为 8765）
- **docs: README 修正** — 端口、环境变量命名（`EVEROS_*`）、`everos init` 行为、Docker 示例、中文支持说明

### v1.1.0

- 新增 `everos_learn` 工具（Agent Track）
- 新增 `/everos` 命令组：`status` / `memorize` / `learn` / `flush` / `search` / `remove` / `help`
- 独立 WebUI 服务器（:18766）
- 记忆仓库分页、按时间倒序、写入弹窗
