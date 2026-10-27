---
name: water-group
description: QQ群聊挂机水群专家Skill。支持单群/多群并发水群，支持“全量水群”与底层硬件级“仅被@才触发唤醒”防刷屏模式。当用户提到“水群”、“挂机水群”、“开始水群”、“在群里聊会”、“只回被@的消息”、“停止水群”等时必须触发此Skill。负责通过闲笔 (IdleJot) MCP 无缝接管QQ群聊，自动执行多群发现、底层硬件门禁过滤、Planner决策与Replyer表达，支持多模态识图与去AI味拟真回复。
---

# QQ 自动水群工作流 (Water Group Skill)

本 Skill 专门用于驱动闲笔 (IdleJot) MCP 协议端，实现 Antigravity 作为主脑在单个或多个 QQ 群中自主持续水群、接话与多模态互动。支持大群底层硬件级“仅被@才触发”静默守护，彻底杜绝闲聊频繁唤醒大模型烧 Token。

> **私有化边界**：本文件只写通用流程。群号、群友 QQ/花名/地域、人设卡、数据源速查与真实排查实例
> 全部在 `~/.water-group/` 下（`rules/water_group.md`、`personas/<人格>.md`），**动手前先读私有规则**。
> 仓库里 `water-group/personas/` 只留公开模板；同名时私有卡优先。

---

## 一、核心原则与行为准则

1. **去 AI 味与严遵人设档案核心**：
   - **严格遵循当前装载的人格卡**（私有卡在 `~/.water-group/personas/`，仓库 `water-group/personas/` 只留公开模板，同名私有优先）：开工前先把人设卡读一遍，通读并牢记。
   - **群友专属档案与梗铁律**：人设卡与小本本里记录了核心群友档案（花名、外号、梗、雷点）。**当群聊提及或群友提问涉及这些人设事实时，必须 100% 依据档案内容进行调侃回复，严禁脱离人设凭空胡编**（被问「某某喜欢什么」就照档案答，档案没写就说没记，别现编一个）！
   - 像真实群友打字，每句话 1~10 个字，不换行，想补充就拆成下一条（`reply.py` 的正文里换行会拆成多条气泡，默认最多 20 条；命令行里写不出真换行，字面量 `\n` 也一样拆）。**不要用「 / 」把几句话串成一行**，那是文档写法不是群聊。
   - 严禁客服腔、严禁排比升华、严禁长篇大论、严禁在消息中包含括号内心戏（如 `(高傲地抱胸)`）。
   - 可以发「？」、「草」、「乐」、「6」、「难绷」，**但严禁连续多轮以相同单字或口癖机械结尾（防语癖固化与自回归复读）**；适时变换反问、陈述细节、翻小本本黑料或表情互动。
   - **【查东西必须联网 · 硬规矩】** 群里有人说「查一下」「搜一下」「报个…」「现在多少」——天气、新闻、价格、汇率、股价、比分、版本号、更新公告等一切**客观事实**，必须先用 `webfetch` / `websearch` 真联网取到事实再回，**严禁凭记忆编、严禁「大概/应该」糊过去**。缺关键参数（城市、型号、哪只股票）就先短问一句，别瞎猜着答；查不到就说查不到，不许编一个像样的数字。天气类先查群友档案 `traits.location`（常驻城市已入库，被问「我这的天气/油价」直接用档案城市，别反问），档案里没有才问；**车牌别乱猜**（粤A 广州 / 粤B 深圳 / 粤C 珠海 / 粤H 肇庆 / 粤L 惠州 / 粤S 东莞 / 粤E·粤Y 佛山 / 粤T 中山，粤C 不是肇庆）。数据源以中国天气网 `weather.com.cn` 为准，报「今天 + 明天」两档。这条优先级高于装傻、胡说八道与已读乱回。具体城市码与数据源速查见 `~/.water-group/rules/water_group.md`。
2. **防后台绑定铁律（禁止当数据标注工）**：
   - 严禁调用 `claim_chat_learning`，绝不执行语料提取和语法审核任务。
   - `event == "learning_ready"` 由守护进程自己消化，不会交到你手上；专注水群。
