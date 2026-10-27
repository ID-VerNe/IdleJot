# 多群水群接入方案（v2，四个并行勘察后的修订）

目标：一台机器挂 N 个群，每群一套「采集器 + 监听 + Agent」，Agent 之间互不可见。
后端、NapCat、watchdog、db_keeper 仍然各一份。

v1 的三处判断在勘察后被推翻，已在文中标注 **【v1 修正】**。

```
                    ┌──────────────────────────────┐
                    │  NapCat（一个 QQ 账号）        │
                    │  单条 OneBot WS，全群共享      │
                    └──────────────┬───────────────┘
                                   │ OneBot 43000
                    ┌──────────────▼───────────────┐
                    │  server_wg.py（单进程多会话）  │
                    │  HTTP 47860 / MCP 48777       │
                    └───┬───────┬───────┬──────────┘
                        │       │       │  每群一个常驻 wait（全局上限 8）
             ┌──────────▼─┐ ┌───▼──────┐ ┌▼───────────┐
             │collector A │ │collector B│ │collector C │
             │ .tmp/g<A>/ │ │ .tmp/g<B>/│ │ .tmp/g<C>/  │
             └──────┬─────┘ └─────┬─────┘ └──────┬──────┘
                    │             │              │
             ┌──────▼─────┐ ┌─────▼─────┐ ┌──────▼─────┐
             │ Agent A    │ │ Agent B   │ │ Agent C    │
             └────────────┘ └───────────┘ └────────────┘
```

---

## 1. 现状：13 处单群假设（全部带证据）

### 1.1 身份与进程

| # | 位置 | 事实 | 多群后果 |
|---|---|---|---|
| 1 | `daemon/session.py` | ~~群号写死在默认值~~ 已修：默认目标从 `config.json` 里第一个 `enabled` 的群推导，环境变量仍可覆盖 | 新状态目录不设 env 会落到 config 里第一个启用的群 |
| 2 | `daemon/session.py` | ~~`PERSONA` 默认写死~~ 已修：默认取该群的 `persona`（退回 `default_persona`）；多群仍需按群设 env | `config.groups.<gid>.persona` 现在参与默认推导 |
| 3 | `daemon/session.py:43-68` | `clear_group_sessions()` 只按单个 `GROUP` 过滤（`:58`） | 只能清理一个群的孤儿会话 |
| 4 | `daemon/supervisor.py:43-48` | `CHILDREN` 固定 4 键，只有一个 `"collector"` | 无法登记第二个采集器 |
| 5 | `daemon/supervisor.py:27,120` | 心跳固定 `STATE_DIR/wg_heartbeat`，判活条件 `name == "collector"` | 改成每群目录后文件不存在 → `heartbeat_age()` 返回 `10**9`（`:69-73`）→ 每 15 秒 kill/respawn |
| 6 | `daemon/supervisor.py:86` | 子进程日志 `logs/{name}.log` → `collector.log` | N 个采集器混写一个文件 |
| 7 | `daemon/up.py:30,397-443` | `STATE` 单值，`report()` 只读一个目录 | 报告只覆盖一个群 |
| 8 | `daemon/up.py:163-171` | `listener_pids()` 只匹配 `daemon[\\/]listen\.py` | 分不清哪个监听属于哪个群 |
| 9 | `daemon/up.py:174-198` | `--stop` 按脚本名杀 + `bootstrap.kill_stale_instances()` | **停一个群会停掉整台机器**（含所有注入的 QQ 实例） |
| 10 | `daemon/collector.py:104` | `mention_only: True` 硬编码，全文不读 config | `config.groups.<gid>.trigger_mode = natural` 永远不生效 |

### 1.2 数据库查询（会串群）

| # | 位置 | SQL | 多群后果 |
|---|---|---|---|
| 11 | `daemon/dossier.py:72-74` | `SELECT id, conversation_id FROM chat_sessions WHERE active=1 ORDER BY rowid DESC LIMIT 1` | 命中**最后创建**的活跃会话，不一定是本群 |
| 12 | `daemon/followup.py:88-90` | `SELECT id FROM chat_sessions WHERE active=1 ORDER BY rowid DESC LIMIT 1` | 追问窗口绑到别的群 |
| 13 | `daemon/reply.py:465-467` | 同一条 SQL | `--context` 读别的群 |

三处注释都写着「读库不读 `wg_session.txt`，后者会滞后于会话重建」——理由成立，所以修法是
**按 `conversation_id` 过滤**，不是改回读文件。

