# `#` 指令直回（slash commands，不过 Agent）

`#体力`、`#帮助` 这类群聊指令由 **`water-group/daemon/commands.py`** 直接回复，
整条链路没有 Agent、没有 LLM、不过 MCP。本文说明机制、配置与运维。

## 1. 为什么不是关键词触发

唤醒链路（`up.py:wake_reason` / `mcp_chat.py:755-799`）只认 @ / 引用 / 关键词，
且 `self_names` 是**子串匹配**——把 `体力` 写成唤醒词，"我没体力了"就会误触发。
`#` 前缀则是精确匹配，且与唤醒规则天然错开：`#xxx` 在 `mention_only` 下服务端直接
丢弃（`mcp_chat.py:807-812`），Agent 从来不是 `#` 的消费者。所以 `#` 走脚本直回、
@/关键词走 Agent，两条链路互不打扰。

例外是**追问窗口**（`followup.py`）：它直接读 `chat_inbox`，绕过了 `mention_only`，
所以 `consume()` 里还有第二道 `up.is_command()` 闸门——`#` 指令不算接话，不扣额度、
不唤醒、不续期，但游标照常推进（issue #70）。`wake_reason()` / `--list` / `--status`
也共用这道闸门。

## 2. 数据流

```
QQ 群 ── NapCat WS 43001 ──┬─ 客户端① chatlocal 后端（现状，喂 Agent）
                             └─ 客户端② commands.py（新增，只转 # 前缀）
                                                    │ 四层过滤
                                                    ▼
                              config.json slash_commands 映射 ──► OneBot HTTP 43000
                                                    send_group_msg（带原生引用）回群
```

多客户端并发已实测（2026-10-22）：第二个 WS 客户端与后端同时在线，事件照收、
原连接无感、`get_login_info` 正常；`reportSelfMessage:true` 下自身消息会回灌，
守护按 `user_id == self_id` 过滤。

## 3. 配置（`water-group/config.json`）

```json
"slash_commands": {
  "enabled": true,
  "prefix": "#",
  "help_keywords": ["帮助", "命令"],
  "cooldown_seconds": 5,
  "ignore_at_self": true,
  "unknown_reply": "",
  "commands": {
    "ping":  {"desc": "连通性测试", "reply": "pong"},
    "体力":  {"desc": "原神体力",   "reply": "体力 180/180"}
  }
}
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 总开关。关掉后守护退出（ supervisor 会反复拉起，属预期） |
| `prefix` | `#` | 指令前缀，可改（如 `!`） |
| `help_keywords` | `["帮助","命令"]` | 内置帮助关键词，自动列出所有指令，无需在 `commands` 里配条目 |
| `cooldown_seconds` | `5` | 同群同关键词冷却；`0` 不限 |
| `ignore_at_self` | `true` | `@机器人 #xxx` 混合消息让给 Agent，避免双响应 |
| `unknown_reply` | `""` | 未配置关键词的回复；留空 = 静默忽略（防刷屏） |
| `commands` | `{}` | 关键词 → `{desc, reply}`，纯静态文本 |
| `miyoushe.admin_qq` | `[]` | 可用全局 CK 的 QQ 白名单（见 §8 隐私变更），现写 `plugins.miyoushe.options.admin_qq` |
| `miyoushe.qr_login` | `{enabled:true, daily_limit:5, session_ttl:300}` | 扫码登录开关/每日次数/二维码有效期（秒），现写 `plugins.miyoushe.options.qr_login` |

`onebot_ws_url`（默认 `ws://127.0.0.1:43001`）也在这里配，与 `onebot_http_url` 并列。

改 `config.json` **30 秒热生效**，不用重启；把 `enabled` 改回 `false` 守护自行退出。

### 3.1 插件配置（`plugins`，issue #4 PB）

`#` 指令背后的插件（米游社取数、60s 资讯）有自己的字段，不再挤在
`slash_commands` 里，而是顶层 `plugins.<名字>`：