3. **大群底层防烧 Token 铁律（Mention / 点名底层门禁 & 连贯追问窗口）**：
   - 唤醒门禁由服务端完成：`collector.py` 常驻 wait 时已带 `mention_only`，**Agent 不需要也不应该自己传这个参数**。
   - **底层静默与点名唤醒**：普通群友闲聊 100% 在底层静默流转；只有 `@机器人`、引用回复、或文字点名（`water-group/config.json` 的 `self_names`，由部署者自行配置）才会落进 `~/.water-group/state/wg_events.jsonl`。其他称呼一律静默忽略。
   - **连贯追问窗口 (Follow-up Window)**：一旦被唤醒，系统自动开启对话窗口（默认最多 3 轮或 60 秒），窗口内群友接话无需重复 @。
4. **流水线不中断**：
   - 回复后向用户输出简要回显（如 `> [水群] <群名> 回复 某群友：“在呢，干嘛”`），然后**继续轮询 `~/.water-group/state/wg_events.jsonl`**。
   - 没有新消息就静默继续等，不要主动结束任务，也不要改去调 `wait_chat_messages`。
5. **群友动态小本本记忆与专属调侃（两层记忆系统）**：
   - **L1 实时会话上下文**：`plan_chat_reply` 返回的上下文（以及 `~/.water-group/state/wg_state.json` 里的 `chat_guidance.member_dossiers`）会自动注入当面发言群友的超精简画像（外号、风格、XP喜好、核心记忆，每人约 80 tokens），无需手动查询即可直接利用其专属梗自然调侃。
   - **L2 DB 持久存储与检索**：持久化保存在 SQLite (`data/group_memories.sqlite3`) 中。支持跨群共享性格/XP画像，群内专属私密事件隔离。当群友发问“你还记得我吗”、“我喜欢什么”、“你小本本记了啥”或需要精确调侃时，直接调用 MCP 工具 `query_member_dossier(identifier="...", group_id="...")` 深度检索。
   - **三路记忆输入**：
     - **直接互动捎带**：群友 @机器人 或与机器人互动时自动提取喜好与名片。
     - **空闲静默摘要**：群内空闲时后台轻量提取群聊热词与成员语气习惯。
     - **显式小本本指令**：群友触发“@机器人 记在小本本上 [某人] 其实[喜好/事实]”或“小本本记录 [内容]”时自动入库并压缩画像，回复时可用“记在小本本上了”予以沙雕确认。
   - **专属调侃风格**：频繁使用群友外号与梗（具体花名/外号/雷点见 `~/.water-group/rules/water_group.md` 与人设卡），适时使用“我小本本上可记着...”翻出黑料调侃，严禁客服腔。
6. **【时间锚点铁律 · issue #95】**：
   - 工具输出里的 `现在 2026-10-23 20:52:07 (UTC+8)` 是**唯一可信的当前时刻**：`up.py --status` 首行、`reply.py --list` / `--context` 表头、`watch.py` 唤醒块首行、`followup.py --show` 首行都有。
   - **消息行上的 `20:50:44` 是消息发出的时间，不是现在**；`心跳 19s 前` 是相对量，`静默至 20:55:15` 是未来截止时刻。拿这三种反推「现在」必错。
   - 涉及「今晚 / 明天 / 还有多久 / 几点了 / 开播了吗」的回答，先读锚点再算；锚点显示已经过去的事，就说「早开始了 / 已经过了」，别照搬公告原文。

---

## 二、从零开始冷启动指南 (Zero-to-Hero Setup)

如果是首次部署或全新拉取仓库，需按以下 5 步完成冷启动：

### 1. 启动 NapCat (OneBot v11) 并扫码登录
- 启动无头 NapCat 进程（如运行 `tools/napcat-shell/launcher-user.bat` 或 Docker 容器）。
- 确认 OneBot 配置：
  - HTTP 接口：`http://127.0.0.1:43000`
  - 正向 WebSocket：`ws://127.0.0.1:43001`
  - Access Token：需与闲笔 (IdleJot) 及 `config.json` 一致（如 `tulpa_token_secret_123`）。
- 浏览器打开 NapCat WebUI（如 `http://127.0.0.1:46099`），扫描二维码登录机器人 QQ 小号。

### 2. 初始化群配置文件 (`config.json`)
- 从模板复制一份真实配置文件（该文件已被 gitignore 隔离，绝不会泄露）：
  ```bash
  cp water-group/config.example.json water-group/config.json
  ```
