# 闲笔 (IdleJot) AI Agent 协作与开发指南 (AGENTS.md)

欢迎使用 闲笔 (IdleJot) 仓库。本指南是专为进入本项目的 AI Agent（以及协同开发者）设计的操作手册，旨在说明项目架构、开发规范、核心子系统设计以及强制性安全门禁。

面向使用者的教程在 `doc/`（水群协议与各 MCP 工具的验证记录、安全说明、两轮 DRY/SRP 审计报告）；**所有 Agent 的仓库改动流程（先发 issue → plan 追评 → worktree → PR → 对账/回签）见 §2.7，操作指引在 `doc/ISSUE_GUIDE.md`（怎么发 issue）与 `doc/PR_GUIDE.md`（怎么开 PR）**；水群子系统的 Agent 侧规则另有 `.agents/rules/water_group.md`（通用骨架）与 `.agents/skills/water-group/SKILL.md`，**私有 SOP（群号、群友花名/QQ/地域、人设卡、数据源速查、排查实例）不在仓库里**，在 `~/.water-group/rules/water_group.md` 与 `~/.water-group/personas/`。本文件面向**仓库内的开发与维护工作**。

## 0. 接管水群（用户说「在 xxx 群挂机水群」时先看这里）

一句话入口，**必须由 explorer 代启动**（前提：`.venv/` 已存在，没有就先跑 `setup.ps1` 或 `uv sync`）：

```bash
.venv/Scripts/python.exe -c "import subprocess,pathlib; subprocess.Popen(['explorer.exe', str(pathlib.Path('water-group/start-bot.pyw').resolve())])"
```

**不要用 `uv run python water-group/bootstrap.py` 起栈。** 代理沙箱给每个子进程注入 `tsbx.dll`，注入沿 `up.py → supervisor → watchdog → NapCatWinBootMain → QQ` 一路继承，QQ 约 20 秒后必崩（`0xC06D007F`，delay-load 的 `ERROR_PROC_NOT_FOUND`），watchdog 于是无限重注入，桌面上堆满崩溃框。`start-bot.pyw` 是 `.pyw`（关联 `pythonw.exe`，无控制台），经 explorer 拉起时父进程是 explorer，整条链干净。`bootstrap.py` 现在检测到注入会直接 `exit 2` 拒绝启动。

起好之后（先读状态，再挂监听）：

```bash
.venv/Scripts/python.exe water-group/daemon/up.py --status            # 只读复查
.venv/Scripts/python.exe water-group/daemon/listen.py --timeout 90    # 挂监听（只活一轮；退出末行给 RESTART 重挂指令）
.venv/Scripts/python.exe water-group/daemon/reply.py --list            # 看谁 @ 了我
.venv/Scripts/python.exe water-group/daemon/reply.py --context 30      # 回之前看群里在聊什么（含闲聊）
.venv/Scripts/python.exe water-group/daemon/reply.py <id> "<正文>"      # 回一条，自动带原生引用；发完默认续挂监听 90s，--no-wait 关闭
.venv/Scripts/python.exe water-group/daemon/reply.py --show <id>       # 读某条消息全文（wg_events 命中带引用展开；缺 id 明说原因）
.venv/Scripts/python.exe water-group/daemon/reply.py <id> "<正文>" --at <花名>  # 正文前 @ 人，可重复；真 @ 不是正文里的文本
.venv/Scripts/python.exe water-group/daemon/reply.py --context 30 --around <id> # 只看这条之前的部分
.venv/Scripts/python.exe water-group/daemon/reply.py --who <花名>     # 解析花名到 QQ 与归属群并给出画像卡，不发送
.venv/Scripts/python.exe water-group/daemon/reply.py <id> "看这张" --image <图片路径>  # 发本地图片，可重复；每张独占一条气泡排在文字之后
.venv/Scripts/python.exe water-group/daemon/look.py --event <id>       # 唤醒行图片标「可补读」时先落盘再读；--pick 看候选、--recent 看最近
```

`reply.py` 共 17 个参数，上面是最常用的；`--also`（连发 @ 用一条答复算作回了多条）、
`--force`（目标唤醒已过待回时效也发）、`--intent`、`--no-quote` 见 `reply.py --help`。
`--image`（issue #128）按**内容**校验：Pillow 把整张图逐帧解开才算数（不是看扩展名、
也不是看 `verify()`——它只对 PNG 有效），格式限 PNG/JPEG/GIF/WebP，单张 ≤6 MiB、
合计 ≤80 兆像素、≤300 帧，一次最多 9 张。任何一张不合格就一张都不发（先校验再动手，
不会发一半）；带图的回复强制走 OneBot 直发，因为计划通道只会发文字、回退过去等于
悄悄吞掉图片。只发图时正文可省。判据的边界是「整张解不出」——解得出、只是少了尾部
（丢 IEND 的 PNG、帧边界截断的 GIF）会放行，发进群是完整但可能短一点的图，不是裂图。
`look.py --frames all` 把动图补成 3 帧（默认只取 1 帧），遇到动图结论可疑时加上。

唤醒行里的图片标记三种形态：`path=…`（已落盘，直接读）、`可补读 look.py --event <id> --index <i>`
（deferred，先跑命令再读）、`未登记`（无任何记录，命令照样能补读）。别对着裸 `[图片]` 说
「看不到图」——先看标记尾缀（issue #35）。

**时间基准（issue #95）**：`up.py --status` 首个非空行、`reply.py --list` / `--context`
表头、`watch.py` 唤醒块首行、`followup.py --show` 首行都带
`现在 2026-10-23 20:52:07 (UTC+8)`。判断「今晚 / 还有多久 / 开播了吗」一律以这行为准，
**不要拿消息自己的时间戳或 `19s 前` 当现在**——消息可能是一小时前的，相对量在你思考
几十秒后就失真了。这几处格式由 `water-group/daemon/wg_clock.py` 统一提供，
`scripts/check_now_clock.py` 门禁钉住。

**等待预算（issue #79 / #83）**：三个数只写在 `water-group/daemon/wg_budget.py`——
宿主工具调用超时 `AGENT_TOOL_TIMEOUT=120`、发送余量 `SEND_BUDGET=30`、单次调用最多能等
`WAIT_BUDGET=90`。`reply.py --wait` 默认、`listen.py` 常驻周期都是 90；**你自己把工具超时
调小过，就必须同步调小**：被截断时父进程和它起的子进程在同一个进程组里一起死，而那条路径
上一个字都不打印——Agent 看到的最后一行停在「等下一次唤醒…」，分不清是被杀还是还在等。
要短一点用 `--wait 60`，要立刻返回用 `--no-wait`。`listen.py` 每轮进入 `watch.py` 之前会打
「等最多 90s（第 1 轮）…」，`RESTART:` 行只推荐不超预算的取值（短的回常驻周期、超的压回
预算），文档里的 `--timeout` 示例也由 `scripts/check_listener_rehang` 逐个扫；
**仓外的私有规则 `~/.water-group/rules/*.md` 同样在扫**（issue #87）——它不在仓内，
但「动手前先读」的最高权威文档不能教超预算的取值。