```json
"plugins": {
  "miyoushe": {
    "enabled": true,
    "options": {"ck": [], "uid": "", "admin_qq": [], "qr_login": {"enabled": true}}
  },
  "sixty": {"enabled": false, "options": {"base_url": "", "allow": ["简报", "天气"]}}
},
"groups": {
  "123456789": {"plugins": {"sixty": false}}
}
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | 插件自报（`miyoushe` 开、`sixty` 关） | 总开关；关掉后该插件的指令全部落空、`#帮助` 也不列 |
| `groups` | `[]` | 只在这些群开；空 = 所有群 |
| `options` | 插件自报 | 插件私有字段；默认值与类型归一（`ck` 字符串→列表一类）由插件负责 |
| `groups.<群号>.plugins.<名字>: false` | — | 群级关闭，优先级最高 |

旧写法 `slash_commands.<名字>` 仍然认：除 `enabled`/`groups` 外的键整体当
`options`，归一化结果与新写法**逐字段相等**（`scripts/check_plugin_registry.py`
直接比对两个 dict）。`up.py --status` 的「插件」一行会标注还在用旧键的插件。
两种写法同名时**新键整体覆盖旧键**，不做字段级合并。

## 4. 行为边界

| 消息 | 谁回 |
|---|---|
| `#ping` | commands.py 直回 |
| `@机器人 在吗` | Agent（mention_only 唤醒） |
| `@机器人 #ping` | Agent（混合消息默认不让给守护） |
| `机器人`（关键词） | Agent |
| 未配置的 `#xxx` | 静默（除非设了 `unknown_reply`） |
| 别的群 / 私聊的 `#xxx` | 不理（只收 `groups` 里 `enabled: true` 的群） |
| 机器人自己的消息 | 不理（`reportSelfMessage` 回灌过滤） |
| 追问窗口里的 `#xxx` | 不算接话：不扣额度、不续期；游标照常推进（#70） |
| `reply.py --list` / `up.py --status` 里的 `#xxx` | 不列 [待回]（`wake_reason` 同一道闸门，#70） |

已知限制：引用机器人**再**发 `#xxx`（reply 段 + `#` 文本）会被守护处理——事件里的
reply 段不带发送者，无法像 @ 那样判是否指向自己。真出现双响应再把这类消息也加进
忽略规则。

## 5. 运维

```bash
# 状态（up.py --status 里有 #指令 一行）
.venv/Scripts/python.exe water-group/daemon/up.py --status

# 日志 / 心跳
tail -f ~/.water-group/logs/wg_commands.log   # 每次回复记一行
cat ~/.water-group/state/wg_commands_heartbeat  # unix 时间戳
```

- 栈运行中由 `supervisor.py` 看护：进程退出或心跳卡死（>300s）自动重启。
- 纯 Python 进程，**不需要** explorer 代启动（那是 NapCat/QQ 注入链的要求）。
- 单实例锁 `~/.water-group/state/wg_commands.lock`：两个守护同时在线会把每条指令回两遍，所以
  第二个实例见到活锁就退出；锁里的 pid 死了才接管。误锁死删这个文件即可。
- 单元检查：`.venv/Scripts/python.exe scripts/check_slash_commands.py`。

## 7. 与 Miao-Yunzai 接入的关系（issue #2）

本守护是「指令不过 Agent」的机制层；取数能力已按方案 D 落地第一波（见 §6）。
Yunzai 桥（方案 B）与 Python handler 是**两条互斥的取数路线**——桥接插件也监听 `#`，
两边同时在线会双响应。接了桥就把本守护 `enabled` 关掉（或把桥的过滤关掉），二选一。
Yunzai 生态独有的价值在渲染图与插件广度；Python handler 的价值在零依赖、无双栈。

## 6. 米游社取数指令（handler，方案 D）

