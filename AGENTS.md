# 闲笔 (IdleJot) AI Agent 协作与开发指南 (AGENTS.md)

欢迎使用 闲笔 (IdleJot) 仓库。本指南是专为进入本项目的 AI Agent（以及协同开发者）设计的操作手册，旨在说明项目架构、开发规范、核心子系统设计以及强制性安全门禁。

面向使用者的教程在 `doc/`（水群协议与各 MCP 工具的验证记录、安全说明、两轮 DRY/SRP 审计报告）；**所有 Agent 的仓库改动流程（先发 issue → plan 追评 → worktree → PR → 对账）见 §2.7，操作指引在 `doc/ISSUE_GUIDE.md`（怎么发 issue）与 `doc/PR_GUIDE.md`（怎么开 PR）**；水群子系统的 Agent 侧规则另有 `.agents/rules/water_group.md`（通用骨架）与 `.agents/skills/water-group/SKILL.md`，**私有 SOP（群号、群友花名/QQ/地域、人设卡、数据源速查、排查实例）不在仓库里**，在 `~/.water-group/rules/water_group.md` 与 `~/.water-group/personas/`。本文件面向**仓库内的开发与维护工作**。

## 0. 接管水群（用户说「在 xxx 群挂机水群」时先看这里）

一句话入口，**必须由 explorer 代启动**（前提：`.venv/` 已存在，没有就先跑 `setup.ps1` 或 `uv sync`）：

```bash
.venv/Scripts/python.exe -c "import subprocess,pathlib; subprocess.Popen(['explorer.exe', str(pathlib.Path('water-group/start-bot.pyw').resolve())])"
```

**不要用 `uv run python water-group/bootstrap.py` 起栈。** 代理沙箱给每个子进程注入 `tsbx.dll`，注入沿 `up.py → supervisor → watchdog → NapCatWinBootMain → QQ` 一路继承，QQ 约 20 秒后必崩（`0xC06D007F`，delay-load 的 `ERROR_PROC_NOT_FOUND`），watchdog 于是无限重注入，桌面上堆满崩溃框。`start-bot.pyw` 是 `.pyw`（关联 `pythonw.exe`，无控制台），经 explorer 拉起时父进程是 explorer，整条链干净。`bootstrap.py` 现在检测到注入会直接 `exit 2` 拒绝启动。

起好之后（先读状态，再挂监听）：

```bash
.venv/Scripts/python.exe water-group/daemon/up.py --status            # 只读复查
.venv/Scripts/python.exe water-group/daemon/listen.py --timeout 300   # 挂监听（只活一轮，WAKE 后必须重挂）
.venv/Scripts/python.exe water-group/daemon/reply.py --list            # 看谁 @ 了我
.venv/Scripts/python.exe water-group/daemon/reply.py --context 30      # 回之前看群里在聊什么（含闲聊）
.venv/Scripts/python.exe water-group/daemon/reply.py <id> "<正文>"      # 回一条，自动带原生引用；发完自动开追问窗口
.venv/Scripts/python.exe water-group/daemon/reply.py --show <id>       # 读某条消息全文（wg_events 命中带引用展开；缺 id 明说原因）
.venv/Scripts/python.exe water-group/daemon/look.py --event <id>       # 唤醒行图片标「可补读」时先落盘再读；--pick 看候选、--recent 看最近
```

唤醒行里的图片标记三种形态：`path=…`（已落盘，直接读）、`可补读 look.py --event <id> --index <i>`
（deferred，先跑命令再读）、`未登记`（无任何记录，命令照样能补读）。别对着裸 `[图片]` 说
「看不到图」——先看标记尾缀（issue #35）。

**时间基准（issue #95）**：`up.py --status` 首个非空行、`reply.py --list` / `--context`
表头、`watch.py` 唤醒块首行、`followup.py --show` 首行都带
`现在 2026-10-23 20:52:07 (UTC+8)`。判断「今晚 / 还有多久 / 开播了吗」一律以这行为准，
**不要拿消息自己的时间戳或 `19s 前` 当现在**——消息可能是一小时前的，相对量在你思考
几十秒后就失真了。这几处格式由 `water-group/daemon/wg_clock.py` 统一提供，
`scripts/check_now_clock.py` 门禁钉住。

追问窗口：回复成功后 `reply.py` 自动开窗（默认再跟 10 条或静默 180 秒），群里继续聊的
几句也会唤醒监听（唤醒行带 `[追问]` 前缀），话题能接着走；`--no-followup` 单次关闭，
`config.json` 的 `followup` 段调参数，详见 `~/.water-group/rules/water_group.md`。

> **职责边界（issue #83）**：接管后主 Agent 只水群——白名单是 `listen.py` / `reply.py` / `up.py --status`（只读）/ 追问窗口接话。
> 改仓库、跑测试门禁、写文档、审 PR、排查一律 `Agent(run_in_background: true)` 派后台 subagent，派完立刻回监听，不原地等。
> 细则见 `.agents/rules/water_group.md` §6，派活 prompt 模板见 `.agents/skills/water-group/SKILL.md` §五。

