# Tulpa → Water-Group-Only 剥离改造方案

> 目标：把仓库从“本地聊天资料汇聚 + MCP 桥接桌面产品”收束为“只让 AI Agent 长期驻留 QQ 群（接管 QQ）”的单一能力。  
> 本文档只给方案与操作清单，**不修改任何代码**。决定采用哪种路线后，再按 Phase 执行。  
> 生成依据：静态导入闭包分析（`scripts/check_water_group_import.py` 与自建 AST 闭包脚本）+ 运行链梳理。
> 2026-10-21 修订：逐条对照仓库核对——§1.3 统计口径确认为 git tracked files（全部吻合）；补齐 6 个门禁、修正 mint_grant/read_product_settings/sync_groups 等 8 处遗漏；原 §11 未决问题已全部决策，改为 §11 决策记录。
> **2026-10-21 二次复核勘误（执行以 `.plan/` 为准）**：4 个并行 subagent 复核推翻了 5 处执行细节——`plan()`/`retrieval` 不可删；`.env` 只可删 `API_*`/`AGENT_*`/`VISION_*`/`VOICE_*`（`REPLY_ONEBOT_*` 必留）；`media.py`/`image_identity.py`/`normalize.py` 随媒体链保留；WGStore 需 `messages`/`import_file` 且 `local_change_meta` 建在 `chats.sqlite3`；scripts 实为 27 个。详见 `.plan/01-review.md` §5。

---

## 1. 目标与范围

### 1.1 要保留的能力（水群闭环）

- NapCat / OneBot 11 接入与保活（watchdog、注入器、端口屏障）。
- 实时消息入库（`chat_inbox`）与 mention 过滤、批次（`chat_input_batches`）。
- Agent 对话接口：`start/get/list/stop_chat_session`、`wait_chat_messages`、`plan_chat_reply`、`send_chat_reply`、`read_chat_forward`。
- 群友记忆：L1 画像卡 + L2 SQLite（`memory_engine`、`query/update/consolidate_member_dossier`）。
- 人格卡热加载（`water-group/personas`）。
- 按花名 @、原生引用回复、连发 @ 不丢失、追问窗口、闲时摘要。
- MCP 鉴权（本地 grant/token）与速率限制。

### 1.2 明确不要的能力

桌面 GUI / Gradio、历史导入（QQ/微信）、全文搜索、调查工具、workspace、artifacts、voice、watch、collections、group admin、Linux 终端版、TUI、多 Agent 内置运行时、SnowLuma 托管产品、图片/表情包/反应（已决策保留，见 §11.1）、DeepSeek 内置模型调用。

### 1.3 量化现状

| 目录 | 文件 | 行数 | 与水群的关系 |
|---|---:|---:|---|
| `chatlocal/` | 166 | 33,908 | 后端核心；其中水群真正需要 ≈ 7,100 行 |
| `web/` | 27 | 11,727 | 前端壳；水群 daemon 不使用 |
| `scripts/` | 191 | 29,512 | 测试/辅助；水群相关 ≈ 24 个（§8 门禁表） |
| `doc/` | 73 | 7,990 | 文档；水群相关 ≈ 3 篇 |
| `water-group/` | 29 | 6,379 | 水群专属（含 3,057 行 daemon） |
| `water_group/` | 1 | 54 | 导入 shim（保留，见 §4.4） |
| `desktop/` | 17 | 2,763 | Windows 桌面壳；不用 |
| `tools/` | 0 (gitignored) | — | NapCat 112MB + SnowLuma 97MB vendored |

改造后估算：**代码 ≈ 1.4 万行**（媒体保留；若删媒体再减 ≈ 2k。含测试 ≈ 1.7 万），比现状减少 ~85%。

---

## 2. 运行链路（必须保留的进程树）

