"""Send one reply to the group, and record that it went out.

The whole plan/send dance lives here so an agent that has just taken over only
needs to supply a message id and some text. Doing it by hand is where the failure
modes hide: a stale batch needs the batch id from updated_chat, an expired one
needs wg_state.json re-read, and a rejected send must not be retried blindly.

The ledger matters for handoff. The collector only delivers a batch when someone
@-mentions the bot, so a reply we just sent is usually not in wg_events.jsonl yet;
without a local record the next agent reads the status report, sees [待回] on a
message that was already answered, and replies to it twice.

Every successful send also opens a follow-up window (追问窗口插件，issue #4 PD2a):
the group can keep talking for a while afterwards and watch.py still wakes on it,
so a reply does not fall on deaf ears the moment it lands. --no-followup turns that off.

Sending also re-hangs the listener by default (issue #69): after SENT this process
parks in listen.py for up to --wait seconds and relays whatever it finds, so the
gap between "replied" and "listening again" is milliseconds rather than a whole
agent turn. --no-wait returns right after sending instead.

Usage:
  reply.py <event_id> "<text>" [--image 路径 ...] [--intent "..."] [--no-quote] [--no-followup] [--no-wait]
  reply.py <event_id> --image 路径           纯图，不带文字
  reply.py --list
  reply.py --context 30 [--around <event_id>]
  reply.py --show <event_id>      读这条消息的全文（行内截断后的补救路径）
  reply.py --who <花名>            只解析到 QQ，并把他的画像卡整张打出来（issue #163）

<text> 里的换行会拆成多条气泡（默认最多 20 条，首条挂引用；`--max-bubbles`
或 config.json 的 `reply.max_bubbles` 可以调小，issue #131）。想连发几句就分行写，
不要拿斜杠把几句话串成一行——QQ 里换行会折叠，斜杠会读成文档。shell 双引号里
写不出真换行，用字面量 \n 也行（反斜杠加 n）；\\n 转义成字面 \n 留在正文里。

--image 把本地图片发进群（可重复），每张独占一条气泡排在文字之后（issue #128）。判据是
Pillow 整张解得开，上限与格式见 `--help`；带图的回复强制走 OneBot，不回退计划通道。
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import mcp_client  # noqa: E402
import plugins  # noqa: E402  (事件型能力派发口：追问窗口，issue #4 PD2a)
import wg_aliases  # noqa: E402  (别名指向渲染，issue #30)
import wg_bubbles  # noqa: E402  (气泡上限与拆句的唯一出口，issue #131)
import wg_budget  # noqa: E402  (等待预算的唯一出口，issue #79/#83)
# --context 的读链（会话 id / 闲聊 / 上下文行）已拆到 wg_context（issue #163）：
# 本文件撞了 1000 行红线。这里再导出，门禁与 `--context` 入口照旧按 reply.* 调。
from wg_context import recent_chatter, show_context  # noqa: E402,F401
import wg_dossiers  # noqa: E402  (群友画像 / 群级术语，issue #163)
import wg_gap  # noqa: E402  (停机窗口缺口账本，issue #114)
import winconsole  # noqa: E402
import wg_clock  # noqa: E402  (「现在」的唯一出口，issue #95)
import wg_config  # noqa: E402  (config.json 的唯一读取口，issue #128)
import wg_event_ref  # noqa: E402  (事件 id 的唯一解析入口，issue #216)
import wg_images  # noqa: E402
import wg_isolation  # noqa: E402  (群身份与跨群护栏，issue #164)
import wg_quotes  # noqa: E402  (引用原文渲染，issue #94)
import wg_send_failure  # noqa: E402  (发送失败的原文 / 禁言诊断 / 失败账本，issue #132)
import wg_text  # noqa: E402  (正文渲染：段占位不占额度，issue #32)

ROOT = HERE.parents[1]
from wg_paths import state_dir
STATE = state_dir()
SID_FILE = STATE / "wg_session.txt"
STATE_FILE = STATE / "wg_state.json"
EVENTS = STATE / "wg_events.jsonl"
REPLIED = STATE / "wg_replied.jsonl"
# 会话库的只读路径在 wg_context 里（`--context` 与 `--show` 的读链都归它），
# 这里不再留一份同义常量：两份路径会各自漂移（issue #163 拆分时清掉）。

RETRY_SLEEP = 3
SOURCE_WAIT = 30
# 预算（宿主超时 / 发送余量 / 单次调用最多能等多久）只写在 wg_budget.py 里，
# listen.py 也用同一套口径（issue #83）。这三个名字在这里重新导出，门禁与调用点
# 照旧读 reply.AGENT_TOOL_TIMEOUT / reply.SEND_BUDGET（issue #79）。
AGENT_TOOL_TIMEOUT = wg_budget.AGENT_TOOL_TIMEOUT
SEND_BUDGET = wg_budget.SEND_BUDGET
# 发送成功后原地续挂监听的默认时长（issue #69）。监听只活一轮，回完消息到
# 下一次挂上之间的空窗正是 @ 丢失的地方；把它做成默认值而不是可选项。
# 取值由宿主超时减去预算算出（issue #79）：默认调用必须在调用方失去耐心之前
# 自己返回并打印 NO_WORK，否则「消除几十秒空窗」会以「无限长空窗」收场——
# 被截断时子监听和父进程一起死，而这条路径上不会留下任何提示。
DEFAULT_WAIT = wg_budget.WAIT_BUDGET
# 多条气泡之间的间隔、单次能发几条，都收在 wg_bubbles 里（issue #131）。这两个
# 名字在这里重新导出：调用点与既有门禁照旧读 reply.BUBBLE_GAP / reply.MAX_BUBBLES。
BUBBLE_GAP = wg_bubbles.BUBBLE_GAP
MAX_BUBBLES = wg_bubbles.MAX_BUBBLES


def session_id():
    try:
        return SID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def batch_id():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return (data.get("input_batch") or {}).get("id") or ""


def read_events():
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


def record(event_ids, text):
    """Ledger every event this one message answers.

    A burst of @-mentions usually gets one reply ("@A 和 @B 都别急"), and all of
    them have to be marked or the next agent re-answers the ones left out.
    """
    if isinstance(event_ids, int):
        event_ids = [event_ids]
    STATE.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    with open(REPLIED, "a", encoding="utf-8") as fh:
        for event_id in event_ids:
            fh.write(json.dumps({"id": event_id, "ts": now, "text": text},
                                ensure_ascii=False) + "\n")


def plan(sid, batch, target, intent, quote, group_id=None):
    """Returns (plan_id, final_target); plan_id is None when planning failed.

    The final target can differ from the requested one when the batch rotated
    mid-compose. The caller needs it to decide whether quoting still makes sense:
    quoting a stranger's message to answer someone else reads as a mistake.

    ``group_id`` 是本会话的群（issue #164 PB）。批次轮换后的重定向**只在同一群的
    id 里挑**：改前直接取 ``allowed[-1]``，批次里最新那条属于哪个群就发到哪个群，
    另一个会话的目标还会被记成「已回」——两个会话都以为对方处理了。
    """
    allowed_in_group = wg_isolation.same_group_ids(read_events(), group_id)
    for attempt in range(8):
        args = {"session_id": sid, "batch_id": batch, "action": "reply",
                "expressions": ["text"], "intent": intent}
        if target:
            args["target_event_id"] = target
        data = mcp_client.call_tool("plan_chat_reply", args, 40)
        state = data.get("state")
        code = data.get("error_code") or ""
        print(f"  plan {attempt + 1}: state={state} code={code or '-'}")
        if state == "READY":
            return data.get("plan_id"), target

        if state == "SUCCEEDED":
            # Planning is keyed on (session, batch, target, intent): asking again
            # for a combination that already finished replays the old plan instead
            # of issuing a new one. That bites when the batch rotated and an earlier
            # attempt already consumed this target, and it looks like a send that
            # never happened -- the group stayed silent while this returned
            # SUCCEEDED. Nudge the intent so the server plans afresh.
            intent = f"{intent}·{attempt + 2}"
            continue

        if code in ("stale", "needs_refresh"):
            updated = data.get("updated_chat") or {}
            new_batch = (updated.get("input_batch") or {}).get("id")
            if new_batch:
                batch = new_batch
                continue
            print("  no replacement batch in updated_chat")
            return None, target
        if code == "target_not_in_batch":
            # The batch rotated while we were composing: every new message forms a
            # new batch, and only its own events are valid targets. The response
            # names the ones that are, so adopt the newest and retry the same batch
            # instead of re-reading the state file and losing another round.
            #
            # issue #164 PB：只在**本群**的 id 里挑。别群的 id 同样是「本批有效」，
            # 取它就会把回复发到另一个群，并把那条记成已回。
            allowed = data.get("valid_event_ids") or []
            if allowed_in_group is not None:
                allowed = [i for i in allowed if i in allowed_in_group]
            if allowed:
                new_target = allowed[-1]
                if new_target != target:
                    print(f"  batch rotated, retargeting {target} -> {new_target}")
                    target = new_target
                    continue
                print(f"  batch rotated; {target} is still the newest in this group")
                return None, target
            print("  no valid target in this batch" if allowed_in_group is None
                  else "  这批里没有本群的目标，不跨群重定向")
            return None, target
        if code == "batch_expired":
            fresh = batch_id()
            if fresh and fresh != batch:
                batch = fresh
                continue
            print("  batch expired and no newer one is on disk; wait for the collector")
            return None, target
        if code == "source_unavailable":
            print(f"  NapCat is reconnecting, waiting {SOURCE_WAIT}s")
            time.sleep(SOURCE_WAIT)
            continue
        if code == "chat_rejected":
            print("  the session is held by another waiter; check `up.py --status`")
            return None, target
        print("  unhandled:", json.dumps(data, ensure_ascii=False)[:300])
        return None, target
    return None, target


# 拆句与上限的唯一实现在 wg_bubbles（issue #131）。这里保留同名入口，是因为门禁与
# 调用点读的一直是 reply.split_bubbles / reply.normalize_newlines。
normalize_newlines = wg_bubbles.normalize_newlines


def split_bubbles(text, limit=MAX_BUBBLES):
    """按换行拆成多条气泡；limit 由调用方按 config 的 reply.max_bubbles 解析后传入。"""
    return wg_bubbles.split(text, limit)


def send(sid, plan_id, texts, quote):
    bubbles = [{"kind": "text", "text": one} for one in texts]
    if quote and bubbles:
        bubbles[0]["quote"] = True
    data = mcp_client.call_tool("send_chat_reply",
                                {"session_id": sid, "plan_id": plan_id,
                                 "bubbles": bubbles, "expression_ids": []}, 45)
    state = data.get("state")
    print(f"  send: state={state} reason={data.get('reason') or '-'}")
    for b in data.get("bubbles") or []:
        print(f"    bubble {b.get('index')} {b.get('state')} "
              f"{(b.get('expression') or {}).get('text', '')}")
    return state == "SUCCEEDED"


def onebot_config():
    data = wg_config.config()
    return str(data.get("onebot_http_url") or "").rstrip("/"), str(data.get("onebot_token") or "")


def self_account():
    return str(wg_config.config().get("self_account") or "")


# 只有写在花括号里的名字才算 @：群里正文的「@群友A」是普通文字，真正的 @ 是消息段，
# 混为一谈会把随口一提变成一次点名。反斜杠一并排除：字面量 \n 归一拆行前若混进
# @{...}，不该拿「余\n子」这种半个名字去查花名册（issue #80）。
AT_TOKEN = re.compile(r"@\{([^{}\n\\]{1,24})\}")


def event_ref(event_id):
    """一条事件的 `Ref`（来源 + 群 + 编号），一次解析到位。

    issue #216：解析归 `wg_event_ref` 一处——来源顺序 `wg_events` → `chat_inbox`，
    字段名兼容与「回查库列」都在那里。改前这里先自己扫一遍事件流、再交给追问插件
    解析 payload 里的 `onebot_message_id`：payload 少一个键就回 `None`，而同一行
    `--show` / `--context` 读得出来，于是「读得到、回不了」。

    解析器两个来源都没有，才问插件那一路：`event_lookup` 是扩展点（私有插件可以
    自己接），默认实现 `followup.lookup` 就是同一个解析器——所以这条只在「id 真的
    不在库里」时多走一次，不会盖掉上面的结论。
    """
    ref = wg_event_ref.resolve(event_id)
    if ref is not None:
        return ref
    target = plugins.emit("event_lookup", {"event_id": event_id})
    if not target:
        return None
    return wg_event_ref.Ref(event_id=int(event_id), source=wg_event_ref.SRC_PLUGIN,
                            group_id=str(target[0]), message_id=str(target[1] or ""))


def event_target(event_id):
    """(group_id, onebot_message_id) for an event the collector already stored.

    群号拿得到、编号拿不到时回 `(群号, "")`：这条能回，只是挂不上引用。改前这种行
    与「id 根本不存在」一样回 `None`，钉死群下直接抛 `CrossGroup`「查不到所属群」。
    """
    ref = event_ref(event_id)
    return ref.target if ref else None


_ROSTER = {}


def group_members(group_id):
    """Live member list for one group: [{"qq", "card", "nickname"}].

    Memoised per process: one reply can carry several @-mentions, and the roster
    is a fat JSON list that costs a round trip each time.
    """
    if group_id in _ROSTER:
        return _ROSTER[group_id]
    base, token = onebot_config()
    if not base or not group_id:
        return []
    try:
        resp = requests.post(f"{base}/get_group_member_list",
                             json={"group_id": int(group_id)},
                             headers={"Authorization": f"Bearer {token}"}, timeout=15)
        rows = resp.json().get("data") or []
    except (requests.RequestException, ValueError) as exc:
        print(f"  member list unavailable: {type(exc).__name__}: {exc}")
        return []
    _ROSTER[group_id] = [{"qq": str(r.get("user_id") or ""),
                          "card": (r.get("card") or "").strip(),
                          "nickname": (r.get("nickname") or "").strip()} for r in rows]
    return _ROSTER[group_id]


def dossier_matches(raw):
    """(exact, loose) profiles from the memory DB; empty pair when it is absent.

    The dossier is a nice-to-have here: @-ing still works off the live roster, so
    a locked or missing database must not take the whole reply down with it.
    """
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from water_group.memory_engine import get_memory_engine

        return get_memory_engine().match_profiles(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"  dossier lookup failed: {type(exc).__name__}: {exc}")
        return [], []


def resolve_member(name, group_id):
    """花名 -> (qq, label), or (None, 原因).

    A bare number is taken as the QQ itself -- the one input that cannot be
    ambiguous. Everything else is tried against the live roster first and the
    dossier second: the group card is how people actually address each other in
    here ("某人的旧名片"、"某人的新名片"), the dossier is what survives a rename and
    holds the shorthand people type ("主名"、"外号"). Each tier is tried exact
    before loose, so a full name never loses to somebody else's substring.

    A name that fits more than one person is reported, not guessed. @-ing the
    wrong person reads as a mistake by the bot in a way that a missed @ does not.
    """
    raw = (name or "").strip().lstrip("@")
    if not raw:
        return None, "空名字"

    members = group_members(group_id)
    in_group = {m["qq"]: m for m in members}
    roster_known = bool(members)
    me = self_account()

    def label_of(qq):
        mate = in_group.get(qq)
        return ((mate["card"] or mate["nickname"]) if mate else "") or qq

    if raw.isdigit():
        if roster_known and raw not in in_group:
            # A number nobody in the group answers to is not an @, it is a typo.
            return None, f"{raw} 不在本群"
        if raw == me:
            return None, "是我自己"
        return raw, label_of(raw)

    # 档案里有、但不在本群的人：不能 @，可往往正是用户记错的那个，值得单独说一句。
    elsewhere = []

    def from_dossier(profs):
        out = []
        for p in profs:
            qq = str(p.get("user_id") or "")
            if not qq:
                continue
            if roster_known and qq not in in_group:
                elsewhere.append((qq, str(p.get("primary_name") or qq)))
                continue
            out.append((qq, label_of(qq) if qq in in_group else str(p.get("primary_name") or qq)))
        return out

    exact, loose = dossier_matches(raw)
    tiers = [("本群名片", [(m["qq"], label_of(m["qq"])) for m in members
                          if raw in (m["card"], m["nickname"])]),
             ("档案", from_dossier(exact))]
    if len(raw) >= 2:
        tiers.append(("本群名片近似", [(m["qq"], label_of(m["qq"])) for m in members
                                      if raw in m["card"] or raw in m["nickname"]]))
        tiers.append(("档案近似", from_dossier(loose)))

    for kind, cands in tiers:
        if len(cands) == 1:
            qq, label = cands[0]
            if qq == me:
                return None, "是我自己"
            return qq, label
        if len(cands) > 1:
            shown = "、".join(f"{label}({qq})" for qq, label in cands[:5])
            return None, f"{kind}里有 {len(cands)} 个：{shown}"

    if elsewhere:
        qq, label = elsewhere[0]
        return None, f"档案里是 {label}({qq})，但不在本群"
    return None, "没找到"


def _tidy(segments):
    """Fold neighbouring text runs into one segment.

    QQ renders two adjacent text segments exactly like one, so this is only about
    the payload being readable in `get_msg` output and in the logs -- which is
    where anyone debugging a missing @ will be looking.
    """
    out = []
    for seg in segments:
        if seg["type"] == "text" and out and out[-1]["type"] == "text":
            out[-1]["data"]["text"] += seg["data"]["text"]
        else:
            out.append(seg)
    return out


def build_segments(text, group_id, leading=()):
    """Turn `@{花名}` into real at segments, keeping everything else as text.

    An unresolved name is left as literal text rather than dropped: the reply is
    usually time-sensitive, and a name that only renders as "@某某" is a smaller
    loss than a message that never goes out.
    """
    segments = []
    for name in leading:
        qq, label = resolve_member(name, group_id)
        if qq:
            segments.append({"type": "at", "data": {"qq": qq}})
            segments.append({"type": "text", "data": {"text": " "}})
            print(f"  at: {name} -> {qq} ({label})")
        else:
            print(f"  at: {name} 未解析（{label}）")
            segments.append({"type": "text", "data": {"text": f"@{name} "}})

    pos = 0
    for hit in AT_TOKEN.finditer(text or ""):
        if hit.start() > pos:
            segments.append({"type": "text", "data": {"text": text[pos:hit.start()]}})
        name = hit.group(1)
        qq, label = resolve_member(name, group_id)
        if qq:
            segments.append({"type": "at", "data": {"qq": qq}})
            print(f"  at: {name} -> {qq} ({label})")
        else:
            print(f"  at: {name} 未解析（{label}），按文字发出")
            segments.append({"type": "text", "data": {"text": f"@{name}"}})
        pos = hit.end()
    if pos < len(text or ""):
        segments.append({"type": "text", "data": {"text": text[pos:]}})
    return _tidy(segments)


def onebot_send(group_id, segments, message_id=None, failure=None):
    """Send raw OneBot segments, with a native quote in front when asked.

    OneBot takes the reply segment directly, which is what the plan path compiles
    down to anyway, so this lands the quote on the right message no matter how many
    batches have rolled over in between.

    `failure` is an optional dict owned by the caller: on refusal the raw retcode /
    message / wording (or the transport exception) goes into it. The return value
    stays a bool -- callers keep their contract (issue #132).
    """
    base, token = onebot_config()
    if not base or not token:
        wg_send_failure.note(failure, message="onebot_http_url / onebot_token 为空")
        return False
    payload = list(segments)
    if message_id:
        payload = [{"type": "reply", "data": {"id": message_id}}, *payload]
    try:
        resp = requests.post(f"{base}/send_group_msg",
                             json={"group_id": int(group_id), "message": payload},
                             headers={"Authorization": f"Bearer {token}"}, timeout=20)
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  send failed: {type(exc).__name__}: {exc}")
        wg_send_failure.note(failure, exc=exc)
        return False
    ok = data.get("retcode") == 0
    print(f"  send: retcode={data.get('retcode')} "
          f"message_id={(data.get('data') or {}).get('message_id')}")
    if not ok:
        # retcode/message/wording 是失败的唯一原始说明（禁言就在 message 里）。
        wg_send_failure.note(failure, data=data)
    return ok


def onebot_send_bubbles(group_id, bubble_segments, message_id=None, failure=None):
    """一条一条发，首条挂引用；返回实际发出去的条数。

    多气泡只能靠多条消息落地：OneBot 的 send_group_msg 一次发一条，QQ 里
    换行会被折叠成空格。首条带 reply 段，后面的裸发，读起来就是真人连发。
    中途失败就停下——后面的补刀接在一条没发出去的消息后面会很怪。

    返回条数而不是布尔值：发了第 1 条、第 2 条失败时，调用方不能退回计划通道，
    否则第 1 条会在群里出现两次。
    """
    for index, segments in enumerate(bubble_segments):
        if not onebot_send(group_id, segments, message_id if index == 0 else None, failure):
            return index
        if index + 1 < len(bubble_segments):
            time.sleep(BUBBLE_GAP)
    return len(bubble_segments)


def list_wakes():
    """Same wake rules and same answered-check as `up.py --status`.

    Importing up keeps one source of truth for "who woke us", "did we answer"
    and "is it still worth answering" — the third one is the reply TTL, so a
    wake older than `pending_reply.max_age_seconds` reads [过期], not [待回].

    issue #164 PA：只看本会话的群。改前遍历全量事件流，钉死群 111 的会话照样
    看到群 222 的唤醒——两个会话都以为那些 @ 是给自己的。未钉死时不过滤（与改前
    一致），但会打一行 `note()` 说清隔离没生效。
    """
    import up

    rows = up.events()
    # issue #164 PA：只看本会话的群。改前遍历全量事件流，钉死群 111 的会话照样
    # 看到群 222 的唤醒——两个会话都以为那些 @ 是给自己的。未钉死时不过滤（与
    # 改前一致），但下面会打 `note()` 说清隔离没生效。
    rows = wg_isolation.rows_of(rows)
    ledger = up.replied_ids()
    # 表头先给「现在」：下面每行只有消息自己的时间戳，没有基准的话，Agent
    # 分不清一条 20:50 的 @ 是刚发的还是十分钟前的（issue #95）。
    print(f"# 唤醒记录 · {wg_clock.now_line()}")
    # issue #164 PA：本会话钉死在哪一群，一眼看得见——下面每行都只来自它。
    print(f"# 本会话群 · {wg_isolation.describe()}")
    warn = wg_isolation.note()
    if warn:
        print(warn)
    # issue #114：停机窗口内漏收的 @ 本机消息在这里列出来，让「没人叫我」与
    # 「有人叫过但机器不在」可区分。抬头由 `wg_gap.header_lines()` 一次给全
    # （段标题 + 窗口起止 + 账本时刻，issue #148）：不给时刻，三小时前的早晨
    # 账本与几分钟前的新缺口读起来一样。
    gap_lines = wg_gap.at_me_lines()
    for ln in ((wg_gap.header_lines() + gap_lines) if gap_lines else []):
        print(ln)
    wg_send_failure.print_last()
    shown = 0
    for e in rows:
        reason = up.wake_reason(e)
        if not reason:
            continue
        mark = up.wake_state(e, rows, ledger)
        stamp = wg_clock.clock_ms(e.get("timestamp"))
        images = wg_images.render(e)
        quotes = wg_quotes.render_event(e)
        # 正文渲染与 watch 唤醒行同一份口径（issue #32）：段占位不占额度，
        # 截断了带 … 和 --show 指引。
        body, _, _ = wg_text.render_body(e.get("content"), wg_text.LIST_BODY_CHARS,
                                          e.get("id"))
        # 人名指向与画像与唤醒行同款（issue #30 / #163）。
        gid = wg_aliases.group_of(e.get("conversation_id"))
        tag, hits, cards = wg_dossiers.anchors(e.get("sender"), e.get("content"), gid)
        print(f"id={e.get('id')} {stamp} [{mark}] [{reason}] {e.get('sender')}{tag}: "
              f"{body}"
              f"{('  ' + quotes) if quotes else ''}{('  ' + images) if images else ''}"
              f"{''.join('  ' + h for h in hits)}{cards}")
        shown += 1
    note = wg_dossiers.group_note_line(
        wg_aliases.group_of(rows[-1].get("conversation_id")) if rows else "")
    if note:
        print(note)
    # 过滤把自己滤空要说出来（issue #164）：群号/self_account/状态目录配错时，
    # 「过滤生效」与「群里没人说话」在输出里完全一样，Agent 会一直等一个不会
    # 来的唤醒。正常空流不吵——`filter_report` 只在「有东西但一条都不属于我」时出声。
    empty = wg_isolation.filter_report(up.events())
    if empty:
        print(empty)
    if not shown:
        print("没有唤醒记录")
    return 0
def latest_group_id():
    for row in reversed(read_events()):
        parts = str(row.get("conversation_id") or "").split(":")
        if len(parts) == 3 and parts[1] == "group":
            return parts[2]
    # 一条唤醒都没有的时候（追问窗口里的闲聊不会进 wg_events），退回全量库问。
    # 同上：DB 兜底不过三态开关（issue #4 PD2a）。
    return plugins.emit("active_group") or ""


def unanswered(answered):
    """Every wake still owed a reply, minus the ones this reply already covers.

    The forward-only check was wrong in the exact case it existed for: answer the
    newest of a burst and the earlier ones stay pending, but because their ids are
    smaller nothing warns about them -- which is how "two @s, one answer" looked
    like success. Both directions have to be checked.

    Expired wakes are excluded too: the NOTE exists to stop a burst from being
    half-answered, and a @ from yesterday in that list is noise -- it reads as
    "you still owe the group an answer" when the topic is long dead.

    issue #164 PA：只算本会话的群。别群的 @ 也进这个清单，就是在提醒本会话去回
    别人的群——而本会话连发都发不出去（PB 护栏会拒）。
    """
    import up

    rows = up.events()
    ledger = up.replied_ids()
    done = set(answered)
    return [e for e in wg_isolation.rows_of(rows)
            if up.wake_reason(e)
            and (e.get("id") or 0) not in done
            and not up.replied_after(e.get("id"), rows, ledger)
            and not up.wake_expired(e)]


def _plugin_settings():
    """追问窗口一类插件的归一化设置（三态门控要它）。

    延迟 import commands：reply 的其余路径不碰插件，别让每条回复都拖指令树
    进来（实测约 80ms；顶层 requests 已花 111ms，可接受但不该白花）。
    读不到配置时回落空设置——插件按 default_enabled 走，回复链路不陪葬。
    """
    try:
        import commands  # noqa: PLC0415
        return commands.load_settings()
    except Exception:  # noqa: BLE001  设置坏了不该让已发出的回复报错
        return {}


def open_followup(event_id, group_id="", wanted=True, not_before=None):
    """发送成功后开追问窗口，让话题继续；wanted=False 或插件停了就不开。

    not_before 是发送开始的墙上时间，只作**预算标记**：处理期间堆下的历史闲聊照常
    唤醒但不扣额度（issue #29 / #117），只有发送开始之后开口的接话才占额度——「开口」
    按消息自己的时间判，不是本地收到时刻（issue #218）。group_id 带进去，三态门控
    按群判（issue #4 PD2a）——这个群关了追问窗口，别的群照开。
    """
    if not wanted:
        return None
    # 开窗是「附加能力」，开不出来最多少收几条接话；已经发出去的那条回复不该因此
    # 变成一次异常退出（issue #227）。save() 的 OSError、插件 fn() 的异常都从这里
    # 冒出来——一个 try 同时盖住，与同文件 _plugin_settings() 的「设置坏了不该让
    # 已发出的回复报错」同一句口径。打一行失败行后返回 None，调用点形状不变：
    # SENT 与退出码仍只由发送结果决定。
    try:
        state = plugins.emit("reply_sent", {
            "settings": _plugin_settings(),
            "group_id": group_id,
            "event_id": event_id,
            "not_before": not_before,
        })
    except Exception as exc:  # noqa: BLE001  开窗坏了不该让已发出的回复报错
        print(f"[追问] 开窗失败：{type(exc).__name__}: {exc}（消息已发送，本轮不会再开窗）")
        return None
    if state:
        print(f"[追问] 窗口已开：还可跟 {state['budget']} 条，"
              f"静默 {state['idle_seconds']}s 后收回")
    return state


def wait_for_next(seconds):
    """Park in the listener loop right after sending, and pass its output through.

    This is the default after a successful send (issue #69). Re-hanging by hand
    leaves a window between the send and the next listen.py in which @-mentions
    land in the file with nobody watching. That window is a whole agent turn --
    tens of seconds -- and it is exactly where a burst gets lost. Because the
    loss depends on the agent remembering, it failed three times in one evening
    (2026-10-24); a safe default cannot depend on memory. --no-wait opts out for
    the one case where the caller is about to do something else.

    The wait has to fit inside the caller's patience (issue #79). DEFAULT_WAIT is
    AGENT_TOOL_TIMEOUT minus SEND_BUDGET, and the bound is printed before the
    wait starts: a host timeout shorter than that kills this process and the
    child listener together, on a path that prints nothing at all. Surviving as
    an orphan is not an option -- watch.py advances its cursor as soon as it
    prints a wake (watch.py:188), so a listener nobody reads would consume the
    wake and lose it, and `reply.py` would refuse to hang a new one because
    up.listener_pids() still matches the orphan.

    `--cycles 1 --no-digest` bounds it: a tool call that never returns cannot
    be told apart from a hang. Since issue #88 the default is already one
    round, but an explicit bound keeps this call short whatever the default
    becomes. The digest pass is skipped because it takes tens of seconds on a
    backlog, which would make "--wait 6" take a minute.

    The output has to be relayed, not inherited (issue #91): this process is
    started by the agent without a console, so a CREATE_NO_WINDOW child would
    print into a console of its own and the wake lines would vanish here even
    after listen.py started printing them.

    Both the printed bound and the child's --timeout are the *effective* wait,
    not the requested one (issue #97). listen.py clamps on its side too (#88),
    but that clamp lives in another process: printing the request made the
    parent promise "about 230s, likely truncated" while the child answered
    "clamped to 90s, not truncated" one second later. The requested value is
    still named in the WARN -- "your 200s was not honoured" is itself worth
    saying -- and the escape hatch is halved from the effective value, so
    following it actually shortens the wait.
    """
    import up

    if up.listener_pids():
        print("已有监听在跑，不再重复挂")
        return 0
    # 实际能等多久：请求值先过一遍预算（issue #97）。子监听也会夹一次（#88），
    # 但那是另一个进程——父进程按请求值打印时，两行就各说各话：这边「合计约
    # 230s、很可能被截断」，那边「已压回 90s、不会因此被截断」。所以边界行、
    # 合计值、WARN 里的实际值和子进程参数统一用这一个数。
    wait = wg_budget.clamp_wait(seconds)
    if wait <= 0:
        # 走到这里只有两种可能：调用方直接传了 0（`main()` 用 `if args.wait` 挡住，
        # 正常路径不会），或者等待预算被收缩到 0。两种情况都不该 spawn 一个等 0s 的
        # 子监听再把 NO_WORK 读回来——那是把「额度用尽」伪装成「等过一轮」
        # （issue #97）。
        print("等待额度已用尽，不再续挂")
        return 0
    print(f"等下一次唤醒（最多 {wait}s）…", flush=True)
    # 边界必须写在等待之前（issue #79）。调用被宿主超时截断时，父进程和它起的
    # 子监听在同一个进程组里会一起被杀；run_passthrough 只在子进程正常返回后才把
    # 输出写回来，所以那条路径上「被杀了」和「还在等」在 Agent 眼里长得一模一样。
    # 秒数、合计值和两个出口写在眼前，是这里唯一来得及说的话。
    # 逃生口必须真的更短（issue #97）：按实际等待值折半，且永远小于等待预算；
    # 折半后不足一秒时只留 --no-wait，不推荐 --wait 0（那等于 --no-wait）。
    shorter = min(wait // 2, wg_budget.WAIT_BUDGET - 1)
    shorter_hint = f"要更短：--wait {shorter}；" if shorter > 0 else ""
    print(f"本次调用合计约 {wait + SEND_BUDGET}s（发送 + 等待）；你自己的工具超时"
          f"短于此值，子监听会跟着被杀且不留提示"
          f"（{shorter_hint}立即返回：--no-wait）", flush=True)
    if wg_budget.over_budget(seconds):
        # 与 listen.py 同一句口径（issue #97）：取值会被压回预算，所以**不会**
        # 因此被截断；照旧写「很可能被截断」就是一句假话，而输出里的假话比没有
        # 输出更坏（#88 的教训）。
        print(f"WARN: --wait {seconds}s 加上发送余量 {SEND_BUDGET}s 已超过常见工具调用"
              f"超时 {AGENT_TOOL_TIMEOUT}s；本次已压回等待预算 {wait}s（issue #88），"
              f"不会因此被截断", flush=True)

    cmd = [sys.executable, str(HERE / "listen.py"), "--timeout", str(wait),
           "--cycles", "1", "--no-digest"]
    # Without this the child gets a console window of its own, and a fresh
    # console reads our UTF-8 as the OEM code page. Both were reported.
    return winconsole.run_passthrough(cmd, cwd=str(ROOT))


def main():
    parser = argparse.ArgumentParser(description="回一条群消息并记账")
    parser.add_argument("target", nargs="?", help="要回复的消息 id（必填，偷懒省略会直接报错）")
    parser.add_argument("text", nargs="?", help="回复正文；@ {花名} 会变成真正的 @；换行或字面量 \\n 拆成多条气泡"
                                                f"（默认最多 {MAX_BUBBLES} 条，--max-bubbles 可调）；只发图时可省略")
    parser.add_argument("--image", action="append", default=[], metavar="路径",
                        help="把本地图片发进群，可重复；整张解码通过才算数（PNG/JPEG/GIF/WebP、"
                             "≤6 MiB、≤80 兆像素、≤300 帧），一次最多 9 张，每张独占一条气泡")
    parser.add_argument("--max-bubbles", type=int, metavar="N",
                        help=f"这次最多发几条气泡（1–{MAX_BUBBLES}；默认取 config.json 的 "
                             f"reply.max_bubbles，缺省 {MAX_BUBBLES}。超出的行截断并打印丢弃清单，issue #131）")
    parser.add_argument("--intent", default="回复群友", help="简短沟通意图")
    parser.add_argument("--no-quote", action="store_true", help="不带引用")
    parser.add_argument("--at", action="append", default=[], metavar="花名",
                        help="在正文前面 @ 某人，可重复")
    parser.add_argument("--also", type=int, action="append", default=[], metavar="ID",
                        help="同一条回复顺带算作回了这些消息（连发的 @ 用一条答完）")
    parser.add_argument("--wait", type=int, default=DEFAULT_WAIT, metavar="秒",
                        help="发完就地等下一条唤醒，不用退出后另外重挂监听（默认 "
                             f"{DEFAULT_WAIT}s，0 等同于 --no-wait）")
    parser.add_argument("--no-wait", action="store_true",
                        help="发完直接返回，不原地等下一次唤醒（关闭默认续挂）")
    parser.add_argument("--wait-unmute", action="store_true",
                        help="禁言失败时解禁后自动补发一次（超预算则如实报告）")
    parser.add_argument("--no-followup", action="store_true",
                        help="这次不开追问窗口（默认回复后会继续听群里聊一阵）")
    parser.add_argument("--force", action="store_true",
                        help="目标唤醒已过待回时效也强制回复（默认拒绝，见 up.wake_expired）")
    parser.add_argument("--who", metavar="花名", help="只解析名字到 QQ，不发送")
    parser.add_argument("--group", help="配合 --who；省略则取最近一条事件所在的群")
    parser.add_argument("--list", action="store_true", help="只列出 @ 过我的消息")
    parser.add_argument("--context", type=int, metavar="N",
                        help="列出最近 N 条聊天（含闲聊），回之前先看群里在聊什么")
    parser.add_argument("--around", type=int, metavar="ID",
                        help="配合 --context：只看这条之前的部分")
    parser.add_argument("--show", type=int, metavar="ID",
                        help="按 id 读这条消息的全文（唤醒行/上下文行截断后的补救，issue #32）")
    args = parser.parse_args()
    # 上限是硬顶（issue #131 明确不做无限条）：显式取值越界就别猜，直接报错。
    if args.max_bubbles is not None and not 1 <= args.max_bubbles <= MAX_BUBBLES:
        parser.error(f"--max-bubbles 取值 1–{MAX_BUBBLES}，收到 {args.max_bubbles}")
    # 显式关闭优先于任何 --wait 取值：`--no-wait` 是「我马上还要做别的事」的
    # 出口，被一个默认值盖过去就等于没有出口（issue #69）。
    if args.no_wait:
        args.wait = 0

    # Line buffered even when piped: --wait parks in a child process, and if
    # the caller's timeout kills the group mid-wait, block buffering throws
    # away the SENT line along with it -- leaving no way to tell whether the
    # message went out.
    winconsole.ensure_utf8_stdio(line_buffering=True)

    if args.context is not None:
        return show_context(args.context, args.around)
    if args.show is not None:
        return wg_text.show_event(args.show)
    if args.list:
        return list_wakes()
    if args.who:
        qq, label = resolve_member(args.who, args.group or latest_group_id())
        print(f"{args.who} -> {qq or '未解析'}（{label}）")
        # 专门问一个人时把画像整张给出来（issue #163）：这是唯一「问他是谁」的
        # 入口，唤醒行那 60 字是给接话用的预算，这里不必省。
        card = wg_dossiers.who_card(qq or args.who, args.group or latest_group_id())
        if card:
            print(card)
        return 0 if qq else 1
    if not args.target or not (args.text or args.image):
        parser.error("需要 <event_id> 和 <text>（只发图就加 --image）")

    sid = session_id()
    if not sid:
        print("没有会话 id，先跑 up.py 让 collector 建立会话")
        return 1

    target = int(args.target)
    # 回复一条已过待回时效的老唤醒，等于回头挖一个没人记得的坟。默认拒绝，
    # 确实要回就 --force 显式放行。追问闲聊不在 wg_events.jsonl 里，走不到
    # 这个检查——追问窗口自带 idle/budget 时效，由 followup.stale 把关。
    import up

    stale_row = next((e for e in up.events() if e.get("id") == target), None)
    if stale_row and up.wake_expired(stale_row):
        age_min = (time.time() - (stale_row.get("timestamp") or 0) / 1000) / 60
        if not args.force:
            print(f"拒绝：id={target} 是 {age_min:.0f} 分钟前的唤醒，超过待回时效 "
                  f"{up.pending_reply_ttl()}s，默认不当新话题回。")
            print("      确认要回这条旧消息就加 --force 再跑一次")
            return 1
        print(f"  note: id={target} 已过待回时效（{age_min:.0f} 分钟前），--force 放行")

    ref = event_ref(target)
    found = ref.target if ref else None
    # issue #164 PB（P0 护栏）：目标事件必须属于本会话的群，否则不进 plan()、
    # 不发送、不记账。改前这里是 `found[0] if found else (args.group or
    # latest_group_id())`——目标事件自己的群直接拿来发，一句归属校验都没有：
    # 钉死群 111 的会话能把话发进群 222，还把群 222 那条记成「已回」，另一个
    # 会话于是永远不再回它（doc/MULTI_GROUP_PLAN.md:63 的 P0）。
    # 未钉死时 target_group 原样返回 found，单群机器行为不变。
    try:
        found = wg_isolation.target_group(target, found)
    except wg_isolation.CrossGroup as exc:
        print(f"拒绝：{exc}")
        print(f"      本会话群：{wg_isolation.describe()}")
        return 1
    group_id = found[0] if found else (args.group or latest_group_id())
    if not group_id:
        print("不知道该发到哪个群：事件里没有 group 信息")
        return 1

    # 正文只拆一次：两个分支各拆一次会让超限提示打两遍（issue #131）。
    limit = (args.max_bubbles if args.max_bubbles is not None
             else wg_bubbles.config_limit(wg_config.config()))
    bubbles = split_bubbles(args.text, limit)
    bubble_segments = [build_segments(bubbles[0], group_id, leading=args.at)]
    bubble_segments += [build_segments(one, group_id) for one in bubbles[1:]]
    answered = [target, *[i for i in args.also if i != target]]
    # issue #164 PB：`--also` 也是记账，别群的 id 记成「已回」等于替另一个会话
    # 静默丢消息——那条会永远停在「已回」，而它的会话根本不知道发生过什么。
    if wg_isolation.pinned():
        by_id = {e.get("id"): e for e in up.events()}
        cross = [i for i in answered
                 if i in by_id and not wg_isolation.belongs(by_id[i])]
        if cross:
            print(f"拒绝：--also 里的 {cross} 不属于本会话的群"
                  f"（{wg_isolation.describe()}）——把它们记成「已回」会让另一个"
                  f"会话永远不再回这些消息")
            return 1
    print(f"target={target} group={group_id} session={sid}"
          + (f" {ref.describe()}" if ref else ""))
    # 图片先校验再动手（issue #128）：坏路径在这里就退，一张都不发，也不打印后面
    # 那行「还有 N 条没回」——那会让 Agent 以为图已经发出去了。`limit` 一起传进去：
    # 图也是气泡，正文 + 图超过上限就在发之前拒（间隔 1.2s 会吃掉发送预算，#131）。
    try:
        bubble_segments = wg_images.attach_images(bubble_segments, args.image, cap=limit)
    except wg_images.ImageError as exc:
        print(f"IMAGE_FAILED: {exc}")
        return 1
    has_at = any(seg["type"] == "at" for segs in bubble_segments for seg in segs)

    # The listener is dead between the wake and the re-hang, so @-mentions that
    # land while this reply is being written are only visible in the file. Saying
    # so here is what keeps a burst from being answered by its first message
    # alone: by the time anyone looks again, those ids would read as [已回].
    # Both directions matter -- answering the *newest* of a pair leaves the older
    # one silent with no warning at all, which is the more common shape.
    #
    # issue #207：**只在发出去之后**说（`note_unanswered`）。它是「这条答完了、
    # 还有 N 条」的提醒，不是发送前的待办——写在发送之前，一次失败的调用会同时
    # 留下「SEND_FAILED」和这句，读者分不清哪条才代表结果。
    def note_unanswered():
        missing = unanswered(answered)
        if not missing:
            return
        ids = "、".join(f"{e.get('id')}" + ("（更早）" if (e.get('id') or 0) < target else "")
                        for e in missing)
        print(f"  NOTE 还有 {len(missing)} 条没回：{ids}")
        print("       一条答得完就加 --also，答不完就先回这条再接着回")

    # 追问窗口的时间下界：只把「发送开始之后**开口**」的发言算成接话（开口按消息
    # 自己的时间判，不是本地收到时刻，issue #218）。Agent 拿到唤醒后可能隔十几分钟
    # 才发出回复，这期间群里堆的闲聊 id 都大于被回复的那条，按 id 扫会把它们全算进
    # 预算，窗口开出来就被自己关掉（issue #29）。
    send_started = time.time()

    # Quote through OneBot first: it is the only path that can point at a message
    # from an older batch, which is the normal case once the group is chatting.
    # It is also the only path that can carry an at or image segment -- plan/send
    # speaks plain text, so a message with either must not be handed to it.
    if has_at or args.image or (not args.no_quote and found):
        # 发之前先报账：间隔会吃掉本次调用的等待额度（整次共享的总额，issue #88）。
        # 这条路径按 `BUBBLE_GAP` 固定节奏算；条数取 `bubble_segments`——图片也是气泡（#136）。
        wg_bubbles.report_cost(len(bubble_segments), args.wait)
        message_id = None if args.no_quote else (found[1] if found else None)
        # 发送 + 失败诊断 + 可选解禁补发全在 wg_send_failure（#132）：首条就失败时
        # 由它说出真实原因，不再是那句「带 @ 只能走 OneBot」。
        sent = wg_send_failure.send(group_id, bubble_segments, message_id,
                                    deliver=onebot_send_bubbles, has_at=has_at,
                                    unmute=args.wait_unmute)
        if sent == len(bubble_segments):
            record(answered, args.text or "")
            print("SENT")
            note_unanswered()
            open_followup(target, group_id=group_id,
                          wanted=not args.no_followup, not_before=send_started)
            return wait_for_next(args.wait) if args.wait else 0
        # issue #207：进了 OneBot 直发就**不回退**计划通道，无论发出去几条、本次带没带
        # `--at`。改前 `if sent or has_at: return 1` 在 `sent=0` 且不带 `--at` 时穿过去，
        # 落到 `falling back to the plan path`——那条路径真的会把消息交给 `send()`，
        # 而 `send()` 的气泡写死 `kind=text`：引用段整个丢掉（正是上面那句注释禁止的），
        # 让 Agent 以为「回退过一次」。首条就失败时 `wg_send_failure.handle()` 已经
        # 打过 `SEND_FAILED: <真实原因>`（#132），这里只补一句为什么不换通道。
        print(wg_send_failure.no_fallback_line(has_at=has_at, has_image=bool(args.image)))
        return 1
    elif not args.no_quote:
        print("  no OneBot id on record for this event; using the plan path")

    # 计划通道条数有限，超过就**在发之前**拒绝，别留下半截回复（issue #131）。
    overflow = wg_bubbles.plan_overflow_message(len(bubbles))
    if overflow:
        print(f"REFUSED: {overflow}")
        return 1
    wg_bubbles.report_cost(len(bubbles), args.wait, plan_path=True)

    batch = batch_id()
    if not batch:
        print("没有可用批次，等 collector 收下一批")
        return 1
    plan_id, final_target = plan(sid, batch, target, args.intent, not args.no_quote)
    if not plan_id:
        print("PLAN_FAILED")
        return 1
    quote = (not args.no_quote) and final_target == target
    if send(sid, plan_id, bubbles, quote):
        record(answered, args.text)
        if final_target != target:
            print(f"note: batch moved, plan target was {final_target}")
        print("SENT")
        note_unanswered()
        open_followup(final_target, group_id=group_id,
                      wanted=not args.no_followup, not_before=send_started)
        return wait_for_next(args.wait) if args.wait else 0
    print("SEND_FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
