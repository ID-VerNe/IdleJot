"""追问窗口：回复之后继续听群里说什么，没人接话就收回。

`mention_only` 让 collector 只投递 @ 和关键词，所以「回复之后群里又聊了什么」
在 wg_events.jsonl 里根本不存在。但 chat_inbox 里有全部消息——后端在决定要不要
推送之前就已经把它们写进库了。追问窗口就走这条路：回复成功后开窗，watch.py 每轮
顺带查一眼窗口之后的新消息，有闲聊就地唤醒，让话题接着往下走；攒满
`max_messages` 条、或者静默超过 `idle_seconds` 秒没人接话，窗口自己关掉，回到只等
@ 和关键词的状态。

窗口期间不动 collector：它继续只投递 @，追问读的是全量库。两条唤醒路径各走各的
——@ 唤醒看 wg_events.jsonl，追问唤醒看 chat_inbox——互不覆盖，也就不会有
「collector 正卡在 170 秒长轮询里，改 mention_only 要等它返回才生效」的竞态。

**别把这里的窗口和 `chatlocal/mcp_chat_wait.py` 的 `dialogue_window` 当成一回事**
（issue #217）：那个是服务端 `wait_chat_messages` 的对话窗口，决定「这次 wait 交不
交付」，参数是 `followup_turns` / `followup_seconds`，状态落在 `chat_sessions` 的
`dialogue_until` / `dialogue_turns` 两列；本模块的窗口在守护侧，决定「watch.py 要不
要唤醒」，状态在 `wg_followup.json`。两者互不依赖，唯一的交集是都读 `chat_inbox`：
服务端跳批的消息只推游标、不置 `delivered`，所以本模块照样看得见它们。

`#` 指令不走 chat_inbox 的投递过滤（它是直接从库里捞的），所以 `consume()` 里还有
第二道闸门：`up.is_command()` 认出的指令不算接话——不扣额度、不唤醒、不续期，
但游标照常推进（跳过 ≠ 卡住，issue #70）。

第三道闸门管「@ 的是谁」（issue #138）：**只 @ 了别人、正文空白**的消息不是
「群里接着聊」——它一句话都没有，唯一的信息是「某人戳了某人一下」。以前它照样
唤醒，追问行又把 @ 段渲染成裸 `@ `，Agent 只能理解成「他在 @ 我」，于是答出
「你 @ 我，是想让我……」这种认错人的话。判据用入库的 `segments`（`mention_info`），
不是 `content` 文本——`content` 里的 @ 已经被人名替换过，看不出指向。

状态文件（TULPA_WG_STATE 下）：
  wg_followup.json  当前窗口；文件不存在就是没开窗
字段：after_id（从这条之后开始看，通常就是刚回复的那条；**是 chat_inbox 的 rowid**，
不是 wg_events 的 id——两套编号会撞号，见 issue #203 与 ``_after_id_of``）、
not_before（发送开始的墙上时间，只作**预算标记**：它不给可见性划界，只决定哪些
接话扣额度，判据见 `sent_at()`）、session（所属会话，换会话即失效，防会话重建后
id 空间对不上）、opened_at、until（静默截止）、budget（还能跟几条，自己被自己
说的话不占额度）、max_messages、idle_seconds、wakes。

Usage:
  followup.py --show   打印当前窗口状态（调试用）
"""
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
import winconsole  # noqa: E402  (stdout 编码 + 控制台代码页，一次收口)
import wg_clock  # noqa: E402  (「现在」的唯一出口，issue #95)
import wg_event_ref  # noqa: E402  (id / qid 两套命名空间，issue #203)
import wg_images  # noqa: E402  (图片标记渲染，issue #35 追问行同款)
import wg_quotes  # noqa: E402  (引用原文渲染，issue #94)
import wg_config  # noqa: E402  (config.json 的唯一读取口，issue #164 PC)
import wg_event_ref  # noqa: E402  (事件 id 的唯一解析入口，issue #216)
import wg_text  # noqa: E402  (issue #169 PC：connect_ro 延迟导入门面)
from wg_paths import data_dir, state_dir
STATE = state_dir()
DB = Path(os.environ.get("IDLEJOT_WG_DB") or os.environ.get("TULPA_WG_DB", data_dir() / "mcp-access.sqlite3"))
CONFIG = wg_config.path()
WINDOW = STATE / "wg_followup.json"