```
explorer.exe
└─ water-group/start-bot.pyw          (.pyw，无控制台，幂等)
   └─ water-group/daemon/up.py        常驻，兼任总监护
      └─ supervisor.py                拉起并重启下面全部
         ├─ server (was app.py)        后端 47860 / MCP 48777
         ├─ collector.py              常驻 wait_chat_messages，消息先落盘
         ├─ db_keeper.py              钉住 SQLite WAL 边车文件
         └─ watchdog.py               NapCat / QQ 保活
            └─ NapCatWinBootMain → QQ.exe
```

 daemon 代码（`water-group/daemon/*.py`）**零 `chatlocal` 导入**——只通过 HTTP/MCP 与后端通信。水群子系统与产品壳的耦合仅两处：`sync_groups.py` / `sync_personas.py` 导入 `chatlocal.mcp_chat_prompts` 与 `chatlocal.sqlite_util`（均在保留集；Phase 3 切换人格卡目录时同步改写 `sync_groups.py`）。

> 进程树只画常驻进程。`reply.py`/`listen.py`/`watch.py`/`followup.py`/`dossier.py`/`session.py`/`mcp_client.py` 是**给外部 Agent 调用的 CLI**（reply.py 经 requests + mcp_client 走后端），由 Agent 按需拉起，不在常驻树内——keep 集覆盖这些文件，裁剪时全部保留。

---

## 3. 三个“枢纽”——裁剪的关键

### 3.1 `chatlocal/mcp_tools.py`（493 行）

MCP 工具注册中心。它把 `agent_tools`（612 行）拉进来，而 `agent_tools` 再拉 `artifacts`、`investigation_tools`、`workspace_tools`、`qq_tools`、`people`、`collections` 等（≈ 3,000+ 行）。  
**裁剪方式**：写一个只注册 `CHAT_TOOLS` +（可选）`MEDIA_TOOLS` / `REACTION_TOOL` / `QQ_TOOLS` 的 `WGTools`，保留 `argument_problems`、`result`、`chat_call`、`chat_media_call` 的分发逻辑。`mcp_server.py` 只需把 `MCPTools(access)` 换成 `WGTools(access)`，接口相同。

### 3.2 `chatlocal/store.py`（406 行）

`_migrate()` 在导入时初始化 15 个子系统（`import_pipeline`、`artifacts`、`voice`、`workspaces`、`collections`、`group_admin`、`tulpa` 等）。  
**裁剪方式**：替换为 `WGStore`（≈ 40 行），只做：创建 data 目录、`connect()`（WAL）、`local_change_meta` 表（供 `mcp_access.epoch()`）。聊天表由 `mcp_chat` 自己创建。

### 3.3 `chatlocal/mcp_access.py`（301 行）

`epoch()` 读 `local_change_meta`；`accounts()` 读 `messages` 表；`plan()` 用 `retrieval.Plan`。  
**裁剪方式**：epoch 返回常量；accounts 从 OneBot login 取；删除 `plan()` 与 `retrieval`/`client_accounts` 依赖。启动时若 `grants` 表无 `water-group` 记录则补建一个（token 落 `.tmp/mcp_token.txt`）；**保留** `daemon/mint_grant.py` 铸造链路与 `mcp_client.py` 的 401 自动重铸作自愈兜底（§11.2），顺带清掉 line 71 对 `terminal_instances` 的 lazy import。

---

## 4. 裁剪后的 keep 集（chatlocal）

### 4.1 必须保留（core，≈ 25 模块 / 6.5k 行）