`miyoushe.py` 在 `resolve_reply` 旁挂了动态 handler：关键词命中后调米游社 API 取数，
返回纯文本。handler 优先于静态 `commands` 条目。接口契约对照 Miao-Yunzai 逆向报告
（`.plan/genshin-miyoushe-api-report.md`）实现：DS 签名、`getFp` 设备指纹、300s 缓存
（只缓存 `retcode===0`）、retcode 错误码翻译。
**issue #55 起**还补了设备侧的三件事（见本节末「设备指纹与风控冷却」）：
持久 `device_id` + 完整设备画像、`getFp` 成功后登记设备、账号级风控冷却。

### 指令表

| 指令 | 数据源 | 需要 |
|---|---|---|
| `#公告` / `#活动` / `#资讯` | BBS `getNewsList` | 无 |
| `#兑换码` | 前瞻直播（官方号动态 → `miyolive` → `refreshCode`） | 无 |
| `#米游社<关键词>` / `#攻略<角色>` | BBS `searchPosts` | 无 |
| `#抽卡记录[武器\|常驻\|集录] <游戏内链接>` | 祈愿记录（链接里的 authkey） | 无 |
| `#探索` / `#深渊` / `#角色` / `#武器` | 游戏记录 `index` / `spiralAbyss` / `character` | CK |
| `#体力` / `#树脂` | `dailyNote`（先 `getFp`） | CK |
| `#原石` / `#札记`（可带 `10月`） | `ys_ledger` | CK |
| `#绝区零体力` | ZZZ `note` | CK |
| `#七圣召唤` | `gcg` `basicInfo` + `deckList` | CK |
| `#登录` / `#登录状态` / `#解绑` | passport 扫码（见 §8） | 无 |

账号类指令的 UID：命令里带（`#体力 123456789`）或配默认 `uid`。**只做国服**，
国际服账号明确拒绝。**issue #6 起**，账号类指令优先用群友本人扫码绑定的 CK；
未绑定的群友不再回落全局 CK（隐私变更，详见 §8）。

### 配 CK

`water-group/config.json` → `plugins.miyoushe.options`（旧写法
`slash_commands.miyoushe` 仍认，见 §3.1）：

```json
"plugins": {"miyoushe": {"enabled": true, "options": {
  "ck": ["ltuid=...; ltoken=...; cookie_token=..."], "uid": "100000000",
  "admin_qq": ["主人的QQ号"]}}}
```

CK 是账号凭证：只放本地 gitignore 的 config.json，**不要**外发、不要贴群、不进日志。
`ck` 支持多行（池），取第一个。改完 30 秒热生效。没配 CK 时账号类指令回配置提示，
不会炸。

全局 CK 只服务两个口子：`admin_qq` 白名单里的号，以及 `--test` 合成事件（user_id=0）。
其他群友必须 `#登录` 扫码绑定自己的账号，否则账号类指令统一回「该指令需要绑定」。

### 实机验证（自问自答）

机器人自己发的 `#` 会被自过滤挡掉（`reportSelfMessage` 回灌保护），群里没法自测，
用 `--test` 口子（合成事件，回复发到首个启用群，不带引用）：

```bash
.venv/Scripts/python.exe water-group/daemon/commands.py --test "#公告"
.venv/Scripts/python.exe water-group/daemon/commands.py --test "#体力 100000000"
```

### 有意不做的（别悄悄扩）

- **充值记录**：档位对账且语义是氪金流水，群聊场景敏感；
- **养成计算 / 尘歌壶 / 留影叙佳期 / 原石统计**：需要角色名→ID 映射、本地历史数据或多页拉取；
- **星铁、国际服、`#兑换码使用`**：报告里是另一套 host/salt/参数，或国服被上游拦截；
- **渲染图**：Yunzai 那些 puppeteer 出图不在本模块职责，全部输出纯文本。

这些要么等 Yunzai 桥（issue 方案 B）整体接入，要么按需在 `miyoushe.py` 加 endpoint。

### 设备指纹与风控冷却（issue #55）

`dailyNote` 这类「记录」接口对**机器**的敏感度远高于「账号」，只带一份 CK 不够。
对着 Miao-Yunzai 生态（`miao-plugin` #396、`Axiu-Plugin` `deviceFp.js`）的做法补齐三处：