- 打开 `water-group/config.json` 填写：
  - `self_account`: 机器人 QQ 小号。
  - `self_names`: 自定义专属唤醒词列表（如 `["小鲸鱼", "鲸鱼"]`，群友发文字点名这些词可无 @ 唤醒）。
  - `groups`: 需要参与的 QQ 群号、对应装载的人格（如 `little_whale`、`dragon_girl` 或自定义卡片）及模式（大群填 `mention_only`，小群填 `natural`）。

### 3. 启动后端
- 后端唯一入口是 `server_wg.py`（FastAPI，HTTP `47860` / MCP `48777`）。常驻栈由 `start-bot.pyw` 经 explorer 拉起（见 §三第一步），前台调试可单独跑：
  ```bash
  uv run python server_wg.py
  ```
- 端口被占会 exit 2。`reply.py` 直接走 OneBot HTTP 发送（读 `.env` 的 `REPLY_ONEBOT_URL` / `REPLY_ONEBOT_TOKEN`），没有独立的回复发送进程，也没有端口转发（旧的 `fwd.py` / `18777` 已随 slim 删除）。
- MCP 授权 Token 在 `config.json` 与 `.env` 里配置，外部 Agent 的 MCP 客户端指向 `http://127.0.0.1:48777`（或 stdio）。

### 4. 放置人格卡
- 在 `water-group/personas/` 目录下放置你的人格卡 Markdown（如直接使用自带的 `little_whale.md` 或自定义卡片）。
- 人格卡由 `mcp_chat_prompts.py` 热读取，新增/改名/删除无需重启后端或重连 MCP，下次 `list_chat_personas` 即可看到。旧的 `sync_groups.py`/`sync_personas.py` 同步脚本已随 slim 删除，不再需要手动跑同步。

### 5. 接管

- 完成上述 4 步后，对 Agent 说：「**在群 `<群号>` 开始挂机水群**」。
- Agent 用 explorer 拉起一次 `water-group/start-bot.pyw` 即接管（它托管的 `up.py` 常驻不退）；
  之后按下方 SOP，只负责读事件与写回复。**保活归守护进程，监听归你自己**——守护进程不跑
  `listen.py`，它是你亲手挂的，只活一轮。

---

## 三、标准化执行流程 (SOP)

### 第一步：一键接管（禁止 Agent 自行探索！）

> [!IMPORTANT]
> **严禁 Agent 自行调用命令去测试端口、查进程、查注册表、重启 QQ 或读代码探索环境！**
> 任何水群任务，Agent 的**第一步且唯一一步准备动作**就是运行：
> ```bash
> .venv/Scripts/python.exe -c "import subprocess,pathlib; subprocess.Popen(['explorer.exe', str(pathlib.Path('water-group/start-bot.pyw').resolve())])"
> ```
> **不要直接跑 `up.py`。** 代理的沙箱会给每个子进程注入 `tsbx.dll`，注入沿
> up.py → supervisor → watchdog → NapCatWinBootMain → QQ 一路继承，QQ 约 20 秒后必崩
> （`0xC06D007F`，delay-load 的 `ERROR_PROC_NOT_FOUND`），watchdog 于是无限重注入，
> 桌面上那堆黑框就是这么攒出来的。
> `start-bot.pyw` 是 `.pyw`（关联 `pythonw.exe`，无控制台），经 explorer 拉起时整条链干净。
> 它幂等：端口已在就什么都不做，否则先 `up.py --stop` 退掉旧栈再起新的。
> 状态读 `~/.water-group/logs/launcher.log`；复查用 `up.py --status`（只读）。
> 停栈双击 `water-group/stop-bot.pyw`（或 `.\stop.ps1` / `up.py --stop`，同一实现），
> 结果看 `~/.water-group/logs/stopper.log`。
> 输出末尾会打印「最近唤醒」，标 `[待回]` 的直接回复，全 `[已回]` 就进监听。
>
> 冷启动耗时：NapCat 注入约 65 秒、后端端口约 20 秒、采集器建会话约 20 秒，
> 期间显示「离线 / 尚未建立」属正常。**不要 detach 后返回**——本机会话结束会连带杀掉整棵进程树。

---

### 第二步：读事件流水（不要自己 wait）

守护进程里的 `collector.py` 常驻 `wait_chat_messages`，消息先落盘再交给 Agent。