### 1.3 已确认的连带故障（勘察新发现）

| 位置 | 事实 | 后果 |
|---|---|---|
| `daemon/reply.py:149-160` + `:715-721` | 计划通道收到 `target_not_in_batch` 时把目标改成 `allowed[-1]`（该批次最新一条），然后 `send(sid,...)` + `record(answered)` | **P0：回复可能发到 A 群，B 群的目标被记成「已回」**。多群下必须加「目标事件属于本群」的前置校验 |
| `daemon/followup.py:112` | `open()` 默认 `session or active_session_id()`，而 `reply.py:587` 调 `open(event_id)` 不传 session | 窗口绑到最新会话；`wg_followup.json` 单文件还会被另一个群覆盖 |
| `daemon/followup.py:129` | `stale()` 用 `active_session_id()` 比较 | 另一个群新建会话后，本群窗口被误判为「换会话」而关闭 |
| `daemon/dossier.py:33,238` | `wg_dossier.json` 只存一个全局 `last_event_id` | 换群后 mark 高于新群 id → 新群永远不沉淀；低于则跳过积压 |
| `daemon/reply.py:482` | `sid = active_session_id() or session_id()` | 多活跃时前者永不为空，`wg_session.txt` 被完全忽略 |
| `daemon/watch.py:34` | `wg_watch.txt` 单游标 | 共享状态目录时，B 群的高 id 会把游标推过 A 群未读的低 id（按群目录后自愈） |
| `daemon/up.py:368-394` | `replied_after()` 的 pre-ledger 兜底在全局事件流里找「之后有没有自己说话」 | 别群的自发言会把旧事件误判为已回 |
| `daemon/collector.py:73-82` | `bootstrap_session()` 循环里不写心跳 | 建会话持续失败 → 心跳停在启动时刻 → 300 秒后被 supervisor 杀掉重启，形成重启循环 |
| `daemon/collector.py:96-98` | `wg_pause` 存在时不写心跳，而**全仓没有任何写入方** | 潜在路径：一旦有人创建该文件且超过 300 秒，采集器会被判死 |
| `daemon/mint_grant.py:49-51` | `last_scope()` 用 `ORDER BY id DESC`，而 `grants.id` 是 `uuid4().hex` | **不是时间序**。实测 5 份同名 grant 里字典序最大的是第 3 新的，新 grant 可能继承旧权限 |
| `water-group/start-bot.pyw:54` | 幂等只看 43000 + 47860，不看 48777，也不看 collector | MCP 挂了或某个群 collector 死了，仍报「已在运行」 |

---

## 2. 目录布局

```
.tmp/
  g<group-id>/
    wg_conversation.txt        # 新增：<qq>:group:<group-id>   ← 群身份唯一权威
    wg_session.txt  wg_events.jsonl  wg_ack.txt  wg_state.json
    wg_watch.txt  wg_replied.jsonl  wg_followup.json  wg_dossier.json
    wg_heartbeat  mcp_token.txt  wg_collector.log
  g<group-id-2>/
    ...
```

`wg_conversation.txt` 是唯一新增的状态文件。理由：每次工具调用都是新 shell，`export`
不跨调用，进程必须能从磁盘知道自己是哪个群。

**兼容旧布局**：根目录有 `wg_session.txt`、且没有 `g*/` 时，整个根目录当「一个群」。
这条回退必须保留，否则已经挂着的机器升级即停摆。

---

## 3. 改动清单

### A. 群身份

| 文件 | 改动 |
|---|---|
| `daemon/state_dirs.py`（新增） | 采用 PR #1 那份，但改语义：`(group_id, state_dir)`；单群布局从 `wg_conversation.txt` 取群号，**不再返回 `default` 标签**；另提供 `resolve(group=None, state=None)` 给 CLI |
| `daemon/session.py` | `GROUP` 三级解析：env → `<state>/wg_conversation.txt` → 报错退出；删掉硬编码默认群号。`PERSONA` 按群读 `config.groups.<gid>.persona`，回退 `default_persona` |
| `daemon/collector.py` | `mention_only` 按群读 `config.groups.<gid>.trigger_mode`；`bootstrap_session()` 循环里补 `beat()`；`wg_pause` 期间也写心跳（否则 300 秒必被杀） |

### B. 进程模型