| 模块 | 行数 | 理由 |
|---|---:|---|
| `__init__.py` | 1 | 包标记 |
| `config.py` | 72 | ROOT/DATA/DB/paths；删 Gradio env、AGENT_LIMITS/agent_options/settings（裁剪后 ≈ 25） |
| `mcp_access.py` | 301 | 鉴权（裁剪后 ≈ 220） |
| `mcp_actions.py` | 164 | `dump()` 被 chat 模块使用（裁剪后 ≈ 60） |
| `mcp_chat.py` | 1,052 | 会话/等待/收件箱核心 |
| `mcp_chat_turns.py` | 580 | plan/send/stale 状态机 |
| `mcp_chat_details.py` | 319 | 引用/详情展开 |
| `mcp_chat_hints.py` | 21 | 提示词辅助 |
| `mcp_chat_limits.py` | 6 | `WAIT_TIMEOUT_SECONDS` |
| `mcp_chat_materials.py` | 97 | 图片材料绑定 |
| `mcp_chat_output.py` | 110 | compact 输出 |
| `mcp_chat_prompts.py` | 280 | 人格卡目录扫描 |
| `mcp_qq_catalog.py` | 140 | OneBot API 白名单 |
| `mcp_qq_permissions.py` | 20 | QQ 权限标志 |
| `mcp_routes.py` | 249 | MCP HTTP 路由（grant/session/stop） |
| `mcp_schemas.py` | 33 | schema 片段工厂 |
| `mcp_server.py` | 267 | Streamable HTTP MCP |
| `message_sender.py` | 233 | 发送回执/uncertain |
| `onebot.py` | 176 | OneBot HTTP 客户端 |
| `onebot_events.py` | 197 | WS 事件接收 |
| `onebot_segments.py` | 27 | 消息段构造 |
| `prompts/mcp_chat/*` | — | behavior.md / examples.json / 人格卡 |
| `reply_timing.py` | 147 | 调用追踪 |
| `sqlite_util.py` | 34 | 连接封装 |
| `version.py` | — | 版本号 |
| `store.py` → `WGStore` | ≈ 40 | 连接 + local_change_meta |

### 4.2 可选保留（full fidelity，≈ 2.0k 行；可按需删除）

| 模块 | 行数 | 说明 |
|---|---:|---|
| `mcp_chat_local_media.py` | 159 | 本地图片导入 |
| `mcp_chat_media.py` | 755 | 表情包/图片/视频/文件 |
| `mcp_qq.py` | 362 | QQ 扩展动作（戳一戳/转发） |
| `mcp_qq_media.py` | 219 | QQ 媒体导入 |
| `mcp_reactions.py` | 452 | 表情回应 |
| `mcp_sticker_library.py` | 316 | 表情包库 |
| `mcp_language_learning.py` | 399 | 边聊边学 |
| `mcp_language_store.py` | 639 | 学习存储 |
| `mcp_expression_evidence.py` | 86 | 表达证据 |
| `mcp_jargon_learning.py` | 82 | 术语学习 |
| `mcp_jargon_match.py` | 30 | 术语匹配 |

> README 已声明“目前只发文字 + @ + 引用”，表情/图片未接通。**已决策：媒体能力先保留**（2026-10-21，见 §11.1）；若确认长期只做文字，可全部删除，再减 ≈ 2.0k 行 + 依赖 `dnspython`/`Pillow`。

### 4.3 必须删除的 chatlocal 模块（≈ 100 个）

`activity`, `agent`, `agent_profiles`, `agent_sessions`, `agent_tools`, `appearance`, `artifact_*`, `artifacts`, `changes`, `chat_view`, `client_accounts`, `collections`, `data_delete`, `data_routes`, `desktop_routes`, `file_hash`, `group_admin`, `group_admin_routes`, `group_knowledge`, `identity`, `image_identity`, `import_pipeline`, `import_scope`, `interactions`, `investigation_*`, `linux_*`, `live*`, `llm`, `media`, `media_refresh`, `mcp_connect`, `native_sources`, `normalize`, `optional_components`, `people`, `platform_*`, `posix_child`, `qq_labels`, `qq_storage`, `qq_tools`, `read_options`, `reader_batches`, `reasoning`, `render`, `replies`, `reply_agent`, `reply_history`, `retrieval`, `snowluma_desktop`, `snowluma_docker*`, `snowluma_local`, `snowluma_manifest.json`, `snowluma_managed`（Phase 2 内联 connection/guard 后删）, `snowluma_runtime`, `snowluma_secure`, `snowluma_setup`, `sync`, `sync_state`, `terminal_*`, `tui/`, `tulpa*`, `ui`, `vision`, `voice*`, `watch_*`, `watches`, `wechat_keys`, `workspace_*`, `workspaces`, `__main__`。