日常循环、按花名 @ 人、连发 @ 的处理、排查表，全部在 **`~/.water-group/rules/water_group.md`**（私有，不在仓库），动手前读它；仓库的 `.agents/rules/water_group.md` 只有通用骨架。本文件后面的内容是仓库开发与维护规范，不是挂机 SOP。

**改了代码要不要重启、群里发指令没反应怎么查：见 §0.2。**

### 0.1 停栈（与启动对称）

三个入口同一个实现（`water-group/daemon/up.py:stop_stack()`），随便用哪个：

```bash
# 最省事：双击 water-group/stop-bot.pyw（停止无注入风险，不必经 explorer）
.\stop.ps1                                                              # 有控制台回显
.venv/Scripts/python.exe water-group/daemon/up.py --stop                # 脚本/Agent 用
```

它会依次：树杀守护（supervisor / 常驻 up.py）+ 看护 → 按脚本指纹扫掉树外残留（`server_wg.py`、`collector.py`、`db_keeper.py`、`commands.py` 这些以前会活下来继续占 47860/48777 的）→ 按 `-q <self_account>` 杀注入 QQ（官方桌面 QQ 不动）→ 清掉 `#` 指令守护的死锁（`taskkill /F` 不给 `release_lock()` 机会，持锁 pid 已死才删，活锁不动）→ 端口兜底。结果写 `~/.water-group/logs/stopper.log` 一行，退出码 0 表示端口全释放。

`--status` 与并发的 `--stop` 互不误杀：只读探针不会被停止流程带走。

### 0.2 改完什么时候生效（别用「热重载」猜）

| 改的东西 | 生效方式 |
|---|---|
| `water-group/config.json`（群列表、各类开关、`plugins.<名字>.enabled`…） | `commands.py` **按消息懒重载**（`RELAY_SECONDS=30`，判断点在下一条消息 `dispatch` 之前），**不用重启** |
| `commands.py` / `sixty.py` / `miyoushe.py` / `qr_login.py` 等 handler 代码 | `.\reload.ps1`：按 AST 闭包只重启 `commands`（`HANDLERS` 是 import 期合成的，进程级重启才会换模块） |
| `memory_engine.py` 及四个分层模块 | `.\reload.ps1`：闭包会命中 `app` / `commands` / `watchdog`，自动重启对应子进程 |
| `up.py` / `bootstrap.py` / `reload.py` / `winconsole.py` / `watchdog.py` / `supervisor.py` / `start-bot.pyw`（启动链、控制台、NapCat 注入） | **必须整栈重启**：`.\stop.ps1`（或 `up.py --stop`）后从 explorer 启动 `start-bot.pyw`；`reload.ps1` 会把 guard-owned 代码拒绝掉，普通重载不会替换已运行的 `up.py` 和它拉起的 NapCat（#4 的旧乱码控制台窗口就是这么留下的） |

> 启动链没有热重载：旧 `up.py` 会一直用它启动时 import 的模块，NapCat 也是它的子进程。判断是否旧进程，看 `up.py --status` 里的守护 pid 对应进程的启动时间是否早于 `up.py` 的 mtime；是就整栈重启。

判据看 `~/.water-group/logs/wg_commands.log` 的启动行：

```
commands start, prefix='#' keywords=[...] groups=[...] handlers=<N> plugins=miyoushe:21,sixty:14
```

这行**时间早于提交时间** = 进程还是旧代码，此时新指令会被当未知指令**静默丢弃**
（`unknown_reply` 默认空串，症状是「发了完全没反应」，见 issue #68）。`plugins=`
里缺某个插件或写成 `:0` = 那个插件没加载起来（issue #4 PA：注册表按插件记
`load_error`，`up.py --status` 的 `插件` 行也会列出来）。

重启单棵树（别整栈停，supervisor 会在 8 秒内拉起）：

```bash
MSYS_NO_PATHCONV=1 taskkill /T /F /PID <up.py --status 里的 #指令 pid>
# Git Bash 里必须带 MSYS_NO_PATHCONV=1，否则 /T、/F 会被当成路径转成 T:/、F:/
```

**群里发 `#` 指令没反应时，按这三步查**（`#` 指令是独立进程，别的日志帮不上忙）：

1. `tail ~/.water-group/logs/wg_commands.log` —— 有 `replied #xxx` 就是已回；**没有**说明压根没被识别，往下走；
2. 看启动行 `commands start ... handlers=<N> sixty=<M>`：时间早于提交时间 = 旧进程，`sixty=0` = 模块导入坏了
   （两种导入失败现在都会 `log`，见 issue #68）；
3. 取数失败看 `~/.water-group/logs/wg_sixty.log` —— 它**只记失败**，成功不落日志，所以「文件不存在」是正常现象。

---

## 1. 项目定位与核心架构

本项目（闲笔 IdleJot）只做一件事：让 Agent 常驻 QQ 群。代码库的早期骨架来自 Tulpa（MIT），当前仓库已按水群场景重构，桌面产品壳（.NET 桌面壳、Web 前端、读微信、Linux 终端、安装包）不在本项目内。后端入口是单一进程 `server_wg.py`（FastAPI），监听 OneBot 11 实时事件，通过 Model Context Protocol (MCP) 向外部 Agent 暴露工具。**Windows only**。