**状态行看得见预算（issue #86）**：`up.py --status` 的「监听」行会报出每个监听**实际的**
`--timeout`（从命令行读，不是从源码推的），超预算的那个带 ⚠ 点名——修复前起的旧监听
（远超预算的旧默认值）在状态里一眼看得出，不必再另跑一条 PowerShell 查 WMI。
`listen.py` 显式传超预算的 `--timeout` 时也会自己打一行 `WARN`。

**监听存亡（issue #112 / #119）**：`listen.py` 每一轮值守都会在
`~/.water-group/state/wg_listeners/<pid>.json` 留一份（起点、退出时刻、退出原因；并行监听
各记各的，后起不覆盖先起），等待期间由 `watch.py` 续心跳；`up.py --status` 的「监听」行在
没人值守时会把上一轮的起止和空窗时长带出来（`；账本：上一轮监听 18:02:01–18:02:01 已退出（NO_WORK），此后 14 分 37 秒无人值守`），
被硬杀写不了退出记录时以心跳过期给出判定。**判定「现在有没有人值守」不依赖谁恰好跑一次
`--status`**——任何一方读这个目录就行（`wg_listener_state.on_watch()` / `describe()`）。

**状态行只说核实过的事（issue #142）**：栈整体静默死亡后，`--status` 以前会同时报
「NapCat: 离线」与「监听：未挂 —— 消息照收」，守护行还会断言「supervisor 应会重启」——
可那时没有任何进程会执行那次重启。现在监听行缺哪环就点名哪环（NapCat 离线 / collector
未运行），只有两环都在线时才说「消息照收」；守护行为空时明说「不会自愈，需要重新拉起
整栈」；账本段的上一轮历史统一带「仅供参考」，不再与现刻结论同形；空栈跑 `--stop`
会多一句「栈本来就没在跑：无事可做」（退出码仍为 0，`start-bot.pyw` 的幂等前停依赖它）。

**WAKE 退出空窗不说「消息照收」（issue #212）**：「消息照收」说的是「NapCat 在线 ∧ collector
在跑」两环，与「有没有人在值守」无关。账本最近一条 stop 原因为 `WAKE` 时，正是刚叫醒、
等重挂的空窗——消息在落盘但没人值守把它变成唤醒，空窗内新 @ 只能靠重挂后首轮 `BACKFILL`
补扫。此刻监听行改说「等待重挂——空窗内新 @ 靠重挂后首轮 BACKFILL 补扫（此刻无人值守）」，
不与「消息照收」并排。判据钉死在 `reason == "WAKE"`：NO_WORK / DOSSIER_DUE / WATCH_FAILED /
硬杀 / 无账本一律仍说「消息照收」。

**禁言解禁会叫醒监听（issue #192）**：账本（`state/wg_send_failure.json`）上压着未定论的
禁言时，`watch.py` 每 15 秒现查一次 `shut_up_timestamp` / `group_all_shut`；确认解禁就打印
`UNMUTED: 禁言已于 HH:MM:SS 实测解除（账本原判 …）`，照常以 `WAKE` 收尾并返回 0，所以
`listen.py` 仍会给出 `RESTART:`。仍禁言 / 探测失败 / 全员禁言一律静默，不刷屏；探测失败与
「全员禁言仍在」都不冒充解禁（#132 的纪律）。全员禁言没有解禁时刻，账本靠 `group_shut=True`
进探测。节流状态记在账本 `rechecked_at`，`reply.py --list` 仍是 60 秒档，两边共用同一份结论。
解禁后先 `reply.py --list` 看待回；重挂监听时首轮 `BACKFILL` 会把禁言期间积压的未回唤醒补扫出来。

追问窗口：回复成功后 `reply.py` 自动开窗（默认再跟 10 条或静默 180 秒），群里继续聊的
几句也会唤醒监听（唤醒行带 `[追问]` 前缀），话题能接着走；`--no-followup` 单次关闭。
它是插件（issue #4 PD2a）：参数与开关在 `config.json` 的 `plugins.followup`
（`enabled` 三态 + `groups` 白名单 + `options.max_messages` / `idle_seconds`），
旧写法顶层 `followup` 段仍逐字段兼容；详见 `~/.water-group/rules/water_group.md`。

**禁言期间不扫追问窗口（issue #197）**：账本上压着未解除的禁言时，那一轮一个字都发不
出去，`watch.py` 跳过追问扫描并打一行 `# 禁言中：暂停追问窗口扫描（…）`；窗口本身不动
（不关、不推游标、不扣额度），解禁后照旧唤醒、过期照旧自关。`@` 唤醒不受影响——有人
明确在找，`--wait-unmute` 能在等待预算内等。判据只看账本、不打接口（`muted_now()` 纯读）；
「是不是真解禁了」仍归上面那条 15 秒探测。

> **职责边界（issue #83 / #160）**：接管后主 Agent 只水群——白名单是 `listen.py` / `reply.py` / `up.py --status`（只读）/ 追问窗口接话 / `tail` 一行日志。
> 改仓库、跑测试门禁、写文档、审 PR、排查**一律立 issue 后停手，不派 subagent、不亲手做**；执行交给用户启动的独立会话（issue #160 撤掉「必须派活」，保留「必须留证据」）。
>
> **发现缺陷立即提 issue（最高优先级，issue #115）**：这一条的优先级高于水群本身——发现缺陷（值守、开发、审 PR、跑门禁时都算）当场立单，**不需要先问用户、不需要攒批、不需要等第二次复现**。**提 issue 不属于「改仓库」**，因此不受上面「主 Agent 只水群」白名单的限制。**提完 issue 主 Agent 的产出到此为止**：修复（issue → plan → worktree → PR）由用户启动的独立会话做，不由主 Agent 派 subagent 代劳。唯一例外：纯进程重启，或用户当次明确说「不用提」。issue 正文仍须过隐私门禁，仍不得暴露真实群号 / QQ / 群名片。
>
> **多群并发 = 用户开 N 个会话，一会话一群**，各会话互不可见；**不是**一个 Agent 派 N 个 subagent。所以「不派活」不削弱多群能力——多群靠会话数量，不靠派活。
>
> 细则见 `.agents/rules/water_group.md` §6，独立会话交接清单见 `.agents/skills/water-group/SKILL.md` §五。

日常循环、按花名 @ 人、连发 @ 的处理、排查表，全部在 **`~/.water-group/rules/water_group.md`**（私有，不在仓库），动手前读它；仓库的 `.agents/rules/water_group.md` 只有通用骨架。本文件后面的内容是仓库开发与维护规范，不是挂机 SOP。

