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

<event_id> 有两套命名空间，解析口是 wg_event_ref.py（issue #203）：裸数字是事件流
`wg_events` 的 id；`q<n>` / `qid:<n>` 是会话库 `chat_inbox` 的 rowid（追问行与
`--context` 打出来的就是它），`id:<n>` 是事件流的显式写法。两套编号会撞号，照抄带
`q` 的写法即可；裸数字在事件流没命中、会话库唯一命中时回库兜底，并打一行源说明，
不静默换源。会话库的行必须属于本会话，钉死会话时跨会话的行直接拒。
`--also` 只做存在性与归属校验，不进 `wg_replied.jsonl`。

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
import wg_event_ref  # noqa: E402  (id / qid 两套命名空间 + 事件 id 的唯一解析入口，#203/#216)
import wg_images  # noqa: E402
import wg_isolation  # noqa: E402  (群身份与跨群护栏，issue #164)
import wg_members  # noqa: E402  (花名 -> QQ 的唯一出口，issue #30/#203)
import wg_quotes  # noqa: E402  (引用原文渲染，issue #94)
import wg_send_failure  # noqa: E402  (发送失败的原文 / 禁言诊断 / 失败账本，issue #132)
import wg_text  # noqa: E402  (正文渲染：段占位不占额度，issue #32)
from wg_wakes import list_wakes  # noqa: E402,F401  (--list 视图，issue #203 搬出)

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


def event_target(event_id):
    """``(group_id, onebot_message_id)``；群号拿得到、编号拿不到时回 ``(群号, "")``。

    issue #203：记号自己带命名空间——``7`` / ``id:7`` 是事件流，``q7`` / ``qid:7``
    是会话库，写法见表在 ``wg_event_ref`` 的模块 docstring。改前这里只有裸 id：
    先遍历 ``wg_events.jsonl``，未命中再回库兜底——两套编号各撞各的号时，追问行那条
    ``id=7`` 会被静默解析成群 111 的事件，引用于是挂到另一个群的消息上。

    issue #216：行在但编号取不到（payload 少键）时不再与「id 根本不存在」一样回
    ``None``——那条能回，只是挂不上引用（钉死群下直接抛 ``CrossGroup`` 是改前的
    谎报）。群号与编号都解析不出来才回 ``None``。
    """
    return resolve_target(event_id)[2]


def resolve_target(event_id):
    """``(namespace, number, found)``：命中的是哪套编号、编号、``(群号, message_id)``。

    与 :func:`event_target` 同一次查找，只是把「实际落在哪套编号」也交出来——
    裸数字回库兜底时调用方要能说出来源，不许把库那条当成事件流那条（#203）。
    非法记号返回 ``(None, None, None)``。
    """
    try:
        namespace, number = wg_event_ref.parse(event_id)
    except wg_event_ref.BadRef:
        return None, None, None
    ref = event_ref(number, namespace)
    return namespace, number, (ref.target if ref else None)


def event_ref(event_id, namespace=wg_event_ref.EVENT):
    """一条事件的 `Ref`（来源 + 群 + 编号），一次解析到位。

    解析归 `wg_event_ref` 一处——字段名兼容与「回查库列」都在那里（issue #216）；
    `namespace` 决定问哪套编号（issue #203），默认事件流。

    解析器两个来源都没有，才问插件那一路：`event_lookup` 是扩展点（私有插件可以
    自己接），默认实现 `followup.lookup` 就是同一个解析器——所以这条只在「id 真的
    不在库里」时多走一次，不会盖掉上面的结论。插件那一路只认库记号（它拿
    ``q<n>`` 去查），所以事件流编号不会走到这里。
    """
    ref = None
    try:
        if namespace == wg_event_ref.INBOX:
            ref = wg_event_ref._ref_from_inbox(int(event_id))  # noqa: SLF001
        else:
            ref = wg_event_ref.resolve_id(event_id)
    except (TypeError, ValueError):
        return None
    if ref is not None or namespace == wg_event_ref.INBOX:
        return ref
    target = plugins.emit("event_lookup", {"event_id": f"qid:{int(event_id)}"})
    if not target:
        return None
    return wg_event_ref.Ref(event_id=int(event_id), source=wg_event_ref.SRC_PLUGIN,
                            group_id=str(target[0]), message_id=str(target[1] or ""))