| 项 | 修前 | 修后 |
|---|---|---|
| `device_id` | 每次进程启动 `urandom` 随机，重启一次等于换一台新机器 | 落 `state/device.json`，16 位 hex，装一次就不变 |
| `getFp` 请求体 | `platform: "1"` + 十几个字段，缺 `aaid`/`oaid`/容量/传感器 | `platform: "2"` + 40 余字段完整画像（Sony XQ-BC52 / Android 13 / qcom），`aaid=oaid=vaid=device_id` |
| 设备登记 | 没有 | `getFp` 成功后补发 `apihub/api/deviceLogin` + `apihub/api/saveDevice`（best-effort，失败只吞掉，不影响取数） |
| 指纹缓存 | 只在内存，进程重启就重取 | 内存 → `state/device.json`，按 `ltuid` 分桶，TTL 7 天 |
| 风控命中 | 原样抛错，群友连发几次 `#体力` 就是连撞几次 | 该账号进 **30 分钟冷却**，冷却期内直接回文案、连 HTTP 都不发 |

冷却的判据是 `1034` / `10035`（人机验证）、`5003` / `10041`（账号异常）——这四个都是
**账号级状态**，重试只会把时间拖更长。冷却按 `ltuid` 分桶，一个号被挡不会连累另一个号；
命中时顺手丢掉被标记的指纹（`fp_fresh`），下次换一个新的去谈。

群友看到的文案：

- `1034`/`10035` → 「米游社要求人机验证：去 App「我的 → 我的角色」过一次验证」
- 冷却期内 → 「这个号刚被米游社风控挡住，还要等约 N 分钟；反复查只会更久。
  去米游社 App「我的 → 我的角色」过一次验证可以立刻解开」

**这两条都不能靠等代码自动解开**：`1034` 是账号级验证码状态，不会自己过期，
必须在 App 里人工过一次。冷却只是防止把风控时间拖长，不是解药。

## 7. 测试

`scripts/check_slash_commands.py` 用合成事件钉住全部纯逻辑：前缀解析、四层过滤、
冷却、有界去重（4096 条窗口）、帮助渲染、消息段构造、`enabled=false` 守门。
`scripts/check_qr_login.py` 钉住扫码登录：passport 双流、凭据兜底解析、DPAPI 往返、
限流、P0 隐私语义（未绑定不回落全局 CK）、轮询通知。
`scripts/check_miyoushe.py` 钉住取数层：DS 签名与请求原文对齐、URL 编码、缓存只存成功、
retcode 翻译，以及 #55 的设备侧行为（`platform=2` + 完整 `ext_fields`、`device_id` 跨重启
不变、设备登记两个请求、失败不拖死取数、冷却按账号隔离并到期放行）。
`scripts/check_sixty.py` 钉住 60s 资讯层（§9）：信封契约、天气参数是 `query` 不是 `city`、
城市后缀归一化、TTL 缓存与失败不进缓存、`allow` 白名单（含 `[]` = 全禁）、
天气三级回退与位置记忆降级、错误上浮不成堆栈，以及热搜七源字段映射、汇率换算方向、
油价 region 后缀匹配、金价常用档位、豆瓣/科技数组契约、随机文案单字段契约
（`kfc`/`duanzi`/`answer`）与 `#KFC` 命令键大小写不敏感。
四个都不发真实网络请求。
群里实机验证：对机器人所在群发 `#ping`，应收到带引用的 `pong`。

## 8. 每用户扫码登录（`#登录`，issue #6）

把「管理员一把全局 CK」升级为「每个群友扫码自助绑定，按 QQ 隔离」。设计文档
`.plan/user-ck-qr-login.md`；实现 `water-group/daemon/qr_login.py` +
`user_store.py`。P0 + P1 已落地；P2 的 stoken 惰性续期与凭证失效提醒见 issue #41-C
（`renew()` / `mark_invalid()`，半个月换一次票、失效后 24h 最多提醒一次）。
方案里的 `#切换uid` 由 `#绑定` 等价覆盖，不再单独提供。

### 流程