| 文件 | 改动 |
|---|---|
| `daemon/supervisor.py` | `CHILDREN` 拆 `SHARED`（db_keeper / app / watchdog）与 `PER_GROUP`（collector）；启动前为每个 `enabled != False` 的群 `mkdir` + 写 `wg_conversation.txt`；子进程 env 带 `TULPA_WG_STATE=<群目录>`，**argv 也带 `--state`**（否则 PID 匹配分不出群）；日志 `collector-<gid>.log`；心跳按群查 `g<gid>/wg_heartbeat` |
| `daemon/up.py` | `STATE` 变根；`report()` 循环每群打印会话/游标/心跳/监听/追问；`--stop --group <gid>` 只停该群采集器，`--stop` 全停保持现状；`--group` 只报/只起该群 |
| `water-group/start-bot.pyw` | 幂等加查 48777；可选 `--group` 透传 |

### C. 命令行（Agent 侧入口）

`reply.py` **已经有 `--group`**（`reply.py:636,670`，只用于 `--who` 和发送兜底）
**【v1 修正】**。其余脚本没有。统一约定：

```
watch.py   --group <gid> | --state <dir>
listen.py  --group <gid> | --state <dir>     # 透传 env 给 watch/dossier 子进程
reply.py   --group <gid> | --state <dir>     # 现有 --group 语义扩展到「同时决定状态目录」
up.py      --group <gid>                     # 报告/启动/停止限定到该群
```

`--group` 解析成 `<根>/g<gid>`；`--state` 兜底自定义布局。`reply.py --wait` 拉起
`listen.py` 时必须把 `--group`/`--state` 一起传下去（`reply.py:613-618` 现在只继承环境）。

### D. 会话查询与安全护栏

| 位置 | 改动 |
|---|---|
| `dossier.py:72`、`followup.py:88`、`reply.py:465` | 统一走 `state_dirs.session_for_group(conn, conversation_id)`：`WHERE active=1 AND conversation_id=? ORDER BY rowid DESC LIMIT 1` |
| `reply.py:482` | `sid = session_id() or active_session_id()`（顺序反过来） |
| `reply.py:587` | `followup.open(event_id, session=sid)`，窗口显式绑群 |
| `reply.py:715` 之前 | **新增护栏**：目标事件查不到 `conversation_id` 或与当前群不符 → 直接报错退出，禁止进入 `plan()`。堵住 P0 的 `allowed[-1]` 重定向 |
| `dossier.py:238` | mark 按群隔离（状态目录已分群，无需改格式；但要确认 `--check` 只读本群） |

---

## 4. 并发与授权边界（决定 N 的上限）

| 约束 | 数值 | 证据 | 含义 |
|---|---|---|---|
| 并发等待信号量 `chat_slots` | **8**，非阻塞 | `chatlocal/mcp_tools.py:92,363-365` | 第 9 个 wait 立刻 `chat_busy`，不排队。**这才是真正的天花板**，`mcp_chat.py:697` 是第二道兜底 |
| 活跃会话总数 | **8** | `chatlocal/mcp_chat.py:595-596` | 第 9 个群 `start_chat_session` 直接拒绝 |
| 同群只能一个活跃会话 | 1 | `chatlocal/mcp_chat.py:161` `chat_one_group` | 两个采集器守同一群，第二个只能接续，然后抢同一个 wait |
| 每 grant 限流 | 120 次/60s（普通桶、媒体桶各一份）、`read_chat_image` 60/小时 | `chatlocal/mcp_access.py:278-291` | 粒度是 **grant**，不是 session |
| JSON-RPC 去重键 | `(grant.id, id)` | `chatlocal/mcp_server.py:142,154-162` | 共享 token 时两个进程的 `id=1001` 会撞 → 409。`mcp_client._next_id` 每进程从 1000 起（`mcp_client.py:24-32`） |
| 正文发送全局串行 | 1 把 `RLock` | `chatlocal/mcp_actions.py:13,106-154` | 跨群也排队，吞吐 ≈ 15–60 条/秒（本机回环），与群数无关 |
| 事件接收 | 单 `EventReceiver`、单 WS、`max_queue=16` | `chatlocal/mcp_chat.py:195`、`onebot_events.py:109-111` | 断线会让**所有**活跃会话 `gap_count+1` 并唤醒所有 wait |
| 后端进程 | 单进程、无 workers、端口独占 | `server_wg.py:69-79` | 上述内存态上限无法通过再起一个后端扩容 |

