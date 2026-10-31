"""停机窗口缺口账本：后端不在的那段时间漏了什么，事后说得出来（issue #114）。

issue #112 修了「监听退不退」——存亡账本回答「上一轮几点到几点、现在为什么没人」。
但监听在不在与「这段时间消息有没有漏收」是两件事：后端（server_wg.py + NapCat）
停机跨过一段时间再拉起，窗口内的群消息不进任何本地存储——`chat_inbox` 由服务端
`chatlocal/mcp_chat.py` 在收到 OneBot 事件时写（NapCat 没收到就不写），`wg_events.jsonl`
由 `collector.capture_events` 写（`mention_only` 按本群 `trigger_mode` 取，#188；
停机时根本没投递）。
于是 daemon 侧所有入口（`up.py --status` / `reply.py --list` / `listen.py NO_WORK`）
都报告「一切正常」，「没人叫我」与「有人叫过但机器不在」无法区分——2026-10-26 那条
08:04 的 @ 就是这样丢的，直到人肉翻 `get_group_msg_history` 才发现。

本模块把那件事写成文件里的事实：`collector` 启动时算一次「本地 `chat_inbox` 最后一条
→ 拉起时刻」与 `get_group_msg_history` 的差，落 `state/wg_gap.json`。任何只读入口
（`--status` / `--list` / `listen NO_WORK`）随时 `describe()` / `at_me_lines()` 报出
「窗口内漏 N 条、@ 本机 M 条」，不再需要谁恰好想起来跑那段 diff 脚本。

分工（与 #112 同口径）：本账本只回答「这段时间漏了什么」，**不参与**续挂判断、
**不补录**进 `wg_events`（补录是 issue #114 候选 3，明确不做——会让 `watch.py` 游标
消费一条新数据流，风险与最小可用相悖）。漏掉的 @ 已过几小时，回复价值存疑；Agent
真要回凭 `--list` 里的 `msg_id` 手动 OneBot 发送。

`get_group_msg_history` 单次最多 200 条。取回的最老一条仍新于本地最后一条时，说明
窗口比 200 条更长，`describe()` 必须说「只能核对最近 200 条，更早的无法核对」——
不能静默只报最近 200 条（候选 4）。本模块不分页：分页是独立增强，超出 issue 的
「只要求说出来」范围。

已知边界：`collector` 单次运行内的二次短暂停机不重算（`wait failed` 有多种非停机
成因，统一触发会噪声化）；NapCat 未响应时该群标 `unreachable`，`describe()` 说
「无法核对」——说不出缺口比说一句假数字好。
"""
import json
import os
import sqlite3
import time
from pathlib import Path

import wg_clock  # noqa: E402  (漏掉的 @ 的时间戳按同一份渲染口径)
import wg_config  # noqa: E402  (config.json 的唯一读取口，issue #4 PA0)
import wg_event_ref  # noqa: E402  (会话号的唯一口径，issue #219)
import wg_text  # noqa: E402  (issue #169 PC：connect_ro 延迟导入门面)
from wg_paths import data_dir, state_dir  # noqa: E402

STATE = state_dir() / "wg_gap.json"
# `chat_inbox` 在 `~/.water-group/data/mcp-access.sqlite3`（schema 见
# `chatlocal/mcp_chat.py:105-109`）。`IDLEJOT_WG_DB` / `TULPA_WG_DB` 可钉单个库给测试用，
# 与 `wg_context.py` 的 `DB` 同款解析（issue #163 把 --context 读链从 `reply.py`
# 拆出去之后，那份解析就住在 wg_context 里）。
DB = Path(os.environ.get("IDLEJOT_WG_DB") or os.environ.get("TULPA_WG_DB",
            data_dir() / "mcp-access.sqlite3"))

# `get_group_msg_history` 单次上限（OneBot 11）。超过时窗口会被截断，`truncated` 标记
# 由此产生；`describe()` 必须把截断说出来（候选 4）。
HISTORY_COUNT = 200
# 漏掉的 @ 正文片段长度，与 `wg_text.STATUS_BODY_CHARS` 同量级——一行报得下。
AT_ME_BODY_CHARS = 40