```
#登录 → passport createQRLogin（App 流优先，失败回退 Web 流）→ 回二维码图片
      → 群友用米游社 App 扫一扫 → 后台轮询（2s）看到 Confirmed
      → 取凭据 + stoken 换 LToken + UID 发现 → DPAPI 加密存 users/<qq>.json
      → @本人 回群通知「绑定成功」
```

`#登录状态` 回绑定时间、QQ 尾号、各游戏 uid。`#解绑` 是两步确认：第一次只提示，
**60 秒内再发一次 `#解绑` 就生效**（显式写 `#解绑 确认` / `确定` 也可一步到位）。
窗口内没再发就作废，防误触。

> 2026-10-23 07:37 修：原来只认 args 字面量等于「确认」，但提示文案写的是
> 「再发一次 #解绑 确认」，群友读成「再发一次 #解绑」，于是每次都停在提示、永远解不掉。
> 现在两步都认，测试里钉了「第二次真的解掉」这条断言。

绑定成功后 24h 内禁止重复发起；每人同时 1 个 pending 会话；每天最多
5 次扫码；二维码 5 分钟过期。进程重启丢 pending 会话，可接受。

### 绑定 / 切换 UID（`#绑定`）

```
#绑定 100000000           # 默认原神
```

**当前只接受原神 UID**。星铁 / 绝区零的绑定入口先关掉（`BINDABLE_GAMES`）：

- 星铁的取数指令还没接（issue #42 A10），绑了也没人消费；
- `#绝区零体力` 读到的 `ctx.uid` 仍是原神 uid，绑了反而会让人以为「查的是自己那个号」。

两者现在回「XX的取数指令还没接入，暂不支持设置默认 UID」。指令接好后把对应游戏加回
`BINDABLE_GAMES` 即可，存储层（`uids` / `active_uid` 多游戏互不覆盖）早就支持。

**语义是「给已扫码的账号指定默认 UID」，不是「绑定账号」**：UID 只是取数参数，
不是凭证——没有 CK 时单绑 UID 取不到任何数据，所以未扫码的用户会先收到
「先发 #登录」的提示。扫码时自动发现的 uid 会写进 `uids`，`#绑定` 只改
`active_uid`。

归属校验：记录里有 stoken 时会再查一次 `getUserGameRolesByStoken`，不属于该账号的
UID 直接拒绝（绑错游戏会提示加前缀）；Web 流没拿到 stoken 时接受未校验绑定，
查询时自然报错，不会越权取数。

> 为什么不做「只发 UID 就能查」：那必须用管理员的全局 CK 代查，等于把 §8 开头
> 说的隐私缺口重新打开（任何群友可查任意 UID 的公开数据 + 消耗主人 CK 配额），
> 与 issue #6 的 P0 修复冲突。

### 凭证存储

- `~/.water-group/users/<qq>.json`，`auth` 字段整体 DPAPI 加密（`win32crypt`），
  绑定当前 Windows 用户——文件被拷到别的机器/用户下无法解密；
- 群聊里只出现二维码与成功/失败通知，**绝不出现 CK 任何片段**；
- 审计日志 `~/.water-group/logs/qr_login.log` 只记 QQ 号、状态与 retcode；
- `#解绑` 直接删文件。

### 依赖与实机复测结果

- 新增依赖 `segno`（纯 Python 二维码），`uv sync` 即可。