**改了代码要不要重启、群里发指令没反应怎么查：见 §0.2。**

### 0.1 停栈（与启动对称）

三个入口同一个实现（`water-group/daemon/wg_up_stack.py:stop_stack()`，issue #37 前在 `up.py`），随便用哪个：

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
| `daemon/plugins/*.py`（插件适配器，issue #4 PA） | `.\reload.ps1`：`commands.py:85` 是模块级 `import plugins`、`load_all` 在 import 期跑，所以插件文件在 `commands` 的 AST 闭包里，不用手写映射 |
| `chatlocal/app_plugins/*.py`（app 侧插件，issue #4 PD2b） | `.\reload.ps1`：`chatlocal/mcp_chat.py:14-15` 模块级 import，闭包命中 `app`（`server_wg`） |
| `memory_engine.py` 及五个分层模块 | `.\reload.ps1`：闭包会命中 `app` / `commands` / `watchdog`，自动重启对应子进程 |
| `up.py` / `wg_up_probe.py` / `wg_up_wake.py` / `wg_up_stack.py` / `bootstrap.py` / `reload.py` / `winconsole.py` / `watchdog.py` / `supervisor.py` / `start-bot.pyw`（启动链、控制台、NapCat 注入） | **必须整栈重启**：`.\stop.ps1`（或 `up.py --stop`）后从 explorer 启动 `start-bot.pyw`；`reload.ps1` 会把 guard-owned 代码拒绝掉，普通重载不会替换已运行的 `up.py` 和它拉起的 NapCat（#4 的旧乱码控制台窗口就是这么留下的） |

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
2. 看启动行 `commands start ... handlers=<N> plugins=<名字:指令数,…>`（例：`plugins=miyoushe:21,sixty:14`）：
   时间早于提交时间 = 旧进程；`plugins=` 里**缺某个插件** = 它压根没加载起来（注册表记 `load_error`），
   写成 `sixty:0` = 插件在但一条指令都没注册（两种导入失败现在都会 `log`，见 issue #68 / #4 PA）；
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
      │  memory_dossier.py · memory_pollution.py        │
      │  memory_commands.py · memory_format.py · bootstrap.py │
      │  sync_groups.py · personas/                   │
      │  daemon/plugins/（# 指令 + 事件插件，issue #4）  │
      │  ⇅ chatlocal/app_plugins/（app 侧插件，PD2b）   │
      └───────────────────────────────────────────────┘
```

### 核心目录职责

| 目录 | 职责 |
|------|------|
| `chatlocal/` | 应用核心：`store.py`（SQLite/WAL 连接策略）、`mcp_*` MCP 工具与路由层（`mcp_server.py`、`mcp_tools.py`、`mcp_chat*.py`、`mcp_qq*.py`、`mcp_reactions.py`、`mcp_sticker_library.py` 等）、`agent*.py` 会话与人格、`onebot*.py` 网关、`voice.py` 云端 ASR。人格卡模板在 `water-group/personas/`（公开）与 `~/.water-group/personas/`（私有，同名优先），`chatlocal/mcp_chat_prompts.py` 负责发现与加载；`app_plugins/` 是 app 侧插件插座（MCP 工具 + 实时事件，见 §1.1） |
| `server_wg.py` | 后端唯一入口：FastAPI，HTTP `47860` / MCP `48777`，`install_mcp_routes` 挂载 MCP；不 import `app.py`/Gradio（已删） |
| `water-group/` | QQ 群水群专属自闭环模块：`bootstrap.py`（一键后台启动、端口屏障与自检）、`memory_engine.py`（群友小本本动态记忆门面，issue #11 PA 起拆为 `memory_store` 存储层 / `memory_profile` 画像层 / `memory_observe` 观察层 / `memory_dossier` 档案层 / `memory_pollution` 跨群污染审计层，对外 API 不变）、`memory_commands.py`（指令语法解析）、`memory_format.py`（L1 画像卡渲染）、`sync_groups.py`（群列表/名片同步）、`daemon/sixty.py`（60s 资讯指令取数，issue #53 方案 A）、`personas/` 角色人格卡。`daemon/` 是常驻守护与 Agent 侧 CLI（**逐文件职责见 `water-group/daemon/README.md`，本表不再重列**），其中 `daemon/plugins/` 是指令与事件插件的注册表（§1.1） |
| `water_group/` | 根目录标准 Python 模块兼容桥接 shim（连字符目录不可直接 import）。**它是唯一知道连字符目录存在的 water-group 文件**：通过扩展自身 `__path__` 使每个文件以 `water_group.<name>` 恰好可达一次；子模块经 PEP 562 懒加载解析。**严禁把连字符目录加入 `sys.path`**——同一文件以两个名字加载会拆分状态（记忆引擎单例曾因此损坏） |
| `scripts/` | 检查与辅助脚本，以及共享基建：`fixture_server.py`（假服务器样板基类）、`check_fixture_support.py`（隔离环境/临时目录/网络护栏）、`incremental_common.py`（增量游标指纹）、`setup-napcat.ps1`（NapCat Shell 拉取器，pinned + sha256 校验）；门禁清单与执行器是 `gates.json` + `run_gates.py`（**唯一事实来源**，§5.1），对账与设置类还有 `check_pr_parity.py` / `check_repo_settings.py` / `check_gate_change_review.py` |
| `doc/` | 水群与 MCP 协议文档：`MCP_CHAT*`、`MCP_REACTIONS*`、`GROUP_MANAGEMENT`、`REPLY_TIMING`、`WATER_GROUP_ONLY_PLAN`、`SECURITY`、`THIRD_PARTY`、`SLASH_COMMANDS`（`#` 指令体系）、`HOT_RELOAD`、`NAPCAT_TROUBLESHOOTING`、`MCP_TOOL_RELIABILITY`，以及流程指引 `ISSUE_GUIDE` / `PR_GUIDE`，及两轮 DRY/SRP 审计报告（具体篇目以磁盘为准，不在此穷举） |
| `tools/` | NapCat Shell（OneBot 11 适配器，`setup-napcat.ps1` 拉取，受 `.gitignore` 保护）。qq-reader / wechat-reader / snowluma 等桌面壳组件不在本项目内 |

