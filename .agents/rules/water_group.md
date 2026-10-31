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
.venv/Scripts/python.exe water-group/daemon/listen.py --timeout 90
```

只活一轮：退出码 0 = 有活（`WAKE` / `DOSSIER_DUE`），**退出末行的 `RESTART:` 就是重挂命令**（照抄）。
忘了挂不是「安静」而是「失聪」——消息照样进 `~/.water-group/state/wg_events.jsonl`，但没人叫醒你。
`reply.py` 发送成功后默认续挂监听 90s（`--no-wait` 关闭），正常回消息不必手动重挂。
90s = 宿主工具调用超时 120s − 发送余量 30s（issue #79 / #83）：**你自己把工具超时调小过，
就要同步调小**——被截断时父进程和子监听同组同死，且不打印任何东西。`listen.py` 每轮进入
`watch.py` 前会打「等最多 90s（第 1 轮）…」，被砍时那是唯一痕迹；`RESTART:` 行不会推荐
超过预算的取值。

`up.py --status` 的「监听」行会报出每个监听**实际**用的 `--timeout`（从命令行读）：带 ⚠ 的
那个超预算了，下一轮等待会被截断且不留输出，按上面的命令重挂即可（issue #86）。

## 2. 回复

```bash
.venv/Scripts/python.exe water-group/daemon/reply.py --list        # 谁 @ 了我
.venv/Scripts/python.exe water-group/daemon/reply.py --context 30  # 最近 30 条（含闲聊）
.venv/Scripts/python.exe water-group/daemon/reply.py <id> "<正文>"  # 自动带原生引用 + 记账
```

- `--no-quote` 发纯文本；`--at <花名>` 真 @（可重复）；`--who <花名>` 只解析不发；`--force` 回过期唤醒。
- **id 有两套（issue #203）**：裸数字 = 事件流 `wg_events` 的 id；带 `q` 的（`q<n>` / `qid:<n>`）= 会话库 `chat_inbox` 的 rowid，追问行和 `--context` 打的就是它。撞号时照抄带 `q` 的写法，别剥前缀；裸数字若事件流没命中而会话库唯一命中会回库兜底并打一行源说明。
- **行首记号原样抄（issue #204）**：行首那整段（`id=7` / `id=q7` / `qid=7` / `7` / `q7`）直接当 `reply.py` 首参都认，不用手工剥前缀；记号写错（`id=-`、`Q7`）会显式拒绝，不会猜着发。
- `--image <路径>` 发本地图片（可重复，每张一条气泡排在文字之后；只发图时正文可省）。
  按内容校验：Pillow 把整张图逐帧解开才算数，格式限 PNG/JPEG/GIF/WebP，单张 ≤6 MiB、
  合计 ≤80 兆像素、≤300 帧，一次最多 9 张；有一张不合格就**一张都不发**；
  发图强制走 OneBot。**画图没有这个能力**，别对群友许诺。
- 正文里**换行会拆成多条气泡**（默认最多 20 条，首条挂引用；`--max-bubbles` 或 config 的 `reply.max_bubbles` 可调小，超出的行会打印丢弃清单，issue #131）：想连发几句就分行写，别用「 / 」串成一行。shell 双引号里写不出真换行，字面量 `\n`（反斜杠加 n）也一样拆；`\\n` 转义成字面 \n 留在正文里。**图也算气泡**：正文条数加图张数超过 20 就在发之前拒（间隔 1.2s 会吃掉发送预算）。
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

触发有三条（`config.json` 的 `dossier` 段），按 `quiet` → `burst` → `idle` 判：
连续 `quiet_rounds`（默认 3）轮 `listen.py` 没人指名、这几轮加起来超过
`quiet_minutes`（默认 4，须小于 3 × 轮长，否则轮次判据成死码）且至少
`quiet_min_messages`（默认 1）条素材；或最近 `burst_window_minutes`（默认 10）内
涌入 `burst_messages`（默认 80）条；或闲置 `idle_minutes`（默认 20）且攒够
`min_messages`（默认 25）。轮次由 `listen.py` 每轮静默后 `--tick` 自己记，`WAKE`
与 `--mark` 清零；`--no-digest` 与 `--probe` 不计。`NO due-hold` 表示上一批 DUE
还没 `--mark`、`listen.py` 已用 `--tick due` 记过，不是异常；超过
`reap_after_minutes`（默认 15）仍无人处理时 `--digest --reap` 会兜底推进游标。

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
水群会话只做水群，**其余一切活立 issue 后停手**（issue #160）。

**适用主语钉死为「主水群 Agent」**：本节只约束**靠 `listen.py`/`reply.py` 监听群的那个会话**
（它必须随时能接话，不能被长任务堵住）。**被用户直接委派做开发/修复/测试/文档的 agent
（如 `agent` 命令直接交代的分节实施）不跑水群监听循环，没有这个阻断诉求，应当就地连续
干活，不套本节的停手约束。**

### 6.1 白名单（只有这些可以亲手做）

- `listen.py --timeout`、`reply.py --list` / `--context` / `--who` / `<id> "<正文>"`（发送后默认续挂，`--wait` / `--no-wait` 可调）
- `up.py --status`（只读探活）
- 追问窗口内接话、按花名 @ 人、连发 @ 的 `--also` 记账
- `tail` 一行日志判断「要不要立 issue」：只读、秒回，**不得扩展成多步排查**
- **提 issue**：不属于「改仓库」，不受本节限制，最高优先级见 AGENTS.md §0（issue #115）

### 6.2 其余一律立 issue 停手

改仓库、跑测试与门禁、写文档、审 PR、查取数失败、整栈诊断，全部**立 issue 后停手**：

- **立完立刻回监听**，不原地开工、不原地等结果；
- **修复由用户启动的独立会话做**（issue → plan → worktree → PR），不由主 Agent 代劳；
- **不启停水群守护**：整栈重启要经 explorer 冷启动（§0），连提 issue 都可省（纯进程重启是 #115 的例外）；
- 私有上下文（群号、花名、QQ、人设卡路径）**不落仓库文件、PR 或 issue**——这条与派不派活无关，是保密要求。

独立会话交接时该给的信息清单见 `.agents/skills/water-group/SKILL.md` §五。

### 6.3 为什么不再派 subagent（issue #160）

不是偏好问题，是成本结构问题——**派 subagent 等于「派活 + 再独立验一遍活」**：

- `doc/DRY_SRP_AUDIT_2026-10-20.md:123-130`：两个只读审计子代理返回**整段虚构**的发现（不存在的目录、`theme.css`、全仓零命中的常量），一组「修复」实施后才被人工复核否决、整体回退；
- 同文件 `:134-138`：工人**两次**报告「完成」并附逼真的 `git status` / harness 输出（含一次伪造的通过记录），磁盘上零改动；
- 子代理与主 Agent 上下文隔离、**看不到水群会话**，群号 / 花名 / 人设卡 / 证据位置必须逐项手写进 prompt；漏传任何一条，它就在缺信息的条件下自己补——按上两条，补出来的可能是虚构；
- 并行派出的复核 agent 会**静默拖死节奏**（PR #150 / #152 各一次，20+ 分钟无产物，最终由主 Agent 自己跑完）。

所以撤掉的是「必须排执行」，保留的是「必须留证据」：**发现 → 立 issue → 回监听**，产出到此为止。

**多群并发 = 用户开 N 个会话，一会话一群**，各会话互不可见。多群靠会话数量，不靠 Agent 派活，所以「不派活」不削弱多群能力。

### 6.4 反模式（明确禁止）

- 主 Agent 亲自跑构建 / 全量测试 / 长排查，让群里「等我这波忙完」；
- 立了 issue 又自己动手修——产出边界越界，等于既当发现者又当执行者；
- 派 subagent 干水群以外的活（本 issue 撤掉的做法）。