### 4.4 其他目录

| 路径 | 处置 |
|---|---|
| `web/` | 删除（无前端壳） |
| `desktop/` | 删除 |
| `harness/` | 删除 |
| `app.py` | 删除（Gradio + 10 路由组） |
| `app_mcp.py` | 删除 |
| `doc/` | 仅保留 `VALIDATION.md`、`SECURITY.md`；其余删除或迁移到 README |
| `scripts/` | 保留 §8 门禁表全部 + 补齐的 4 个（见 §8），合计 ≈ 24 个；`check_watch_sync.py` 测 `Store` 内部，随 store 替换删除；其余 ≈ 167 个删除 |
| `requirements/` | 删除目录，根 `requirements.txt` 改为最小集 |
| `tools/` | 仅保留 `napcat-shell/`（gitignored）；删除 `qq-reader`、`wechat-reader`、`snowluma-*`、`reader-manifest.json` |
| `setup.ps1`/`start.ps1`/`stop.ps1` | 重写为 slim 版 |
| `README.linux.md` / `TESTING.txt` | 删除 |
| `chatlocal/tui/` | 删除 |
| `tulpa`（根目录启动器） | 删除（exec `chatlocal.terminal_cli`，Linux 支持一并移除） |
| `setup-linux.sh` | 删除 |
| `licenses/` | 删除（PaddleOCR/whisper 许可证随 OCR/voice 移除） |
| `imports/`、`build/`、`reports/`、`tmpseg1k_v5/` | gitignored 工作残留，清理 |
| 根目录 0 字节 `chatlocal.db`、`mcp-access.sqlite3` | 垃圾文件，删除 |
| `water_group/` | **保留**（54 行 shim；`chatlocal/mcp_chat.py` 反向依赖 `water_group.memory_engine`，`check_memory_engine_imports` 守护单实例，见 §11.4） |
| `water-group/sync_groups.py` | **保留并改写**（bootstrap 启动调用；Phase 3 切换人格卡目录时去掉其对 `sync_personas` 的 import） |

---

## 5. 配置与数据兼容合同

### 5.1 配置合并

现状：`water-group/config.json`（OneBot URL/token/群/人设）与根 `.env`（`REPLY_ONEBOT_*`）重复。  
改造：`onebot.configuration()` 优先读 `water-group/config.json` 的 `onebot_http_url`/`onebot_token`。**`.env` 可整体删除**——`API_BASE`/`API_KEY`/`MODEL` 的消费者（`agent.py`/`llm.py`/`terminal_features.py`/`desktop_routes.py`）全在删除集，trim 后无人读取。Token 供应（决策，见 §11.2）：**保留现有 mint 链路**——server 启动时若无 `water-group` grant 则补建并写 `.tmp/mcp_token.txt`；`daemon/mint_grant.py`（POST `/api/mcp/connections`）与 `mcp_client.py` 的 401 自动重铸保留作自愈兜底。

### 5.2 数据兼容（不可破坏）

- `data/mcp-access.sqlite3`：保留文件名与 `chat_sessions`/`chat_inbox`/`chat_events`/`chat_input_batches`/`chat_turn_plans`/`chat_detail_cache`/`chat_media_assets`/`chat_sticker_*`/`chat_reaction_*`/`grants`/`calls`/`settings`/`client_settings` 表结构与列（媒体已决策保留，媒体表照常更新；§8 补齐的门禁见该表）。
- `data/group_memories.sqlite3`：由 `memory_engine` 管理，文件名不变。
- `.tmp/wg_*.jsonl` / `wg_session.txt` / `wg_ack.txt` / `wg_replied.jsonl` / `wg_watch.txt` / `wg_followup.json`：daemon 契约，字段不变。
- `water-group/personas/*.md`：保留；人格卡根目录切换（`mcp_chat_prompts.ROOT` → `water-group/personas`）放 **Phase 3**（R9 时序），届时删除 `sync_personas.py` 并同步改写 `sync_groups.py`（它 `import sync_personas`，直接删会断启动链路）。