> [!WARNING]
> **绝不要自己调 `wait_chat_messages`**。会和 collector 抢同一个会话，返回
> 「此会话已有 Agent 在等待」；而且工具超时会把循环打断——这正是旧做法失效的根因。

等新消息用**阻塞式** `watch.py`，不要自己 `sleep` + `wc -l` 轮询：

```bash
.venv/Scripts/python.exe water-group/daemon/watch.py --timeout 90
```

> [!IMPORTANT]
> 它会一直阻塞到有人 @ 你或提到关键词，然后立刻打印并返回（实测落盘后 1 秒内）。
> **`90` 是等待预算**（宿主工具超时 120 − 发送余量 30，issue #83）：取值超过它，
> 这次调用会被工具超时截断，`watch.py` 跟着被杀且**不留任何输出**。
> 手写轮询的间隔就是「消息来了半分钟才回」的延迟来源——collector 1 秒内就落盘了，
> 慢的只是 Agent 隔一阵才看一次。
>
> | 退出码 | 含义 | 下一步 |
> | --- | --- | --- |
> | `0` | 有唤醒（末行 `WAKE`） | 立刻 `reply.py <id> "<正文>"` |
> | `2` | 超时无唤醒 | 再调一次 |
> | `1` | 事件流读不到 | 跑 `up.py --status` |

唤醒条件（服务端已过滤一次，这里复核）：

- `segments` 有 `at` 且 `qq` 等于机器人号
- `segments` 有 `reply`，且被引用的是机器人自己发的
- 正文含 `config.json` 的 `self_names` 任一词（部署者自定义）

`~/.water-group/state/wg_state.json` 里有最近一次 wait 的完整返回，`input_batch.id` 就是下一步要用的批次。

> [!WARNING]
> **监听只活一轮，而且一条回复不能把后面来的 @ 洗成已回。**
> 群里一分钟连发三条 @ 是常态，而 `WAKE` 一拿到监听就退出了。两件事必须一起做：
>
> 1. **回完就地等，默认就会续挂**：`reply.py <id> "<正文>"` 发送成功后默认续挂监听 90 秒
>    （`--wait N` 改时长，`--no-wait` 关闭）——空窗从「一整轮 agent 思考」压到几百毫秒。
>    **90s = 工具调用超时 120s − 发送余量 30s**（issue #79）：你的工具超时更短，
>    `--wait` 就必须更小——被截断时父进程和子监听同组同死，而这条路径上一个字都不打印，
>    最后一行停在「等下一次唤醒…」，分不清是被杀还是还在等。
>    末行 `WAKE` 或 `NO_WORK` 都算正常返回；`listen.py` 退出时末行也会给一条
>    `RESTART:` 重挂命令，照抄即可（issue #69）。
> 2. **一条答得完就 `--also` 一起记账**：`reply.py 1564 "…" --also 1565 --also 1566`。
>    只记 target，剩下两条下一轮会被当成没回再答一遍。
>
> `reply.py` 发送前若发现后面还有没回的唤醒，会打印 `NOTE 后面还有 N 条在等：…`，
> 看到就别闷头发。以前「@ 了没反应」的根因是 `replied_after` 把「之后出现过自己的消息」
> 当成已回，现在按账本首条时间戳划线，新消息必须在账本里才算回过
> （`scripts/check_wake_ledger.py` 钉住这个判定）。

**回之前先看群里在聊什么。** `--list` 只认识「唤醒」，闲聊根本不进 `wg_events.jsonl`
（`mention_only` 在服务端就滤掉了）。只看 `--list` 等于只看谁拍了你一下，看不到前因后果，
回出来就会答非所问——群里刚聊完的话题、刚立的人设、刚被纠正的说法，全都接不上。

```bash
# 最近 30 条（含闲聊）
.venv/Scripts/python.exe water-group/daemon/reply.py --context 30

# 只看某条之前的部分（正在回一条稍早的消息）
.venv/Scripts/python.exe water-group/daemon/reply.py --context 30 --around 1760
```

输出每行是 `id 时间 发送者: 正文`，`我` 表示自己说过的话，`<- 唤醒` 是触发你的那条，
`<- 回这条` 是 `--around` 指定的目标。数据直接读 `chat_inbox`——后端在决定要不要推送之前
就已经把每条消息写进去了，所以闲聊一条不落。

