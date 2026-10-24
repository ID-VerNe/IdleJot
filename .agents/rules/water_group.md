# QQ 水群规则（通用骨架）

用户说「在群 `<群号>` 开始挂机水群」「继续水群」「接管水群」时按本文做。
**私有 SOP（群号、群友 QQ/花名、地域、人设卡、数据源速查、排查实例）不在仓库里**，
在 `~/.water-group/rules/water_group.md`——动手前先读它。

## 0. 起栈 / 停栈

```bash
# 起（必须由 explorer 代启动，绕开代理沙箱的 tsbx.dll 注入，否则 QQ 20 秒必崩）
.venv/Scripts/python.exe -c "import subprocess,pathlib; subprocess.Popen(['explorer.exe', str(pathlib.Path('water-group/start-bot.pyw').resolve())])"

# 停（三个入口同一实现：stop.ps1 / stop-bot.pyw / up.py --stop）
.venv/Scripts/python.exe water-group/daemon/up.py --stop

# 只读复查
.venv/Scripts/python.exe water-group/daemon/up.py --status
```

**「现在」只认工具给的锚点**（issue #95）：`--status` 首行、`--list` / `--context`
表头、`watch.py` 唤醒块首行、`followup.py --show` 首行都会打
`现在 2026-10-23 20:52:07 (UTC+8)`。消息行上的 `20:50:44` 是**消息时间**，
`19s 前` 是**相对量**，都不能当基准——消息可能是一小时前的，相对量在你思考几十秒后
就失真了。涉及「今晚 / 明天 / 还有多久 / 开播了吗」的判断，先看这行再开口。

`start-bot.pyw` 幂等且**永不返回**（设计如此），要后台跑，日志在 `~/.water-group/logs/launcher.log`。
停完端口应全空：`netstat -ano | grep LISTENING | grep -E ":43000|:43001|:47860|:48777"`。

## 1. 监听（唯一唤醒源）

```bash
.venv/Scripts/python.exe water-group/daemon/listen.py --timeout 300
```

只活一轮：退出码 0 = 有活（`WAKE` / `DOSSIER_DUE`），**回完必须立刻重挂**。
忘了挂不是「安静」而是「失聪」——消息照样进 `~/.water-group/state/wg_events.jsonl`，但没人叫醒你。

## 2. 回复

```bash
.venv/Scripts/python.exe water-group/daemon/reply.py --list        # 谁 @ 了我
.venv/Scripts/python.exe water-group/daemon/reply.py --context 30  # 最近 30 条（含闲聊）
.venv/Scripts/python.exe water-group/daemon/reply.py <id> "<正文>"  # 自动带原生引用 + 记账
```

- `--no-quote` 发纯文本；`--at <花名>` 真 @（可重复）；`--who <花名>` 只解析不发；`--force` 回过期唤醒。
- 正文里**换行会拆成多条气泡**（最多 3 条，首条挂引用）：想连发几句就分行写，别用「 / 」串成一行。shell 双引号里写不出真换行，字面量 `\n`（反斜杠加 n）也一样拆；`\\n` 转义成字面 \n 留在正文里。
- 唤醒三态：`[已回]` / `[待回]` / `[过期]`（超 TTL 默认不回）。
- **时间判断先看 `现在 ...`**：`--list` / `--context` 表头和唤醒块首行都有；不要拿
  消息时间戳当现在，也不要把 `静默至 20:55:15` 当成当前时刻。
- **只看 `--list` 不够**，回之前一定先 `--context` 看前因后果。

## 3. 闲时沉淀群友画像

```bash
.venv/Scripts/python.exe water-group/daemon/dossier.py --check            # 该提炼了吗
.venv/Scripts/python.exe water-group/daemon/dossier.py --dump 80          # 取素材
.venv/Scripts/python.exe water-group/daemon/dossier.py --mark --through <末条id>
```

写档案只走 MCP `update_member_dossier` / `query_member_dossier` / `consolidate_chat_memory`，
**禁止直连 SQLite**。`traits` 必须传 dict（传数组会被静默丢空）。

## 4. 数据存哪

仓库只有代码与公开模板，运行时数据全在 `~/.water-group/`（`WATER_GROUP_HOME` 可改根）：

