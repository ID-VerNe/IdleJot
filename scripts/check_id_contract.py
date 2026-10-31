"""钉住「输出记号 ↔ 解析写法」这条往返（issue #204）。

issue #203 拆开了两套编号，形态已经可分辨：唤醒行打 `id=<n>`，追问行打
`id=q<n>`（`water-group/daemon/watch.py:335` / `:386`）。但**形态可分辨不等于抄得回来**
——#204 的缺口全在解析侧这一半：

    $ .venv/Scripts/python.exe .cache/audit-204/roundtrip.py   # 修复前
    id=7  -> BadRef: 看不懂的记号 'id=7'：需要 id=<n>、qid=<n>、<n> 或 q<n> 之一
    id=q7 -> BadRef: 看不懂的记号 'id=q7'：……
    qid=7 -> BadRef: 看不懂的记号 'qid=7'：……

`wg_event_ref.parse()` 当时只认 `id:7` / `qid:7` / `7` / `q7`（冒号分隔），而输出侧
打的是等号：唤醒行与 `--list` 打 `id=7`、追问行与 `--show` 首行打 `id=q7`、追问窗口
状态行（`followup.py:541`）与拒绝信息（`reply.py`）打 `qid=7`。更糟的是拒绝文案自己
宣传的正是 `id=<n>` / `qid=<n>` 这套等号写法——**文案与解析器互相打架**，读者只能靠
手工剥前缀，剥错一次就掉进 BadRef。

本门禁钉四件事，任何一件红都说明契约又断了：

  1. 往返：输出侧打的记号原样抄回 `parse()` 必须认，且落进打它的那套编号
     （`id=7` → 事件流、`id=q7` / `qid=7` → 会话库）；`format_ref()` 打出去的也必须
     原样回得来（渲染 ↔ 解析互逆）；
  2. 文案一致：拒绝文案（`wg_event_ref.HINT`，`reply.py` 复用同一份）里列出来的每一种
     写法都得真的 `parse()` 得过——列出来的写法 parser 不认，报错本身就在教人写错；
  3. 两套编号分别解析到位：撞号夹具（事件流第 7 条与会话库第 7 行）下 `id=7` 与
     `id=q7` 必须各回各的消息（群号与消息编号都不同）；
  4. 输出形态登记在册：`watch.py` 两种行、`up.py --list`、`followup` 状态行的行首形态
     与解析侧一一对应；`--show` / `--context` 的**真实渲染**首行抄回来同样指回同一条。

反例：`id=-`（`wg_gap.py` 的漏行占位，明确不可 `reply.py <id>`）、`Q7`、`q 7`、
`qid=q7` 必须仍然拒绝——放宽到「怎么写都认」等于没有契约。

基线验证（未打补丁的 base 上必须红，且红在原症状）：

    ✗ 行首记号原样抄回解析器: id=7 -> BadRef: 看不懂的记号 'id=7' ……（三条）
    ✗ 撞号时两种记号各回各的消息: id=q7 -> (None, None, None)

    .venv/Scripts/python.exe scripts/check_id_contract.py
"""
import contextlib
import io
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "water-group" / "daemon"

import check_fixture_support as fs  # noqa: E402  (issue #169 PE: path 注入收口)
from check_fixture_support import pin_state_env, scratch_dir  # noqa: E402
fs.bootstrap_paths(scripts=False, root=False)

TMP = scratch_dir(prefix="wg_id_contract_")
DB = TMP / "fixture.sqlite3"
STATE = TMP / "state"
STATE.mkdir(parents=True, exist_ok=True)
# 两套 DB 变量都钉死：优先级是 IDLEJOT_* > TULPA_*，只钉一套时，机器上残留的另一套
# 会把解析指回实机库——夹具是空的，撞号用例会红在「查不到」而不是契约上。
for _name in ("IDLEJOT_WG_DB", "TULPA_WG_DB"):
    os.environ[_name] = str(DB)
# state 两腿钉死同走 pin_state_env（#281：IDLEJOT 先于 TULPA，两腿都钉堵穿透）。
pin_state_env(STATE)
# 钉死群：`--context` / 隔离路径要一个确定的本会话（与 check_event_ref 同一口径）。
os.environ["IDLEJOT_WG_CONVERSATION"] = "10001:group:123"