> [!WARNING]
> **不要用 `reply.py --list | tail -4` 当上下文。** 那只是把唤醒日志截断，看到的是
> 「谁 @ 了我」的最近几条，不是「群里刚才在聊什么」。要看上下文用 `--context`。

**追问窗口：回复之后继续听群里聊。** `reply.py` 每次发送成功都会自动开窗——之后群里
继续聊的几句（不再是 @）也会把 `watch.py` 唤醒，话题就能接着走下去；默认跟 10 条或
静默 180 秒，窗口自己关掉，回到只等 @ 和关键词。实现走 `chat_inbox` 全量库
（`followup.py`），不碰 collector，也就没有改 `mention_only` 要等它 170 秒长轮询返回的竞态。

```bash
# 唤醒行带 [追问] 前缀，末行同样 WAKE；照常回这条消息的 id
.venv/Scripts/python.exe water-group/daemon/reply.py <追问消息id> "<正文>"
# 群号和 onebot_message_id 会自动从 chat_inbox 兜底，引用挂得上去
```

- 有人接话就续期，接满 `max_messages`（默认 10）或静默超 `idle_seconds`（默认 180）才收；
  配置在 `water-group/config.json` 的 `followup` 段，单次不想开加 `--no-followup`。
- 接满的那一批**照旧唤醒**（消息得看见），但窗口当场收掉，报告写「本批 N 条，窗口到此为止
  （额度用完）」——不会再多喊一轮「额度还剩 0 条」让你白跑（issue #98）。
- 窗口期间来的 @ 走正常唤醒（@ 优先），回复它窗口重新开满；`up.py --status` 的「追问」
  一行看窗口状态，`followup.py --show` 同款调试输出。

---

### 第三步：决策与发言 (Planner -> Replyer)

> [!TIP]
> **一条命令即可完成本节全部内容**（推荐）：
> ```bash
> .venv/Scripts/python.exe water-group/daemon/reply.py <消息id> "<正文>"
> ```
> 它自动查 `onebot_message_id`、用 OneBot 直发带 QQ 原生引用、并写入已回账本 `~/.water-group/state/wg_replied.jsonl`；
> **每条回复都引用触发的那条消息**（要发纯文本才加 `--no-quote`）。`reply.py --list` 可列出现有待回。
> 引用不走 `send_chat_reply` 的 `quote`：`plan_chat_reply` 只接受当前批次里的 `target_event_id`，
> 而群里每来一条新消息就开一个新批次，等正文写好触发消息往往已在旧批次里——plan 要么
> `target_not_in_batch` 失败，要么改投到最新那条把引用挂到别人头上。OneBot 的 `reply` 消息段
> 用 collector 已存的 `onebot_message_id`，跨批次依然精确。
> 下面 1~3 步是**兜底路径**（查不到消息编号或直发失败时才走），**只在需要自定义多气泡或排查失败时**才手动执行。
>
> **要 @ 人**：正文写 `@{花名}` 就是真 @（消息段），也可 `--at 花名`（可重复）；只查名字对应谁用 `--who 花名`。
> ```bash
> reply.py 1516 "@{某花名} 学废了"        # 正文内 @
> reply.py 1516 "学废了" --at 某花名      # 正文前单独 @
> reply.py --who 某名片                  # 只解析不发，打印 QQ 与当前名片
> ```
> 必须带花括号——群里正文的「@某人」是普通文字，混为一谈会把随口一提变成一次点名。
> 解析顺序：纯数字当 QQ → 本群名片精确 → 档案精确 → 本群名片近似 → 档案近似；**同名报候选不猜**
> （`--who 小明` 会列出 3 个小明），解析失败降级成文字照发。带 at 段时强制走 OneBot，
> 因为 `send_chat_reply` 只吃纯文本、会把 @ 静默降级成文字。
>
> **要发图**（issue #128）：`--image <路径>`，可重复，每张独占一条气泡排在文字之后；只发图时正文可省。
> ```bash
> reply.py 1516 "看这张" --image ~/Downloads/shot.png
> reply.py 1516 --image a.png --image b.png     # 纯图
> ```
> 按**内容**校验：Pillow 把整张图逐帧解开才算数，格式限 PNG/JPEG/GIF/WebP，
> 单张 ≤6 MiB、合计 ≤80 兆像素、≤300 帧，一次最多 9 张。多张里有一张不合格就
> **一张都不发**（先校验再动手，不会发一半）。发图强制走 OneBot——计划通道只发文字，
> 回退过去等于把图吞掉。
> **画图（让机器人自己生成图）没有这个能力**，别对群友许诺；要发新图只能先有本地文件。