| 复测点 | 结论 | 证据 |
|---|---|---|
| NapCat `base64://` 图片段（§7.2） | ✅ 通 | 实测发往群后 `send_group_msg` 返回 `retcode=0`。不需要退 `file://` 落盘方案 |
| 守护加载新代码 | ✅ 通 | 重启 `daemon/commands.py` 后日志出现 `qr-login poller started` / `commands start`；`commands.py --test "#公告"` 拿到真实公告并在群里发出（`msg_id=674038479`） |
| App 流 Confirmed 是否仍带 stoken（§7.1） | ✅ 通（但形状变了） | 2026-10-23 07:17 真人扫码：`data.tokens[]` 里 `token_type==1` 才是 stoken，`user_info.{aid,mid}` 给账号；响应**不带 Cookie**。已按此解析（`_app_stream_auth`） |
| stoken 换 ltoken / cookie_token（§3.3） | ✅ 通 | `getLTokenBySToken`、`getCookieAccountInfoBySToken` 都是 **GET + Cookie 认证**（`stoken`/`mid`/`stuid` + `x-rpc-client_type: 1` + Hyperion UA）。POST 会 `405`，`GET ?stoken=` 会 `-100` |
| `getUserGameRolesByStoken`（§7.3） | ❌ 已废弃 | 无论哪种传参都回 `retcode=-10001 invalid request`；改用 `getUserGameRolesByCookie` + CK 串，返回全部游戏（原神/星铁/绝区零/崩三） |
| `dailyNote` 取数（§7.5） | ⚠️ 风控 | 同一份 CK 查 roles `retcode=0`，但 `api-takumi-record` 的 `dailyNote` 回 `1034`/`5003`（验证码 / 账号异常）。与扫码链路无关：设备侧已按 #55 补齐（持久 device_id、完整 getFp 画像、设备登记、30 分钟冷却），但 `1034` 是账号级状态，必须在米游社 App「我的 → 我的角色」**人工过一次验证**才会解 |

扫码、换票、uid 发现三步已在 2026-10-23 实机跑通。`dailyNote` 的风控已按 #55 做尽
代码侧能做的部分（设备指纹 / 设备登记 / 冷却），剩下的账号解封只能人工操作。

## 9. 60s 资讯指令（sixty.py，issue #53）

`sixty.py` 挂了第二组动态 handler：数据源是 `vikiboss/60s`（MIT）的实例
`config.json` 里 `plugins.sixty.options.base_url` 指向的实例，群友发 `#简报` / `#热搜` / `#天气` 直接取数回帖，
同样不过 Agent。与米游社那组的区别：**无鉴权、无凭证、与账号绑定体系完全无关**，
所以全部 `need_ck=False`，未绑定群友照样用。

### 指令表

| 指令 | 数据源 | 说明 |
|---|---|---|
| `#简报` | `/v2/60s` | 每日 60 秒，默认 5 条（`max_items` 可调，上限 15），带农历与 tip |
| `#热搜 [源]` | `/v2/weibo` 等 7 个源 | 无参默认微博；可选知乎 / 百度 / B站 / 头条 / 小红书 / 抖音；只透传标题 + 热度 + 链接 |
| `#天气 [城市]` | `/v2/weather/realtime` | 无参时走三级回退（见下） |
| `#预报 [城市]` | `/v2/weather/forecast` | 逐日预报，默认 5 天（`max_items` 可调）；**上游第 0 条常是昨天，按日期过滤** |
| `#历史` | `/v2/today-in-history` | 历史上的今天，默认 5 条，只透传年份 + 标题 + 链接 |
| `#摸鱼` | `/v2/moyu` | 打工人日历：农历 / 节气 / 进度 / 倒计时 / 格言 |
| `#运势` | `/v2/luck` | 当日运势，按日期定住（同一天同一签） |
| `#行情 [类别]` | `/v2/exchange-rate` `fuel-price` `gold-price` | 无参默认汇率；`油价 [地区]` / `金价`。汇率翻成「1（或 100）外币兑多少人民币」 |
| `#豆瓣` | `/v2/douban/weekly/movie` | 一周口碑电影榜，默认 5 条（标题 + 评分 + 链接） |
| `#科技` | `/v2/it-news/rank` | IT 之家日榜，默认 5 条（标题 + 链接） |
| `#KFC` | `/v2/kfc` | 疯狂星期四文案，上游随机一条；`#kfc` 也能命中（键大小写不敏感） |
| `#笑话` | `/v2/duanzi` | 随机段子 |
| `#答案` | `/v2/answer` | 答案之书，随机一句（中文，不上英文原文） |
| `#一言` | `/v2/hitokoto` | 随机一句话 |