```
        ┌──────────────────────────────────────────────────┐
        │            外部 Agent / 桌面客户端                 │
        │   (Gemini, Claude, Codex, DeepSeek, WorkBuddy…)  │
        └───────────────┬──────────────────────────────────┘
                        │ MCP (stdio/SSE)  HTTP/WebSocket
              ┌─────────▼──────────┐  ┌──────────────────────┐
              │  chatlocal/ 引擎    │  │  OneBot 11 网关      │
              │ - store.py SQLite  │  │ (NapCat, HTTP / WS)  │
              │ - mcp_* MCP 工具层  │  │  43000 / 43001       │
              │ - mcp_chat_prompts│  └──────────┬───────────┘
              └───────────┬───────┘             │
      ┌───────────────────┴─────────────────────┘
      │          water-group (水群子系统)              │
      │  memory_engine.py(门面) · memory_store.py      │
      │  memory_profile.py · memory_observe.py          │
      │  memory_dossier.py · memory_commands.py          │
      │  memory_format.py · bootstrap.py               │
      │  sync_groups.py · personas/                   │
      └───────────────────────────────────────────────┘
```

### 核心目录职责

| 目录 | 职责 |
|------|------|
| `chatlocal/` | 应用核心：`store.py`（SQLite/WAL 连接策略）、`mcp_*` MCP 工具与路由层（`mcp_server.py`、`mcp_tools.py`、`mcp_chat*.py`、`mcp_qq*.py`、`mcp_reactions.py`、`mcp_sticker_library.py` 等）、`agent*.py` 会话与人格、`onebot*.py` 网关、`voice.py` 云端 ASR。人格卡模板在 `water-group/personas/`（公开）与 `~/.water-group/personas/`（私有，同名优先），`chatlocal/mcp_chat_prompts.py` 负责发现与加载 |
| `server_wg.py` | 后端唯一入口：FastAPI，HTTP `47860` / MCP `48777`，`install_mcp_routes` 挂载 MCP；不 import `app.py`/Gradio（已删） |
| `water-group/` | QQ 群水群专属自闭环模块：`bootstrap.py`（一键后台启动、端口屏障与自检）、`memory_engine.py`（群友小本本动态记忆门面，issue #11 PA 起拆为 `memory_store` 存储层 / `memory_profile` 画像层 / `memory_observe` 观察层 / `memory_dossier` 档案层，对外 API 不变）、`memory_commands.py`（指令语法解析）、`memory_format.py`（L1 画像卡渲染）、`sync_groups.py`（群列表/名片同步）、`daemon/sixty.py`（60s 资讯指令取数，issue #53 方案 A）、`personas/` 角色人格卡 |
| `water_group/` | 根目录标准 Python 模块兼容桥接 shim（连字符目录不可直接 import）。**它是唯一知道连字符目录存在的 water-group 文件**：通过扩展自身 `__path__` 使每个文件以 `water_group.<name>` 恰好可达一次；子模块经 PEP 562 懒加载解析。**严禁把连字符目录加入 `sys.path`**——同一文件以两个名字加载会拆分状态（记忆引擎单例曾因此损坏） |
| `scripts/` | 检查与辅助脚本，以及共享基建：`fixture_server.py`（假服务器样板基类）、`check_fixture_support.py`（隔离环境/临时目录/网络护栏）、`incremental_common.py`（增量游标指纹）、`setup-napcat.ps1`（NapCat Shell 拉取器，pinned + sha256 校验） |
| `doc/` | 水群与 MCP 协议文档：`MCP_CHAT*`、`MCP_REACTIONS*`、`GROUP_MANAGEMENT`、`REPLY_TIMING`、`WATER_GROUP_ONLY_PLAN`、`SECURITY`、`THIRD_PARTY`，及两轮 DRY/SRP 审计报告 |
| `tools/` | NapCat Shell（OneBot 11 适配器，`setup-napcat.ps1` 拉取，受 `.gitignore` 保护）。qq-reader / wechat-reader / snowluma 等桌面壳组件不在本项目内 |

**运行时/本地数据（仓库内只剩配置，数据全部在 `~/.water-group/`）**：本地数据根 `~/.water-group/`（`state/` 守护状态、`logs/` 全部日志、`data/` SQLite 库与媒体缓存、`tmp/` 临时目录；`WATER_GROUP_HOME` 可改根，`IDLEJOT_WG_STATE` / `TULPA_WG_STATE` 可改 state 目录，`IDLEJOT_WG_DB` / `IDLEJOT_WG_MEMORY_DB` 可钉单个库给测试用）；仓库内受 `.gitignore` 保护的仅剩 `imports/`、`reports/`、`build/`、`.env`、`.cache/`、`water-group/config.json`、`tools/`、以及部署者自建的私有人设卡。路径单一事实来源：`water-group/daemon/wg_paths.py`；`chatlocal/config.py` 与 `water-group/memory_store.py` 因不能 flat import 而按同规则复述一份，改规则时三处一起改，`scripts/check_path_hygiene.py` 会拦住漏改。