import reply  # noqa: E402
import wg_event_ref  # noqa: E402
import wg_text  # noqa: E402

GROUP_EVENTS = "123"
GROUP_INBOX = "222"
SID = "session-contract"
EVENTS = STATE / "wg_events.jsonl"

# 撞号夹具：事件流第 7 条（群 123 / 消息 7007）与会话库第 7 行（群 222 / 消息 7777）
# 同号——#203 的证据链就是这个形状，两套编号各自分配，撞号是常态。
CLASH = 7
CONTEXT_ROW = 8


def build():
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE chat_sessions (id TEXT, conversation_id TEXT, active INTEGER)")
    db.execute("CREATE TABLE chat_inbox (id INTEGER PRIMARY KEY, session_id TEXT,"
               " message_id TEXT, payload TEXT, at REAL,"
               " media TEXT NOT NULL DEFAULT '[]')")
    db.execute("INSERT INTO chat_sessions VALUES (?, ?, 1)",
               (SID, f"10001:group:{GROUP_INBOX}"))
    for row_id, conversation, message_id, content in (
            (CLASH, f"10001:group:{GROUP_INBOX}", "7777", "会话库那条"),
            # 第 8 行属于钉死的本群：`--context` 要能列出它，好让「上下文行的行首
            # 记号抄回来」这条往返走到真输出上。
            (CONTEXT_ROW, f"10001:group:{GROUP_EVENTS}", "8888", "上下文那条")):
        payload = {"conversation_id": conversation, "sender": "乙", "content": content,
                   "onebot_message_id": message_id,
                   "timestamp": 1792500000000 + row_id}
        db.execute("INSERT INTO chat_inbox (id, session_id, message_id, payload, at)"
                   " VALUES (?, ?, ?, ?, ?)",
                   (row_id, SID, message_id, json.dumps(payload, ensure_ascii=False),
                    1792500000.0 + row_id))
    db.commit()
    db.close()
    EVENTS.write_text(json.dumps({
        "id": CLASH, "conversation_id": f"10001:group:{GROUP_EVENTS}",
        "onebot_message_id": "7007", "sender": "甲", "content": "事件流那条",
        "timestamp": 1792500000000,
    }, ensure_ascii=False) + "\n", encoding="utf-8")


# 输出侧打出去的每一种行首写法：(记号, 期望命名空间)。
# 这张表就是「输出即输入」的契约清单——少一格，那处输出就又变成抄不回来的记号。
WRITE_FORMS = (
    ("7", wg_event_ref.EVENT),       # 唤醒行 / --list / --show 首行的行首
    ("id=7", wg_event_ref.EVENT),    # 同上，带 id= 前缀
    ("id:7", wg_event_ref.EVENT),    # #203 起的冒号写法（内部 / 文档）
    ("q7", wg_event_ref.INBOX),      # --context 行 / format_ref 的产物
    ("id=q7", wg_event_ref.INBOX),   # 追问行 / --show 首行的行首
    ("qid=7", wg_event_ref.INBOX),   # followup 状态行 / reply 拒绝信息
    ("qid:7", wg_event_ref.INBOX),   # #203 起的冒号写法
)

# 明确不是记号的写法（全部必须继续抛 BadRef）。
REJECTED = ("", "id=-", "Q7", "q 7", "qid=q7", "id:q7", "q", "-7", "7.0")

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def fail(*lines):
    """红的时候把签名打出来：门禁只报 True/False 的话，复现时看不出红在哪。"""
    for line in lines:
        print(f"      {line}")
    return False


@case
def 行首记号原样抄回解析器():
    """issue #204 的本体：输出侧打的记号，原样抄给 parse() 必须认且归对套。"""
    wrong = []
    for text, want in WRITE_FORMS:
        try:
            got = wg_event_ref.parse(text)
        except wg_event_ref.BadRef as exc:
            wrong.append(f"{text} -> BadRef: {exc}")
            continue
        if got != (want, 7):
            wrong.append(f"{text} -> {got}，期望 {(want, 7)}")
    return True if not wrong else fail(*wrong)