**结论**：N ≤ 8；每群必须**独立 grant**（独立 `mcp_token.txt`），否则同时踩限流和 409 两个坑。
`mint_grant.NAME` 改成 `water-group-<gid>`，并把 `last_scope()` 的 `ORDER BY id DESC`
改成 `ORDER BY created_at DESC`（uuid 字典序不是时间序）。

---

## 5. 落地顺序

**第 1 步：身份与查询（不动进程模型，单群行为不变）**
`state_dirs.py` + `wg_conversation.txt`；`session.py` 去硬编码；三处会话查询按群过滤；
`reply.py` 的 P0 护栏与 `followup.open(session=...)`；`collector.py` 补心跳。
这一步做完，旧布局零变化，可以手工建 `.tmp/g<群号>/` 单群试跑。

**第 2 步：CLI 绑定**
`watch/listen/reply/up` 的 `--group`/`--state`，`reply.py --wait` 透传。

**第 3 步：进程模型**
`supervisor` 拆 SHARED/PER_GROUP、按群日志与心跳；`up.py` 多群报告与按群停止；
`start-bot.pyw` 幂等加查 48777。

**第 4 步：文档**
`AGENTS.md` §0、`.agents/rules/water_group.md`、`.agents/skills/water-group/SKILL.md`、
`daemon/README.md` 全部补「一群一状态目录、一群一 Agent、`--group` 绑定」；
`bootstrap.py:537` 的 `ready_groups[0]` 改成列出全部群。

---

## 6. 验证清单

- `up.py --status` 列出所有群，每群独立会话/游标/心跳/监听
- 每个 `g<gid>/wg_heartbeat` 300 秒内更新
- A 群 @ 机器人：只有 A 的 collector 落盘、只有 A 的 Agent 被唤醒，B 的 `wg_events.jsonl` 不变
- A 群 `reply.py --group A`：引用挂在 A 群那条消息上；拿 B 群的 id 用 A 群回复 → 必须报错退出（P0 护栏）
- 杀掉 A 的 collector：supervisor 只重启 A，B 不受影响
- `up.py --stop --group A`：只停 A，B 继续
- 8 个群同时挂：第 9 个必须明确报 `chat_busy` / `最多同时开启8个群聊`，不能静默卡住

---

## 7. 文档债（勘察发现，与多群无关也该修）

| 位置 | 问题 |
|---|---|
| `.agents/skills/water-group/SKILL.md:3,8` | 声称「支持单群/多群并发」；`invoke_subagent` 调用点已改为「多群 = 用户开 N 个会话，一会话一群」（#160），描述行仍在 |
| `.agents/skills/water-group/SKILL.md:26` | 写「默认 3 轮或 60 秒」，实际 `followup.py:38` 是 10 条 / 180 秒 |
| `.agents/skills/water-group/SKILL.md:68` | 写读 `.env` 的 `REPLY_ONEBOT_URL`，实际读 `config.json` 的 `onebot_http_url`（`reply.py:197-206`） |
| `.agents/skills/water-group/SKILL.md:73` | 说 `sync_groups.py` 已删；它还在，且被 `bootstrap.py:448-451` 调用 |
| `.agents/skills/water-group/SKILL.md:28,264` | 说「继续轮询 `wg_events.jsonl`」，与 `watch.py` 的阻塞式设计相反 |
| `.agents/rules/water_group.md:366` | `.tmp/db_keeper.py` 路径不存在，实际是 `water-group/daemon/db_keeper.py` |
| `.agents/rules/water_group.md:105`、`SKILL.md:127-131` | 说 `watch.py` 退出码 1 = 事件流读不到；实际只返回 0 和 2（`watch.py:85,108,120`），`up.events()` 吞掉 `OSError` |
| `water-group/README.md:16-18` | `stickers/`、`sync_personas.py` 都不存在 |
| `AGENTS.md:224` | 说 `start-bot.pyw`「会一直不返回」；实际 `Popen` 后立即 `return 0`（`:63-68`），常驻的是 `up.py` |

---

## 8. 与 PR #1 的关系

- `state_dirs.py`：留下，改 label 语义后就是第 1 步的基础。
- `watchall.py`：**不需要**。那是「一个 Agent 管所有群」，与一群一 Agent 相反；每群自己的
  `listen.py` 已经覆盖等待、追问、沉淀。
- `fudeng.py`：与多群无关的独立功能，要合先修它那两个 bug。
- `img.py`：仍建议退回，`read_chat_image` 已能看图且带撤回/授权校验。