| 路径 | 内容 |
| --- | --- |
| `state/` | 唤醒事件、已回账本、游标、会话、追问窗口、锁与心跳 |
| `logs/` | supervisor / collector / napcat / watchdog / launcher / stopper |
| `data/` | `group_memories.sqlite3`（小本本）、`mcp-access.sqlite3`（消息与 MCP）、`chats.sqlite3` |
| `tmp/` | **只放机器产物**（Gradio TEMP、voice 抽取、测试夹具残留）。它是 `TEMP` 的重定向目标，语义就是「随时可整目录删」，**不得放脚本** |
| `bin/` | 需要保留、需要复用的脚本，必须在 `bin/INDEX.md` 登记一行 |
| `archive/` | 退役脚本与草稿，`archive/scratch/<YYYY-MM>/` 按月分桶；只增不删，不承诺可运行 |
| `drafts/` | issue / PR 草稿，**发完即删**（远端已留存）；`bin/draft.py post` 负责门禁 + 发布 + 删除 |
| `rules/` | **私有 Agent 规则**（本文的私有版） |
| `personas/` | **私有人员卡**（仓库 `water-group/personas/` 只留公开模板，同名私有优先） |

路径单一事实来源：`water-group/daemon/wg_paths.py`；`chatlocal/config.py` 与
`water-group/memory_engine.py` 按同规则复述一份，改规则时三处一起改
（`scripts/check_path_hygiene.py` 会拦漏改）。

**别在 `tmp/` 里写脚本**（issue #6）：`TEMP` / `TMP` 在 `chatlocal/config.py:22-23` 就被
改写到它，任何「磁盘紧张先清 temp」的动作都有权把它整目录删掉。脚本放 `bin/`（复用）
或 `archive/`（退役），一次性产物用完归档。`scripts/check_tmp_hygiene.py` 四条断言巡检：
`tmp/` 顶层有 `.py/.md/.ps1` 或 `bin/`、`archive/` 里有脚本没登记索引，都直接红。

## 5. 出问题先看三处

```bash
tail -20 ~/.water-group/logs/wg-supervisor.log
tail -20 ~/.water-group/logs/wg_collector.log
tail -20 ~/.water-group/logs/napcat-watchdog.log
```

`napcat.log` 是混编文件（UTF-8 + 936），用逐行嗅探的读取器，别直接 `cat`/`grep 中文`：

```bash
.venv/Scripts/python.exe water-group/daemon/wg_logs.py ~/.water-group/logs/napcat.log --tail 40
```

更多排查项（注入崩溃、WAL 竞态、@ 不变蓝、进程越攒越多）见私有规则文件的排查表。

## 6. 职责边界：主 Agent 只水群

**核心诉求：主 Agent 的水群流程永不被长任务阻断**（issue #83）——群里不发生「大片等待 → 事后追平」。
水群会话只做水群，其余一切活派后台 subagent。

**适用主语钉死为「主水群 Agent」**：本节的「其余一律后台派」只约束**靠 `listen.py`/`reply.py`
监听群的那个会话**（它必须随时能接话，不能被长任务堵住）。**被用户直接委派做开发/修复/测试/文档
的 agent（如 `agent` 命令直接交代的分节实施）不跑水群监听循环，没有这个阻断诉求，应当就地连续
干活，不套本节的派活约束。**

### 6.1 白名单（只有这些可以亲手做）

- `listen.py --timeout`、`reply.py --list` / `--context` / `--who` / `<id> "<正文>"` / `--wait`
- `up.py --status`（只读探活）
- 追问窗口内接话、按花名 @ 人、连发 @ 的 `--also` 记账
- `tail` 一行日志判断「要不要转交」：只读、秒回，**不得扩展成多步排查**

### 6.2 其余一律后台派

改仓库、跑测试与门禁、写文档、审 PR、查取数失败、整栈诊断，全部
`Agent(run_in_background: true)` 派出去：

- **派完立刻回监听**，不原地等结果；回报在水群轮次间隙查收，需要时再转述；
- **一个 subagent 只做一件事**，不把不相关的活串在一起；
- **subagent 不启停水群守护**：整栈重启要经 explorer 冷启动（§0），不在派活范围；
- 私有上下文（群号、花名、QQ、人设卡路径）**只写进派活 prompt**，不落仓库文件、PR 或 issue。

派活 prompt 的必传清单与模板见 `.agents/skills/water-group/SKILL.md` §五。

### 6.3 反模式（明确禁止）

- 主 Agent 亲自跑构建 / 全量测试 / 长排查，让群里「等我这波忙完」；
- 派了 subagent 却原地阻塞等它跑完再回监听——等于没派；
- 一个 subagent 里串多个不相关的活。