### 5.3 可选能力开关

若决定删除图片/表情/反应/学习，需同步：
- `CHAT_TOOLS` 从 `mcp_chat.py` 移除对应工具名；
- `mcp_routes.py` 删除 `/api/mcp/chats/{sid}/learning*` 与 media 相关路由；
- `reply.py` 不受影响（它只用 plan/send + OneBot）；
- `requirements.txt` 删除 `dnspython`、`Pillow`、`pysilk-mod`、`opencc`、`imageio-ffmpeg`。

---

## 6. 分阶段执行

### Phase 0 — 基线冻结（0.5 天）

```bash
git tag pre-slim
git checkout -b slim
# 跑基线门禁，记录输出
uv run python scripts/check_group_memory.py
uv run python scripts/check_mcp_chat_turns.py
uv run python scripts/check_plan_recovery.py
uv run python scripts/check_anti_habituation.py
uv run python scripts/check_mcp_chat_output.py
uv run python scripts/check_bootstrap_health.py
uv run python scripts/check_water_group_import.py
uv run python scripts/check_wake_ledger.py
uv run python scripts/check_followup.py
uv run python scripts/check_mcp.py
uv run python scripts/check_mcp_chat.py
uv run python scripts/check_mcp_onebot.py
uv run python scripts/check_memory_engine_imports.py
uv run python scripts/check_reply_timing.py
uv run python scripts/check_reply_context.py
uv run python scripts/check_reply_quote.py
uv run python scripts/check_publication.py
```

备份 gitignored 资产：`.env`、`water-group/config.json`、`tools/napcat-shell/`，以及数据根 `~/.water-group/`（`rules/`、`personas/` 下的私有人设卡、`data/`）。

### Phase 1 — 最小 server 入口（1 天，非破坏性）

新增根文件 `server_wg.py`：

```python
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from chatlocal.config import ROOT
from chatlocal.mcp_routes import install_mcp_routes

def create_app():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_mcp_routes(app)
    @app.get('/')
    def health():
        return {'ok': True, 'service': 'water-group'}
    return app

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(create_app(), host='127.0.0.1', port=47860, access_log=False)
```

`supervisor.py` 的 `CHILDREN["app"]` 指向 `server_wg.py`（或新增 env `TULPA_WG_SERVER`）。  
验收：冷启动、`up.py --status` 显示后端在线、collector 建立会话、一次 @ 回复成功。  
此 Phase 不删任何文件，可随时回退。

### Phase 2 — 裁剪三个枢纽（2-3 天）

1. **mcp_tools.py**：用 `WGTools` 替换 `MCPTools`；删除 `READ_TOOLS`/`MCPChatTools`/`agent_tools` 导入；保留 `argument_problems`、`result`、`chat_call`、`chat_media_call`、`qq_call`（若保留 QQ 扩展）。
2. **store.py**：替换为 `WGStore`（≈ 40 行）。
3. **mcp_access.py**：epoch 常量、accounts 从 OneBot login、启动时无 grant 则补建、删除 `plan()`/`retrieval`/`client_accounts`；顺带清掉 line 71 对 `terminal_instances.validate_mcp_port` 的 lazy import（`terminal_instances` 在删除集）。
4. **onebot.py / onebot_events.py / message_sender.py**：内联 `snowluma_managed.connection/guard`（返回 None / no-op）；删除对 `snowluma_secure/runtime/local` 的导入。注意 `message_sender.py`（保留）也 import `snowluma_managed`，易漏。
5. **mcp_chat_prompts.py**：`ROOT` 改为 `water-group/personas` 放 Phase 3；本 Phase 保留 sync（R9）。
6. **mcp_routes.py**：删除历史调查/voice/workspace 路由；保留 grant/session/stop/learning（媒体已决策保留，learning 保留）；**`/api/mcp/chats` 列表路由与 `/api/mcp/chats/{sid}/stop` 必须保留**（`clear_group_sessions` 经 HTTP 调用，见 R10）。
7. **mcp_server.py**：`self.tools = WGTools(access)`；`self.tools.actions.recover()` 改为 `WGActions.recover()` 或 no-op；readOnlyHint 一行引用的 `QQ_WRITES`/`MEDIA_WRITES` 随 WGTools 调整。
8. **mcp_routes.py 的 `read_product_settings()`**：定义在删除集的 `desktop_routes.py:20`，`mcp_routes.py:48` 在用——必须内联或去掉该响应字段（§4.3 裁剪清单遗漏点，执行时优先处理）。