@case
def 渲染与解析互逆():
    """`format_ref()` 打出去的记号必须原样回得来——渲染与解析只许一份口径。"""
    wrong = []
    for namespace in wg_event_ref.NAMESPACES:
        text = wg_event_ref.format_ref(namespace, CLASH)
        try:
            got = wg_event_ref.parse(text)
        except wg_event_ref.BadRef as exc:
            wrong.append(f"format_ref({namespace})={text!r} 抄不回来：{exc}")
            continue
        if got != (namespace, CLASH):
            wrong.append(f"format_ref({namespace})={text!r} -> {got}")
    return True if not wrong else fail(*wrong)


@case
def 拒绝文案列的写法解析器都认():
    """文案与解析器逐字一致：HINT 里列出来的每种写法都得 parse() 得过。

    拒绝文案是读者唯一看得见的「合法写法清单」；它列的写法 parser 不认，就等于
    报错本身在教人写一个跑不通的记号（#204 改前的原症状）。
    """
    wrong = []
    body = wg_event_ref.HINT.removeprefix("需要 ").removesuffix(" 之一")
    forms = [part.strip() for part in re.split(r"、| 或 ", body) if part.strip()]
    if len(forms) < 4:
        wrong.append(f"HINT 拆不出写法清单：{wg_event_ref.HINT!r}")
    for form in forms:
        text = form.replace("<n>", "7")
        try:
            wg_event_ref.parse(text)
        except wg_event_ref.BadRef as exc:
            wrong.append(f"HINT 列出的 {text} 抄不回来：{exc}")
    # reply.py 的拒绝信息必须复用同一份文案，不许自己再抄一遍写法清单。
    reply_source = (DAEMON / "reply.py").read_text(encoding="utf-8")
    if "wg_event_ref.HINT" not in reply_source:
        wrong.append("reply.py 的拒绝信息没有复用 wg_event_ref.HINT（写法清单又会有两份）")
    return True if not wrong else fail(*wrong)


@case
def 撞号时两种记号各回各的消息():
    """`id=7` 与 `id=q7` 指向两条不同的消息（群号与消息编号都不同）。

    这是 #203/#204 的核心断言：同号不同消息，记号写错就挂到别的群去。
    """
    wrong = []
    event = reply.resolve_target(f"id={CLASH}")
    inbox = reply.resolve_target(f"id=q{CLASH}")
    if event != (wg_event_ref.EVENT, CLASH, (GROUP_EVENTS, "7007")):
        wrong.append(f"id=7 -> {event}，期望 (event, 7, ('{GROUP_EVENTS}', '7007'))")
    if inbox != (wg_event_ref.INBOX, CLASH, (GROUP_INBOX, "7777")):
        wrong.append(f"id=q7 -> {inbox}，期望 (inbox, 7, ('{GROUP_INBOX}', '7777'))")
    # 简写与裸数字各有各的口径（#203 的语义不变）。
    if reply.resolve_target(str(CLASH)) != event:
        wrong.append("裸 7 不再是事件流那条")
    if reply.resolve_target(f"q{CLASH}") != inbox:
        wrong.append("q7 不再是会话库那条")
    return True if not wrong else fail(*wrong)


@case
def 输出侧各处的行首形态都对得上解析侧():
    """`watch.py` 两种行 / `--list` / 追问状态行的行首形态，与解析口径对得上。

    渲染点是散的（四个文件各打各的），解析口径只有一份——形态一旦被人改回去
    （追问行打回裸数字、状态行换个前缀），这条就红，不必等 Agent 在群里踩。
    """
    checks = (
        ("water-group/daemon/watch.py", 'print(f"id={e.get(\'id\')}', "7"),
        ("water-group/daemon/watch.py", 'print(f"id={ref} {stamp} [追问]', "id=q7"),
        ("water-group/daemon/watch.py", "wg_event_ref.format_ref(wg_event_ref.INBOX", None),
        ("water-group/daemon/up.py", "id={e.get('id')}", "id=7"),
        ("water-group/daemon/followup.py", "qid={window.get('after_id')}", "qid=7"),
        ("water-group/daemon/wg_text.py", 'print(f"id={ref} ', "id=q7"),
    )
    wrong = []
    for rel, needle, token in checks:
        if needle not in (ROOT / rel).read_text(encoding="utf-8"):
            wrong.append(f"{rel} 里找不到行首形态 {needle!r}")
        if token is not None:
            try:
                wg_event_ref.parse(token)
            except wg_event_ref.BadRef as exc:
                wrong.append(f"{rel} 打出的 {token!r} 抄不回来：{exc}")
    return True if not wrong else fail(*wrong)


