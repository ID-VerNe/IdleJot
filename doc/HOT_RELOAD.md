# 全栈热重载（issue #58）

改完 Python 代码后，跑一次 `.\reload.ps1`，guard 会按依赖闭包重启受影响的子进程。改 `commands.py` 的文案不需要再停栈、重新注入 QQ。

## 快速使用

```powershell
# 改完代码，默认等待预算秒数（90，= wg_budget.WAIT_BUDGET，issue #230）
.\reload.ps1

# 只看计划，不执行
.\reload.ps1 --dry-run

# 只重启 # 指令守护（调试用）
.\reload.ps1 --only commands

# 打开/关闭保存后自动重载（默认关）
.\reload.ps1 --watch
.\reload.ps1 --no-watch

# 看上次结果
.\reload.ps1 --status
```

CLI 只做两件事：写 `~/.water-group/state/wg_reload.request`，轮询结果文件。它**不 spawn 任何进程**。真正执行重启的永远是 guard（`up.py`）。这是硬约束：从 Agent 沙箱 spawn 的进程会把 `tsbx.dll` 继承到 QQ，约 20 秒后 QQ 必崩。

## 哪些改动会重启谁

| 改动 | 行为 |
| --- | --- |
| `water-group/daemon/commands.py` | 只重启 `commands` |
| `water-group/daemon/collector.py` | 只重启 `collector` |
| `chatlocal/**`（`app` 的闭包） | 重启 `server_wg` |
| `water-group/daemon/wg_paths.py` | 重启全部 5 个子进程，并 warning：guard 侧仍是旧副本 |
| `water-group/daemon/up.py` / `wg_up_probe.py` / `wg_up_wake.py` / `wg_up_stack.py` / `supervisor.py` / `reload.py` / `winconsole.py` / `bootstrap.py` | **拒绝**；这些是 guard-owned（issue #37 把 up.py 拆成「门面 + 三个模块」，改哪一块都算 guard 代码），必须从 explorer 重启 `water-group/start-bot.pyw` |
| `pyproject.toml` / `uv.lock` | **拒绝**；先 `uv sync`，再整栈启动 |
| `doc/**`、`*.md`、`*.json`、`personas/**` | 不重启；数据变更按读取端是否每次读盘决定 |
| 一次性 CLI（`reply.py` / `listen.py` / `watch.py` / `look.py` / `dossier.py`） | 不重启；下次调用天然是新进程 |

映射不是手写文件表，而是从 5 个入口文件做 AST import 闭包。新增一个没人 import 的 `.py` 会让 `scripts/check_hot_reload.py` 的 fail-closed 覆盖检查变红，逼作者显式分类。

## 子进程重启语义

| target | 方式 | 会丢什么 |
| --- | --- | --- |
| `db_keeper` | 先起新的，等 2 秒，再停旧的 | 无状态；任何时刻至少一个 keeper |
| `app` | 先停旧进程，等 47860/48777 释放，再起新进程并 `GET /` 验证 | 进程内 MCP 长连接、FastAPI lifespan、内存缓存 |
| `collector` | 停旧、起新，验证心跳 | 内存 `seen`（启动时从 `wg_events.jsonl` 重建）、在飞长轮询 |
| `commands` | 写 `wg_reload.soft.commands`，等 5 秒合作退出；超时才单进程 terminate | WS 连接、去重窗口、进行中的扫码登录 |
| `watchdog` | 仅在 OneBot `get_login_info` 健康时停旧起新；否则 deferred 到下一 tick | 连续失败计数、退避状态 |

`watchdog` 的重启路径**只用 `Popen.terminate()`，绝不用 `taskkill /T`**。QQ 是 watchdog 的孙进程，树杀会连带杀掉 QQ；停机路径 `stop.ps1` 才允许树杀。

## 安全与失败处理

- **语法错整批拒绝**：guard 用 `compile()` 预检所有变更文件；任一文件有 `SyntaxError`，整批不执行，旧进程继续跑，结果里带行号。
- **启动失败熔断**：同一 target 连续 3 次启动失败 → 进入 quarantine，`supervise` 不再反复重启；5 分钟后半开重试，失败按 5→10→20 分钟退避，上限 1 小时。状态在 `~/.water-group/state/wg_reload_failed.json`。
- **请求原子认领**：请求先 `os.replace` 到 `wg_reload.request.inflight`，结果写完才删。guard 中途被杀，下次启动会看到未完成请求并写失败结果。
- **deferred 不阻塞**：NapCat 不健康时 `watchdog` 不重启；其余 target 照常处理。
- **`--full` 未实现（P0）**：改 guard 自身代码后，请从 explorer 双击 `water-group/start-bot.pyw`。`--full` 会直接返回 2 并提示。

## 状态与日志

- `~/.water-group/state/wg_guard.json`：guard pid / 启动时间 / sandbox / build。
- `~/.water-group/state/wg_reload_state.json`：watch 开关、上次时间/耗时/文件/重启 target。
- `~/.water-group/state/wg_reload_failed.json`：quarantine 状态。
- `~/.water-group/state/wg_reload_snapshot.json`：上次成功重载后的文件指纹。
- `~/.water-group/logs/wg-reload.log`：每次重载一行。

`up.py --status` 会多一行“热重载”，直接读上述 state 文件，不需要 guard 配合。

## 明确不做

- **NapCat / QQ 不热重载**。改注入参数要整栈重启，重新注入约 65 秒。
- **不用 `importlib.reload` / `uvicorn --reload`**。长命进程里的模块级单例、线程、FastAPI 路由和 SQLite 连接不会随 reload 正确退出，只会把旧代码和新代码混在一起跑。
- **不接管正在执行的 MCP 调用**。server 重启会断开在飞调用，调用方需重试。
- **不重读模块级环境变量**。改 `IDLEJOT_WG_STALE` 等要整栈重启。
- **不做 DB schema 迁移**。表结构变更必须停机窗口。

## 排障

1. `.\reload.ps1 --status` 看上次结果和 quarantine。
2. `.\reload.ps1 --dry-run` 看当前差异被映射到哪些 target。
3. 结果里 `rejected` 有 `SyntaxError line N` → 改好再跑，旧进程没动。
4. `guard 不在线` → 先双击 `water-group/start-bot.pyw`；不要从沙箱起栈。
5. `ports 47860/48777 still busy; not spawning` → 旧 server 没退干净；看 `~/.water-group/logs/app.log`，必要时 `.\stop.ps1` 后从 explorer 重启。
6. 改了 `up.py` / `supervisor.py` / `reload.py` / `bootstrap.py` → 普通 reload 必然拒绝，这是设计，不是 bug。

## 相关

- [README](../README.md)
- [AGENTS.md](../AGENTS.md) §0（从 explorer 起栈）与 §2.2（禁止通配杀进程）
- issue #58 的完整设计与分期