def _now():
    return int(time.time())


def _db():
    """只读连接 chat_inbox；库不存在或读坏时返回 None。

    issue #169 PC：URI 形态（`Path.as_uri()` + `?mode=ro`）合流到
    `chatlocal.sqlite_util.connect_ro`，经 `wg_text.connect_ro` 延迟导入。这条注释
    原本就是「Windows 反斜杠直接拼 `?mode=ro` 会被 sqlite 当文件名的一部分」的
    知识——现在知识进助手，注释退成指向。
    """
    if not DB.exists():
        return None
    try:
        return wg_text.connect_ro(DB, timeout=5)
    except (sqlite3.Error, OSError):
        return None


def _local_state(group_id):
    """该群在 chat_inbox 里已有的 onebot_message_id 集合 + 最新一条 epoch 秒。

    与 issue #114 现象段那条复现命令同款 diff：`conversation_id` 形如 `1:group:123`，
    按 `wg_event_ref.group_of(cid) == group_id` 认群（字段结构判定，issue #211）。
    `at` 是 chat_inbox 的 REAL 列（epoch 秒），不从 payload 解析——`max(at)` 就是
    窗口左边界。

    会话号走 `wg_event_ref.row_conversation()`（payload 优先、会话行兜底，issue #219）：
    只读 payload 时，缺这个键的行会被静默判成别的群，缺口于是报得比实际大。

    issue #211：认群用 `group_of(cid) == gid` 而不是 `endswith(':' + gid)`。`row_conversation`
    取回的是完整 cid（`<account>:group:<gid>` 或 `<account>:direct:<uid>`），`endswith`
    不校验字段结构——同库里一个 `:direct:<user_id>` 会话的 user_id 恰好等于某群号时
    （`100000:direct:12345` 同样以 `:12345` 结尾），私聊消息会被当成「本机已收的群消息」，
    缺口少报、`last_time` 取到别会话的时间。`group_of` 按 `split(':')` 取
    `parts[1]=='group'` 才认，私聊返回空串不串。`group_of` 与 `row_conversation` 同在
    `wg_event_ref`，无新 import。
    """
    gid = str(group_id or "")
    ids = set()
    last_time = None
    con = _db()
    if con is None:
        return ids, last_time
    # 会话号一并取回（LEFT JOIN，issue #219）：payload 缺 `conversation_id` 的行
    # 以前在这里被判成「不是这个群」，那一批消息既不算进 ids，也推不动窗口左边界
    # ——缺口会被报成比实际更大。口径同 `wg_event_ref.row_conversation()`。
    #
    # 但不能因此把「会话表读不到」变成「缺口算成空的」：这里只读 `chat_inbox` 是
    # issue #114 起的既有契约（`check_gap_disclosure` 的夹具就只建那一张表），而
    # LEFT JOIN 撞上缺表会抛 `sqlite3.Error`，被下面那句 `except` 静默吞掉，缺口语
    # 报得比实际更小——比改前的「报得更大」更危险。所以缺表时退回只读 payload 的
    # 老写法：宁可少算兜底那部分，也不让整段静默归零。
    sql = ("SELECT i.at, i.payload, s.conversation_id"
           " FROM chat_inbox i LEFT JOIN chat_sessions s ON s.id = i.session_id"
           " ORDER BY i.at")
    try:
        rows = list(con.execute(sql))
    except sqlite3.Error:
        rows = [(at, payload, None) for at, payload
                in con.execute("SELECT at, payload FROM chat_inbox ORDER BY at")]
    try:
        for at, payload, session_cid in rows:
            try:
                pl = json.loads(payload)
            except (ValueError, TypeError):
                continue
            if not isinstance(pl, dict):
                continue
            cid = wg_event_ref.row_conversation(
                {"payload": pl, "conversation_id": session_cid})
            if wg_event_ref.group_of(cid) != gid:
                continue
            if pl.get("onebot_message_id"):
                ids.add(str(pl["onebot_message_id"]))
            if at:
                last_time = int(at)
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return ids, last_time


