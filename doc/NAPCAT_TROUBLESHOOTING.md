# NapCat 排查手册（登录失败 / 二维码循环 / 端口假活）

> 配套 issue #50。本文覆盖「栈起着但机器人不在线」这一类故障的判读与处置。
> 所有日志都在 `~/.water-group/logs/` 下；仓库代码不含任何账号信息。

## 0. 先看哪本日志

| 现象 | 第一现场 | 说明 |
|---|---|---|
| 群里有反应、值守 Agent 说没唤醒 | `napcat-watchdog.log` | 守护进程视角：launch / online / halt |
| 登录失败、扫码被拒 | `napcat.log` | NapCat/QQ 视角：`Login Error,ErrType: 1  ErrCode: 3` |
| 注入是否真的起来 | `napcat.log` 的 `Boot Command` / `Process exited` | 每次注入尝试都留一行 |

两条时间线是分开的，所以「watchdog 说 online」不等于「账号登上去了」——
issue #50 的修复把 napcat.log 里新出现的 Login Error 映射进了
`napcat-watchdog.log`（同错误码 300 秒内只记一次），现在先看后者就够。

## 1. Login Error ErrCode:3（同账号冲突 / 授权被拒）

napcat.log 里约每两分钟一条：

```
10-24 22:33:53 [error] [Core] [Login] Login Error,ErrType:  1  ErrCode: 3
```

含义：快速登录被拒——同一个账号在别处登录着（桌面 QQ、手机 QQ，或上一个
还没退干净的注入实例）。NapCat 会持续重试，次次被拒。

**处置步骤**（按顺序，实测退出冲突端后一扫即过）：

1. 退出桌面 QQ（完全退出，不是关窗口——托盘右键退出）。
2. 退出手机 QQ 登录，或确认手机端没有顶号。
3. 确认没有残留注入实例：
   `powershell "Get-CimInstance Win32_Process -Filter \"Name='QQ.exe'\" | Select-Object ProcessId,CommandLine"`
   命令行带 `-q <self_account>` 的都是注入实例；桌面 QQ 不带任何开关。
   残留的用 `up.py --stop` 收（一次收全，别逐个 taskkill）。
4. 重冷启栈：`start-bot.pyw`（从资源管理器启动，见 §4）。
5. 扫码登录（`tools/napcat-shell/cache/qrcode.png` 每 30–60 秒换一张，
   **不要连环冷启**——每次重拉都生成新二维码，扫不上就永远赶不上）。

## 2. 反复弹二维码、登不上

检查清单，从上到下：

1. **config.json 在不在**：`water-group/config.json` 丢失时注入命令会少传
   账号参数，NapCat 只能弹二维码。`up.py --status` 的「配置」行会直接说缺什么
   （issue #51 之后缺失/占位符都会 fail-loud，不再静默）。
2. **self_account 是不是真 QQ 号**：占位符（`YOUR_QQ_ACCOUNT` 之类）不算，
   同上看「配置」行。
3. **同账号冲突**：见 §1——桌面 QQ / 手机 QQ 登录着同一账号时，快速登录
   必然被拒。
4. **沙箱注入**：栈从 Agent 沙箱里拉起时，注入的 QQ 约 20 秒后必崩
   （`tsbx.dll`，0xC06D007F），watchdog 日志会带
   `WARN this process carries tsbx.dll`。只能从资源管理器冷启，见 §4。
5. **二维码扫不上**：先确认账号已登录 QQ 再冷启栈，顺序反了就永远是
   「新二维码 → 没扫 → 重拉 → 新二维码」的循环。

## 3. 端口在但 OneBot 无响应

`netstat` 显示 43000/43001 在 LISTENING，探针却不答：

- 端口由 NapCat 的 node sidecar 占着，QQ 主进程崩了它还能应答好几分钟——
  所以「端口在」从来不是「在线」。判定以 `get_login_info` 的 HTTP 应答为准
  （issue #50 之后 launch 早退路径也先问登录态，不再只看端口）。
- 手动探一次：
  `curl -X POST http://127.0.0.1:43000/get_login_info -H "Authorization: Bearer <onebot_token>" -H "Content-Type: application/json" -d '{}'`
  返回 `{"status":"ok","data":{"user_id":...}}` 才是真的登录着。
- `napcat-watchdog.log` 里 `unresponsive: 无 LISTENING 监听者` 与
  `unresponsive pid=<pid>（端口在但 OneBot 不应答）` 是两回事：前者实例已经
  退出，后者桥挂了——该不该重注入看这一行。

## 4. 冷启的正确姿势

1. 先人工登录 QQ（桌面端或扫码），确认没有同账号冲突。
2. 从**资源管理器**启动 `water-group/start-bot.pyw`（不要从 Agent 沙箱里拉，
   沙箱注入的 `tsbx.dll` 会让注入实例 20 秒内崩）。
3. `up.py --status` 十行齐了才算起来；NapCat 行读 halt 标记
  （`state/napcat_halt.json`），降级文案会说冷却原因。

反复冷启解决不了登录问题——它只会堆实例（每个约 190 MB，抢同一份
user-data-dir 的 GPUCache，互相杀 GPU 进程）。收栈用 `up.py --stop`。

## 5. halt 标记怎么读

`~/.water-group/state/napcat_halt.json`：

| 字段 | 含义 |
|---|---|
| `kind` | `crash loop` / `boot timeout` / `zombie` |
| `fails` | 连续失败次数（达到上限后冷却 600s 重置重试，永不永久停机） |
| `first_fail_at` | 这轮失败是从什么时候开始的——链路已经挂了多久 |
| `reason` | 病因；boot timeout 带 `last launch: <note>`，配置缺失与登录被拒在此可分辨 |

恢复（healthy 再次为真）即自动清除，`--status` 不会再显示降级文案。