**运行时/本地数据（仓库内只剩配置，数据全部在 `~/.water-group/`）**：本地数据根 `~/.water-group/`（`state/` 守护状态、`logs/` 全部日志、`data/` SQLite 库与媒体缓存、`tmp/` 临时目录；`WATER_GROUP_HOME` 可改根，`IDLEJOT_WG_STATE` / `TULPA_WG_STATE` 可改 state 目录，`IDLEJOT_WG_DB` / `IDLEJOT_WG_MEMORY_DB` 可钉单个库给测试用）；仓库内受 `.gitignore` 保护的仅剩 `imports/`、`exports/`、`reports/`、`build/`、`release/`、`instances/`、`research/upstream/`、`.env`、`.cache/`、`water-group/config.json`、`tools/`、以及部署者自建的私有人设卡。路径单一事实来源：`water-group/daemon/wg_paths.py`；`chatlocal/config.py` 与 `water-group/memory_store.py` 因不能 flat import 而按同规则复述一份，改规则时三处一起改，`scripts/check_path_hygiene.py` 会拦住漏改。

### 1.1 插件体系（issue #4）

可选能力的统一插座，**分两半且不合并**：daemon 侧管 `#` 指令与事件，app 侧管 MCP 工具与实时事件——宿主不同、生命周期不同、授权模型不同，合并会逼一方假装拥有另一方的保证。契约原文在两处模块 docstring，**改协议前先读它们，不要只看这里**：`water-group/daemon/plugins/base.py`、`chatlocal/app_plugins/base.py`。

| | daemon 侧 | app 侧 |
|---|---|---|
| 目录 | `water-group/daemon/plugins/` | `chatlocal/app_plugins/` |
| 现有插件 | `miyoushe_plugin.py` / `sixty_plugin.py` / `followup_plugin.py` / `busboss_plugin.py` / `repeater_plugin.py` | `language.py` / `reactions.py` |
| 静态面 | `plugins/__init__.py:55` 的 `LOADERS`、`:64` 的 `EVENT_LOADERS`（**显式清单，不扫目录**） | `spec.py:42` 的 `PLUGINS`（`REACTIONS` / `LANGUAGE` 两块）+ `loader.py:10` 的 `build()` |
| 扩展点 | `Command`（指令型，走 `Registry.commands()` 合成表）+ `Hooks.on_event`（事件型，没有表） | `AppPlugin` 的方法族：工具 / 事件 / 生命周期 / 描述子 |
| 开关 | `config.json` 的 `plugins.<名字>` 三态 + 群级覆盖 | `spec.policy` 文件名（文件/DB 数据态）或插件自判 |

**三条硬边界**（`plugins/base.py` 模块 docstring 的原话，别在实现里绕开）：

1. **插件 = 可选行为；平台 = 必需通路。** 进程生命周期（`up.py` / `supervisor` / `watchdog`）、OneBot 连接、MCP 装配不是插件。
2. **文件不搬迁。** 适配器放 `plugins/`，取数与传输模块留在原位；插件化改的是注册方式，不是目录结构。
3. **静态 import 的显式注册表。** `check_hot_reload.py` 要求 watch 目录下每个 `.py` 都能从 5 个入口的 AST 闭包到达；运行时扫目录既过不了门禁，也让 `reload` 无法把改动映射到进程。包内**一律绝对 import**（`import plugins.base`），相对 import 会把路径算错且是静默算错。

**加一个 daemon 插件**要改三处：`plugins/` 放适配器（模块级 `PLUGIN = Plugin(...)`）、`plugins/__init__.py` 的 `LOADERS` 加一行（有事件型能力再加 `EVENT_LOADERS`）、`config.json` 的 `plugins.<名字>` 加配置。**`commands.py` 不用改**——`HANDLERS` 由 `plugins.load_all(...).commands()` 在 import 期合成（`commands.py:85,97`）。

**加一个 app 插件**要改三处：`spec.py` 加一块（`tools` / `scope` / `expression` / `policy`）、同目录加模块、`loader.py` 的 `build()` 元组加类。宿主里**不许出现插件名字面量**，`scripts/check_app_plugins.py` 钉住。

**开关与兼容**：`Plugin.enabled()`（`plugins/base.py:137`）是三态——总开关 `plugins.<名字>.enabled` → `groups` 白名单 → 群级 `groups.<id>.plugins.<name>: false`；`group_id` 为空（`up.py --status` 那类全局查询）只判总开关，否则配了白名单的插件会在状态里显示停用。没迁完的老配置段用 `Plugin.legacy_section` 声明（只有 `followup` 有），宿主把它平铺读：`enabled` 进三态、其余键进 `options`。

**事件流总闸**：`commands` 进程的总闸是 `slash_commands.enabled`，关掉整个不启动。靠这条 WS 事件流吃饭的插件（目前只有 `repeater`，声明 `Plugin.needs_event_loop`）会让 `commands.event_stream_needed()` 在指令关闭时单独把它拉起来——否则「复读没反应」在日志里只有一行「不启动」，与「插件坏了」无法区分。追问窗口不声明：它由 reply/watch/up 三个一次性 CLI 派发，不依赖这个进程。

**两条派发路径（同一个 loader、同一个模块级 `PLUGIN`，不存在两份状态）**：

- commands 进程：`Registry.emit(event, ctx)`（`plugins/registry.py:127`）按注册序问每个声明了 `on_event` 的插件，**首个非 `None` 胜出**；事件是否受三态门控由插件自己决定，宿主不替它选。
- 一次性 CLI（`reply.py` / `watch.py` / `up.py`）：`plugins.emit(...)`（`plugins/__init__.py:88`）只过 `EVENT_LOADERS`，**不建 `CommandsHost`**——只为一条追问窗把米游社取数层拖进进程是错的。

**app 侧派生语义**（宿主遍历顺序 = 注册序，实测于 `chatlocal/app_plugins/base.py` 的 `AppPlugins`）：

| 语义 | 方法 |
|---|---|
| 全量 fan-out（每个插件都被调用） | `scope` / `decorate` / `reset` / `resume` / `close` / `suspend` / `on_stored` / `stopped` |
| 首个非 `None` 胜出 | `receive` / `wake` / `wake_hint` / `wake_signal` / `plan_context` / `select` |
| 聚合 | `batch` / `hints` / `stamps` |
| 经 `tool_owner` 直取、**不** fan-out | `handoff` / `durable` |

`suspend()` 必须独立存在：不能并进 `close()`（`close` 在锁外、`suspend` 在锁内 invalidate，合并会破坏锁序），也不能并进 `reset()`（`reset` 表示新连接接管，会轮转 reactions 的 epoch；suspend 没有后继连接，epoch 必须保留）。