def _launch_epoch():
    """栈拉起时刻（`napcat_launch.json:started_epoch`，issue #114 窗口右边界）。

    `bootstrap._record_launch` 在每次 spawn 时写；读不到时回落 None，调用方按
    `computed_at` 兜底——缺口右边界取「现在」比取 0 安全。
    """
    try:
        rec = json.loads((state_dir() / "napcat_launch.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    try:
        return int(rec.get("started_epoch"))
    except (TypeError, ValueError):
        return None


def enabled_group_ids():
    """config.json 里 `enabled` 的群号列表（与 commands.settings_from 同口径）。"""
    cfg = wg_config.config()
    return [str(g) for g, gc in (cfg.get("groups") or {}).items()
            if (gc or {}).get("enabled")]


def _is_at_me(msg, account):
    """OneBot 原始消息里有没有 @ 本机（issue #114 复现命令同款判定）。

    `get_group_msg_history` 返回的段是 OneBot 原始格式 `{"type":"at","data":{"qq":...}}`，
    不是 chatlocal 拍平后的 `{"type":"at","qq":...}`——两套格式，`data` 层不能漏。
    """
    if not account:
        return False
    for seg in msg.get("message") or []:
        if not isinstance(seg, dict):
            continue
        if seg.get("type") == "at" and str((seg.get("data") or {}).get("qq")) == account:
            return True
    return False


def _extract_text(msg):
    """OneBot 原始 text 段拼接（`{"type":"text","data":{"text":...}}`）。"""
    parts = []
    for seg in msg.get("message") or []:
        if not isinstance(seg, dict) or seg.get("type") != "text":
            continue
        text = str((seg.get("data") or {}).get("text") or "")
        if text.strip():
            parts.append(" ".join(text.split()))
    return " ".join(parts)


def _at_me_line(msg):
    """一条漏掉的 @ 的可读行原料（时间戳由调用方按 wg_clock 渲染）。"""
    sender = msg.get("sender")
    if isinstance(sender, dict):
        sender = sender.get("nickname") or sender.get("user_id") or ""
    return {
        "msg_id": str(msg.get("message_id") or ""),
        "time": msg.get("time") or 0,
        "sender": str(sender or ""),
        "body": _extract_text(msg)[:AT_ME_BODY_CHARS],
    }


def _empty(window_start, window_end, unreachable=False):
    return {"missing": 0, "at_me": 0, "window_start": window_start,
            "window_end": window_end, "truncated": False,
            "unreachable": bool(unreachable), "at_me_lines": []}


def _compute_one(group_id, fetcher, launch_epoch, now):
    """单群缺口：本地 chat_inbox vs get_group_msg_history 的 diff，限定在停机窗口内。"""
    local_ids, window_start = _local_state(group_id)
    if window_start is None:
        # 本地一条都没有：首次部署 / 换群 / 新群，不是停机缺口。报 0 不报缺口，
        # 与「停机漏收」区分——否则新部署会把群里全部历史都算成「漏的」。
        return _empty(None, launch_epoch)
    window_end = launch_epoch or now
    account = str(wg_config.config().get("self_account") or "")
    try:
        history = fetcher(group_id) or []
    except Exception:  # noqa: BLE001  NapCat 未响应不该让 collector 崩
        return _empty(window_start, window_end, unreachable=True)
    missed = [m for m in history
              if str(m.get("message_id")) and str(m.get("message_id")) not in local_ids]
    if not missed:
        return _empty(window_start, window_end)
    # 只算窗口内 (window_start, window_end] 的：时间缺失（0）的不算，宁可少报也不假报。
    in_window = [m for m in missed
                 if (m.get("time") or 0) and window_start < (m.get("time") or 0) <= window_end]
    # 200 上限：history 最老一条仍新于 window_start → 可能有窗口内的消息被截掉。
    # history 非空（missed 是它的子集），oldest=0 只在某条 time 缺失且全是 0 时出现，
    # 那种数据异常不报截断（不造假事实）。
    oldest = min((m.get("time") or 0) for m in history) if history else 0
    truncated = bool(oldest and oldest > window_start)
    at_me_msgs = [m for m in in_window if _is_at_me(m, account)]
    return {
        "missing": len(in_window), "at_me": len(at_me_msgs),
        "window_start": window_start, "window_end": window_end,
        "truncated": truncated, "unreachable": False,
        "at_me_lines": [_at_me_line(m) for m in at_me_msgs],
    }


def compute(group_ids=None, fetch=None, now=None):
    """算缺口，返回可落盘的 dict（不写文件）。

    `group_ids` 默认从 config.json 的 enabled groups 取；`fetch(group_id)` 默认
    `wg_up_wake.group_msg_history`（延迟 import 避免模块级拉起 wg_up_wake）；`now`
    只给测试钉时刻用。
    """
    moment = _now() if now is None else int(now)
    if group_ids is None:
        group_ids = enabled_group_ids()
    launch_epoch = _launch_epoch()
    record = {"computed_at": moment, "groups": {}}
    for gid in group_ids:
        record["groups"][str(gid)] = _compute_one(str(gid), fetch, launch_epoch, moment)
    return record


def record(entry):
    """落盘缺口记录（原子写）。写不进不崩——账本不该让 collector 挂。

    issue #169 PD：原写法 `tmp = STATE.with_name(STATE.name + ".tmp")` 是**固定名**，
    没有 pid、没有随机后缀。两个进程同时 record() 会互相覆盖对方的临时文件——
    一个 os.replace 把另一个进程刚写的 tmp 替换掉，落盘的是后写者的一部分内容，
    账本数据被悄悄截断。改走 `wg_daemon.write_json`：唯一临时名（pid + 随机后缀）
    + os.replace，并发写互不踩。`write_json` 失败返回 False 而不抛，与原 `except
    OSError: pass` 的「写不进不崩」语义一致。
    """
    import wg_daemon
    wg_daemon.write_json(STATE, entry)


def read():
    """当前记录（dict）；文件不存在或读坏时 None。"""
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear():
    """删掉记录（测试用；生产路径只写不删）。"""
    try:
        STATE.unlink()
    except OSError:
        pass


def _span_bounds(groups):
    """所有可核对群缺口窗口的并集边界 `(start, end)`；取不到时 `(None, None)`。

    各群共用同一个右边界（`launch_epoch` 或 `computed_at`）、左边界各异。并集边界
    是「这段时间」的真话；各报一遍会把状态行撑成多行（issue #148）。
    """
    starts = []
    ends = []
    for entry in groups.values():
        if not isinstance(entry, dict) or entry.get("unreachable"):
            continue
        if not entry.get("missing"):
            continue
        start = entry.get("window_start")
        end = entry.get("window_end")
        if start is not None:
            starts.append(start)
        if end is not None:
            ends.append(end)
    if not starts or not ends:
        return None, None
    return min(starts), max(ends)


def window_note(now=None):
    """缺口窗口的上下文括号内容：`停机窗口 07:21:40–09:30:26（账本 09:30:47 计算，3 小时前，仅供参考）`。

    issue #148：`wg_gap.json` 只在 collector 启动时算一次，之后整天的 `--status` /
    `--list` / `NO_WORK` 都重复同一行——不带时刻时，三小时前的早晨停机账本与几分钟
    前的新缺口同形。这里把窗口起止、账本计算时刻、距今多久三件事一次说清，并明写
    「仅供参考」，与 #142 给监听账本历史段加的注脚同口径。

    无缺口 / 无记录 / 窗口取不到时返回空串（调用方不加这句话，不造假事实）。
    `now` 只给测试钉时刻用，生产走默认。
    """
    rec = read()
    if not rec:
        return ""
    groups = rec.get("groups") or {}
    start, end = _span_bounds(groups)
    if start is None or end is None:
        return ""
    pieces = []
    shown = wg_clock.window(start, end)
    if shown:
        pieces.append(f"停机窗口 {shown}")
    computed = rec.get("computed_at")
    try:
        computed = int(computed)
    except (TypeError, ValueError):
        computed = None
    if computed:
        age = wg_clock.ago(computed, now=now)
        detail = f"账本 {wg_clock.stamp(computed)} 计算"
        if age:
            detail += f"，{age}"
        pieces.append(f"（{detail}，仅供参考）")
    return "".join(pieces)


def describe(now=None):
    """一行人话给 `up.py --status`。无缺口 / 无记录 / 全 unreachable 时返回空串。

    空串=不造假事实：与 #112 `wg_listener_state.describe()` 同口径，恒加注脚的实现
    红在「无缺口时也加字」上（排除型对照）。

    issue #148：计数前面必须带窗口上下文（`window_note()`）——只有「漏 29 条」
    的话，一个只在 collector 启动时算过一次的账本会被读成现刻事故。
    """
    rec = read()
    if not rec:
        return ""
    groups = rec.get("groups") or {}
    if not groups:
        return ""
    total_missing = 0
    total_at_me = 0
    unreachable = []
    truncated = False
    for _gid, e in groups.items():
        if not isinstance(e, dict):
            continue
        if e.get("unreachable"):
            unreachable.append(str(_gid))
            continue
        total_missing += e.get("missing") or 0
        total_at_me += e.get("at_me") or 0
        if e.get("truncated"):
            truncated = True
    if total_missing == 0 and not unreachable:
        return ""
    parts = []
    if total_missing:
        note = window_note(now=now)
        lead = f"{note}，漏 {total_missing} 条" if note else f"停机窗口漏 {total_missing} 条"
        parts.append(f"{lead}（@ 本机 {total_at_me} 条），见 reply.py --list")
    if truncated:
        parts.append("只能核对最近 200 条，更早的无法核对")
    if unreachable:
        parts.append(f"群 {','.join(unreachable)} 无法核对（NapCat 未响应）")
    return "缺口：" + "；".join(parts)


def at_me_lines():
    """漏掉的 @ 本机消息，多行给 `reply.py --list` / `listen.py` 提示。

    行首 `id=-`（占位，明确不可 `reply.py <id>`）+ `msg_id=`（OneBot 原始 id，Agent
    真要回手动 OneBot 发送用），区别于真唤醒行的 `id=<正数>`。

    时间戳用 `wg_clock.stamp()`（带日期，issue #148）：漏收行与唤醒行（`HH:MM:SS`）
    因此不同形——漏掉的 @ 可能是昨夜停机时段的，只给时分秒会读成「刚漏的」。
    """
    rec = read()
    if not rec:
        return []
    lines = []
    for _gid, e in (rec.get("groups") or {}).items():
        if not isinstance(e, dict) or e.get("unreachable"):
            continue
        for m in e.get("at_me_lines") or []:
            stamp = wg_clock.stamp(m.get("time"))
            lines.append(f"id=- msg_id={m.get('msg_id')} {stamp} [漏] "
                         f"{m.get('sender')}: {m.get('body')}")
    return lines


def header_lines():
    """`reply.py --list` 缺口段的抬头行（issue #114 / #148）。

    放在这里而不是调用方，是因为「段标题 + 窗口上下文」必须与 `describe()` 同一份
    措辞；而且 `reply.py` 已贴 1000 行红线（`check_module_size.py`），多一行注释都
    会把门禁顶红——抬头由本模块一次给全，调用方只负责打印。
    """
    lines = ["# 停机窗口漏掉的 @（不在唤醒账本里，不可 reply.py <id>）："]
    note = window_note()
    if note:
        lines.append(f"# {note}")
    return lines


def print_hint():
    """`listen.py` NO_WORK 路径插一行：让「没人叫我」与「有人叫过但机器不在」可区分。

    只在缺口非空时出声——WAKE 路径不需要它（Agent 已被叫醒），NO_WORK 才是 issue
    点名的症状（Agent 据 NO_WORK 误判「没人叫我」）。
    """
    line = describe()
    if line:
        print(f"⚠ {line}")


def compute_and_record(group_ids=None, fetch=None, now=None):
    """`collector` 启动时调一次：算缺口并落盘。返回记录供调用方记日志。"""
    entry = compute(group_ids=group_ids, fetch=fetch, now=now)
    record(entry)
    return entry