def _inbox_target_of(event_id):
    """会话库兜底：追问唤醒要回的是被 mention_only 滤掉的闲聊，事件流里没有它。

    回库拿编号，引用才挂得上，群号也只能从这里来。DB 兜底归追问插件，不过它的
    三态开关：关了窗口也要能回 ``--context`` 里看到的闲聊（PD2a）。
    """
    return plugins.emit("event_lookup", {"event_id": f"qid:{event_id}"})


def _inbox_session_of(event_id):
    """这条会话库行是哪个会话收到的；读不到返回空串（issue #203）。"""
    return wg_event_ref.inbox_session_of(event_id)


def resolve_member(name, group_id):
    """花名 -> (qq, label)：实现搬到 `wg_members`（issue #203），这里只是再导出。"""
    return wg_members.resolve_member(name, group_id)


def _looking_for(who, group_id):
    """`--who` 的打印：解析结果 + 整张画像卡（issue #163 的唯一「问他是谁」入口）。

    返回退出码：认到人才是 0，认不到给 1（调用方当失败处理）。
    """
    qq, label = resolve_member(who, group_id)
    print(f"{who} -> {qq or '未解析'}（{label}）")
    # 唤醒行那 60 字是给接话用的预算，专门问一个人时不必省。
    card = wg_dossiers.who_card(qq or who, group_id)
    if card:
        print(card)
    return 0 if qq else 1


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


def _resolve_or_reject(parser, args, sid):
    """解析 target 记号并做归属校验；通过就返回 (namespace, number, found, group_id)。

    失败一律返回 None（拒绝理由已打印），调用方直接 `return 1`。
    """
    try:
        ref_namespace, target = wg_event_ref.parse(args.target)
    except wg_event_ref.BadRef as exc:
        parser.error(f"target {exc}")
    # 回复一条已过待回时效的老唤醒，等于回头挖一个没人记得的坟。默认拒绝，
    # 确实要回就 --force 显式放行。追问闲聊不在 wg_events.jsonl 里，走不到
    # 这个检查——追问窗口自带 idle/budget 时效，由 followup.stale 把关。
    import up

    # 待回时效只对**事件流**的唤醒有意义：`q7` 是库里的闲聊，不在 `up.events()`
    # 里，按数字去比对会拿别群同号那条的时效来判它（issue #203）。
    stale_row = None
    if ref_namespace == wg_event_ref.EVENT:
        stale_row = next((e for e in up.events() if e.get("id") == target), None)
    if stale_row and up.wake_expired(stale_row):
        age_min = (time.time() - (stale_row.get("timestamp") or 0) / 1000) / 60
        if not args.force:
            print(f"拒绝：id={target} 是 {age_min:.0f} 分钟前的唤醒，超过待回时效 "
                  f"{up.pending_reply_ttl()}s，默认不当新话题回。")
            print("      确认要回这条旧消息就加 --force 再跑一次")
            return None
        print(f"  note: id={target} 已过待回时效（{age_min:.0f} 分钟前），--force 放行")

    # issue #203：查找按记号自己的命名空间走，撞号时只可能指回自己那条。裸数字在
    # 事件流里查不到、回库兜底命中时，这里把来源说出来——「引用挂上了」必须能看出
    # 挂的是哪套编号里的那条。
    resolved_ns, resolved_no, found = resolve_target(args.target)
    if resolved_ns is None:
        print(f"拒绝：看不懂的记号 {args.target!r}（{wg_event_ref.HINT}）")
        return None
    if resolved_ns != ref_namespace and found is not None:
        print(f"  note: {args.target} 是事件流编号，事件流里没有它；按"
              f"{wg_event_ref.describe(resolved_ns)}的同号消息解析（要回库那条请写 "
              f"q{resolved_no}）")
    target = resolved_no
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
        return None
    # issue #203 修复候选 3：库里的行还要满足「确实属于本会话」——撞号时
    # `target_group` 校验的是本行自己的群号，但库那张表是跨会话的，光有群号
    # 分不出「本会话在群里看到的那条」与「另一个会话在同一个群里的另一条」。
    if resolved_ns == wg_event_ref.INBOX and wg_isolation.pinned():
        owner = _inbox_session_of(target)
        if owner and owner != sid:
            print(f"拒绝：qid={target} 属于另一个会话（{owner}），本会话是 {sid}"
                  f"——那条是别的会话在群里收到的，回它会把它的记账搅乱")
            return None
    return resolved_ns, target, found