@case
def 只读输出的首行抄回来也指回同一条():
    """真实渲染（`--show` 两种命名空间 / `--context` 行）的行首记号，各归各的。"""
    wrong = []
    outs = {}
    for namespace in wg_event_ref.NAMESPACES:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = wg_text.show_event(CLASH, namespace=namespace)
        if code != 0:
            wrong.append(f"--show {namespace} 退出码 {code}")
            continue
        outs[namespace] = buf.getvalue().splitlines()[0].split(" ", 1)[0]
    for namespace, token in outs.items():
        try:
            got = wg_event_ref.parse(token)
        except wg_event_ref.BadRef as exc:
            wrong.append(f"--show {namespace} 行首 {token!r} 抄不回来：{exc}")
            continue
        if got != (namespace, CLASH):
            wrong.append(f"--show {namespace} 行首 {token!r} -> {got}")
    # `--context` 行的行首是 `q<n>`（不带 id=），同样要抄得回来。
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        reply.show_context(20)
    line = next((ln for ln in buf.getvalue().splitlines()
                 if ln.startswith(f"q{CONTEXT_ROW} ")), "")
    if not line:
        wrong.append(f"--context 没列出 q{CONTEXT_ROW}：{buf.getvalue()!r}")
    else:
        token = line.split(" ", 1)[0]
        if wg_event_ref.parse(token) != (wg_event_ref.INBOX, CONTEXT_ROW):
            wrong.append(f"--context 行首 {token!r} 归错套")
    return True if not wrong else fail(*wrong)


@case
def 不是记号的写法仍然拒():
    """放宽到「怎么写都认」等于没有契约：占位符与打错的记号必须显式拒绝。"""
    wrong = []
    for text in REJECTED:
        try:
            got = wg_event_ref.parse(text)
        except wg_event_ref.BadRef:
            continue
        wrong.append(f"{text!r} 竟然被认成 {got}")
    return True if not wrong else fail(*wrong)


@case
def 自检_解析退化成一律事件流时撞号用例必须翻转():
    """判据随实现翻转：把 parse 钉成「剥掉记号一律当事件流」，本门禁必须红。

    改前那一代正是这样：不认记号的形状，见数字就当事件流 id，于是 `id=q7` 被
    解析成事件流第 7 条——引用挂到另一个群那条消息上。
    """
    real = wg_event_ref.parse

    def degraded(ref):
        digits = re.sub(r"\D", "", str(ref))
        return wg_event_ref.EVENT, int(digits)

    wg_event_ref.parse = degraded
    try:
        event = reply.resolve_target(f"id={CLASH}")
        inbox = reply.resolve_target(f"id=q{CLASH}")
        merged = (event == inbox
                  or event != (wg_event_ref.EVENT, CLASH, (GROUP_EVENTS, "7007"))
                  or inbox != (wg_event_ref.INBOX, CLASH, (GROUP_INBOX, "7777")))
        try:
            wg_event_ref.parse("id=q7")
            shaped = False   # 退化后不再认记号形状：这正是改前的行为
        except wg_event_ref.BadRef:
            shaped = True
        if merged and not shaped:
            return True
        return fail(f"degraded 后 event={event} inbox={inbox}",
                    f"记号形状仍被保留（{shaped}）——自检没有真的翻转判据")
    finally:
        wg_event_ref.parse = real


def main():
    return fs.run_cases(CASES, setup=build)


if __name__ == "__main__":
    sys.exit(main())