改完插件怎么生效见 §0.2。剩余未迁移：PD2c（群友画像）、PD2d（媒体，本机不可验证，垫底），进度在 issue #4。

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
- **单一职责（SRP）与 DRY 原则**：杜绝多功能堆砌与复制粘贴，复用逻辑必须抽象为独立函数或模块。两轮 DRY/SRP 审计的**方法论**见 `doc/DRY_SRP_AUDIT_2026-10-19.md` 与 `doc/DRY_SRP_AUDIT_2026-10-20.md`，动大手术前先读——但两份报告是上游快照带入的历史文档，基线 commit 与 `web/`/`desktop/` 范围在本仓不存在，行数口径也与本仓实测不符（详见各报告开头的「来源与失效边界」标注，issue #169 PG-a）。**作为方法论参考，不作为现状基线**；现状以本仓 `slim` HEAD 为准，第三轮报告 `doc/DRY_SRP_AUDIT_2026-10-27.md` 已用本仓基线重写（issue #169 PG-b），作为现状基线；两份旧报告仅作方法论参考。
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
- **本机跑法（本文件所有 `uv run python scripts/...` 都适用）**：先显式清掉宿主注入的 `PYTHONPATH`，否则本机 `import chatlocal` 会静默成功、CI 上才炸（#4 PE 的假绿），跑出来的绿不算绿：
  ```bash
  /usr/bin/env -u PYTHONPATH .venv/Scripts/python.exe scripts/check_publication.py
  ```
  必须写全 `/usr/bin/env`，别只写 `env`——PATH 里的 `env` 是沙箱 shim，会吞掉子进程 stdout 却照样 `exit 0`（连 `> file` 都是空文件），绿得毫无信息。

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

1. **先发 issue（发现即提，不等批准）**：发现缺陷当场立单，**不需要用户同意**，最高优先级规则见 §0「职责边界」（issue #115）。按 [`doc/ISSUE_GUIDE.md`](doc/ISSUE_GUIDE.md) 立单，正文四要素（现象 / 证据链 / 影响面 / 修复候选）齐全——issue 是执行与验收的唯一依据。行为类 issue 的现象段必须带**一条可直接粘贴的复现命令**（期望输出 / 实际输出 / 触发条件齐全），给不出就贴 `needs-repro`，见 [`doc/ISSUE_GUIDE.md`](doc/ISSUE_GUIDE.md)「复现命令与 `needs-repro`」。**发布前正文过隐私门禁**（`scripts/check_publication.py --require-private --text <草稿>`），见 §5.3。
2. **写 plan 追评到 issue 下面**：探索结论（带 `文件:行号` 证据）+ 方案 + 明确不做 + 验证方式，并含一行「用 issue 的复现命令复现：成功 / 失败（贴原始输出）」——复现失败退回发现者补充，不进实施；定稿后实施只追加不改口。
3. **开 worktree 改代码**：`git worktree add .tmp/wt-<issue> -b type/<issue>-<slug> slim`，不在主工作区切分支；分支 / PR 标题 / commit 三处带 issue 号。大 issue 拆多个 PR 时，分节用 `P+字母`、优先级用 `P+数字`，命名落点见 [`doc/PR_GUIDE.md`](doc/PR_GUIDE.md)「拆分与命名规范」。
4. **开 PR**：按 [`doc/PR_GUIDE.md`](doc/PR_GUIDE.md)，正文分段齐全，修红门禁必须带基线验证段；**CI 全绿 + 无冲突才合并**（`slim` 已开分支保护、required check 为 `checks`，CI 没绿时 GitHub 直接判 `BLOCKED`；设置漂移跑 `scripts/check_repo_settings.py` 复现，见 [`doc/PR_GUIDE.md`](doc/PR_GUIDE.md)「合并条件」）。行为类 PR 用 `Refs #NN`，纯文档 / 纯重构才用 `Closes #NN`（见 [`doc/PR_GUIDE.md`](doc/PR_GUIDE.md)「关闭方式」）；diff 触及门禁链（`scripts/check_*.py` / `scripts/gates.json` / `scripts/run_gates.py` / `.github/workflows/ci.yml` / `.githooks/` 下钩子）时正文另带 `## 门禁改动` 与不同上下文的复核结论（见 [`doc/PR_GUIDE.md`](doc/PR_GUIDE.md)「门禁改动与独立复核」，`scripts/check_gate_change_review.py` 在 CI 上校验）。PR 标题 / 正文 / commit message 同样先过 §5.3 发布门禁。
5. **合并后对账与回签**：修复者先做自述 vs 实际逐项核对（`git status`、文件存在性、门禁原始输出），并给行为类 issue 贴 `needs-live-verify`；再由发现者或全新上下文的验证 agent 按 [`doc/PR_GUIDE.md`](doc/PR_GUIDE.md)「合并后：对账与回签」的模板回签，**回签通过才关 issue**（修复者不能给自己回签）。欠账转新 issue（#41 模式）。

与 §0 的边界（issue #83 / #160）分工：那里管「水群回路里主 Agent 只水群、发现即立 issue 且不派活」，本节管「仓库改动怎么走 issue/PR」；独立会话交接清单见 `.agents/skills/water-group/SKILL.md` §五。

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
- **Tier 2 (L2 DB)**: SQLite 持久化（`~/.water-group/data/group_memories.sqlite3`；仓库里的 `data/` 不是它，那是 `.gitignore` 掉的本地目录），包含完整时间戳事件轴、引用原话与结构化槽位。

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
   - 若 `state == "SILENT"`：表示当前语境适合潜水无需回复，`turn_complete=true`，流程正常结束（注意动作名是 `silence`、状态名是 `SILENT`，别按动作名去判状态；见 `chatlocal/mcp_chat_turns.py:413`）。
   - 若 `state in ("SUCCEEDED", "INTERRUPTED")`：本轮已结束。`SUCCEEDED` = 队列全部落地，`INTERRUPTED` = 只有部分落地（失败项既非成功也非 `UNKNOWN`，`chatlocal/mcp_chat_turns.py:557`）——**不要拿旧队列重发**，按下一轮的 `input_batch` 重新规划。

### 4.2 反语癖固化机制 (Anti-Habituation)
自回归生成容易将前序回复中的单一口癖（如频繁以“……草”、“难绷”结尾）不断回喂放大。系统内置了标点清洗与多重后缀检测，当触发连续口癖时会自动在 `social_state` 提示词中注入多样性约束，Agent 应当主动响应约束、变换句式。

---

## 5. 关键验证测试套件

在向仓库推送或交付任何改动前，必须依次执行（**安静窗口单独按序运行**）并通过以下门禁。

### 5.1 水群与 MCP 聊天核心门禁

门禁清单只有一个事实来源：[`scripts/gates.json`](scripts/gates.json)。每个 `scripts/check_*.py` 恰好归入 `ci` / `manual` / `stale` / `dead` / `support` 之一（档位语义见 §5.2）；`ci` 的顺序就是本机全量执行顺序，条目上的 `shard` 决定 CI 走哪个 matrix 分片（**同片串行、片间并行**，issue #60）；单门禁参数（如 `check_publication.py --history`）也写在清单里。`scripts/check_gate_manifest.py` 断言磁盘、清单、本节生成段与 `ci.yml` 的分片矩阵四者一致，**新增门禁只改清单**：