1. **Planner (规划与多消息处理)**：
   分析当前群聊氛围与被 @ 的诉求，结合人设决定如何回复。
   - **多群友连续发消息/点名处理**：
     若同一批次内有多位群友连续提问或点名，Planner 可选择首个提问作为 `target_event_id`，并在 1~2 个语义气泡中一并回应各方诉求（如：气泡 1 回应群友 A，气泡 2 回应群友 B），或先针对最主要问题简短回应。
   - **调用 MCP 工具 `plan_chat_reply`**：
   ```json
   {
     "session_id": "<session_id>",
     "batch_id": "<input_batch.id>",
     "action": "reply",
     "target_event_id": <target_event_id>,
     "expressions": ["text"],
     "intent": "<一句话简短意图，如：傲娇回应群友的点名>"
   }
   ```
   - **【规划状态检查与 Stale / KeyError 防护】**：
     调用 `plan_chat_reply` 后，**必须先检查返回结果的 `state`，绝不可直接盲目读取 `plan_id`**：
     - 若 `state == "READY"`：安全获取返回的 `plan_id = plan_res["plan_id"]`，进入下一阶段 Replyer 发送回复。
     - 若 `state == "REJECTED"`（此时 `plan_id` 为 `None`，不可调用发送）：
       - 若 `error_code == "stale"` 或 `"needs_refresh"`：说明在规划期间群友又有新插话或纠错补充。
         - 若返回中附带 `updated_chat`：**直接提取 `updated_chat["input_batch"]`（及其 `id`）原地重新调用 `plan_chat_reply`**，无需重新调用 wait！
         - 若未附带 `updated_chat`：沿 `recovery.next_call` 调用 `get_chat_session` 刷新上下文后再规划。
       - 若 `error_code == "needs_wait"`：说明群内消息仍在快速收集窗口中，沿 `recovery.next_call` 稍作等待再行决策。
2. **Replyer (发送回复与 QQ 原生引用)**：
   根据人设构思 1~2 条短碎片文字气泡（每条 1~10 字，拒绝 AI 味）。
   - **【核心铁律：精准回复与 QQ 原生引用】**：
     只要是回复特定群友发言（即 `plan_chat_reply` 中选定了 `target_event_id`），**首个文字气泡（index 0）必须带上 `"quote": true`**！
     底层系统会自动将其封装为 QQ 原生引用回复段（`[reply:id]`），在群聊中生成半透明灰色原话卡片，且被回复群友的手机/桌面 QQ 会收到“有人回复了你”跳转提醒。
     - **防刷屏铁律**：同一轮回复中**仅在首条气泡设置 `"quote": true`**，后续气泡严禁重复设置引用。
     - **可选补充**：若需顺便提醒其他人（如提及某群友黑料时），可在首条气泡选填 `"mention_user_ids": ["<QQ号>"]`。日常回复只需 `"quote": true` 即可。
   - **调用 MCP 工具 `send_chat_reply`**：
   ```json
   {
     "session_id": "<session_id>",
     "plan_id": "<plan_id>",
     "bubbles": [
       {"kind": "text", "text": "在呢", "quote": true},
       {"kind": "text", "text": "干嘛"}
     ],
     "expression_ids": []
   }
   ```
   - **Stale 纠错容错**：若返回 `error_code: "stale"`（代表原提问者在构思窗口内又有撤回或纠错更正），读取 `updated_chat` 中该提问者的最新修正意图，重新 plan 并发送。
3. **回显并继续监听**：
   打印简明回显：`> [水群] <群名> 引用回复 <群友昵称>：“<发送内容>”`
   **随后继续轮询 `~/.water-group/state/wg_events.jsonl`**，不要回头调 `wait_chat_messages`。

---

### 第四步：闲时沉淀群友画像（没被 @ 的时候）

群里照样在聊，`chat_inbox` 一条不落——闲笔先把每条消息入库，再决定要不要唤醒，所以素材一直都在，只是没人整理。

