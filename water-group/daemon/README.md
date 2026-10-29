# 水群常驻守护

挂机水群不该依赖 Agent 的某一次工具调用活着。MCP 长轮询一旦超时或断线，
监听就断了；这里把「等消息」和「保活 QQ」都挪到独立进程里，Agent 只负责写回复。

## 进程

| 脚本 | 职责 |
| --- | --- |
| `supervisor.py` | 总监护，拉起并重启下面两个子进程；采集器靠心跳判活，卡死也会被杀掉重启 |
| `collector.py` | 常驻 `wait_chat_messages`（`mention_only`），每条消息先落盘再交给 Agent |
| `watchdog.py` | 每 10 秒 `get_login_info` 健康探测；无响应就按端口反查 PID、杀掉、重新注入 QQ |

配套脚本：

- `up.py`：一键启停与 `--status`。它只是门面，四块实现在同目录三个模块里（issue #37）：
  `wg_up_probe.py`（哪些 pid/端口是我们的）、`wg_up_wake.py`（配置、OneBot 账号探测、
  唤醒与账本判定）、`wg_up_stack.py`（taskkill、注入、stop/spawn）。门面名字与归属模块
  同一对象，`scripts/check_up_facade.py` 钉住；改这三个模块等于改 guard，必须整栈重启
- `session.py`：确保目标群有活的持续会话（会话绑定在 grant 上，grant 轮换会一起没）
- `mint_grant.py`：本地 MCP 授权失效时重新签发，沿用上一次的权限范围
- `mcp_client.py`：最小 JSON-RPC 客户端，遇到 `denied` 自动续签并重放一次

## `#` 指令与扫码登录

| 模块 | 职责 |
| --- | --- |
| `commands.py` | 群里的 `#关键词` 直接回复，不过 Agent（`doc/SLASH_COMMANDS.md`） |
| `plugins/` | 指令插件的契约与注册表：`base.py`（`Plugin` / `Command` / `Hooks`）、`registry.py`、`*_plugin.py`（issue #4）。新增插件 = 一个适配器文件 + `LOADERS` 一行 |
| `miyoushe.py` | 米游社取数 handler（`#体力` / `#深渊` / `#原石` …） |
| `qr_login.py` | 每用户扫码登录：会话表 + 单轮询线程（`#登录`，issue #6） |
| `user_store.py` | `~/.water-group/users/<qq>.json` 凭证存储（auth 字段 DPAPI 加密） |

## 状态目录

默认 `~/.water-group/state/`，可用 `WATER_GROUP_HOME` 改整个数据根、`IDLEJOT_WG_STATE` / `TULPA_WG_STATE` 改 state 目录（测试隔离用）。单一事实来源是同目录 `wg_paths.py`。Agent 与守护进程通过这些文件协作：

| 文件 | 含义 |
| --- | --- |
| `wg_events.jsonl` | 已送达的群友消息流水（自己的发送回执不落盘），按 event id 去重，Agent 断线期间也不会丢 |
| `wg_state.json` | 最近一次 `wait` 的完整返回，含 `input_batch` |
| `wg_ack.txt` | Agent 已处理到的游标，采集器下一轮用它确认进度 |
| `wg_pause` | 暂停标记，判定归 `wg_pause.py`（issue #189）：一行 JSON `{"reason","pid","at","until"}`，owner 存活 + 未到时限才算数；无主/超时/非规范内容（手工 `touch` 的 `placeholder`、坏 JSON）过了 `LEGACY_GRACE` 就当失效，由采集器当轮删掉并恢复采集。采集器在暂停期间照写心跳，不会因暂停被判死 |
| `wg_heartbeat` | 采集器最后一次完成循环的时间，供总监护判活 |
| `wg_session.txt` | 当前会话 id |
| `mcp_token.txt` | 当前授权 token |
| `wg_send_failure.json` | 上一次发送失败的账本（真实 retcode / 禁言时刻 / 是否已恢复，issue #132）。禁言时刻是**失败那一刻的预测**，所以 `reply.py --list` 打表头前会复查一次（`mute_until` 到点且 `recovered` 未定时现查 `shut_up_timestamp`，issue #145）：实测解禁就写 `recovered_at` 并改口说「实测解禁」，查不动就降级成「未复查 / 复查未能确认」——不再拿过期预测当此刻的事实 |

## 两套 id：事件流 `id` 与会话库 `qid`（issue #203）

同一时刻可能有两种编号在流转，撞号时只靠裸数字分不清，所以 `wg_event_ref.py` 是唯一
的解析口：**裸数字 = 事件流 id**（`wg_events`），**`q<n>` / `qid:<n>` = 会话库 `chat_inbox`
的 rowid**（追问行、`--context` 的表现就是 `q<n>`，`id:<n>` 是显式写法）。

| 入口 | 规则 |
| --- | --- |
| `reply.py <反射> …` / `--show` | 带 `q<n>` 就去会话库取，裸数字先在事件流找；**事件流没命中、会话库唯一命中时回库兜底，并打出源说明**（不静默） |
| `reply.py --also` | 只做存在性与归属校验，不进 `wg_replied.jsonl`（取答记录只认真回复） |
| `followup.py` / 追问窗 | 游标记号由 `wg_event_ref.format_ref(namespace, n)` 生成，`after_ref` 把 `after_id` 的库 rowid 语义显式化 |
| `look.py` | `INBOX` 直接当库 rowid 补读，`EVENT` 经验证反查库行 |
| 渲染侧（`wg_text` / `wg_context` / `wg_images` / `watch` / `followup`） | 一律打印 `q<n>`，不让 Agent 拿到会被误解的裸库 rowid |

会话库行必须属于本会话：钉死会话时（`wg_isolation.pinned()`）跨会话的行直接拒，不回退
到别群的消息上。

## 时间基准

所有面向 Agent 的输出都带「现在」，格式统一由 `wg_clock.py` 提供（issue #95）：

| 输出 | 位置 |
| --- | --- |
| `up.py --status` | 首个非空行：`现在 2026-10-23 20:52:07 (UTC+8)` |
| `reply.py --list` / `--context` | 表头 |
| `watch.py` | 唤醒块首行（`id=` 行之前） |
| `followup.py --show` | 首行 |

消息行上的 `20:50:44` 是消息时间戳，`心跳 19s 前` 是相对量，`静默至 20:55:15` 是未来
截止时刻——三者都不是当前时刻，拿它们反推「现在」必错。`scripts/check_now_clock.py`
钉住这四处。

## 启动

```bash
.venv/Scripts/python.exe water-group/daemon/supervisor.py
```

## 为什么用 HTTP 探测而不是端口探测

QQ 被注入后会出现「端口还在 LISTENING、进程也在，但 OneBot 不再应答」的假活。
只探端口会把假活当成健康，于是守护进程全程沉默。这里探的是 `get_login_info`
的真实返回，实测假活能被识别并在约 8 秒内重新上线。
