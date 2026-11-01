"""`reply.py --context` 的读侧：回之前先看群里在聊什么（issue #31/#94/#163）。

`--list` 只认识唤醒——`mention_only` 在服务端就把闲聊滤掉了，`wg_events.jsonl`
里一条都没有。所以上下文只能从 `chat_inbox` 取，而后端在决定要不要推送之前
就已经把每条消息写进去了。

从 `reply.py` 拆出来的是同一条读链（`check_module_size` 的 1000 行红线，
与 `wg_gap` / `wg_send_failure` 同一套姿势）：

- :func:`active_session_id`：会话 id 以库里的 `chat_sessions.active` 为准，
  `wg_session.txt` 会在会话重建后落后；
- :func:`recent_chatter`：**每个**群成员说的话，不只是叫醒我们的；
- :func:`show_history`：从「我上次发言」到「这次发言」之间那段（issue #130）。它**不**
  自己再写一遍取数 SQL——按 QQ / 时间 / 上界过滤是 `recent_chatter` 的参数，
  边界披露读的是会话行的 `dropped_through`（`:func:`window_bounds`）；
- :func:`render_rows`：一行一条的渲染口径（引用原文、图片标记、人名指向、画像），
  `--context` 与 `--history` 共用，谁都不许再复制一份 `print`;
- :func:`show_context` / :func:`show_history`：各自的表头 + 共用渲染。

**边界披露只报原值**：`dropped_through` 是「被裁掉的最新的那条 rowid」，不是
「裁掉了多少条」。`chat_inbox.id` 是跨会话共享的 AUTOINCREMENT rowid，同一会话
里被挤掉的旧行也会推高它（与 `wg_events` 编号毫无关系），所以任何「本应有 N 条」
的算术都要另编一个口径——这里不做减法，只把库里那一列原样说出来，让读的人自己
判断「这段可能不全」。

`reply.py` 仍按 `reply.recent_chatter` / `reply.show_context` 再导出：门禁
（`check_reply_context` / `check_alias_visibility` / `check_quote_visibility`）
和 `--context` 入口一直这么调，换路径等于让判据盯着一个搬走的旧地址。
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import wg_clock  # noqa: E402  (「现在」的唯一出口，issue #95)
import wg_dossiers  # noqa: E402  (群友画像，issue #163)
import wg_event_ref  # noqa: E402  (id / qid 两套命名空间 + 来源标记，#203/#216)
import wg_images  # noqa: E402  (图片标记渲染，issue #27)
import wg_quotes  # noqa: E402  (引用原文渲染，issue #94)
import wg_text  # noqa: E402  (正文渲染：段占位不占额度，issue #32)
import wg_videos  # noqa: E402  (视频标记渲染，issue #200)
from wg_paths import data_dir, state_dir

DB = Path(os.environ.get("IDLEJOT_WG_DB")
          or os.environ.get("TULPA_WG_DB", data_dir() / "mcp-access.sqlite3"))

# 事件流的路径与读法也归这里（issue #130）：`--history` 要拿 `latest_group_id`
# 认群，而它读的正是这份文件。`reply.py` 撞着 1000 行红线（issue #11），
# 这条读链和 `--context` 一样是"从磁盘读到窗口"的一段，放回去就成了两份副本。
EVENTS = state_dir() / "wg_events.jsonl"


def read_events():
    """``wg_events.jsonl`` 的全部事件行，坏行跳过。

    坏行跳过的口径与 `collector` 一致：一条写了一半的行不该让整份账本读不出来。
    """
    rows = []
    try:
        for line in EVENTS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def latest_group_id():
    """最近一条唤醒来自哪个群；一条唤醒都没有时退回 ``plugins.emit("active_group")``。

    追问窗口里的闲聊不会进 ``wg_events``，所以事件流为空并不等于"没有群"，那一步
    得问全量库（DB 兜底不过三态开关，issue #4 PD2a）。
    """
    import plugins  # 延迟导入：这里只在兜底分支用到，别把插件层拖进纯读路径

    for row in reversed(read_events()):
        gid = wg_event_ref.group_of(row.get("conversation_id"))
        if gid:
            return gid
    return plugins.emit("active_group") or ""

# 一次读多少行的缺省值（``--history --limit`` 的默认值就是它，``reply.py`` 从
# 这里 import，不再各写一份）。与后端 ``chatlocal/mcp_chat.py`` 的保留条数对齐：
# 会话只留最近 1000 条，往回扫更多也拿不到（issue #130）。
CONTEXT_SCAN = 1000

# ``--history`` 最多往回翻几批（issue #130）。边界可能远在会话开头，但每批都要
# 真读一次库；翻满这么多批仍没找到边界，就按「窗口里没有上次发言」说出来，
# 不假装已经把整段读全。
_HISTORY_BATCHES = 8


def active_session():
    """``(session_id, dropped_through)`` for the session the collector holds.

    ``dropped_through`` is the newest ``chat_inbox`` rowid the backend has already
    rolled off this session (``chatlocal/mcp_chat.py`` keeps only the last 1000).
    It is a **raw value**, not a count: rowids are global AUTOINCREMENT, shared
    with every other session. Nothing here subtracts it into a "how many were
    lost" figure — that arithmetic needs a口径 this module does not own
    (issue #130).
    """
    try:
        db = wg_text.connect_ro(DB, timeout=5)
    except sqlite3.Error:
        return "", None
    try:
        row = db.execute(
            "SELECT id, dropped_through FROM chat_sessions"
            " WHERE active=1 ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        # 老库没有 `dropped_through` 列：退回只要会话 id，不能把「没这一列」
        # 当成「没有会话」——那会让 `--context` 整屏变成「读不到聊天记录」。
        try:
            row = db.execute(
                "SELECT id FROM chat_sessions"
                " WHERE active=1 ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if row:
                row = (row[0], None)
        except sqlite3.Error:
            return "", None
    finally:
        db.close()
    if not row:
        return "", None
    return (row[0] or ""), (row[1] if len(row) > 1 else None)


def active_session_id():
    """The session the collector is holding, read from the database.

    wg_session.txt can lag behind a rebuilt session, and the context query has to
    follow whichever session the messages were actually written under.
    """
    return active_session()[0]


def recent_chatter(limit, around=None, sender_id=None, since=None, until=None):
    """Every message the group sent, not just the ones that woke us.

    mention_only keeps ordinary chatter out of wg_events.jsonl (natural groups now
    let it in, #188, but mention_only groups still do not), so the daemon files
    cannot answer "what were they talking about just now". chat_inbox can:
    the backend stores each event before deciding whether to deliver it.

    Filters (issue #130) narrow the same read, they do not add a second one:

    - ``sender_id``: QQ as a string, matched against ``payload.sender_id``. Never
      the display name — names change and two people can share one;
    - ``since`` / ``until``: epoch **milliseconds**, inclusive on both ends
      (``payload.timestamp`` is ms, same unit as ``wg_events.jsonl`` — see
      ``wg_clock.clock_ms``);
    - ``around``: upper bound on the ``chat_inbox`` rowid (``q<n>``), as before.

    Note these are **not** applied in SQL: the sender and timestamp live inside the
    JSON payload, and filtering post-hoc keeps one statement (and one ordering口径)
    for every caller. That has a cost the caller must know about: the row cap is
    applied *before* the filter, so ``limit`` is a scan budget, not a result count.

    A time window plus a small scan is the dangerous combination — ``ORDER BY i.id
    DESC LIMIT 100`` then dropping everything older than ``since`` returns only the
    newest few rows and makes a wide window look almost empty (issue #130). So when
    ``since`` or ``until`` is given, ``limit`` is read as "how many rows of the
    window I want" and the scan is widened until it is satisfied or the session
    runs out of rows. ``sender_id`` alone keeps the plain scan: the boundary logic
    in ``show_history`` supplies its own upper bound instead.
    """
    # 延迟 import：reply.py 在模块级再导出本模块，顶层反向 import 会成环。
    # `session_id()` 读的是 reply 那边 `wg_session.txt` 的会话 id，是兜底。
    import reply

    sid = active_session_id() or reply.session_id()
    if not sid:
        return []
    try:
        db = wg_text.connect_ro(DB, timeout=5)
    except sqlite3.Error:
        return []
    # 会话号一并取回（LEFT JOIN，issue #219）：payload 缺 conversation_id 的行走
    # 会话行那一路；JOIN 会把这些行整批吞掉，所以要 LEFT。
    sql = ("SELECT i.id, i.payload, i.media, s.conversation_id"
           " FROM chat_inbox i LEFT JOIN chat_sessions s ON s.id = i.session_id"
           " WHERE i.session_id=?")
    params = [sid]
    if around:
        # 行号本身后来改打 `q<n>`，但 `around` 一直是库 rowid（issue #203）。
        # 这里只限**上界**：`i.id<=?` 取的是「q<n> 及之前的最近 N 行」，配上
        # `ORDER BY id DESC LIMIT N` 本身就够用，不需要再猜一个下界。
        # （issue #130：以前额外加了 `i.id>=around-limit`，那会让分批往回翻的第二批
        # 直接取空——下界是按*上一批*的锚点算出来的，一往下传就把更早的行全砍掉。
        # 砍掉下界反而更省事：`chat_inbox` 因全局 AUTOINCREMENT 留下的空洞本来就
        # 会让「编号差」高估真实行数，猜出来的下界保守到常常不含交叉点。）
        # 列名带 `i.` 前缀（issue #219 起 `SELECT` 用全限定名）。
        try:
            number = int(around)
        except (TypeError, ValueError):
            number = 0
        sql += " AND i.id<=?"
        params.append(number)
    sql += " ORDER BY i.id DESC LIMIT ?"
    sender = str(sender_id).strip() if sender_id is not None else ""

    def scan(rows_wanted):
        """Run the statement once; return (rows, exhausted).

        ``exhausted`` is True when the scan returned fewer rows than asked for,
        i.e. the session has no older rows to give — retrying with a bigger cap
        would only repeat the same work.
        """
        rows = db.execute(sql, params + [max(1, int(rows_wanted))]).fetchall()
        return rows, len(rows) < max(1, int(rows_wanted))

    wanted = max(1, int(limit))
    windowed = since is not None or until is not None
    cap = max(wanted, CONTEXT_SCAN) if windowed else wanted
    # 有时间窗口时「取满 wanted 条」才停：否则窗口被 SQL 的 LIMIT 先砍一刀，
    # 只剩最新几条，看起来像「这段时间群里没说话」（issue #130）。
    while True:
        scanned, exhausted = scan(cap)
        if not windowed or exhausted or cap >= CONTEXT_SCAN * 8:
            break
        kept = 0
        for _eid, payload, _media, _cid in scanned:
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            ts = data.get("timestamp")
            if not isinstance(ts, (int, float)):
                continue
            if (since is not None and ts < since) or (until is not None and ts > until):
                continue
            kept += 1
        if kept >= wanted:
            break
        cap *= 4
    out = []
    try:
        for event_id, payload, raw_media, session_cid in reversed(scanned):
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("synthetic"):
                continue
            if sender and str(data.get("sender_id") or "").strip() != sender:
                continue
            if since is not None or until is not None:
                ts = data.get("timestamp")
                if not isinstance(ts, (int, float)):
                    # 没有时间戳的行无法判断在不在窗口里：宁可漏也不要谎报在窗口内。
                    continue
                if since is not None and ts < since:
                    continue
                if until is not None and ts > until:
                    continue
            try:
                media = json.loads(raw_media or "[]")
            except ValueError:
                media = []
            out.append({
                "id": event_id,
                "sender": data.get("sender") or "",
                # QQ 是筛选的判据（issue #130）：花名会改、会撞，号码不会。缺
                # 这个键的行在 `--sender` 下被跳过，不会因为「名字对上了」混进来。
                "sender_id": str(data.get("sender_id") or "").strip(),
                "text": " ".join((data.get("content") or "").split()),
                "ts": data.get("timestamp"),
                "self": bool(data.get("is_self")),
                "recalled": bool(data.get("recalled")),
                # 群号：别名指向按群取覆盖行并集（issue #30）。payload 优先、会话行
                # 兜底，与 `followup.since` / `wg_event_ref.resolve` 同一口径
                # （issue #219：各读一次 payload，缺键就把整批读成跨群）。
                "conversation_id": wg_event_ref.row_conversation(
                    {"payload": data, "conversation_id": session_cid}),
                # 引用原文：payload 里只有编号，父消息在同一会话的库里就查得到（issue #94）。
                "quote": wg_quotes.render_payload(db, sid, data),
                # 图片标记让 Agent 知道这条能看图，以及是否已经落盘（issue #27）。
                # 补读命令必须打 `q<n>`：这行 q<n> 是库 rowid，`--event <裸数字>`
                # 在撞号时会去读事件流那条的图（issue #203）。
                "images": wg_images.render(
                    dict(data, id=event_id), media,
                    ref_name=wg_event_ref.format_ref(wg_event_ref.INBOX, event_id)),
                # 视频同理（issue #200）：上下文里群友发的小视频改前只剩裸 [视频]，
                # 补读口最常被用的正是这条入口。记号同样打 `q<n>`。
                "videos": wg_videos.render(
                    dict(data, id=event_id),
                    ref_name=wg_event_ref.format_ref(wg_event_ref.INBOX, event_id)),
            })
    except sqlite3.Error:
        pass
    finally:
        db.close()
    return out


def render_rows(rows, wakes=(), around=None, hot=None):
    """Print one line per message. Shared by ``--context`` and ``--history``.

    The line format is a contract, not a preference: ``q<n> src=chat_inbox …`` is
    what ``reply.py <记号>`` / ``--show`` / ``look.py --event`` parse back, and
    ``check_id_contract`` pins the round trip (issue #204). Two call sites must not
    drift apart, so the body lives here and callers only choose the header.
    """
    for row in rows:
        stamp = wg_clock.clock_ms(row["ts"])
        who = "我" if row["self"] else (row["sender"] or "?")
        # 上下文行的编号是 chat_inbox 的全局 rowid，与唤醒行的 wg_events id 会撞号
        # （issue #203）。行首、截断提示、补读命令三处打同一个 `q<n>` 记号，
        # `--around q<n>` / `--show q<n>` / `look.py --event q<n>` 都认它。
        ref = wg_event_ref.format_ref(wg_event_ref.INBOX, row["id"])
        # 上下文行同一份渲染口径（issue #32）：段占位不占额度，截断了说被切过。
        text, _, _ = wg_text.render_body(row["text"], wg_text.CONTEXT_BODY_CHARS,
                                         row["id"], name=ref)
        # 人名指向与唤醒行同款（issue #30）；自己说的话不指、不挂画像。
        tag, hits, cards = wg_dossiers.anchors(
            "" if row["self"] else row["sender"],
            "" if row["self"] else row["text"],
            wg_event_ref.group_of(row.get("conversation_id")),
            with_card=not row["self"] and row["id"] in (hot or wakes))
        marks = []
        if row["recalled"]:
            marks.append("撤回")
        if row["id"] in wakes:
            marks.append("唤醒")
        if around and row["id"] == int(around):
            marks.append("回这条")
        tail = f"  <- {'、'.join(marks)}" if marks else ""
        quote = ("  " + row["quote"]) if row.get("quote") else ""
        alias_tail = "".join("  " + h for h in hits)
        # 行首编号后面紧跟来源（issue #216 / #204 的同形契约）：这些 id 只活在
        # chat_inbox，`reply.py <id>` 与 `look.py --event <id>` 都按这个来源解析。
        # 编号本身打 `q<n>`（issue #203），与唤醒行的裸事件 id 在字面上就分得开。
        print(f"{ref} src={wg_event_ref.SRC_INBOX} {stamp} {who}{tag}: {text}{quote}{('  ' + row['images']) if row.get('images') else ''}{('  ' + row['videos']) if row.get('videos') else ''}{alias_tail}{cards}{tail}")


def show_context(limit, around=None):
    """Print recent chatter so a reply can follow the thread, not just the ping.

    A wake line says "someone @'d you". It does not say what the group had been
    talking about for the last twenty minutes, and replying off the ping alone is
    how a bot answers a question that was already settled three messages later.
    """
    import up

    rows = recent_chatter(limit, around)
    if not rows:
        # 读不到记录时「现在」更要给：Agent 至少知道该不该追问、隔了多久。
        print(wg_clock.now_line())
        print("读不到聊天记录：会话没建立，或数据库里还没有这批消息")
        return 1
    wakes = {e.get("id") for e in up.events() if up.wake_reason(e)}
    # 画像只挂在这两类行上：叫醒我们的那条、和 Agent 要回的那条。每条闲聊都挂
    # 一张卡，这一屏就从「在聊什么」变成「他们是谁」（issue #163）。
    hot = wakes | ({int(around)} if around else set())
    span = f"{around} 之前" if around else "最近"
    # 每行只有 HH:MM:SS，表头不带日期的话跨零点就没法判断「昨天还是今天」。
    # 表头念**实际取到几条**，不是入参 N：读侧有一次静默裁剪，`--context 2000`
    # 只拿到 1000 时旧表头照念「最近 1000 条…」之外的 2000，读者无从知道被截了
    # （issue #130 的复现命令 2）。超了就说超了，并给能拉长窗口的出口。
    head = f"# {span} {len(rows)} 条（含闲聊，不只是 @）· {wg_clock.now_line()}"
    if limit and len(rows) < int(limit):
        head += (f"；要了 {int(limit)} 条只取到 {len(rows)} 条"
                 f"（会话只留最近 {CONTEXT_SCAN} 条或更少）")
    print(head)
    render_rows(rows, wakes=wakes, around=around, hot=hot)
    return 0


# ---- 从「我上次发言」到「这次发言」之间（issue #130） ----------------------


def resolve_sender(value, group_id=None):
    """花名或 QQ -> ``(qq, label)``；认不出就 ``(None, 原因)``。

    名字走 ``wg_members.resolve_member``（按群别名 / 名片 / 昵称），号码原样
    认作 QQ。两条路都**不猜**：不唯一（本群有 N 个重名）、不认识、就是本机自己
    时返回原因是给调用方原样打出来的，不做「那就挑一个吧」的回落。
    """
    import wg_members

    raw = str(value or "").strip()
    if not raw:
        return None, "空名字"
    if raw.isdigit():
        # 纯数字按 QQ 认：花名是给人看的，号码才是判据。
        return raw, raw
    qq, label = wg_members.resolve_member(raw, group_id)
    if not qq:
        return None, label or "找不到这个人"
    return str(qq), label or raw


_TIME_FORMATS = (
    ("%Y-%m-%d %H:%M:%S", True),
    ("%Y-%m-%d %H:%M", True),
    ("%Y-%m-%dT%H:%M:%S", True),
    ("%Y-%m-%dT%H:%M", True),
    ("%H:%M:%S", False),
    ("%H:%M", False),
)


def parse_time(text, now=None):
    """``HH:MM[:SS]`` / ``YYYY-MM-DD HH:MM[:SS]`` -> ``(epoch 毫秒, 说明)``。

    返回**毫秒**，因为窗口的另一头是 ``payload.timestamp``，那个数是毫秒
    （``wg_clock.clock_ms`` 的入参口径）。混用秒与毫秒会让 ``--since 20:00``
    永远命中不到任何一行——窗口看起来「是空的」，其实是单位错了。

    没带日期的时间点按**最近一次出现**解释（now 往前找，找不到就跨一天再往前
    找一次）——「今晚 20:00 之前」在 21:00 说的时候指两小时前，不是明晚。这个
    解释必须被调用方打进表头：猜出来的窗口不写出来，读者就只能靠猜。

    认不出就抛 ``ValueError``，由 CLI 报错，不静默取「现在」。
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("空时间")
    # ``now`` 沿用 epoch **秒**（与 ``wg_clock.now_ts`` 同口径），换成本地
    # 时刻只在这一处、只为了做「最近一次」的日历比较。
    base = now if now is not None else wg_clock.now_ts()
    for fmt, has_date in _TIME_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if has_date:
            # 带日期的时间点按本地时区落到 epoch（行时间戳同样是本地时区的 epoch）。
            return int(parsed.timestamp() * 1000), f"{raw}（{fmt}）"
        current = datetime.fromtimestamp(base)
        for shift in (0, -1):
            guess = current.replace(hour=parsed.hour, minute=parsed.minute,
                                    second=parsed.second, microsecond=0)
            if shift:
                guess += timedelta(days=shift)
            # 不带日期的写法只会指向「现在或之前」的那一次。
            if guess.timestamp() <= base:
                when = guess.strftime("%Y-%m-%d %H:%M:%S")
                return int(guess.timestamp() * 1000), f"{raw} → {when}（按最近一次解释）"
        # 走不到：shift=0 在 now 之前（或就是 now）时命中，否则 shift=-1 必命中。
    raise ValueError(f"认不出的时间：{raw}（认 HH:MM、HH:MM:SS、YYYY-MM-DD HH:MM[:SS]）")


def window_bounds(since=None, until=None, before=None):
    """把 ``--since/--until/--before`` 合成一个上界 + 一句人话。

    ``until`` 与 ``before`` 同时给时取**更早**的那个，并说出来：两个上界各自
    限制不同的东西（时间 vs 行号），拍板规则不写出来，读者会以为两个都生效。
    """
    note = ""
    upper = until
    if before is not None:
        cut = _row_time(int(before))
        if until is not None and cut is not None and until <= cut:
            upper = until
            note = f"--until 与 --before 同时给了，取更早的 --until（{_hhmmss(until)} 早于 q{before}）"
        elif cut is not None:
            upper = cut
            note = f"--until 与 --before 同时给了，取更早的 --before（q{before} 在 {_hhmmss(cut)}）"
    if since is not None and upper is not None and since > upper:
        note = (note + "；" if note else "") + "下界晚于上界，窗口是空的"
    return since, upper, note


def _hhmmss(ts):
    """Epoch **milliseconds** -> ``HH:MM:SS`` (``wg_clock.stamp`` takes seconds)."""
    return wg_clock.stamp(ts / 1000) if ts is not None else "?"


def _row_time(row_id):
    """A ``chat_inbox`` rowid -> that row's timestamp (None if it is gone)."""
    try:
        db = wg_text.connect_ro(DB, timeout=5)
    except sqlite3.Error:
        return None
    try:
        row = db.execute("SELECT payload FROM chat_inbox WHERE id=?", (int(row_id),)).fetchone()
    except (sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        db.close()
    if not row:
        return None
    try:
        data = json.loads(row[0])
    except (ValueError, TypeError):
        return None
    ts = data.get("timestamp") if isinstance(data, dict) else None
    return ts if isinstance(ts, (int, float)) else None


def _own_qq():
    """This bot's own QQ as a string, or ``""`` when the config has no account."""
    import wg_members

    return str(wg_members.self_account() or "").strip()


def _bands(rows, boundary):
    """Split ``rows`` at ``boundary`` — the last row in ``rows`` that matches it.

    Returns ``(body, anchor)``: ``body`` is everything after the boundary (the
    stretch being asked about), ``anchor`` is that boundary message itself so the
    reader can see where the window starts (``[]`` when nothing matched).
    ``boundary`` is a predicate over a row.
    """
    for index in range(len(rows) - 1, -1, -1):
        if boundary(rows[index]):
            return rows[index + 1:], [rows[index]]
    return rows, []


def show_history(limit, sender=None, since=None, until=None, before=None):
    """Print the stretch between my last message and now, and say where it stops.

    The question is "从我上次发言到现在，群里都说了些什么" — so the answer is
    **everyone's** messages in that window, not the sender's own. That is why
    ``sender`` is the *boundary* and not a filter: without it the boundary is this
    bot's own last message (``payload.is_self``, or ``sender_id`` == our account);
    ``--sender`` moves the boundary to somebody else and says so in the header.

    What this cannot do is invent messages that were rolled off the ring buffer.
    ``dropped_through`` is printed raw when the window might reach into it; the
    caller is told the window may be incomplete instead of being handed a
    confident-looking summary of half a conversation.
    """
    import up

    # 上界按行号/时间收窄，下界留给「上次发言」那条来定：边界本身就是取数结果，
    # 先拿 SQL 卡时间再找它会漏掉边界之后、上界之前的正常消息。
    since_ts, until_ts, note = window_bounds(since=since, until=until, before=before)
    scan = int(limit) if limit else CONTEXT_SCAN
    # 边界是谁：显式 --sender 用它自己，否则用本机的 QQ（认不出身份时退回 is_self）。
    who = sender or _own_qq()
    if who:
        labelled = f"QQ {who}"
        boundary = lambda r: r["sender_id"] == who
    else:
        labelled = "本机的发言（配置里没有账号，按 is_self 认）"
        boundary = lambda r: r["self"]
    # 取数口径（issue #130）：`recent_chatter` 只会「最近 N 行」，于是
    # `--sender <别人>` 只扫到最新几条，边界（别人的旧发言）根本不在扫描范围里，
    # 整段窗口被误报成「只有 1 条」。这里按**批次**往回翻，每一批都把上界抬到
    # 已取到的最老那行之前，直到在某一批里找到边界，或会话见底、翻够扫描上限。
    # 边界只在**最新**的那一批里定一次：更早的批次只会给出与本题无关的旧边界
    # （`_bands` 取的是批内最后一条匹配行，所以每批至多贡献一个边界）。
    # 边界自己独占 `anchor`，`collected` 只收边界之后的行——两者不同时持有同一行，
    # 否则工作窗口（`--limit` 小于边界之前那一段）会把边界行重复打印一遍。
    # `cursor` 每批都往下传：`recent_chatter` 的 `around` 只限上界，所以第二批
    # 才真能翻到更早的行（issue #130 修过一次下界把第二批砍空）。
    collected = []
    anchor = []
    cursor = before
    for _ in range(_HISTORY_BATCHES):
        batch = recent_chatter(scan, around=cursor, since=since_ts, until=until_ts)
        if not batch:
            break
        body, found = _bands(batch, boundary)
        collected = body + collected
        if found:
            anchor = found
            break
        oldest = batch[0]["id"]
        if cursor is not None and oldest >= int(cursor):
            break
        cursor = oldest
        if len(batch) < scan:
            break
    rows = anchor + collected
    _sid, dropped = active_session()
    if not rows:
        print(wg_clock.now_line())
        print("这个窗口里没有任何消息：会话没建立、消息被裁掉了，或筛选条件没命中")
        if note:
            print(f"# {note}")
        if dropped is not None:
            print(f"# 保留上限：chat_sessions.dropped_through={dropped}（已裁掉的最新 rowid）")
        return 1
    wakes = {e.get("id") for e in up.events() if up.wake_reason(e)}
    # 工作窗口：`--limit` 要的是「边界之后最近 N 行」，不是「含边界的 N 行」。
    # 超额时从**最老**那头砍——问的是「到现在」，最近的那几条才是要读的。
    # 边界行本身不算进额度，砍完还留在最前面，读者仍看得出窗口从哪开始。
    body = collected[-int(limit):] if (limit and len(collected) > int(limit)) else collected
    kept = len(anchor) + len(body)
    shown = anchor + body
    hot = wakes | {rows[-1]["id"]}
    # 段首段尾都打出来：message 自己的 HH:MM:SS 不带日期，跨天时就得靠这两头。
    # 时间戳是毫秒，`wg_clock.stamp` 收秒（issue #130 的单位口径）。
    head = (f"# 窗口 {_hhmmss(shown[0]['ts'])} → {_hhmmss(shown[-1]['ts'])}"
            f" · {len(body)} 条{'' if anchor else '（窗口里没有上次发言）'}"
            f"{'（另含边界行本身 1 条）' if anchor else ''}"
            f" · 含闲聊，不只是 @ · {wg_clock.now_line()}")
    print(head)
    bits = [f"边界：{labelled}"]
    if since is not None:
        bits.append(f"起 {_hhmmss(since_ts)}")
    if until_ts is not None:
        bits.append(f"止 {_hhmmss(until_ts)}")
    if before is not None:
        bits.append(f"上界 q{before}")
    print(f"# {'；'.join(bits)}")
    if note:
        print(f"# {note}")
    # 这里**不**复用 `--context` 的「要了 N 条只取到 M 条」：那条说的是读侧把入参悄悄
    # 砍到 CONTEXT_SCAN。`--history` 是先按批次回溯定位边界、再对边界之后那一段套
    # `--limit`，窗口比 limit 小是常态（边界就在最近几条），照搬会变成每张表头都说
    # 「只取到 M 条」，读者以为被截了。只在真被 limit 砍过时才开口（下面 elif）。
    if limit and len(collected) > int(limit):
        print(f"# 边界之后共 {len(collected)} 条，只打了最近 {int(limit)} 条"
              f"（更早的 {len(collected) - int(limit)} 条没打；要看全就加大 --limit）")
    # 保留上限披露：只说库里那个原值，不做「少了多少条」的减法（见模块 docstring）。
    # 词头用「保留上限」而不是「边界」：边界在本表头里特指「我上次发言那条」，
    # 两行都顶着「边界：」时读者分不清哪个窗口是被裁过的（issue #130）。
    if dropped is not None:
        print(f"# 保留上限：chat_sessions.dropped_through={dropped}"
              f"（此值及之前的消息已被裁掉；本窗口可能不全）")
    if shown:
        render_rows(shown, wakes=wakes, hot=hot)
    return 0


def run_history(args, parser, latest_group_id=None):
    """``reply.py --history``: 把命令行参数翻成 :func:`show_history` 的入参。

    三个入口都**不猜**：认不出的花名、认不出的时间、认不出的记号一律
    ``parser.error`` 并以退出码 2 结束——猜一个花名等于把窗口边界安在别人身上，
    猜一个时间等于给出一个没被要求的窗口。

    放在这里而不是 ``reply.py``：那个文件已经顶着 1000 行红线（issue #11），
    参数到入参的翻译和取数是同一条读链的两个半截，分开就只剩「传话」。
    ``latest_group_id`` 由调用方给（``reply.latest_group_id`` 依赖事件流与插件
    派发，逆着 import 会成环），省略时只按 ``--group`` 认群。
    """
    sender = None
    if args.sender:
        group = args.group or (latest_group_id() if latest_group_id else "")
        sender, label = resolve_sender(args.sender, group)
        if not sender:
            parser.error(f"--sender {args.sender}：{label}")
    try:
        since = parse_time(args.since)[0] if args.since else None
        until = parse_time(args.until)[0] if args.until else None
    except ValueError as exc:
        parser.error(str(exc))
    before = None
    if args.before is not None:
        try:
            before = wg_event_ref.parse(args.before)[1]
        except wg_event_ref.BadRef as exc:
            parser.error(f"--before {exc}")
    if args.limit is not None and args.limit < 1:
        parser.error(f"--limit 至少 1，收到 {args.limit}")
    return show_history(
        limit=args.limit or CONTEXT_SCAN, sender=sender,
        since=since, until=until, before=before)