```bash
uv run python scripts/run_gates.py ci                  # 按清单顺序跑完全部 ci 门禁（本机全量）
uv run python scripts/run_gates.py ci --shard static   # 只跑一个分片（CI matrix 同一入口）
uv run python scripts/run_gates.py ci --fail-fast      # 本机快速试错：见到红就停
uv run python scripts/run_gates.py --list              # 看档位计数、CI 分片与 ci 顺序
uv run python scripts/check_gate_manifest.py --write   # 改完清单后重写下面这段
```

**本机跑法同 §2.4**：一律 `/usr/bin/env -u PYTHONPATH .venv/Scripts/python.exe scripts/run_gates.py …`（清掉注入的 `PYTHONPATH`，并写全 `/usr/bin/env`，否则沙箱 `env` shim 会吞掉子进程输出还报 `exit 0`）。下面的 `uv run` 是 CI 等价写法。

<!-- gates:ci:begin -->
> 本节由 `scripts/check_gate_manifest.py --write` 从 `scripts/gates.json` 生成，**不要手改**；顺序即本机全量执行顺序，`分片` 列即 CI matrix 分片（同片串行、片间并行）。

| # | 分片 | 门禁 | 防什么 |
|---|---|---|---|
| 1 | `static` | `check_publication.py --history` | 发布门禁：凭证、个人路径、私有库 token、图片白名单与未知二进制 fail-closed；--history 另扫 commit 身份与文案 |
| 2 | `static` | `check_gate_manifest.py` | 元门禁：磁盘 check_*.py、本清单、AGENTS §5.1 生成段与 ci.yml 分片矩阵四者零漂移 |
| 3 | `static` | `check_gate_change_review.py` | 门禁链改动必须在 PR 正文 `## 门禁改动` 段逐个交代（#62；#107 起 `.githooks/` 钩子改动同样触发）；无 PR 上下文时 SKIP |
| 4 | `static` | `check_path_hygiene.py` | 路径卫生：运行时数据不许从仓库根推导（防 #14 复发） |
| 5 | `static` | `check_no_console_child.py` | 子进程不开控制台窗口：运行时树 spawn 必须带抑制标志（防 #39 复发） |
| 6 | `static` | `check_listen_passthrough.py` | 唤醒内容不被 listen.py 吞掉（#91） |
| 7 | `static` | `check_listener_rehang` | 监听空窗：退出必带重挂指令、回复默认续挂、过期唤醒不静默、退出留痕且空窗可由只读入口报出（#69/#112） |
| 8 | `static` | `check_gap_disclosure.py` | 停机窗口缺口披露：后端不在的那段时间漏了什么事后说得出来，up.py --status / reply.py --list / listen.py NO_WORK 三处只读入口报出窗口内漏 N 条、@ 本机 M 条，不再报告一切正常（#114） |
| 9 | `static` | `check_status_napcat_line.py` | up.py --status 的 NapCat 行：括号内是 OneBot 真账号而非字段名；生产者形状与解析口径两侧各钉一条（只桩最底层 OneBot 调用），反例对照确认判据抓得住（#156/#159） |
| 10 | `static` | `check_wg_pause.py` | 采集暂停标记（#189）：有主 + 有时限才算数，owner 退出/超 TTL/遗留哑文件都判失效并由采集器当轮清理；暂停期间照写心跳、状态行报出原因与清理人，热重载不再被哑文件永久挡住 |
| 11 | `static` | `check_module_size.py` | 模块体量红线：文件超 1000 行或类超 800 行即 fail（#11） |
| 12 | `static` | `check_tmp_hygiene.py` | 私有目录巡检：tmp/ 禁脚本、bin 与 archive 须登记（#6） |
| 13 | `static` | `check_sender_name.py` | 发信人名形态：QQ 引用气泡文案不得进 sender 或 aliases（#39） |
| 14 | `static` | `check_text_truncation.py` | 长文本截断契约（#90） |
| 15 | `static` | `check_unsupported_segment.py` | 白名单外的消息段必须保真：type 恒英文 unsupported、kind 与受限长度 raw 都留下来，中文标签不许回流成段类型（#165） |
| 16 | `static` | `check_app_log_once.py` | app.log 一次后端启动只写一份横幅（#97） |
| 17 | `static` | `check_log_encoding.py` | 控制台与日志编码两层钉死（#4） |
| 18 | `static` | `check_slash_commands.py` | # 指令守护纯逻辑：过滤、冷却、去重、帮助 |
| 19 | `runtime` | `check_daemon_entries.py` | daemon 入口冒烟：以 python <script> 方式启动每个入口，验证函数内延迟导入可达 |
| 20 | `runtime` | `check_stop_stack.py` | 一键停栈完整性：孤儿清扫、自杀防护、只读探针不被误杀、端口兜底 |
| 21 | `runtime` | `check_bootstrap_health.py` | 一键后台启动的健康检查与代理隔离 |
| 22 | `runtime` | `check_napcat_launch_order.py` | NapCat 启动顺序：直接注入优先，注入失败不降级到 .bat（防 #39 黑框复发）；端口在≠登录态在（#50）：早退三问 get_login_info 才说在线、注入后等登录态确认 |
| 23 | `runtime` | `check_config_fail_loud.py` | 配置缺失 fail-loud：注入前拒绝、监听拒绝启动、--status 说出缺什么（#51） |
| 24 | `runtime` | `check_water_group_import.py` | water_group 桥接 shim：单实例、模块身份唯一 |
| 25 | `runtime` | `check_watchdog_token.py` | watchdog 探针现读 OneBot token（防 #17 复发） |
| 26 | `runtime` | `check_watchdog_halt.py` | watchdog halt 可恢复：冷却重试，不许永久停机（#45） |
| 27 | `runtime` | `check_watchdog_launch_identity.py` | 注入实例按启动记录认领，--stop 计数不再漏报（#46） |
| 28 | `runtime` | `check_watchdog_login_error.py` | Login Error 进 watchdog 时间线：napcat.log 新错误按 (ErrType, ErrCode) 去重 300s 映射、未映射码原文照录；unresponsive 分型；halt reason 带 last launch note（#50） |
| 29 | `runtime` | `check_collector_self_echo.py` | collector 不落盘自身回显（防 #28 复发） |
| 30 | `runtime` | `check_up_facade.py` | up.py 门面完整性：公开定义必须能从门面拿到且同一对象（#37） |
| 31 | `runtime` | `check_napcat_onebot_contract.py` | NapCat 风格 WebSocket 帧契约 |
| 32 | `runtime` | `check_napcat_qq_probe.py` | NapCat 探针契约：假消息 + loopback HTTP |
| 33 | `runtime` | `check_qq_account_diagnostics.py` | QQ 账号诊断：目录夹具、失败分类、隐私 |
| 34 | `mcp` | `check_issue_16_audit_fixes.py` | #16 审计缺陷回归：store 异常类型、grants 排序、MCP 响应解析、会话活性 |
| 35 | `mcp` | `check_mint_grant_scope.py` | 冷启动 grant 默认 scope 不许静默开 chat_video/chat_files（#201）：常量口径与 collector 实际所需对齐、空表回落不泄漏、mint() 真实 POST body 媒体授权为关且字段齐全 |
| 36 | `mcp` | `check_mcp.py` | MCP 协议层契约：真实 MCP HTTP + 隔离源夹具，从不发 QQ 消息 |
| 37 | `mcp` | `check_mcp_onebot.py` | OneBot 设置到工具可见性再到 HTTP notice 读取（隔离夹具） |
| 38 | `mcp` | `check_mcp_chat_output.py` | 轮询输出截断、游标与版本刷新语义 |
| 39 | `mcp` | `check_mcp_chat_turns.py` | 对话轮次生命周期与原生引用（19 项并发与防竞争检查） |
| 40 | `mcp` | `check_mcp_chat_dialogue_window.py` | mention_only 对话窗口跨 wait 调用与进程重启存活、本机回复重开窗口、跳批只推游标不置 delivered（#217） |
| 41 | `mcp` | `check_mcp_chat_prompts.py` | 提示词装配与人格卡：目录发现、缓存、体积上限与社交状态（#64） |
| 42 | `mcp` | `check_mcp_picker.py` | 平台过滤先于分页、会话搜索与参数校验（#64） |
| 43 | `mcp` | `check_mcp_reactions.py` | 表情回应 dry-run：真实 MCP HTTP + loopback OneBot |
| 44 | `mcp` | `check_mcp_language_learning.py` | 实时边聊边学契约（合成夹具，无模型调用） |
| 45 | `mcp` | `check_mcp_language_integration.py` | 语言学习跨层集成（真实 MCP wire + 合成 WS） |
| 46 | `mcp` | `check_mcp_multi_group_learning.py` | 一套凭证两群库的隔离与持久化 |
| 47 | `mcp` | `check_mcp_jargon_matching.py` | 群友黑话再发现：仅 peer、合成批次 |
| 48 | `mcp` | `check_mcp_jargon_pipeline.py` | 黑话流水线各阶段独立校验 |
| 49 | `mcp` | `check_mcp_expression_evidence.py` | 表达证据 vs 机器人政策回归 |
| 50 | `mcp` | `check_plugin_registry.py` | 插件注册表（#4 PA 与 PB） |
| 51 | `mcp` | `check_app_plugins.py` | app 侧插件协议：显式 scope/expression 声明、静态表、停用即降级、宿主无插件名硬编码（#4 PD/PD2b） |
| 52 | `storage` | `check_artifacts.py` | 工件导入、SHA256 共享解析、引用门槛、缓存回收与 OneBot/HTTP 边界（#64） |
| 53 | `storage` | `check_memory_engine_imports.py` | 水群模块单实例身份（动 shim 或 sync_* 时必跑） |
| 54 | `storage` | `check_group_memory.py` | 记忆引擎、MCP 读写工具与两层存储 |
| 55 | `storage` | `check_group_notes.py` | 群级结构化术语读写闭环（#31） |
| 56 | `storage` | `check_dossier_ingest.py` | dossier 写入动词：--ingest / --through / --apply / --dry-run（#6） |
| 57 | `storage` | `check_dossier_cadence` | 画像沉淀时机：连续 N 轮无人指名即触发，quiet_since 只写首轮、WAKE 与 --mark 清零、--no-digest 与 --probe 不计数、NO 行字段逐项正确（#162）；积压不再冒充爆发、同一批 DUE 只叫一次、--digest --reap 兜底推进停滞游标（#183）；下限的读法与机器速度无关、夹具下限与用例 13 共用常量且调松即红（#196/#215）。#247：单文件顶到 1000 行红线后按 #169 PF-2 先例拆成 `scripts/check_dossier_cadence/` 包（_common/quiet_rounds/clock_fields/floor_reading/backlog_cursor），入口名不变、对 gates.json 与 CI 透明。 |
| 58 | `storage` | `check_pollution_scrub.py` | 跨群主档污染审计、清理与还原（#34） |
| 59 | `storage` | `check_qr_login.py` | 每用户扫码登录：passport 双流、DPAPI、限流、隐私（#6） |
| 60 | `storage` | `check_upgrade.py` | 升级用真实 SQLite/WAL 夹具 |
| 61 | `storage` | `check_hot_reload.py` | 全栈热重载：闭包覆盖、target 映射、debounce、quarantine、不 spawn（#58） |
| 62 | `storage` | `check_sync_groups_participation.py` | sync_groups 不得用派生缺省改写会话参与度：未配 participation 的群三轮同步后库值不动且无改写日志、显式配置按配置对齐且打「配置同步」而非「热重载」、非法值告警不写库、人格变更仍报热重载、status.json 仍派生 trigger_mode（#234） |
| 63 | `reply` | `check_anti_habituation.py` | 反语癖固化与多样性提示词注入 |
| 64 | `reply` | `check_plan_recovery.py` | stale 拒绝后的重规划（plan_id 为 None 也不崩） |
| 65 | `reply` | `check_wake_ledger.py` | 已回判定：一条回复不得把后续 @ 洗成已回 |
| 66 | `reply` | `check_forward_wake.py` | 合并转发里的 @ 与关键词参与唤醒（#9） |
| 67 | `reply` | `check_followup.py` | 追问窗口：回复之后还能听见群里说什么 |
| 68 | `reply` | `check_repeater.py` | 复读/接龙：三人不同 QQ 才跟、接龙四种格式跟下一号、指令/敏感词/本机名字/外链/超长/带 @ 带图一律不跟、一波只尝试一次且冷却跨重启（#155）；「本机名字」闸门读配置而非夹具字面值（换名照样挡、空表照跟两条对照），夹具里不留像真实昵称的常量（#254） |
| 69 | `reply` | `check_pending_reply.py` | 待回时效：超时唤醒不再算待回 |
| 70 | `reply` | `check_reply_context.py` | reply.py --context 回之前看得到群聊上下文 |
| 71 | `reply` | `check_event_ref.py` | 事件 id 只有一个权威解析入口（#216）：字段名兼容（onebot_message_id/message_id）、列兜底（chat_sessions.conversation_id / chat_inbox.message_id）、来源顺序 wg_events 优先；两种「查不到」分开说；--context 的行能被 reply.py 解析到本群且自带 src=chat_inbox；批量读点（followup.since / wg_context.recent_chatter / wg_gap._local_state）与单条解析同一口径——payload 缺键时回查会话行、非对象 payload 不拖垮整批（#219） |
| 72 | `reply` | `check_bubble_cap.py` | 单次回复气泡上限只有一个数、超限有完整清单、计划通道提前拒绝（#131） |
| 73 | `reply` | `check_reply_quote.py` | OneBot mention 与引用预检 |
| 74 | `reply` | `check_reply_send_failure.py` | 发送失败要说真实 retcode 与禁言解禁时刻，不许再报成「@ 的锅」（#132）；禁言到点必须复查，过期预测不许继续当此刻事实（#145） |
| 75 | `reply` | `check_unmute_probe.py` | 禁言解禁要叫醒值守的监听：解禁瞬时 WAKE、仍禁言/探测失败/全员禁言静默且不冒充解禁、节流两档、只读入口口径不变、listen 转述完整（#192）；禁言期间不扫追问窗口、窗口原封不动、解禁与无账本两组对照照常 [追问] 唤醒（#197） |
| 76 | `reply` | `check_quote_visibility.py` | 引用原文必须到达 Agent，缺了也要说清原因（#94） |
| 77 | `reply` | `check_image_visibility.py` | 唤醒行图片标记可执行，不许出现裸 [图片]（#35） |
| 78 | `reply` | `check_reply_image.py` | 回复能发本地图片：--image 出 base64 图段、四种格式都真能发、整张解码才放行、半截图/超大像素/超帧数零请求、图也算气泡额度（超了拒、额度内照发）、不回退计划通道、发图也记账（#128） |
| 79 | `reply` | `check_alias_visibility.py` | 正文外号必须能指到人，认不出也不许编（#30） |
| 80 | `reply` | `check_wake_dossier.py` | 沉淀的群友画像必须到达唤醒行 / --list / --context（不是只挂在 MCP 的 chat_guidance 里）：命中才附、不串到别人身上、每人一行有硬顶且不挤正文额度、群级术语一次一块不跨群、引擎坏了照常唤醒（#163） |
| 81 | `reply` | `check_at_segment_name.py` | at 段带显示名：入库/转发体两侧透传 name，@<数字> 与纯数字别名不得在正文命中里自指（#52） |
| 82 | `reply` | `check_reply_timing.py` | 回复时序、密钥排除、轮换与失败透明 |
| 83 | `reply` | `check_now_clock.py` | 值守工具链必须带当前时刻（#95） |
| 84 | `reply` | `check_miyoushe.py` | 米游社取数 handler：DS 签名、URL 参数、缓存、错误码 |
| 85 | `reply` | `check_sixty.py` | 60s 资讯层契约：信封判 code、query 参数、白名单、降级（#53） |
| 86 | `reply` | `check_busboss.py` | 车盈网班次契约：节点 value 嵌套形状、日期结构判别、凭证轮换写回、缓存与零请求（#103） |
| 87 | `reply` | `check_multigroup_isolation.py` | 一个会话只看一个群：watch/--list 读侧按 conversation_id 过滤（含「本群那条必须在」与「未钉死才不过滤」两组对照）、跨群回复被拒且不记账、批次轮换重定向只挑本群、身份四档解析与多群未钉死拒绝启动（#164） |
| 88 | `reply` | `check_trigger_mode.py` | 按群选择 natural/mention_only 触发模式：collector 请求值按 trigger_mode 取（不再硬编码 True）、natural 群普通消息 wake_reason 放行、@/关键词/is_self/指令早退不破坏、两群不同配置按群生效（#188） |