---

## 2. 强制性行为与开发规范

所有在本仓库工作的 Agent 必须严格遵守以下准则：

### 2.1 包管理器与模块导入规范
- **Python 包管理器**: 强制使用 `uv` 管理依赖与执行命令（如 `uv run python ...`），**严禁使用传统的 `pip`**。
- **Node 脚本**: 仓库无 `package.json`，`.cjs` / `.mjs` 均为零依赖直跑（`node scripts/xxx.cjs`）；**如确需引入 Node 依赖，必须使用 `pnpm`，严禁 `npm` / `yarn`**。
- **模块导入规范**: 导入水群子系统模块时，必须统一使用标准 Python 命名 `import water_group...`；**严禁**为连字符目录 `water-group/` 编写 `sys.path.insert(0, ...)`。

### 2.2 进程与系统安全规范
- **严禁使用宽泛通配符杀进程**：坚决禁止执行 `taskkill /F /IM node.exe` 或 `python.exe`（会连带杀死当前 Agent 宿主）。
- **保护宿主机 QQ 进程**：宿主机正在运行的官方桌面 QQ 是通信源，**绝对不可终止**。杀注入实例时**认 `-q <self_account>`**，别认启动器开关，更不要按进程名通配。
- **端口清理三步走**：
  1. `netstat -ano | findstr :<port>` 查找精确 PID。
  2. `tasklist /FI "PID eq <pid>"` 验证进程属性。
  3. 确认非 Agent 与非关键系统进程后，使用 `taskkill /F /PID <pid>` 精准关闭。

### 2.3 代码架构与文件修改规范
- **单一职责（SRP）与 DRY 原则**：杜绝多功能堆砌与复制粘贴，复用逻辑必须抽象为独立函数或模块。两轮 DRY/SRP 审计的方法论与既有结论见 `doc/DRY_SRP_AUDIT_2026-10-19.md` 与 `doc/DRY_SRP_AUDIT_2026-10-20.md`，动大手术前先读。
- **禁止 Shell 直接写文件**：严禁使用 `echo`、PowerShell `Out-File` 或重定向符（`>`、`>>`）修改/创建代码文件，必须使用专用编辑写入工具以防转义与编码丢失。
- **文件局部编辑优先**：修改现有代码时优先使用精确匹配替换，避免全文件盲目重写。
- **保留既有安全语义**：不得削弱数据迁移、范围校验、审批与原文来源（provenance）检查；模型不能自行批准发送或群管理操作。

### 2.4 隐私与版本控制规范
- **严禁提交敏感文件**：`.env`、`water-group/config.json`、`water-group/status.json`、部署者自建的真实人设卡、`data/`、`imports/`、`reports/`、`tools/` 均在 `.gitignore` 保护范围内，严禁强制添加到 git。
- **禁止批量 `git add .`**：永远使用 `git add <file>` 逐个暂存文件。
- **代码提交前门禁**：任何 commit 前必须运行并通过安全扫描：
  ```bash
  uv run python scripts/check_publication.py
  ```
  扫描范围是 `git ls-files` 追踪的工作区 Markdown 文件的本地链接完整性与必需文档清单；不扫提交历史。

### 2.5 单一归属目录（新增代码的落脚点）

复制粘贴前先查下表；新代码必须收敛到既有单一归属，而不是再造副本：