def _check_also(also_inbox, answered):
    """`--also` 的归属校验：库编号查存在性，事件编号查是不是本会话的群。"""
    import up

    for no in also_inbox:
        _, _, hit = resolve_target(f"q{no}")
        if hit is None:
            print(f"拒绝：--also q{no} 在会话库里查不到这条消息")
            return False
        if wg_isolation.pinned() and str(hit[0]) != wg_isolation.group_id():
            print(f"拒绝：--also q{no} 属于群 {hit[0]}，不属于本会话的群"
                  f"（{wg_isolation.describe()}）")
            return False
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
            return False
    return True


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


def open_followup(event_id, group_id="", wanted=True, not_before=None, after_ref=None):
    """发送成功后开追问窗口，让话题继续；wanted=False 或插件停了就不开。

    not_before 是发送开始的墙上时间，只作**预算标记**：处理期间堆下的历史闲聊照常
    唤醒但不扣额度（issue #29 / #117），只有发送开始之后开口的接话才占额度——「开口」
    按消息自己的时间判，不是本地收到时刻（issue #218）。可见性由 ``after_ref`` 划界，
    见下。group_id 带进去，三态门控按群判（issue #4 PD2a）——这个群关了追问窗口，
    别的群照开。

    ``after_ref`` 是窗口游标**在这条回复链路上**的记号（issue #203）：回的是唤醒行
    就给事件流写法（裸数字 / ``id:<n>``），回的是追问行就给 ``q<n>``——由这里定
    命名空间，插件原样转给 ``followup.open``。省略时退回 ``event_id``，与改前一致。
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
            # 窗口游标的记号（issue #203）：回唤醒行给裸数字 / ``id:<n>``，回追问行
            # 给 ``q<n>``。省略时退回 ``event_id``，与改前一致。
            "after_ref": event_id if after_ref is None else after_ref,
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


def _build_parser():
    """16 个参数的登记表；取值校验与副作用都在 `main()` 里。

    单独拎出来只为让 `main()` 读得懂（issue #11 的体量红线），没有别的作用。
    """
    parser = argparse.ArgumentParser(description="回一条群消息并记账")
    parser.add_argument("target", nargs="?",
                        help="要回复的消息：行首记号原样抄即可（唤醒行/--list 的 id=<n>、"
                             "追问行/上下文行的 id=q<n> 与 q<n>、状态行的 qid=<n> 都认；"
                             "issue #204），只写 <n> 或 q<n> 也行（必填，偷懒省略会直接报错；issue #203）")
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
    parser.add_argument("--also", action="append", default=[], metavar="ID",
                        help="同一条回复顺带算作回了这些消息（连发的 @ 用一条答完）；"
                             "写法同 target：<n>/id=<n> 是事件流，q<n>/id=q<n> 是会话库")
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
    parser.add_argument("--around", metavar="ID",
                        help="配合 --context：只看这条之前的部分（编号同上下文行；"
                             "上下文行是会话库的 q<n>，写成 q<n> 或 <n> 都行）")
    parser.add_argument("--show", dest="show", metavar="ID",
                        help="按 id 读这条消息的全文（唤醒行/上下文行截断后的补救，issue #32）："
                             "<n> 读事件流，q<n> 读会话库")
    return parser


def _run_readonly(args, parser):
    """只读分支（--context / --show / --list / --who）：有命中就返回退出码，否则 None。"""
    if args.context is not None:
        around = None
        if args.around is not None:
            # 上下文行打的是会话库编号（q<n>），`--around` 也按库口径解释；
            # 写成 `q7` / `7` 都落到同一个 rowid（issue #203）。
            try:
                around = wg_event_ref.parse(args.around)[1]
            except wg_event_ref.BadRef as exc:
                parser.error(f"--around {exc}")
        return show_context(args.context, around)
    if args.show is not None:
        try:
            namespace, number = wg_event_ref.parse(args.show)
        except wg_event_ref.BadRef as exc:
            parser.error(f"--show {exc}")
        return wg_text.show_event(number, namespace=namespace)
    if args.list:
        return list_wakes()
    if args.who:
        return _looking_for(args.who, args.group or latest_group_id())
    return None

def main():
    parser = _build_parser()
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

    readonly = _run_readonly(args, parser)
    if readonly is not None:
        return readonly
    if not args.target or not (args.text or args.image):
        parser.error("需要 <event_id> 和 <text>（只发图就加 --image）")

    sid = session_id()
    if not sid:
        print("没有会话 id，先跑 up.py 让 collector 建立会话")
        return 1

    resolved = _resolve_or_reject(parser, args, sid)
    if resolved is None:
        return 1
    resolved_ns, target, found = resolved
    # 这条的 `Ref` 只用来打来源标记：解析结论已由 `resolve_target` 给出，同一结论
    # 不再走第二遍（`--show` 与唤醒行用的是同一个 `src=` 口径，issue #216）。
    ref = event_ref(target, resolved_ns)
    group_id = found[0] if found else (args.group or latest_group_id())
    if not group_id:
        print("不知道该发到哪个群：事件里没有 group 信息")
        return 1

    # issue #208: `--at` 全部未解析时提前拦下。改前 build_segments 把名字按文字
    # 发出，has_at=False，用户的 @ 意图静默丢失，OneBot 直发失败时还打「未退回
    # 计划通道：带引用…」——与「没 @ 成」自相矛盾。零请求、不发、不记账（与 #131
    # 超限提前拒绝同口径）。部分解析时照发（时间敏感优先），未解析的名字仍由
    # build_segments 的 `at:` 前缀行说明。
    if args.at:
        unresolved = [(name, reason)
                      for name in args.at
                      for qq, reason in [resolve_member(name, group_id)]
                      if not qq]
        if unresolved and len(unresolved) == len(args.at):
            print("AT_UNRESOLVED: " + "、".join(f"{n}（{r}）" for n, r in unresolved))
            return 1

    # 正文只拆一次：两个分支各拆一次会让超限提示打两遍（issue #131）。
    limit = (args.max_bubbles if args.max_bubbles is not None
             else wg_bubbles.config_limit(wg_config.config()))
    bubbles = split_bubbles(args.text, limit)
    bubble_segments = [build_segments(bubbles[0], group_id, leading=args.at)]
    bubble_segments += [build_segments(one, group_id) for one in bubbles[1:]]

    # issue #203：`--also` 里的库编号（q<n>）不进取答账本——账本只服务
    # wg_events 的「已回」判定，把库 rowid 写成数字会让一个同号的事件流唤醒
    # 被静默标成已回（这正是撞号的本体）。库那条的接话由追问窗口自己推游标，
    # 这里只需校验它确实存在、且属于本会话的群，再在记账时跳过它。
    also_event, also_inbox = [], []
    for ref in args.also:
        try:
            ns, no = wg_event_ref.parse(ref)
        except wg_event_ref.BadRef as exc:
            print(f"拒绝：--also {exc}")
            return 1
        (also_inbox if ns == wg_event_ref.INBOX else also_event).append(no)
    answered = [target, *[i for i in also_event if i != target]]
    if not _check_also(also_inbox, answered):
        return 1
    if also_inbox:
        print(f"  note: --also {'、'.join(f'q{i}' for i in also_inbox)} 是会话库里的"
              f"闲聊，不进取答账本（追问窗口自己推游标）")
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
    # 追问窗口的游标是 **chat_inbox 的 rowid**，与事件流 id 是两套编号：回一条
    # 追问行必须把它记住成 `q<n>`，否则开出来的窗口从别群同号那条往后看，
    # 回复挂的引用与窗口听得见的话会指向两条不同的消息（issue #203）。
    followup_ref = wg_event_ref.format_ref(resolved_ns, target)

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
                          wanted=not args.no_followup, not_before=send_started,
                          after_ref=followup_ref)
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
                      wanted=not args.no_followup, not_before=send_started,
                      after_ref=followup_ref)
        return wait_for_next(args.wait) if args.wait else 0
    print("SEND_FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