共 88 个 CI 门禁，分 5 个分片（`static` 18、`runtime` 15、`mcp` 18、`storage` 11、`reply` 26）；`manual` 15、`stale` 0、`dead` 0、`support` 1 见 `scripts/gates.json` 的对应档位（理由写在那里）。
<!-- gates:ci:end -->

黑框闪（issue #39）分两步查：静态门禁管“别再写错”；真闪了要抓现行，挂
`uv run python scripts/check_console_window.py --minutes 30`（命中直接打印 pid + 窗口类），
`--self-test` 是 4 组 A/B 对照（会故意闪两次黑框，属正常）：`visible` / `detached-cmd`
两组必须命中，用来证明探针没瞎、并且这台机器还能复现 DETACHED 的 bug；`hidden` /
`nowindow-grandchild` 两组必须 0 命中。只跑后两组等于没做对照 —— 一个 0 命中什么都证明不了。

### 5.2 不在 CI 跑的门禁（档位说明）

`ci` 之外的四档理由逐条写在 [`scripts/gates.json`](scripts/gates.json)：

- `manual`：需要真实桌面会话（`check_console_window.py`）、真实 MCP/OneBot 重集成（`check_mcp_chat.py` 等）或外部模型（`check_mcp_chat_materials_clients.py`），本机按需跑；
- `stale`：本机当前为红、原因已定位并带跟进 issue 号，修好前不进 CI（不许用「人工跑」掩盖红门禁）；
- `dead`：被测能力已删除，待删队列，删完即出列；
- `support`：被门禁 import 的共享模块（如 `check_fixture_support.py`），本身不是门禁。