### 配置（`plugins.sixty.options`）

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 关着时指令静默落空，**也不会出现在 `#帮助` 里** |
| `base_url` | 部署者自建的 60s API 实例 | 换实例只改这里 |
| `timeout` | `6` | 秒 |
| `max_items` | `5` | 列表类指令条数 |
| `default_city` | `""` | 天气 / 油价兜底城市 |
| `allow` | `["简报","热搜","天气","预报","历史","摸鱼","运势","行情","豆瓣","科技","KFC","笑话","答案","一言"]` | 白名单；`[]` = 全禁，缺省 = 默认名单 |

`enabled` 与 `allow` 是两层闸门：插件总开关（含群级覆盖）在前，`allow` 在后。
旧写法 `slash_commands.sixty.{enabled, base_url, ...}` 仍然认（见 §3.1），
归一化结果与上表逐字段相等。

### 天气三级回退

```
① 发件人 traits.location（记忆库，画像卡「地域:」同款字段，归一化后）
② config default_city
③ 回「带上城市名再查」
```

**隐私边界**：位置只在本机用于拼天气请求，回复里只出现天气结果，不出现位置原文、
不落新存储、不进日志。群友位置由 LLM 消化路径写入，覆盖率天然不全（2026-10-23
实测 5/67），所以 `default_city` 兜底是必需的，不是可选项。

### 接口契约（照上游源码钉住，文档缺参数表）

- 统一信封 `{code, message, data}`，**判成功只看 `code===200`**；
- 天气定位参数叫 **`query`** 不是 `city`——传 `city` 会被上游**静默忽略**，
  永远返回实例所在城市的天气。`city` / `province` 只是同音多选时的消歧参数；
- 城市名不能带「市」「省」后缀（`?query=上海市` → `code:404` 未找到城市），
  `normalize_city()` 负责剥；
- 业务错误（城市找不到）是 **HTTP 200 + `code:404`**；路由不存在才是真 HTTP 404，
  此时 urllib 抛 `HTTPError`，统一降级成「资讯源暂不可用」。

### 端点分级（别悄悄扩）

| 处置 | 端点 |
|---|---|
| 放行 | `60s` `weibo` `zhihu` `baidu/hot` `bili` `toutiao` `rednote` `douyin` `weather/realtime` `weather/forecast` `today-in-history` `moyu` `luck` `exchange-rate` `fuel-price` `gold-price` `douban/weekly/movie` `it-news/rank` `kfc` `duanzi` `answer` `hitokoto` |
| 二审后放行 | `lunar` `dad-joke` `ncm-rank` |
| 禁 | `ip` `beta/qq/profile`（隐私）、`qrcode`（钓鱼）、`og`（**SSRF**：会让实例抓任意 URL）、`password*` `whois` `fanyi` `hacker-news` `ai-news` |

新增端点先过合规评审再进 `HANDLERS` 和 `allow` 默认名单。

### 降级与自保

- 域名是自建单点：网络失败 / 信封错误 / 非 JSON 全部翻成 `SixtyError`，
  `run_handler` 把它当回复文案返回，**不抛到 `dispatch()`**；
- 失败结果不进缓存（一次抖动不被放大成一整段 TTL）；
- 网络异常（超时 / 断网 / 5xx）与解码异常（HTTP 200 但报文不是 JSON）翻成**两句**
  不同的降级文案，别把反代错误页报成断网；
- TTL 按端点分档：`60s` 300s / 热搜七源 180s / `weather` 600s / `forecast` 1800s /
  `exchange-rate` `fuel-price` `douban` 3600s / `gold-price` `it-news` 600s /
  `kfc` 60s / `duanzi` `answer` `hitokoto` 30s（随机文案只吸收连刷，别把同一段
  文案按在那儿好几分钟）/ `moyu` `today-in-history` `luck` 是「一天一份」，缓存键带日期标签、
  TTL 给一天，同一天只取一次；
- 缓存有淘汰：写满 `CACHE_MAX` 时先清过期键，仍满就按到期时间踢最早的一批
  （日期标签跨天后不再命中，不淘汰会一直堆内存）；