# 跟多少、跟多久，都可以在 config.json 里改：旧键是顶层 followup 段，新键是
# plugins.followup.options（插件经 legacy_section 平铺读旧段，见
# commands._plugins_from）。**跟不跟（三态开关）归插件**，不归这个模块——
# 开关在 plugins/base.py 的 Plugin.enabled，本文件只负责窗口机件。
DEFAULTS = {"max_messages": 10, "idle_seconds": 180}


def settings():
    """Config 的 followup 段，缺项用代码默认值补齐。

    只服务**不经过插件**的直接调用方：``open()`` 的无参默认值、``--show``
    一类调试路径。运行期开窗参数由插件从归一后的 options 显式传进来
    （issue #4 PD2a），不走这个读者。
    """
    data = dict(DEFAULTS)
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw = cfg.get("followup") or {}
        for key in DEFAULTS:
            if key in raw:
                data[key] = raw[key]
    except (OSError, ValueError):
        pass
    return data


def load():
    try:
        data = json.loads(WINDOW.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # 写一半的脏文件可能是 list/str；watch.py 的循环没有 try 兜底，
    # 这种窗口宁可当没有，也不能把监听打死。
    return data if isinstance(data, dict) else None


def save(state):
    STATE.mkdir(parents=True, exist_ok=True)
    WINDOW.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def close():
    try:
        WINDOW.unlink()
    except OSError:
        pass


def active_session_id():
    """当前会话 id，读库不读 wg_session.txt——后者会滞后于会话重建。"""
    try:
        db = wg_text.connect_ro(DB, timeout=5)
    except sqlite3.Error:
        return ""
    try:
        row = db.execute(
            "SELECT id FROM chat_sessions WHERE active=1 ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        db.close()
    return row[0] if row else ""


def open(after_id, max_messages=None, idle_seconds=None, session=None, not_before=None,
         after_ref=None):
    """回复成功后开窗。after_id 通常就是刚回复的那条消息。

    ``after_id`` 是 **chat_inbox 的 rowid**，``since()`` 直接 `id>?` 用它——这一点
    改前就有，但谁也说不出来，因为调用方传的是事件流 id（issue #203 §2 方案 6）。
    现在 ``after_ref`` 显式说明它：``q<n>`` / ``qid:<n>`` / 裸数字**都**落到库
    rowid（裸数字在这里就是库口径，与 ``reply.py`` 的 target 不是同一套解释），
    由 :func:`_after_id_of` 统一转换；``after_id`` 保留给直接按 rowid 调的旧调用方
    （门禁夹具、``followup.py --show``）。给了 ``after_ref`` 就以它为准。

    not_before 是发送开始的墙上时间，用作**预算标记**：Agent 从拿到唤醒到真正把
    回复发出去，中间经常隔着十几分钟，这段积压的 id 都大于被回复的那条，只按 id
    扫会把它们全算成接话，一次吃光 budget、窗口开出来就被自己关掉（issue #29）。
    带上时间标记后，`consume()` 把 pre-reply 的接话识别出来——照常唤醒、照常推进
    游标，但不算额度（issue #117：以前 `since()` 在 SQL 层用 `at>=?` 过滤，这些行
    根本不返回，处理期间群里发的消息就永久从追问窗口里消失了）。

    故意不用「当前最新消息 id」当起点：发送过程中群里照旧在说话，拿最新 id 会把
    这几句吞掉。从被回复的那条往后看，它们才算「回复之后群里说的」。
    """
    cfg = settings()
    if max_messages is None:
        max_messages = int(cfg["max_messages"])
    if idle_seconds is None:
        idle_seconds = int(cfg["idle_seconds"])
    now = time.time()
    state = {
        "after_id": _after_id_of(after_id if after_ref is None else after_ref),
        "not_before": float(not_before or 0),
        "session": session or active_session_id(),
        "opened_at": now,
        "until": now + float(idle_seconds),
        "budget": int(max_messages),
        "max_messages": int(max_messages),
        "idle_seconds": int(idle_seconds),
        "wakes": 0,
    }
    save(state)
    return state


def _after_id_of(ref):
    """窗口游标的记号 -> chat_inbox 的 rowid（issue #203）。

    窗口游标的语义**只有一种**：库 rowid。``q7`` 是它的标准写法，``qid:7`` 是它的
    长写法，裸数字是改前就已经在用的写法（``reply.py`` 一直传裸数字），三者在这里
    一起落到同一个 rowid。**不**经 ``wg_event_ref.resolve_id``：那条链会先问事件流，
    而游标问事件流只会把「刚回复的那条 @ 唤醒」与「同号的闲聊」搅在一起。
    看不懂的记号返回 0（= 从头看），不让开窗本身把回复链路打挂。
    """
    import wg_event_ref  # noqa: PLC0415

    try:
        return wg_event_ref.parse(ref)[1]
    except wg_event_ref.BadRef:
        return 0


def stale(window, now=None):
    """窗口为什么该关了：None 表示还开着。"""
    if not window:
        return "gone"
    now = time.time() if now is None else now
    if window.get("session") and window["session"] != active_session_id():
        # 会话重建过，after_id 指的是旧会话的编号，接着看会对错人。
        return "session"
    if window.get("budget", 0) <= 0:
        return "budget"
    if now >= float(window.get("until") or 0):
        return "idle"
    return None


# 从 `content` 里认 `@<qq>`：只服务没有 `segments` 的老行/超限行。`content` 里的
# @ 已经被人名替换过（`mcp_chat.receive` 拼 `@<显示名>`，缺名字才是 `@<qq>`），
# 所以纯文本认不出「@ 的是谁」是常态——但那种形态一定带正文，判据用不上它。
AT_MENTION = re.compile(r"@(\d{5,})")


def mention_info(raw, segments, account):
    """一条消息的 @ 指向与可见正文：(body, at_self, at_others, has_content)。

    `segments` 是入库时的段清单（`chatlocal/mcp_chat.py` 的 `at_segment`），最权威：
    `at` 段带 `qq`（有显示名时还带 `name`）。老行、或 payload 超 20KB 被入库清空
    `segments` 的行没有它，退化成从 `raw` 认 `@<qq>`——那种形态下认不出的
    `@<显示名>` 一定带着正文，不影响「只 @ 别人、正文空白」这条判据（issue #138）。

    `has_content` 只看正文与非文本段（图片 / 语音 / 转发 / 引用…），**不看 @ 段**：
    `@<别人> ` 这种「@ + 一个空格」正是要拦的形态，而纯图片消息是 #35 要保的形态，
    两者混成一个布尔就会误杀一边。`self_account` 取不到时 `at_self` 恒假——一条只有
    `@<qq>`、没有正文的消息无论如何都不值得唤醒，@ 我的那条由 `[at]` 主链路负责。
    """
    account = str(account or "")
    at_self = False
    at_others = []
    parts = []
    media = False
    if isinstance(segments, list) and segments:
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            kind = seg.get("type")
            if kind == "at":
                target = str(seg.get("qq") or "")
                if target and target == account:
                    at_self = True
                else:
                    at_others.append(str(seg.get("name") or target or "?"))
                continue
            if kind == "text":
                parts.append(str(seg.get("text") or ""))
            else:
                media = True
    else:
        text = str(raw or "")
        for match in AT_MENTION.finditer(text):
            target = match.group(1)
            if target == account:
                at_self = True
            else:
                at_others.append(target)
        parts.append(AT_MENTION.sub("", text))
    body = " ".join("".join(parts).split())
    return body, at_self, at_others, bool(body) or media


def bare_at_others(row, account):
    """只 @ 了别人、正文空白 → 不是「在聊」，不唤醒（issue #138）。

    游标照常推进——跳过 ≠ 卡住，与 `#` 指令同一口径。
    """
    _body, at_self, _others, has_content = mention_info(
        row.get("raw") or row.get("text"), row.get("segments"), account)
    return not has_content and not at_self


def since(after_id, session, not_before=None, limit=50):
    """窗口之后的全部消息，含自己说的（调用方按 is_self 过滤要不要唤醒）。

    只捞 id、payload、media、at 四列，跟 `reply.recent_chatter` 同一份库同一套
    字段；区别只在它是往前看而不是取最近 N 条。`at` 一并取回，给 `consume()` 兜底
    ——判 pre/post-reply 用消息自己的 `ts`（`sent_at()`），`at` 只在 `ts` 缺失时
    顶上（issue #218）。`not_before` 不再在 SQL 层过滤（issue #117：处理期间
    群友发的消息被过滤掉就永久消失，`consume()` 的游标只推进它看得见的行，被过滤
    的行下一轮仍在 SQL 层挡掉）。

    `not_before` 参数保留（不改调用接口），现在只是预算标记的输入：pre-reply 行
    照常返回、照常唤醒，由 `consume()` 决定扣不扣额度，而不是在这里让它们从查询
    结果里消失。

    行数超过 limit 时循环分页读完：以前只取前 50 条，consume() 却把游标推到最后一
    行，第 51 条起会被静默跳过，之后再也不会被任何一轮看到。
    """
    if not session:
        return []
    try:
        db = wg_text.connect_ro(DB, timeout=5)
    except sqlite3.Error:
        return []
    limit = max(1, int(limit))
    sql = "SELECT id, payload, media, at FROM chat_inbox WHERE session_id=? AND id>?"
    params = [session, int(after_id or 0)]
    sql += " ORDER BY id LIMIT ?"
    out = []
    try:
        while True:
            rows = db.execute(sql, (*params, limit)).fetchall()
            if not rows:
                break
            for event_id, payload, raw_media, at in rows:
                try:
                    data = json.loads(payload)
                except ValueError:
                    continue
                try:
                    media = json.loads(raw_media or "[]")
                except ValueError:
                    media = []
                out.append({
                    "id": event_id,
                    "sender": data.get("sender") or "",
                    # 追问行也要能取到群号：别名指向按群取覆盖行并集（issue #30）。
                    "conversation_id": data.get("conversation_id") or "",
                    # 闸门用原始文本：text 已经 collapse 空白，会把 " #ping" 洗成
                    # "#ping"；而 commands.command_key 要求行首、不 strip（#70）。
                    "raw": data.get("content") or "",
                    "text": " ".join((data.get("content") or "").split()),
                    "ts": data.get("timestamp"),  # 毫秒，消息自己的时间（issue #218）
                    "at": float(at) if at is not None else 0.0,  # 秒，本地入库时刻
                    "self": bool(data.get("is_self")),
                    "synthetic": bool(data.get("synthetic")),
                    "recalled": bool(data.get("recalled")),
                    # 追问行和 @ 唤醒行一样要能看出引用原文（issue #94）。
                    "quote": wg_quotes.render_payload(db, session, data),
                    # 追问窗里收到的图也要有路径/补读提示，不能只剩裸 [图片]（issue #35）。
                    # 补读命令打 `q<n>`：追问行的 id 是库 rowid，与事件流 id 撞号时
                    # `--event <裸数字>` 会指到另一条消息的图（issue #203）。
                    "images": wg_images.render(
                        dict(data, id=event_id), media,
                        ref_name=wg_event_ref.format_ref(wg_event_ref.INBOX, event_id)),
                    # @ 指向只在段里（issue #138）：`content` 已被人名替换过，
                    # `consume()` 判「只 @ 别人、没有正文」要靠这一列。
                    "segments": data.get("segments") if isinstance(
                        data.get("segments"), list) else [],
                })
            params[1] = rows[-1][0]
            if len(rows) < limit:
                break
    except sqlite3.Error:
        pass
    finally:
        db.close()
    return out


def sent_at(row):
    """一条消息「开口」的时刻（秒）：`ts` 优先，`at` 兜底（issue #218）。

    两个时间来源不同、单位也不同，混用正是 #218 的根因：

    - `ts`：消息自己的时间，取自 `chat_inbox.payload.timestamp`，**毫秒**（OneBot
      v11 的 `time` 是整数秒，`chatlocal/mcp_chat.py` 入库时乘 1000），Unix 时间戳，
      时钟是 QQ 服务器的；
    - `at`：`chat_inbox.at`，**秒**，本地入库那一刻的墙上时间（入库写的是
      `received_at=time.time()`），与 `not_before` 同源。

    额度问的是「谁在发送开始之后开口」，只有 `ts` 答得上来：`at` 会把「发送开始
    之前发出、发送期间才收到」的消息算成接话，而多气泡回复在途的这几秒
    （`BUBBLE_GAP=1.2s` × 条数）里，群里说的话几乎全是这个形态——`max_messages`
    就这么被整段绕过去了（issue #218）。

    `ts` 缺失（老行、payload 超限被清空、手搓行）时退回 `at`：宁可多扣一次，也不能
    把一条接话算成 0 额度、让窗口无上限地跟下去。

    两个已知边界，写在这里免得下次再混：`ts` 是整数秒，边界有 ≤1s 的粒度残差，
    方向是「少扣」；本机时钟与 QQ 服务器时间若有偏差，边界整体平移（本机时钟是
    全局时间基准，见 issue #95）——拿 `at` 判看似免偏差，代价是整段发送在途全判错。
    """
    ts = row.get("ts")
    if type(ts) in (int, float) and ts > 0:
        return float(ts) / 1000.0
    return float(row.get("at") or 0)


def consume(window, rows):
    """消化一批查询结果：返回该唤醒的闲聊，并原地更新窗口。

    自己说的话、系统提示和 `#` 指令也推进 after_id——不然每轮都要重新扫过它们
    ——但不占 budget、不唤醒：机器人自言自语不算接话，`#` 指令归 commands.py
    （issue #70）。**只 @ 了别人、正文空白**的消息同理（issue #138）：它没有一句话
    可回，却会被当成「他在 @ 我」，Agent 照着答就是认错人。

    pre-reply 的闲聊（**开口**早于 `not_before`，Agent 处理回复期间群里说的话）
    **照常唤醒**、照常推进游标，但不占 budget——它们是处理期间漏掉的接话，得让
    Agent 看见（issue #117：以前在 `since()` 的 SQL 层过滤，处理期间发的消息永久
    从查询结果里消失）。`not_before` 从「可见性过滤」退化为「预算标记」：可见性交给
    `since()` 一次返回全量，预算只扣真正在发送开始之后**开口**的接话——判据取消息
    自己的时间（`sent_at()`），不是本地收到时刻（issue #218）。

    有人接话就续期（until 推后）；这一批把 budget 扣到 0 就**当场收窗**，不留到
    下一轮 stale()（issue #98：以前那批照样唤醒，报告还写着「额度还剩 0 条」，
    Agent 白跑一趟）。最后一批照旧全部返回——不唤醒等于吞消息，比多一次往返更糟。
    """
    if not rows:
        return []
    # 延迟导入：followup 现在由 followup_plugin 在插件注册期加载（commands
    # import 期），而那条链可能从 `python up.py --status` 起步——顶层互导会把
    # up 二次加载成 `__main__` 和 `up` 两份。
    import up  # noqa: PLC0415

    not_before = float(window.get("not_before") or 0)
    # @ 指向要问配置（谁是「我」）；问不到就恒假，见 mention_info 的说明。
    account = up.self_account()
    woke = [r for r in rows
            if not r["self"] and not r["synthetic"]
            and not up.is_command(r.get("raw") or r["text"])
            and not bare_at_others(r, account)]
    # 只扣发送开始之后**开口**的接话：pre-reply 是处理期间漏掉的，不该再算一次
    # 额度（issue #117）；它仍要唤醒，预算只是真正的接话上限。判据取消息时间而不是
    # 本地收到时刻——发送在途那几秒「早发出、晚收到」的话不算接话（issue #218）。
    chargeable = [r for r in woke if sent_at(r) >= not_before]
    window["after_id"] = max(r["id"] for r in rows)
    if woke:
        # 钳在 0：背着聊天记录回来时一批可能远超额度，那是「这段时间群里都在聊」，
        # 一次唤醒给全比拆成几轮更省事；额度只是窗口的上限，不是要透支成负数。
        window["budget"] = max(0, int(window.get("budget") or 0) - len(chargeable))
        window["until"] = time.time() + float(window.get("idle_seconds") or 180)
        window["wakes"] = int(window.get("wakes") or 0) + 1
    # 有没有唤醒都要落盘：整批都是 `#` 指令时 after_id 也必须推进，否则下一轮
    # 会把同一批重新捞出来（跳过 ≠ 卡住）。
    if int(window.get("budget") or 0) <= 0:
        # 额度用完：这一批就是最后一批，窗口立刻失效。留着它只会让下一轮再喊一次
        # 「额度还剩 0 条」（issue #98）。
        close()
    else:
        save(window)
    return woke


def wake_report(window, batch):
    """唤醒时给 Agent 看的那句报告（issue #98）。

    额度扣到 0 的那一批仍然是有效唤醒——它带着真实的接话，Agent 得看见；但报告
    不能再写「还剩 0 条」，那是自相矛盾的，读到的人只会以为窗口还有额度。
    """
    budget = int(window.get("budget") or 0)
    if budget <= 0:
        return f"[追问] 群里还在聊：本批 {int(batch)} 条，窗口到此为止（额度用完）"
    return (f"[追问] 群里还在聊：额度还剩 {budget} 条，"
            f"静默 {window.get('idle_seconds')}s 后收回")


def status_text():
    """``up.py --status`` 追问行的窗口部分；没开窗或已过期返回 None。

    不过三态门控——那是插件的事（``window_status`` 事件先问开关），这里只
    回答窗口本身的状态。``--show`` 有自己的格式，不复用这一句。
    """
    window = load()
    if not window or stale(window):
        return None
    until = window.get("until") or 0
    left = wg_clock.seconds_left(until)
    left_text = f"，还剩 {left}s" if left is not None else ""
    return (f"进行中（回复后还可跟 {window.get('budget')} 条，"
            f"静默至 {wg_clock.clock(until)}{left_text}）")


def _group_of(payload):
    """(group_id, onebot_message_id) from an inbox payload, or None.

    字段名兼容（`onebot_message_id` / `message_id`）归 `wg_event_ref`（issue #216）：
    这里只剩「只有 payload、没有行」时的薄封装，别再各写一份键名清单。
    """
    return wg_event_ref.group_of_payload(payload)


def lookup(event_id):
    """任意事件的 (group_id, onebot_message_id)，闲聊也找得到。

    reply.py 以前只查 wg_events.jsonl，而追问唤醒要回的消息是被 mention_only
    滤掉的闲聊，压根不在那里面。chat_inbox 有这些行，拿它兜底，引用才挂得上去。
    id 是 AUTOINCREMENT 的全局 rowid，跨会话唯一，不用再按会话过滤。

    issue #216：解析统一走 `wg_event_ref`——payload 缺字段时它回查
    `chat_sessions.conversation_id` 与 `chat_inbox.message_id` 列。改前这里只认
    payload 里的两个键，少一个就静默 `None`，而同一行 `--show` 读得出来。

    issue #203：这个 id 是**会话库 rowid**，所以只问库（``namespace=INBOX``）——
    拿它去问事件流，撞号时会如实返回别群那条，引用就挂错了。
    """
    ref = wg_event_ref.resolve_id(event_id, namespace=wg_event_ref.INBOX)
    return ref.target if ref else None


def latest_group_id():
    """当前会话最新一条消息所在的群；没有会话时返回空串。"""
    sid = active_session_id()
    if not sid:
        return ""
    try:
        db = wg_text.connect_ro(DB, timeout=5)
    except sqlite3.Error:
        return ""
    try:
        row = db.execute(
            "SELECT id FROM chat_inbox WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (sid,)).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        db.close()
    if not row:
        return ""
    ref = wg_event_ref.resolve_id(row[0], namespace=wg_event_ref.INBOX)
    return ref.group_id if ref else ""


def show():
    # 先给基准：「静默至 20:09:07」是未来时刻，没有现在就算不出还剩多久（issue #95）。
    print(wg_clock.now_line())
    window = load()
    if not window:
        print("追问窗口：未开窗")
        return 0
    reason = stale(window)
    if reason:
        print(f"追问窗口：过期（{reason}），下次监听路过时关掉")
    else:
        until = window.get("until") or 0
        left = wg_clock.seconds_left(until)
        left_text = f"，还剩 {left}s" if left is not None else ""
        print(f"追问窗口：进行中，从 qid={window.get('after_id')} 之后看，"
              f"还可跟 {window.get('budget')} 条，静默至 "
              f"{wg_clock.clock(until)}{left_text}")
    return 0


def main():
    winconsole.ensure_utf8_stdio()
    if "--show" in sys.argv[1:]:
        return show()
    print(__doc__.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