`scripts/` 已按 issue #22 清掉桌面壳遗留的死脚本（模块级 import 无法解析者，含传递闭包共 62 个），issue #59 又删掉 9 个被测能力已删的脚本。新增脚本必须是活门禁或共享基建：进清单某一档，否则按死代码处理。

**红门禁归因纪律**：失败时先用 git worktree pristine HEAD 对照——若同族失败则为既有环境问题（沙箱 WAL 竞态等，见审计报告第 3 节），记录在案，不要盲目"修复"。

### 5.3 发布门禁（隐私 / 个人信息）

`scripts/check_publication.py` 是唯一的发布门禁，**仓库里不写死任何真实号码或花名**——名单每次运行从私有库（`~/.water-group`，`WATER_GROUP_HOME` 可改）实时派生：`config.json`（账号 / 群号 / 成员映射）、`personas/*.md`、`data/*memories*.sqlite3`（群友档案，随记忆增长自动覆盖新群友）、`rules/water_group.md` 里的数字、`publication_denylist.txt`（handle / 域名）。通用词例外放私有 `publication_allow.txt`。

| 入口 | 覆盖 |
|---|---|
| `check_publication.py`（CI 第一道） | 全部已跟踪文件：凭证、个人路径、私有库 token、图片白名单、未知二进制 fail-closed |
| `--history` | commit 作者/提交者邮箱（只放行 GitHub noreply）与 commit message |
| `--text <文件>` / `--text -` | issue / PR / commit 文案草稿，发布前跑 |
| `.githooks/commit-msg` | 每次 commit 自动扫 message |
| `.githooks/pre-push` | 每次 push 自动跑 `--require-private --history`，再对每个被推送的 commit 跑 `scripts/check_pr_parity.py`（干净 worktree + 空数据根复跑门禁，issue #61）；`SKIP_PARITY=1` 可跳过第二步并在 stderr 留痕 |

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