验收：`check_mcp_chat_turns`、`check_plan_recovery`、`check_anti_habituation`、`check_mcp_chat_output`、`check_group_memory` 全绿；端到端 @ 回复。

### Phase 3 — 删除产品壳（1-2 天，机械但量大：≈167 个 scripts + 约 100 个 chatlocal 模块）

按 §4.3/§4.4 删除文件，更新 import。  
验收：`check_water_group_import`、`check_bootstrap_health`、`check_publication` 全绿；`git status` 无意外；`uv run python server_wg.py` 可启动。

### Phase 4 — 收尾（可选）

- 重写 `README.md` / `AGENTS.md` / `.agents/rules/water_group.md`（指向 slim 结构）。
- 精简 `requirements.txt`（见 §7）。
- 重命名 `chatlocal` → `wgserver`：**不做**（§11.5）。
- 可选：新仓库（fresh clone + keep set），保留原仓库作为上游参考。

---

## 7. 精简后的依赖

```
fastapi==0.141.1
uvicorn==0.53.0
httpx==0.28.1
python-dotenv==1.2.3
mcp==1.26.0
websockets==15.0.1
jsonschema==4.26.0
# 仅在保留图片/表情/OCR 时：
# dnspython==2.9.0
# Pillow==12.3.0
# pysilk-mod==1.6.4
# opencc-python-reimplemented==0.1.7
# imageio-ffmpeg==0.6.0
```

删除：`gradio`、`jieba`、`apsw`?（SQLite 用 stdlib sqlite3；apsw 可删）、`deepseek-harness-sdk`、`docling-*`、`numpy`、`rapidocr`、`onnxruntime`、`opencv-*`、`textual*`、`cryptography`、`zstandard`、`pycryptodome`、`pypdf`。

> 注：`jieba` 仅历史搜索使用，可删；`apsw` 未在 chat 路径使用，可删。

---

## 8. 测试门禁映射

