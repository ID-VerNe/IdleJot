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
| `wg_pause` | Agent 规划/发送期间存在，保证批次不被下一轮等待打散 |
| `wg_heartbeat` | 采集器最后一次完成循环的时间，供总监护判活 |
| `wg_session.txt` | 当前会话 id |
| `mcp_token.txt` | 当前授权 token |

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