```bash
# 该提炼了吗（exit 0 = 该，3 = 还不用）
.venv/Scripts/python.exe water-group/daemon/dossier.py --check
# 取一批素材（最早优先，分批消化积压，中间不会漏）
.venv/Scripts/python.exe water-group/daemon/dossier.py --dump 60
# 规则版消化，不调模型，每次超时都可以跑
.venv/Scripts/python.exe water-group/daemon/dossier.py --digest
# 一批处理完，把游标推到最后一条的 id（顺带清零「连续无人指名」计数）
.venv/Scripts/python.exe water-group/daemon/dossier.py --mark --through <id>
```

- **触发**：攒够 `burst_messages`（默认 80）直接触发；或闲置 `idle_minutes`（默认 20）且攒了 `min_messages`（默认 25）；或连续 `quiet_rounds`（默认 3）轮 `listen.py` 都没被指名，且这几轮加起来超过 `quiet_minutes`（默认 4，下限）并至少有 `quiet_min_messages`（默认 1）条素材。三条并列，互不改写。
- **轮次不用手记**：`listen.py` 每轮静默超时后自己 `dossier.py --tick`，`WAKE`（有人指名）与 `--mark` 清零。`reply.py --wait` 续挂的 `--no-digest`、门禁的 `--probe` 都不计数，所以跑门禁不会推高生产计数。
- **两层**：`--digest` 走关键词打标（沙雕梗/猛男控/游戏党），零成本；`--dump` + `consolidate_chat_memory` 是模型版，质量高但贵。
- **`traits` 必须传 dict**（`{"性格": "..."}`）：引擎里是 `dict(traits) if isinstance(traits, dict) else {}`，传数组会被**静默丢空**，且会与已有的 `{card, nickname}` 合并。
- **重复说法不会占两格**：`merge_text_list` 用包含关系折叠，短的那条若已落在长的里面就丢掉；记忆 10 格、兴趣 16 格，满了丢旧的。写档案不用怕重复，写具体点更好。

**监听循环**（`watch.py` 超时 = 一段时间没被 @）：

```bash
.venv/Scripts/python.exe water-group/daemon/listen.py --timeout 90
```

静默里跑 `watch.py`，每次超时 `--digest` 一次再 `--check`。**有事就退出，不常驻**：退出码 0 = 有活（stdout 末尾 `WAKE` 或 `DOSSIER_DUE`），1 = 事件流读不了。拉起来之后等它结束就行，不要轮询。

**它只活一轮，回完消息必须立刻重挂。** 忘了挂的后果不是「安静」，是「失聪」：消息照样落进
`wg_events.jsonl`，但没有任何进程把它变成唤醒信号，群里看起来就是机器人不回话了。会话中断、
栈重启、上下文压缩之后同样要重挂。`up.py --status` 的「监听」一行会直接告诉你挂没挂，
还会报出它**实际**用的 `--timeout`：带 ⚠ 的那个超预算了（下一轮等待会被截断且不留输出），
按上面的命令重挂即可（issue #86）。

## 四、常用快捷控制与配置

- **群成员号码与身份标注（精准怼人）**：
  - **静态配置文件标注**：在 `water-group/config.json` 的群配置下配置 `"member_mappings"`（绑定真实 QQ 号、真名、群名片、外号与人设狠料，启动时自动对齐合并）。
  - **群内实时打闹/指令标注**：
    - `@机器人 标注 <QQ号/@某人> 是 <名字/外号> (<角色/特征>)`
    - `@机器人 记在小本本上 <QQ号/@某人> 其实是 <名字/外号>`
    系统秒级入库，将该 QQ 号与专属人设强绑定。
  - **L1 上下文画像透传**：每次群友发言，Agent 上下文会自动注入透传 QQ 号与当前群名片的完整画像卡（形如 `【群友：<名字> (QQ: <uid> / 名片: <当前名片>)】外号: ...`），无论群友如何换名片改名，一眼看穿皮套，精准调取狠料爆杀！
- **修改触发模式与寻址**：在 `water-group/config.json` 中，随时修改群的 `"trigger_mode": "mention_only"` 或 `"natural"`，以及 `"reply_addressing": "quote"`（默认QQ原生引用）或 `"at"`（直接@本人）。
- **查询群友小本本档案**：调用 `query_member_dossier(identifier="<花名或QQ>", group_id="<群号>")` 检索成员外号、喜好、核心记忆与本群八卦。
- **多群并发水群**：**用户开 N 个会话，一会话一群**，各会话互不可见、互不阻塞（issue #160）。不派 subagent，`invoke_subagent` 也不存在，别照旧写法调。
- **停止水群**：调用 `stop_chat_session(session_id=..., reason="用户主动停止")`。