| 关注点 | 唯一归属 |
|--------|----------|
| MCP wire-contract schema 片段（`session_id`/`idempotency_key`/`event_id`） | `chatlocal/mcp_schemas.py`（工厂函数返回 fresh dict，避免就地改写泄漏） |
| 聊天等待超时默认值 | `chatlocal/mcp_chat_limits.py: WAIT_TIMEOUT_SECONDS` |
| 文件 SHA-256 / MD5 | `chatlocal/artifacts.py`（`hashlib.file_digest` 流式，禁止 `read_bytes()` 全量读入） |
| 假服务器样板 | `scripts/fixture_server.py`（`FixtureHandler` + `fixture_server()` contextmanager） |
| 测试隔离环境 / 临时目录 / 网络护栏 | `scripts/check_fixture_support.py`（`isolated_env` / `temp_dir` / `network_guard`）。**模块顶层已钉数据根并立即 import `memory_engine` 冻结常量**（issue #89，`#79` 第二例）：`WATER_GROUP_HOME` / `IDLEJOT_WG_MEMORY_DB` 是 import 期求值的（`memory_store.py` 顶部常量、`wg_paths.py:29`），而 `chatlocal/mcp_chat.py` 的 `get_memory_engine()` 全是延迟导入（`399/547/868/1005/1018/1042` 行）、事件又跑在 `onebot_events.py:169` 的后台线程里——只靠 `main()` 里的 `patch.dict` 会被线程逃逸，11 个门禁就这样把夹具档案写进实机库。子进程探针仍要把 `WATER_GROUP_HOME` / `IDLEJOT_WG_MEMORY_DB` 透传进 `env=`（issue #79） |
| 增量导入游标指纹 | `scripts/incremental_common.py` |
| 混编日志逐行解码（UTF-8 + GBK 嗅探） | `water-group/daemon/wg_logs.py`（`decode_log_line` / `read_log_lines`，CLI 带 `--tail` / `--grep`） |
| 引用原文渲染（唤醒行 / `--context` / 追问行） | `water-group/daemon/wg_quotes.py`（`render_event` 读 wg_events 的 `referenced_messages`，`render_payload` 按 `message_id` 本地反查；「没有原文」必须由状态说出来，issue #94） |
| 控制台代码页 + stdout 编码 | `water-group/daemon/winconsole.py`（`ensure_utf8_stdio`，入口禁止再手写 `sys.stdout.reconfigure`） |
| 长文本截断与截断标记 | `chatlocal/text_limits.py`（`clip()` 返回 `text/truncated/original_length`；上限按用途命名：`READ_PREVIEW_CHARS` 读聊天预览、`EVENT_TEXT_CHARS` 记忆事件单字段。**禁止在调用点再写裸切片**，静默腰斩是 issue #90） |
| 子进程开窗抑制（防黑框闪） | daemon 侧 `water-group/daemon/winconsole.py`（`run_hidden` / `popen_hidden`，会 OR 进调用方自带的 `creationflags`，并**把 `DETACHED_PROCESS` 换成 `CREATE_NO_WINDOW`**——前者不是窗口抑制器，孙代进程反而会开窗）；后端侧 `chatlocal/platform_process.py`（`run_hidden` / `owned_popen`）。禁止任何地方再手写 `getattr(subprocess, "CREATE_NO_WINDOW", 0)`，`check_no_console_child.py` 门禁 |
| 数据根路径 | `water-group/daemon/wg_paths.py`（daemon flat import）；`chatlocal/config.py` 与 `water-group/memory_store.py` 复述同一规则，禁止再从 `ROOT` 推导 `data/`、`diagnostics/`、`logs/`、`state/`（`check_path_hygiene.py` 门禁） |
| `#` 指令守护单实例锁 | `water-group/daemon/commands.py`（`LOCK` / `pid_alive` / `acquire_lock` / `clear_stale_lock`）；`up.py` 停止时只调用 `clear_stale_lock()`，禁止在别处复制锁路径或存活性判断 |

注：`scripts/` 中大量存量脚本使用仓库根 `sys.path.insert` 模式（见二轮审计第 4 节"记录不修"），**新脚本必须遵循 canonical 模式**，不得再扩散手写样板。

### 2.6 证据与验证纪律（二轮审计的流程教训）
- **无证据不修复**：任何 finding 必须附带可直接粘贴的 `文件:行号` 证据才可进入修复；无证据一律视为虚构（曾有子代理报告整段虚构目录与常量）。
- **完成声明以磁盘为准**：工人/子代理报告"已完成"后，lead 必须独立用 `git status`、文件存在性、门禁原始输出复核，不得采信转述。
- **门禁安静窗口 solo 执行**：测试按序单独运行，不在并发写窗口内跑；红门禁先用 git worktree pristine HEAD 对照归因，再决定是否修复。

### 2.7 仓库改动流程（issue → plan → worktree → PR）

所有仓库改动（代码、文档、门禁、配置）按五步走，缺一步即不合规：

1. **先发 issue**：按 [`doc/ISSUE_GUIDE.md`](doc/ISSUE_GUIDE.md) 立单，正文四要素（现象 / 证据链 / 影响面 / 修复候选）齐全——issue 是派活与验收的唯一依据。**发布前正文过隐私门禁**（`scripts/check_publication.py --require-private --text <草稿>`），见 §5.3。
2. **写 plan 追评到 issue 下面**：探索结论（带 `文件:行号` 证据）+ 方案 + 明确不做 + 验证方式；定稿后实施只追加不改口。
3. **开 worktree 改代码**：`git worktree add .tmp/wt-<issue> -b type/<issue>-<slug> slim`，不在主工作区切分支；分支 / PR 标题 / commit 三处带 issue 号。大 issue 拆多个 PR 时，分节用 `P+字母`、优先级用 `P+数字`，命名落点见 [`doc/PR_GUIDE.md`](doc/PR_GUIDE.md)「拆分与命名规范」。
4. **开 PR**：按 [`doc/PR_GUIDE.md`](doc/PR_GUIDE.md)，正文分段齐全，修红门禁必须带基线验证段；**CI 全绿 + 无冲突才合并**。PR 标题 / 正文 / commit message 同样先过 §5.3 发布门禁。
5. **合并后对账**：自述 vs 实际逐项核对，欠账转新 issue（#41 模式）。

与 §0 的边界（issue #83）分工：那里管「水群回路里主 Agent 只水群、重活派后台 subagent」，本节管「仓库改动怎么走 issue/PR」；派活 prompt 模板见 `.agents/skills/water-group/SKILL.md` §五。

---

## 3. 水群记忆引擎与 MCP 原生交互体系