| 门禁脚本 | 覆盖 | Phase 0/2/3 |
|---|---|---|
| `check_group_memory.py` | 记忆引擎 + MCP 读写 | 全程 |
| `check_mcp_chat_turns.py` | 轮次/引用/竞争 | 全程 |
| `check_plan_recovery.py` | stale 重规划 | 全程 |
| `check_anti_habituation.py` | 口癖多样性 | 全程 |
| `check_mcp_chat_output.py` | 输出截断/游标/ACK | 全程 |
| `check_bootstrap_health.py` | 启动健康/代理隔离 | 全程 |
| `check_water_group_import.py` | 模块桥接 | 全程 |
| `check_wake_ledger.py` | 连发 @ 不丢 | 全程 |
| `check_followup.py` | 追问窗口 + `#` 指令闸门（#70） | 全程 |
| `check_mcp_chat.py` | MCP 聊天契约 | 全程 |
| `check_mcp.py` | MCP 协议层 | 全程 |
| `check_mcp_onebot.py` | OneBot 契约 | 全程 |
| `check_memory_engine_imports.py` | `water_group` shim 单实例/水群模块身份（动 shim 或 `sync_*` 时必跑） | 全程 |
| `check_reply_timing.py` | `reply_timing` 时序/密钥排除/轮换 | 全程 |
| `check_reply_context.py` | `reply.py --context` 群聊上下文 | 全程 |
| `check_reply_quote.py` | OneBot mention/引用预检 | 全程 |
| `check_mcp_qq_extensions.py` | QQ 扩展 | 若保留 |
| `check_mcp_reactions.py` | 表情回应 | 若保留 |
| `check_mcp_sticker_library.py` | 表情包库 | 若保留 |
| `check_mcp_chat_media.py` | 媒体 | 若保留 |
| `check_mcp_language_*.py` | 边聊边学 | 若保留 |
| `check_publication.py` | 敏感信息扫描 | commit 前 |
| `check_stop_stack.py` | 一键停栈完整性（孤儿清扫 / 自杀防护 / 端口兜底 / 死锁清理 / 壳不复制逻辑） | 全程 |
| `check_path_hygiene.py` | 路径卫生（运行时数据不许从仓库根推导，防 issue #14 复发） | 全程 |
| `check_no_console_child.py` | 子进程不开控制台窗口（运行时树 spawn 必须带 `creationflags`；flag 只在两个 helper 模块里拼写，防 issue #39 复发） | 全程 |
| `check_issue_16_audit_fixes.py` | #16 审计缺陷回归（store 异常类型 / grants 排序 / MCP 响应解析 / 会话活性） | 全程 |
| `check_watchdog_token.py` | watchdog 探针现读 OneBot token（轮换 config.json 后探针必须跟随，防 #17 复发） | 全程 |
| `check_collector_self_echo.py` | collector 不落盘自身回显（唤醒流只留群友消息；游标/去重仍按整批推进，防 #28 复发） | 全程 |
| `check_qr_login.py` | 每用户扫码登录（passport 双流 / 凭据兜底解析 / DPAPI 往返 / 限流 / P0 隐私不回退全局 CK，issue #6） | 全程 |

删除的测试（无对应能力）：`check_chat_browser*`、`check_desktop.py`、`check_linux_suite.py`、`check_terminal_tui.py`、`check_group_admin.py`、`check_wechat_*`、`antigravity_*`、`check_antigravity_reply_timing.py`、`check_watch_sync.py`（测 `Store` 内部，随 WGStore 替换删除）、`check_reply_agent.py`/`check_reply_copilot.py`/`check_reply_history.py`（依赖 `agent`/`replies`/`tulpa` 产品链，均在删除集）。

---

## 9. 风险登记

| # | 风险 | 缓解 |
|---|---|---|
| R1 | `mcp_tools`/`store` 导入扇出，删错导致 ImportError | Phase 2 先用 AST 闭包重新计算；每删一批跑 `check_water_group_import` |
| R2 | grant/session 绑定；改动鉴权导致会话失效 | Phase 2 保持 grant 表与 `chat_sessions` 不变；启动补建复用已有 grant id/scope（§11.2），mint 自愈链路保留 |
| R3 | DB 文件名/表结构变更破坏 daemon | §5.2 兼容合同；Phase 2 不改表 |
| R4 | NapCat vendored 路径（gitignored，112MB） | 新仓库需提供安装脚本；保留 `tools/napcat-shell` |
| R5 | 沙箱注入导致 QQ 崩溃 | 保留 `start-bot.pyw` + explorer 启动；`bootstrap.py` 保留注入检测 |
| R6 | Windows 专用（taskkill/WMI/.pyw） | 明确声明 Windows-only；删除 Linux 支持 |
| R7 | 私有人设卡泄露 | 人设卡与私有规则已移出仓库，住在 `~/.water-group/personas` 与 `~/.water-group/rules`；仓库 personas/ 只保留公开模板 |
| R8 | 删除 `jieba`/`apsw` 后隐式依赖 | Phase 2 后 `grep -rn "jieba\|apsw"` 验证无引用 |
| R9 | `mcp_chat_prompts.ROOT` 路径变更导致人格卡找不到 | Phase 2 先保留 sync，Phase 3 再切 ROOT 并跑 `list_chat_personas` |
| R10 | `session.clear_group_sessions()` 经 HTTP 调 `/api/mcp/chats` 与 `/stop`（`mcp_routes.py`，**不是 web/ 壳**） | 已降级：Phase 1 装上 `install_mcp_routes` 后天然兼容，保留两路由即可，无需改 MCP 调用 |