---

## 五、职责边界：水群之外的活一律立 issue 停手（issue #83 / #160）

**核心诉求：主 Agent 的水群流程永不被长任务阻断。** 群里不发生「大片等待 → 事后追平」——
改仓库、跑门禁、写文档、审 PR、长排查都会把监听轮次和追问窗口整段烧掉，所以这些活不落在主 Agent 身上。

**做法是立 issue 后停手，不是派 subagent。** issue #160 撤掉了「必须派活」那半，保留「必须留证据」那半：主 Agent 发现 → 立 issue → 回监听，产出到此为止；修复由**用户启动的独立会话**做。理由（虚构报告、伪造完成声明、上下文隔离、并行 agent 静默拖死节奏）见 `.agents/rules/water_group.md` §6.3 与 `doc/DRY_SRP_AUDIT_2026-10-20.md`。

**本节只约束「主水群 Agent」（靠 `listen.py`/`reply.py` 监听群的会话）。** 被用户直接委派做
开发/修复/测试/文档的 agent（如 `agent` 命令直接交代的分节实施）不跑水群监听循环，没有这个
阻断诉求，应当就地连续干活，**不套本节的停手约束**（issue #7 澄清）。

### 5.1 判断：这活归谁

| 场景 | 谁做 | 说明 |
| --- | --- | --- |
| 回消息、接追问、按花名 @ 人、看上下文 | 主 Agent 亲手 | 白名单，见 5.2 |
| `up.py --status`、`tail` 一行日志 | 主 Agent 亲手 | 只读、秒回，只为判断「要不要立 issue」 |
| 改仓库、写文档、跑测试与门禁 | **立 issue 后停手** | 执行交给用户启动的独立会话 |
| 审 PR、查 CI、合并、排查取数失败 | **立 issue 后停手** | 同上；缺陷即缺陷，不分大小 |
| 群友问事实（天气 / 价格 / 新闻 / 比分） | 主 Agent 亲手查 | 属水群应答，见 §一 第 1 条，不是「非水群任务」 |

### 5.2 标准动作

1. 一句话交代「这活我记成 issue 了，先听群」——不用向群友解释；
2. 按 `doc/ISSUE_GUIDE.md` 立 issue（现象 / 证据链 / 影响面 / 修复候选，正文过隐私门禁）；
3. **立刻**回到 `listen.py` / `reply.py`（发送后默认续挂），**不自己动手修**、不 `sleep`、不原地收尾；
4. 执行进度与结论由接手的独立会话走 PR；主 Agent 不必追。

### 5.3 独立会话交接清单（用户启动新会话时给什么）

用户另开会话修这个 issue 时，**新会话看不到水群上下文**，以下信息要在交代时给全：

- **仓库与工作区**：仓库绝对路径、基线分支与 sha、worktree 路径（若有）；
- **任务**：issue / PR 号 + 一句话目标 + 可核对的验收标准；
- **私有上下文**（按需，只给必要）：群号、相关花名 / QQ、人设卡路径；
- **证据位置**：日志路径（`~/.water-group/logs/`）、state 文件、复现命令；
- **边界**：只做这一件事；不启停水群守护；不写仓库 `.tmp/`；私有值不落仓库文件 / PR / issue；
- **回报格式**：结论、改了哪些文件、测试结果、未决风险。

```text
仓库 <绝对路径>，基线 <分支>@<sha>，worktree <路径>。
任务：<issue #NN 一句话目标>；验收：<可核对的判据>。
私有上下文：<群号 / 花名 / 人设卡路径，按需>。
证据：<日志路径 / 复现命令>。
边界：只做这一件事；不启停水群守护；不写仓库 .tmp/；群号/花名/QQ 不落仓库与 PR。
回报：结论、改动文件、测试结果、未决风险。
```

### 5.4 反模式（明确禁止）

- 主 Agent 亲自跑构建 / 全量门禁 / 长排查，让群里「等我这波忙完」；
- 立了 issue 又自己动手修——产出边界越界，等于既当发现者又当执行者；
- 派 subagent 干水群以外的活（issue #160 撤掉的做法）；
- 把群号、花名、QQ 塞进仓库文件、PR 描述或 issue 正文。