### 3.1 三源数据输入 (Three-fold Input)
1. **显式调侃指令**: 实时识别群聊打闹命令并落库（语法解析在 `memory_commands.py`）：
   - `@机器人 记在小本本上 [某人] 其实 [喜好/狠料]`
   - `@机器人 标注/认认/绑定 <QQ号/@提及> 是 <人名> (<角色>)`
2. **交互捎带提取 (Piggyback)**: 在直接回复或 @ 对话中，轻量提取发言者的口癖、态度与交互偏好。
3. **空闲摘要消化 (Idle Digest)**: 在轮询等待的空闲期，小批次异步分析未处理的历史消息，丰富人物档案。

### 3.2 两层存储架构 (Two-tier Storage)
- **Tier 1 (L1 Context)**: 超紧凑画像卡（约 80 tokens/人，渲染逻辑在 `memory_format.py`），发言时自动注入模型上下文：
  `【群友档案：<人名> (QQ: <uid> / 名片: <card>)】外号: ... 狠料: ...`
- **Tier 2 (L2 DB)**: SQLite 持久化（`data/group_memories.sqlite3`），包含完整时间戳事件轴、引用原话与结构化槽位。

### 3.3 MCP 原生读写工具闭环（严禁 Agent 直连写 SQLite）
Agent 必须且仅能通过标准化 MCP 工具维护群友记忆，**严禁自行编写 ad-hoc 脚本直接修改 SQLite**：
- `query_member_dossier`: 查询指定群友的小本本记忆、性格特征、别名与事件时间轴。
- `update_member_dossier`: 原生更新群友的画像字段（`key_memories`、`traits`、`aliases`、`role`），支持追加与覆盖模式（`overwrite=True`）。
- `consolidate_chat_memory`: 将一段群聊事件批量提炼、落库并同步更新相关成员的结构化档案。

### 3.4 群成员号码与名片对齐矩阵
群友频繁修改花哨的群名片（例如将名片改为梗名）会导致系统失去上下文。本系统通过三层对齐彻底解决该问题：
1. **静态配置映射**: 在 `config.json` 的 `member_mappings` 绑定 QQ 号与 `seed_key`（如 `seed:example_a`、`seed:example_b`），启动时自动合并孤儿种子。
2. **OneBot 名片同步**: 每次启动自动通过 `get_group_member_list` 获取所有成员的最新名片并推入别名表（`sync_groups.py`）。建档主名按 **昵称 → 名片 → QQ 号** 回落，名片只进 `aliases` 与 `traits.card`，绝不定成主名（issue #37）；已建档档案的名片变更不得回写 `primary_name`。
3. **群内动态认亲**: 群友发送 `@机器人 标注 <QQ号> 是 <花名>` 即可秒级更新映射。

---

## 4. 水群状态机与回复 SOP 规范

### 4.1 规划校验与防崩溃（Planner State Machine）
调用 `plan_chat_reply` 后，**严禁假设返回值必定包含 `plan_id`**：
1. 校验返回结果的 `state`：
   - 若 `state == "READY"`：安全获取 `plan_id = result["plan_id"]`，进入发送流程。
   - 若 `state == "REJECTED"` 且 `error_code == "stale"`：说明群内切入了目标相关的新消息。此时 `plan_id` 为 `None`，必须提取返回的 `updated_chat["input_batch"]`，使用最新批次原地重规划，切勿崩溃退出。
   - 若 `state == "SILENCE"`：表示当前语境适合潜水无需回复，流程正常结束。

### 4.2 反语癖固化机制 (Anti-Habituation)
自回归生成容易将前序回复中的单一口癖（如频繁以“……草”、“难绷”结尾）不断回喂放大。系统内置了标点清洗与多重后缀检测，当触发连续口癖时会自动在 `social_state` 提示词中注入多样性约束，Agent 应当主动响应约束、变换句式。

---

## 5. 关键验证测试套件

在向仓库推送或交付任何改动前，必须依次执行（**安静窗口单独按序运行**）并通过以下门禁。