- 每群每关键词 5s CD 由 `dispatch` 统一把守，sixty 不再自己限流——刻意不在
  handler 里 sleep（§4.3：同步等会卡住 WS 事件循环）；
- 日志 `~/.water-group/logs/wg_sixty.log`，只记失败的类型与路径，不记请求参数。

### 实机验证（2026-10-23）

开关是 `config.json` 的 `plugins.sixty.enabled`（该文件不入库）。`commands.py` 的配置
**按消息懒重载**（`RELAY_SECONDS=30`，判断点在消息循环里）：改完不必重启，下一条群消息之前就生效；
`--test` 是独立进程，直接读新配置。

但**改 `commands.py` / `sixty.py` 这类 handler 代码必须重启 `#指令` 守护** —— `HANDLERS` 是
import 期合成的，热重载不重算它。判据与重启姿势见 `AGENTS.md` §0.2（issue #68 就是这么踩的：
进程跑着 08:31 的旧代码，四条指令被当未知指令静默丢弃，日志里一个字都没有）。

| 指令 | 结果 | 证据 |
|---|---|---|
| `#简报` | 5 条 + 农历 + tip | 实测返回，见 `wg_commands.log` |
| `#热搜` | 微博热搜前 5，标题 + 热度 + 链接 | 直连取数通过 |
| `#热搜 知乎` / `百度` / `头条` / `小红书` / `抖音` | 各源前 5，标题 + 热度 + 链接 | 逐源实测通过；`B站` 上游当时 500，走降级文案 |
| `#天气 广州` | 【广东广州】多云 26℃ · 西北风 4-5 级 · 湿度 58% · 空气优（AQI 24） | |
| `#天气`（无参、无默认城市） | 回「带上城市名再查」 | 三级回退落到第③级 |
| `#预报 广州` | 5 行逐日预报（昨日的第 0 条被过滤） | 实测 `daily_forecast` 7 条，`[0].date` 是昨天 |
| `#历史` | 【历史上的今天 10-23】+ 5 条（年份 / 标题 / 链接） | 实测 `items` 12 条 |
| `#摸鱼` | 公历 / 农历 / 节气 / 三档进度 / 倒计时 / 格言 | 实测返回结构一致 |
| `#运势` | 当日一签（`luck_desc` + `luck_tip`） | 上游每次随机，按日期取模传 `id` 定住 |
| `#行情` | 6 个币种 + 更新时间 | 上游汇率按天更新 |
| `#行情 油价 广州` | 【今日油价 广东广州】+ 4 档 + 调价预告 | `region` 后缀匹配，`广州市` 会被归一化 |
| `#行情 金价` | 5 档金属报价（金 / 伦敦金 / 白银 / 铂金） | 上游另有门店/银行/回收价，刻意不搬 |
| `#豆瓣` | 一周口碑电影榜前 5（标题 + 评分 + 链接） | 实测 `data` 是数组，10 条 |
| `#科技` | IT 之家日榜前 5（标题 + 链接） | 实测 `data` 是数组，12 条 |
| `#KFC` / `#kfc` | 【疯狂星期四】+ 一条文案 | 上游随机；两种大小写都命中 |
| `#笑话` | 一条段子（无标题前缀） | |
| `#答案` | 【答案之书】+ 中文答案 | `answer_en` 刻意不上屏 |
| `#一言` | 正常返回 | |

`#帮助` 里十四条都在（受 `allow` 闸门约束）；把 `allow` 改成 `[]` 后一起消失，`enabled: false`
同样不列——闸门是 `allow` 与 `enabled` 双条件，不是只看白名单。

两点现场观察：

- `--test` 用合成事件（`user_id=0`），**位置记忆那条分支必然落空**，要验三级回退第①级
  只能靠真人发 `#天气`；
- 连续四条取数无超时；`_opener()` 显式传空 `ProxyHandler({})`，宿主注入的 `HTTP_PROXY`
  不会被 urllib 带进请求（否则整机代理一挂，资讯层跟着全挂）。


