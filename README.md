# 闲笔 (IdleJot)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**把 AI Agent 塞进 QQ 群，让它当个群友，而不是当客服。**

> *“闲笔勾勒群友百态，静水深流长驻人间。”*

Agent 常驻一个 QQ 群：被叫到就回，没被叫就潜水看别人聊，顺手把群友的脾气记下来。回复是 Agent 读上下文之后现场写的，不是关键词表里查出来的固定话术；人格来自可热切换的人格卡，记忆存在本地 SQLite 里。

默认触发模式是 `mention_only`：只在被 **@**、被**引用**、或者正文里出现它的名字时才醒。群友不会每条消息都接话，这里也一样。

## 和常见 QQ 机器人的区别

| | 常见 QQ 机器人 | 闲笔 (IdleJot) |
| --- | --- | --- |
| 谁在说话 | 关键词 / 指令表驱动的固定回复 | Agent 读上下文现场写，带人格卡 |
| 触发方式 | 指令前缀 | @ / 引用 / 叫名字，按群配置 |
| 记忆 | 无，或全局一个库 | 群友画像按群沉淀，闲时自动整理 |
| 接入方式 | 插件 / 框架 | MCP：Agent 用命令行读事件、写回复 |
| 部署 | 面板 + 插件 | 一个常驻进程栈，双击启动 |

整个 `water-group/` 子系统是本项目的核心：常驻栈负责收消息与保活，Agent 只负责读事件、写回复。人设卡体系与群友记忆引擎都在这里。

## 它现在能做什么

- **被 @ 就回，带原生引用**。每条回复引用触发它的那条消息，用的是 OneBot `reply` 消息段，不是把文字拼在前面。
- **按花名 @ 人**。正文里写 `@{群友A}` 就是真的 @，会变蓝、会响。四层解析（QQ 号 → 本群名片 → 档案别名 → 近似匹配），同名时报候选而不是替你选一个。
- **连着几条 @ 不会吞**。监听退出到重新挂上之间来的 @ 会落盘，不会因为「看起来像回过了」被静默过滤掉。
- **回之前看得到上下文**。`--context 30` 直接读库里的全部聊天，不只是 @ 你的那几条——群里刚才在聊什么、谁被谁怼了、话题拐到哪儿了，都看得见，接话才接得住。
- **回复之后话题接着聊**。每次回复成功都会自动开一个追问窗口：群里继续聊的几句（不用再 @）也会唤醒 Agent，默认跟 10 条或静默 180 秒，没人接话就自动收回，回到只等 @。
- **闲时沉淀群友画像**。没被 @ 的时候群里照样在聊，消息全部入库；攒够了就提炼一遍——谁爱玩什么梗、谁在群里是什么角色、谁换过名字。
- **人格卡热切换**。仓库 `water-group/personas/` 放公开模板，私有卡放 `~/.water-group/personas/`，同名私有优先；换个文件就是换个人。
- **崩了会自己爬起来**。守护进程探的是 OneBot 的真实应答（`get_login_info`），不是端口——QQ 那种「端口还在但不应答」的假活能被识别，约 8 秒重新上线。

目前只发**文字 + @ + 引用**。表情、图片、戳一戳这些能力在 MCP 工具层有，命令行这条路没接。

## 快速开始

### 你需要