### 5.1 水群与 MCP 聊天核心门禁
```bash
# 1. 动态记忆引擎、MCP读写工具与两层存储测试 (12项核心门禁)
uv run python scripts/check_group_memory.py

# 1b. 群级结构化术语（黑话/店名/场地/群内约定）读写闭环（issue #31）：
#     group_notes 建表与 (group_id, term) 主键；consolidate notes 写入后按 term 读回；
#     重复写走 upsert 不加行；跨群隔离；L1 注入封顶 GROUP_NOTE_L1_CAP；
#     MCP query_group_notes 工具声明
uv run python scripts/check_group_notes.py

# 2. 一键后台启动健康检查与代理隔离测试
uv run python scripts/check_bootstrap_health.py

# 3. 根目录 Python 模块化兼容桥接测试
uv run python scripts/check_water_group_import.py

# 4. 反语癖固化与多样性提示词注入测试
uv run python scripts/check_anti_habituation.py

# 5. 状态机拒绝 (stale) 与重规划健全性测试
uv run python scripts/check_plan_recovery.py

# 6. MCP 聊天输出稳健性测试 (输出截断、游标与ACK校验)
uv run python scripts/check_mcp_chat_output.py

# 7. 对话轮次生命周期与原生引用测试 (19项高并发与防竞争检查)
uv run python scripts/check_mcp_chat_turns.py

# 8. 发布代码敏感信息与泄露扫描 (工作区 Markdown 链接完整性 + 必需文档清单)
uv run python scripts/check_publication.py

# 9. daemon 入口冒烟 (以 `python <script>` 方式启动每个入口，验证函数内延迟导入可达)
uv run python scripts/check_daemon_entries.py

# 10. 一键停栈完整性 (孤儿清扫 / 自杀防护 / 只读探针不被误杀 / 端口兜底)
uv run python scripts/check_stop_stack.py

# 11. 路径卫生 (运行时数据不许从仓库根推导；防 issue #14 复发)
uv run python scripts/check_path_hygiene.py

# 12. issue #16 审计缺陷回归 (store 异常类型 / grants 排序 / MCP 响应解析 / 会话活性)
uv run python scripts/check_issue_16_audit_fixes.py

# 13. watchdog 探针现读 OneBot token（轮换 config.json 后探针必须跟随，防 #17 复发）
uv run python scripts/check_watchdog_token.py

# 14. collector 不落盘自身回显（唤醒流只留群友消息，防 #28 复发）
uv run python scripts/check_collector_self_echo.py

# 15. 子进程不开控制台窗口（运行时树 spawn 必须带 creationflags，防 #39 复发）
uv run python scripts/check_no_console_child.py

# 16. NapCat 启动顺序：直接注入优先，注入失败不降级到 .bat（防 #39 的 cmd 黑框复发）
uv run python scripts/check_napcat_launch_order.py

# 17. 60s 资讯层契约：信封判 code、天气参数是 query、城市后缀归一化、allow 闸门（issue #53）
uv run python scripts/check_sixty.py

# 18. 全栈热重载（issue #58）：闭包覆盖 fail-closed、target 映射、语法错整批拒绝、
#     debounce 合并、请求单飞、watchdog deferred、db_keeper 先起新再停旧、
#     quarantine 熔断、端口未释放不 spawn、CLI 无 subprocess
uv run python scripts/check_hot_reload.py

# 19. 长文本截断契约（issue #90）：取数层不截断、summary 完整落库、
#     超限写入带 truncated/original_length、旧库幂等迁移
uv run python scripts/check_text_truncation.py

# 20. app.log 唯一性（issue #97）：一次后端启动只写一份启动/停止横幅，
#     MCP 线程 INFO 不再冒泡成第二份、WARNING 带 [mcp] 仍可见、过滤器不残留
uv run python scripts/check_app_log_once.py

# 21. 引用原文可见性（issue #94）：带 reply 段的消息必有 referenced_messages 且 state 明确；
#     唤醒行/--context 印原文或明说「未解析 / 不在本地缓存」，不靠不印表达失败
uv run python scripts/check_quote_visibility.py

# 22. 「现在」锚点（issue #95）：status 首行 / --list / --context 表头 /
#     watch 唤醒块 / followup --show 都必须带当前时刻，且与 time.time() 对得上
uv run python scripts/check_now_clock.py

# 23. 沉淀写入动词（issue #6）：--ingest 落一条 source=summary 事件；超 12000 字
#     exit 2 且不写库；--through 只在写入成功后推进游标；--apply 带 group_id 追加
#     只写覆盖行（#5 边界）；未知字段 fail-closed；--dry-run 零写入
uv run python scripts/check_dossier_ingest.py

# 24. 私有目录巡检（issue #6）：tmp/ 顶层禁 .py/.md/.ps1（它是 TEMP 的重定向目标）；
#     bin/ 与 archive/ 的脚本必须在对应 INDEX.md 登记；drafts/ 非空只警告；
#     archive/ 条目缩水只报告。无数据根时自动跑 A/B 自检，不空转
uv run python scripts/check_tmp_hygiene.py

# 25. 转发体唤醒（issue #9）：合并转发里的 @ 与关键词必须参与 wake_reason；
#     首层/嵌套两层、不可得时不猜、节点 100/字符 4000 封顶、# 指令闸门优先
uv run python scripts/check_forward_wake.py

# 26. 模块体量红线（issue #11）：单文件 >1000 行或单类 >800 行即 fail，
#     700+ 行进提示带；豁免名单逐条注明由哪个分节消除，清空即拆分完成
uv run python scripts/check_module_size.py

# 27. app 侧插件协议（issue #4 PD）：工具名/授权位/表达三张静态表与宿主常量一致；
#     停用即降级（工具消失 + 授权位清空）；私聊自动关掉自己；不吃别人的事件；
#     `chatlocal/*.py` 里不得再出现插件名硬编码（宿主只认注册表）
uv run python scripts/check_app_plugins.py

# 28. 跨群主档污染审计/清理/还原（issue #34）：--audit-pollution 纯只读且数值
#     与手工 SQL 一致；--scrub-pollution 只剔覆盖行已持有的条目（仅主档条目、
#     无覆盖行群友、越 scope 群友一律不动）；清理后本群 _memory_view 并集逐条
#     不变、无 group_id 查询不再泄漏群内事实；原主档列表入 key_memories_backup，
#     --restore-pollution 原样还原并清槽；两个写动词都有 --dry-run 且零写入
uv run python scripts/check_pollution_scrub.py
```

黑框闪（issue #39）分两步查：静态门禁管“别再写错”；真闪了要抓现行，挂
`uv run python scripts/check_console_window.py --minutes 30`（命中直接打印 pid + 窗口类），
`--self-test` 是 4 组 A/B 对照（会故意闪两次黑框，属正常）：`visible` / `detached-cmd`
两组必须命中，用来证明探针没瞎、并且这台机器还能复现 DETACHED 的 bug；`hidden` /
`nowindow-grandchild` 两组必须 0 命中。只跑后两组等于没做对照 —— 一个 0 命中什么都证明不了。

### 5.2 仓库级回归门禁
```bash
# MCP 协议层契约
uv run python scripts/check_mcp.py
```

完整 25 门禁清单与顺序见 `doc/WATER_GROUP_ONLY_PLAN.md` §8。`scripts/` 已按 issue #22 清掉桌面壳遗留的死脚本（模块级 import 无法解析者，含传递闭包共 62 个），现存的每个脚本自身 import 都能解析。新增脚本必须是活门禁：要么进 §5.1/§5.2，要么进 CI 清单，否则按死代码处理。

**红门禁归因纪律**：失败时先用 git worktree pristine HEAD 对照——若同族失败则为既有环境问题（沙箱 WAL 竞态等，见审计报告第 3 节），记录在案，不要盲目"修复"。

### 5.3 发布门禁（隐私 / 个人信息）

`scripts/check_publication.py` 是唯一的发布门禁，**仓库里不写死任何真实号码或花名**——名单每次运行从私有库（`~/.water-group`，`WATER_GROUP_HOME` 可改）实时派生：`config.json`（账号 / 群号 / 成员映射）、`personas/*.md`、`data/*memories*.sqlite3`（群友档案，随记忆增长自动覆盖新群友）、`rules/water_group.md` 里的数字、`publication_denylist.txt`（handle / 域名）。通用词例外放私有 `publication_allow.txt`。

| 入口 | 覆盖 |
|---|---|
| `check_publication.py`（CI 第一道） | 全部已跟踪文件：凭证、个人路径、私有库 token、图片白名单、未知二进制 fail-closed |
| `--history` | commit 作者/提交者邮箱（只放行 GitHub noreply）与 commit message |
| `--text <文件>` / `--text -` | issue / PR / commit 文案草稿，发布前跑 |
| `.githooks/commit-msg` | 每次 commit 自动扫 message |
| `.githooks/pre-push` | 每次 push 自动跑 `--require-private --history` |

钩子靠本地配置生效：`git config core.hooksPath .githooks`（换机器后重设一次）。新图片必须先人工打开确认，再登记进 `REVIEWED_IMAGES`，否则门禁 fail。

---

## 6. 快速启动指南

### 6.1 启动水群后台服务

**唯一入口是 `water-group/start-bot.pyw`，必须由 explorer 代启动**（原因见 §0：沙箱注入会杀死 QQ）：

```bash
.venv/Scripts/python.exe -c "import subprocess,pathlib; subprocess.Popen(['explorer.exe', str(pathlib.Path('water-group/start-bot.pyw').resolve())])"
```

它幂等：端口已在就什么都不做；否则先 `up.py --stop` 退掉旧栈再起新的。会一直不返回，输出在 `~/.water-group/logs/launcher.log`。

`water-group/bootstrap.py` 降级为**只读自检**。在代理沙箱里跑它会被直接拒绝（exit 2），因为从那里起的栈会把 `tsbx.dll` 一路继承到 QQ：

```bash
# 仅检查端口、OneBot 与后端健康（不保持运行、不拉起任何进程）
uv run python water-group/bootstrap.py --status-only
```

### 6.2 启动主后端（前台调试用）

常驻栈走 §6.1 的 `start-bot.pyw`（它内部拉起 `supervisor → server_wg.py + collector + db_keeper + watchdog`）。只在需要前台看日志时单独跑后端：

```bash
uv run python server_wg.py
```

`server_wg.py` 是 slim 的后端唯一入口：FastAPI，HTTP `47860` / MCP `48777`。端口被占会 exit 2。

### 6.3 停栈

```powershell
.\stop.ps1     # 委托 up.py --stop：守护/看护/树外残留/注入实例/端口兜底一次清完
```

三个入口同一实现：双击 `water-group/stop-bot.pyw`、`.\stop.ps1`、`up.py --stop`。细节见 §0.1。

### 6.4 常用辅助
```powershell
# 拉 NapCat Shell（首次或换版本；pinned + sha256 校验）
.\scripts\setup-napcat.ps1
.\scripts\setup-napcat.ps1 -Update   # 拉最新版并打印新 pin，供人工复核后写回脚本
```