---

## 10. 回滚

- Phase 1：删除 `server_wg.py`，supervisor 指回 `app.py`，无其他改动。
- Phase 2：每个枢纽单独 commit；`git revert <commit>`。
- Phase 3：从 `pre-slim` tag 恢复整个树；或 `git checkout pre-slim -- <path>`。
- 数据：Phase 0 备份的 `data/` 可覆盖恢复。

---

## 11. 决策记录（2026-10-21 已确认，不再未决）

1. **媒体能力（图片/表情包/反应/边聊边学）先保留**：Phase 2/3 不动 media/reactions/sticker/language 模块及 5 个可选门禁；仅当确认“纯文字水群”时再按 §5.3 删除。
2. **Token 供应链保留现有 mint 链路**：server 启动时无 `water-group` grant 则补建并写 `.tmp/mcp_token.txt`；`daemon/mint_grant.py` 与 `mcp_client.py` 401 自动重铸保留作自愈兜底；不删 `/api/mcp/connections`。
3. **仓库路线：slim 分支先行**：Phase 0-3 在 `slim` 分支完成，每阶段独立 commit；验证 2 周后再决定是否拆新仓库。
4. **`water_group/` shim 保留**：`chatlocal/mcp_chat.py` 经 `water_group.memory_engine` 调记忆引擎，`check_memory_engine_imports` 继续守护单实例。
5. **`chatlocal` 不重命名**（Phase 4 也不做）。
6. **`tools/napcat-shell` 保持 gitignored**，另写下载/安装脚本（参考 `desktop/fetch_runtime.py` 思路）。
7. **scripts 保留名单**：§8 门禁表全部 + 补齐 4 个（`check_memory_engine_imports`/`check_reply_timing`/`check_reply_context`/`check_reply_quote`）≈ 24 个。
8. **人格卡目录切换放 Phase 3**：届时删 `sync_personas.py` 并改写 `sync_groups.py`（它 import sync_personas，直接删会断启动链路）。

---

## 附录 A：Phase 1 最小 server 入口（预览）

见 §6 Phase 1 代码块。

## 附录 B：WGStore 草图（Phase 2）

```python
import sqlite3
from pathlib import Path
from chatlocal.config import DATA
from chatlocal.sqlite_util import connect as sqlite_connect

class WGStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else (DATA / 'mcp-access.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS local_change_meta(
                id INTEGER PRIMARY KEY CHECK(id=1), epoch TEXT NOT NULL,
                high_water INTEGER NOT NULL DEFAULT 0)''')
            db.execute('INSERT OR IGNORE INTO local_change_meta(id,epoch) VALUES(1,?)',
                       (__import__('uuid').uuid4().hex,))
    def connect(self):
        return sqlite_connect(self.path, timeout=10)
```

## 附录 C：grant 兜底草图（Phase 2，配 §11.2 决策）

server 启动时：若 `mcp-access.sqlite3` 中无 `water-group` grant，则补建一个全 chat 权限 grant（scope 沿用 `daemon/mint_grant.py` 的 `SCOPE_FIELDS`——媒体已保留，scope 不变），token 写入 `.tmp/mcp_token.txt`。`daemon/mint_grant.py` 与 `mcp_client.py` 的 401 自动重铸**保留**，collector/session.py 无需改动。

---

*文档结束。确认路线后按 Phase 执行。*