- Windows 10 / 11 x64
- Python 3.13
- 一个能登录的 QQ 账号，以及一个 OneBot 11 实现（推荐 [NapCat](https://github.com/NapNeko/NapCatQQ)），开 **HTTP + 正向 WebSocket**
- 一个愿意干这活的 Agent（能跑命令、能读文件就行）

### 1. 装依赖

```powershell
git clone https://github.com/ID-VerNe/IdleJot.git
cd IdleJot
.\setup.ps1
```

`setup.ps1` 会建好 `.venv` 并装齐依赖。之后新增依赖用 `uv`，别拿 `pip` 往里混装。

### 2. 配 OneBot

复制 `water-group/config.example.json` 为 `water-group/config.json`，填这几项：

```json
{
  "onebot_http_url": "http://127.0.0.1:43000",
  "onebot_token": "你的 Token",
  "self_account": "机器人 QQ 号",
  "self_names": ["小鲸鱼", "鲸鱼"],
  "groups": {
    "群号": {
      "name": "群名",
      "persona": "little_whale",
      "trigger_mode": "mention_only"
    }
  }
}
```

`self_names` 是「别人不 @ 你、只是提到你的名字」时也会唤醒的关键词。填几个群里真会用的叫法。

`config.json` 在 `.gitignore` 里，不会被提交——但它里面有 Token 和群号，别手动往仓库里塞。

### 3. 起栈

双击 `water-group/start-bot.pyw`。

它会按顺序拉起 NapCat → 后端 → 采集与保活守护，然后进入守护循环**永不退出**。第一次冷启动大约需要：NapCat 注入 65 秒、后端起端口 20 秒、采集器建立会话 20 秒。这段时间看到「离线 / 未建立」是正常的。

它是幂等的：端口已经在就什么都不做，直接退出。输出写在 `water-group/logs/launcher.log`。

> **不要**从 Agent 的终端里直接跑 `up.py`。某些 Agent 的沙箱会给子进程注入 DLL，注入会一路继承到 QQ，QQ 大约 20 秒后必定崩溃，然后保活进程无限重拉。`.pyw` + explorer 启动的整条链是干净的。`bootstrap.py` 检测到注入会直接 `exit 2` 拒绝启动。

### 3.1 停栈

双击 `water-group/stop-bot.pyw`（或者 `.\stop.ps1`、`up.py --stop`，三个入口是同一个实现）。

停止没有启动那种注入风险，所以不必经 explorer，双击就行。它会树杀守护与看护、按脚本指纹扫掉树外残留（以前会留下 `server_wg.py` / `collector.py` 继续占端口）、按 `-q <账号>` 只杀注入的 QQ 实例、最后按端口兜底。结果写 `~/.water-group/logs/stopper.log` 一行；退出码 0 表示四个端口全部释放。

### 3.2 改完代码热重载

```powershell
.\reload.ps1                 # 按依赖闭包重启受影响的子进程
.\reload.ps1 --only commands # 只重启 # 指令守护
.\reload.ps1 --status        # 看上次结果
```

CLI 只写请求文件，执行者始终是 guard；不会从沙箱 spawn 任何进程。改 `commands.py` 文案这类操作 5 秒内生效，不需要重新注入 QQ。改 `up.py` / `supervisor.py` / `reload.py` / `bootstrap.py` 会被拒绝，需要从 explorer 双击 `water-group/start-bot.pyw` 整栈重启。完整语义见 [`doc/HOT_RELOAD.md`](doc/HOT_RELOAD.md)。

### 4. 让 Agent 接管

对 Agent 说一句：

> 在群 123456789 挂机水群

它会去读 `AGENTS.md` 的 §0 和 `.agents/rules/water_group.md`，那两份是给 Agent 看的操作手册，比这份 README 细得多。

## 日常命令

状态都在 `~/.water-group/state/`（可用环境变量 `WATER_GROUP_HOME` 改整个数据根，`IDLEJOT_WG_STATE` / `TULPA_WG_STATE` 改 state 目录）。

```bash
# 只读复查：NapCat / 后端 / 守护 / 监听 / 最近唤醒
.venv/Scripts/python.exe water-group/daemon/up.py --status

# 停栈（三个入口同一实现，选一个）
#   双击 water-group/stop-bot.pyw
#   .\stop.ps1
.venv/Scripts/python.exe water-group/daemon/up.py --stop

# 挂监听。只活一轮，WAKE 后必须重挂
.venv/Scripts/python.exe water-group/daemon/listen.py --timeout 300

# 看谁 @ 了我
.venv/Scripts/python.exe water-group/daemon/reply.py --list

# 回之前看群里在聊什么（含闲聊，不只是 @）
.venv/Scripts/python.exe water-group/daemon/reply.py --context 30

# 回一条（自动查消息编号 + 原生引用 + 记账）
.venv/Scripts/python.exe water-group/daemon/reply.py 1667 "喜欢，但猫不搭理我"
```

`up.py --status` 长这样：

```
NapCat    : 在线 (10001 小鲸鱼)
后端      : 在线  (47860 / 48777)
守护      : 运行中 pid=56640  心跳 21s 前（wait 周期 170s，正常）
监听      : 已挂 pid=61208
追问      : 未开窗（只等 @ 和关键词）
会话      : e200c0d5...
游标      : 1667   事件 182 条
最近唤醒  :
  id=1664 21:10:46 [已回] [at] 群友A: @10001 还在吗
  id=1667 21:15:31 [待回] [at] 群友B: @10001 你喜欢猫嘛？
```

**`监听 : 未挂` 是最要命的一行。** 消息照收、照落盘，但没有任何进程把它变成唤醒信号，表现出来就是「发了消息没反应」。会话中断、栈重启、上下文压缩之后都要重挂。

### 回复的几种姿势

```bash
# 带 @（花名必须写在花括号里）
reply.py 1667 "@{群友A} 出来一下"

# 正文之外单独 @，可重复
reply.py 1667 "学废了" --at 群友A --at 群友B

# 只查这个名字对应谁，不发
reply.py --who 群友

# 一波连发用一条答完，顺带记多条账
reply.py 1664 "别急，都在" --also 1665 --also 1666

# 这次不开追问窗口（默认回复后会继续听群里聊一阵）
reply.py 1664 "别急，都在" --no-followup

# 回完还想做别的事，别续挂监听
reply.py 1664 "在的" --no-wait
```

发送成功后**默认就地续挂监听 240 秒**（`--wait N` 可改时长，`--no-wait` 或 `--wait 0` 关闭），把「回复 + 重挂」合成一次调用，空窗从一整轮思考压到几百毫秒（issue #69：不默认续挂时，回完到下次挂载之间的空档里 @ 会静默丢失）。前台跑会被工具超时砍掉——消息其实发出去了，只是等待被打断。

## 它是怎么工作的

### 常驻栈

```
explorer.exe
└─ water-group/start-bot.pyw          .pyw，无控制台，幂等
   └─ water-group/daemon/up.py        常驻，兼任总监护
      └─ supervisor.py                拉起并重启下面全部
         ├─ server_wg.py               后端 47860 / MCP 48777
         ├─ collector.py              常驻 wait_chat_messages，消息先落盘
         ├─ db_keeper.py              钉住 SQLite WAL 边车文件
         └─ watchdog.py               NapCat / QQ 保活
            └─ NapCatWinBootMain → QQ.exe
```

端口：NapCat OneBot `43000` / `43001`，后端 `47860`，MCP `48777`。

为什么要把「等消息」从 Agent 的工具调用里挪出来：MCP 长轮询一旦超时或断线，监听就断了，Agent 还在傻等。现在等消息和保活都在独立进程里，Agent 只负责**读事件、写回复**两件事。

### 一次唤醒的完整链路

1. `collector.py` 常驻 `wait_chat_messages`，把每条消息追加进 `.tmp/wg_events.jsonl`，按 event id 去重。
2. 服务端先过滤一遍：消息段里有 `at` 且指向机器人、有 `reply` 且引用的是机器人自己发的、或者正文命中 `self_names`。
3. `listen.py` 在静默里轮询这份流水，命中就退出，并把 `watch.py` 打印的唤醒行（`id=… [at] 某人: …`，追问窗口是 `[追问]`）连同收尾的 `WAKE` 一起转给调用方——它设计上只活一轮，让 Agent 能观察到「有活干」这个事实。子进程的输出是**捕获后整段写出**的：父进程没有控制台时，带 `CREATE_NO_WINDOW` 的子进程 stdout 指向它自己的新控制台，靠继承会把唤醒内容全丢掉，Agent 只看到一个孤零零的 `WAKE`（issue #91）。
4. Agent 跑 `reply.py`，查消息编号 → OneBot 直发带引用 → 写已回账本 `.tmp/wg_replied.jsonl`。发送成功会顺手开一个追问窗口。
5. 回完**默认就地续挂监听**（`reply.py --wait`，默认 240s，`--no-wait` 关闭）；没走回复路径时按 `listen.py` 退出时打印的 `RESTART:` 行重挂。
6. 追问窗口开着的时候，群里继续聊的几句（不用再 @）也会唤醒监听，话题能接着走；没人接话窗口自己收回。

### 为什么引用不走 `send_chat_reply`

`plan_chat_reply` 只接受**当前批次**里的 `target_event_id`，而群里每来一条新消息就开一个新批次。等 Agent 写好正文，触发它的那条通常已经落在旧批次里了——此时 plan 要么直接 `target_not_in_batch` 失败，要么改投到最新那条、把引用挂到别人头上。

所以引用改走 OneBot 原生 `reply` 消息段，用 collector 已经存下的 `onebot_message_id`，跨多少批次都不受影响。plan 路径保留为兜底。

### 按花名 @ 人

正文里的 `@{花名}` 会被换成 `{"type":"at","data":{"qq":...}}` 消息段。**必须带花括号**：群里正文的「@群友A」是普通文字，真正的 @ 是消息段。混为一谈会把随口一提变成一次点名。

解析顺序：

1. 纯数字 → 直接当 QQ 号（唯一不会歧义的输入）
2. **本群名片 / 昵称精确**
3. 档案（主名 / 别名）精确
4. 本群名片近似 → 档案近似（单字不做近似，太容易撞）

名片排在档案前面，因为「花名」在群里最直白的读法是名片；档案兜住改名前的老叫法和只有熟人知道的外号。

**同名就报，不猜**。`--who 小明` 会列出 3 个小明，`--who 阿伟` 会列出两张撞车的名片。@ 错人比漏 @ 更像一次失误，所以歧义一律降级成文字。解析不出来也不拒发，名字原样以 `@某某` 文字送出。

带 @ 的消息**只能走 OneBot**：`send_chat_reply` 只吃纯文本，会把 at 段静默降级成文字。所以一旦发现 at 段就强制走 OneBot，OneBot 不可用时直接 `SEND_FAILED`，不会退化成一条没有 @ 的消息。

### 连发的 @ 不会丢

群里一分钟内连发三条 @ 是常态，而监听在拿到 `WAKE` 那一刻就退出了。这个窗口里来的 @ 不会丢，但以前会被「已回」误判吞掉。

根因是判定过宽：老逻辑的兜底是「这条之后只要出现过自己的消息就算回过了」。回第 1563 条的那条回复落在 1564 之后，于是 1564 被记成已回——`--list` 显示 `[已回]`，监听也把它滤掉，群里看到的就是「@ 了没反应」。

现在按账本首条时间戳划线：比它新的事件**只认账本**，兜底仅对账本建立之前的老消息生效。回归测试在 `scripts/check_wake_ledger.py`，四条断言钉住这个边界。

### 回复之后，话题还能接着聊

`mention_only` 让 collector 只投递 @ 和关键词，所以「回复之后群里又聊了什么」在 `wg_events.jsonl` 里根本不存在。但 `chat_inbox` 里有**全部**消息——后端在决定要不要推送之前就已经把它们写进库了。追问窗口就走这条路：

```
reply.py 发送成功
  └─ 开窗（wg_followup.json：从这条之后看，额度 10 条 / 静默 180 秒）
       └─ watch.py 每轮顺带查一眼 chat_inbox
            ├─ 有群友接话 → 唤醒（行带 [追问] 前缀）→ Agent 回它 → 窗口重新开满
            └─ 接满 10 条 或 静默 180 秒 → 收窗，回到只等 @
```

唤醒行带 `[追问]` 前缀、末行同样是 `WAKE`；回这条闲聊时群号和 `onebot_message_id` 会自动从 `chat_inbox` 兜底查到，引用挂得上去。窗口期间来的 @ 走正常路径（@ 优先），回复它窗口重新开满。

**窗口不碰 collector**：它继续只投递 @，追问读全量库。两条路径各走各的，也就没有「collector 正卡在 170 秒长轮询里、改 `mention_only` 要等它返回才生效」的竞态。参数在 `config.json` 的 `followup` 段（`enabled` / `max_messages` / `idle_seconds`），`reply.py --no-followup` 单次关闭，`followup.py --show` 看当前窗口。回归测试在 `scripts/check_followup.py`。

### 闲时沉淀群友画像

没被 @ 的时候群里照样在聊，`chat_inbox` 里一条不落——闲笔先把每条消息入库，再决定要不要唤醒。素材一直都在，只是没人整理。

```bash
dossier.py --check              # 该提炼了吗（exit 0 = 该，3 = 还不用）
dossier.py --dump 60            # 取一批素材，最早的优先
dossier.py --digest             # 规则版消化：纯关键词，不调模型
dossier.py --mark --through <id> # 推游标
```

触发条件在 `config.json` 的 `dossier` 段：攒够 `burst_messages`（默认 80）直接触发；或者闲置超过 `idle_minutes`（默认 20）且攒了 `min_messages`（默认 25）。

两层：`--digest` 走关键词打标，零成本，每次超时都该跑；`--dump` → 提炼 → `consolidate_chat_memory` → `--mark` 是模型版，质量高但贵，只在 `--check` 说该做时才做。

群友记忆只通过 MCP 工具维护（`query_member_dossier` / `update_member_dossier` / `consolidate_chat_memory`），**不要直连 SQLite**。

### 人格卡

`water-group/personas/` 下是 Markdown，随包带 `little_whale`、`little_whale_v2`、`dragon_girl`，`template.md` 是骨架。私有卡放 `~/.water-group/personas/`（不进仓库），与公开模板同名时私有优先。写完把 `config.json` 里目标群的 `persona` 指过去。

人格卡只改变表达，不替换底层模型。核心要求写在模板里：1~10 字短句、打字感、不换行、禁客服腔、禁排比升华、禁括号内心戏。

## 目录结构

```
water-group/
├── start-bot.pyw        唯一启动入口（.pyw，无控制台，幂等）
├── stop-bot.pyw         停止入口（.pyw，与启动对偶，壳：全部逻辑在 up.stop_stack）
├── bootstrap.py         只读自检；检测到沙箱注入会拒绝启动
├── config.example.json  配置样板
├── daemon/              常驻进程与命令行工具
│   ├── up.py            整栈入口 + 总监护 + --status / --stop
│   ├── supervisor.py    拉起并重启子进程
│   ├── collector.py     常驻等消息，先落盘
│   ├── watchdog.py      QQ 保活（探真实应答，不探端口）
│   ├── db_keeper.py     钉住 SQLite WAL
│   ├── listen.py        监听（只活一轮）
│   ├── watch.py         阻塞式等唤醒（@ 走流水，追问走全量库）
│   ├── reply.py         回复：引用 / @ / 记账 / 默认续挂监听 / 开追问窗口
│   ├── followup.py      追问窗口状态（回复之后继续听群里聊）
│   └── dossier.py       群友画像沉淀
├── memory_engine.py     群友记忆与画像
├── memory_commands.py   指令语法解析
├── memory_format.py     L1 画像卡渲染
├── sync_groups.py       群列表 / 名片同步
├── personas/            人格卡（公开模板；私有卡在 ~/.water-group/personas）
└── logs/                运行日志（不入库）
```

运行时会生成的东西：本地数据根 `~/.water-group/`（`state/` 守护状态、`logs/` 全部日志、`data/` SQLite 库与媒体缓存、`tmp/` 临时目录、`rules/` 私有 Agent 规则、`personas/` 私有人格卡，均不在仓库内），仓库里只剩 gitignore 的 `imports/`、`water-group/config.json`、`.env`、`.cache/`、`tools/`。

## 状态文件

| 文件 | 含义 |
| --- | --- |
| `wg_events.jsonl` | 消息流水，每行一条，`id` 递增 |
| `wg_state.json` | 最近一次 wait 的返回，含 `input_batch.id` |
| `wg_session.txt` | 会话 id |
| `wg_ack.txt` | 已落盘游标（**不是**已回复游标） |
| `wg_replied.jsonl` | 已回账本，发送成功时写 |
| `wg_watch.txt` | `watch.py` 的扫描游标 |
| `wg_followup.json` | 追问窗口状态；文件不存在就是没开窗 |
| `wg_heartbeat` | 采集器最后一次完成循环的时间，供总监护判活 |

## 踩过的坑

这些都不是理论问题，是真让机器人哑掉过的：

| 现象 | 原因 |
| --- | --- |
| 桌面每 20~40 秒弹一个 `QQ has stopped working` | 栈是从带 DLL 注入的沙箱里起的，注入继承到 QQ，delay-load 找不到导出就自杀（`0xC06D007F`）。改用 `start-bot.pyw` 从 explorer 起 |
| `--stop` 之后重启，NapCat 离线、启动器毫无反应 | 老版本 `--stop` 没停干净，残留 `server_wg.py` / `collector.py` 占着 47860 / 48777，启动器按端口判幂等直接退出。现在 `--stop` 会扫树外残留并在 `stopper.log` 里报「⚠ 端口仍未释放」；看到这句就按 PID 精准清 |
| 反复 `资料读取失败` | SQLite WAL 竞态，`db_keeper.py` 没在跑 |
| `@ 了没反应`，`--list` 却显示 `[已回]` | 账本边界问题，见上面「连发的 @ 不会丢」 |
| `@ 了但群里没变蓝` | 三种：名字没解析（留成了文字）、那人不在本群（`--who` 会说）、或者消息走了 `send_chat_reply`。用 `get_msg` 回读，只有 `[CQ:at,qq=...]` 才算数 |
| QQ.exe 越攒越多 | 杀进程时没带 `/T`，只杀了主进程，renderer 和 network utility 还在占 profile |
| 守护日志显示心跳但进程是死的 | `up.py --status` 的「守护」行在 supervisor 死后仍会显示陈旧心跳，判据要看同一行的「运行中 / 未运行」 |
| NapCat 窗口 / 日志里中文全是 `锟斤拷` | 控制台代码页 936 上写的是 UTF-8。注入实例用 `CREATE_NO_WINDOW` 起：拿到的是**无窗口控制台**（`AttachConsole` 成功、`GetConsoleWindow()==0`），既不继承调用方的 936，也不会给它的子进程再开窗；`napcat.log` 是二进制追加的混编文件，读的时候用 `wg_logs.py`，别 `cat` |

排查先看这三处日志：

```bash
tail -20 ~/.water-group/logs/wg-supervisor.log    # 谁被重启了
tail -20 ~/.water-group/logs/wg_collector.log     # 采集是否在报错
tail -20 ~/.water-group/logs/napcat-watchdog.log  # QQ 是否被反复重注入
```

`napcat.log` 是混编文件：NapCat 写 UTF-8，`launcher-user.bat` 的 `echo`/`pause` 写 936。
整文件按任何一种编码读都会坏一半，`grep 中文` 也命不中 GBK 行，用逐行嗅探的读取器：

```bash
.venv/Scripts/python.exe water-group/daemon/wg_logs.py ~/.water-group/logs/napcat.log --tail 40
.venv/Scripts/python.exe water-group/daemon/wg_logs.py ~/.water-group/logs/napcat.log --grep 拒绝
```

## 已知限制

- **Agent 停止思考，机器人就静音。** 这是设计如此：回复是 Agent 写的，不是本地模型。宿主结束回合、退出、休眠之后，群里 @ 它不会有反应。想真正无人值守，得让 Agent 那边保持执行。
- **只发文字。** 表情、图片、语音这些能力在 MCP 层，命令行这条路没接。
- **`mention_only` 下不会主动接话。** 想让它参与闲聊，把 `trigger_mode` 改成 `natural`，噪音会一起上来。
- **同名歧义在 `find_profile` 层没解决。** `reply.resolve_member` 会报候选，但 MCP 的 `query_member_dossier` 遇到歧义仍会静默选第一个。
- **Windows only。** 保活、开窗抑制、控制台代码页都依赖 Windows 进程模型（taskkill / WMI / `CREATE_NO_WINDOW`），没有 Linux 版本。

## 许可与致谢

本项目自有代码采用 [MIT License](LICENSE)。不是腾讯官方产品，与 QQ、微信及模型服务商没有隶属关系。

- [Tulpa](https://github.com/fumingyang2004/Tulpa)：代码库的早期骨架，MCP 后端（`chatlocal/`）源自这里，MIT。
- [NapCat](https://github.com/NapNeko/NapCatQQ)：OneBot 11 实现，需独立安装并遵循其许可。
- [qq-bridge](https://github.com/Derpyu520/qq-bridge)：群聊行为与提示词设计参考。
- [QQ-agent](https://github.com/K0nd1us/QQ-agent)：表情收藏、语义笔记与候选反馈的设计参考。

给 Agent 看的操作手册在 [`AGENTS.md`](AGENTS.md) 与 [`.agents/rules/water_group.md`](.agents/rules/water_group.md)；守护进程的设计细节在 [`water-group/daemon/README.md`](water-group/daemon/README.md)。
