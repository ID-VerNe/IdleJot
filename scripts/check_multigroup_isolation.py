"""issue #164 gate: 一个会话只看一个群——读侧过滤、发送前校验、身份收口。

症状（issue #164）：`wg_events.jsonl` 单文件混装所有群，而读侧与写侧都不认群。

  - `watch.py` 的 `fresh` 只看 `id > cursor` / `wake_reason` / `replied_after` /
    `wake_expired`，**没有一条与 `conversation_id` 有关**；
  - `reply.list_wakes()` 遍历全量事件流，同样不认群；
  - `reply.py` 拿到目标事件**自己所在的群**就直接发（`found[0]`），没有一句
    「这条属于本会话该管的群吗」——钉死群 111 的会话能把话发进群 222，还把群 222
    那条记成「已回」，另一个会话于是永远不再回它（P0）；
  - `session.py` 自己 `json.loads(ROOT/water-group/config.json)` 绕过 `wg_config`，
    群号取「第一个 enabled 的群」——多群时由配置书写顺序决定。

本门禁钉十件事，每条都带对照组（#153 教训：只断言「看不到别群」会被「怎么改都
看不到任何消息」这种坏实现骗过）：

  1. 钉死群 111 → `watch.py` 只出 111 那条，**且 111 那条必须在**；
  2. 钉死群 111 → `reply.py --list` 同上，用正则捕获组取 `id=`（#151 教训）；
  3. 未钉死 + 多群 → 两个群都看得到（改前行为不变；证明 1/2 不是「一律清空」）；
  4. 钉死群 111 → `reply.py 2 "x"` 报错退出、零请求、`wg_replied.jsonl` 无 `id=2`；
  5. 钉死群 111 → `reply.py 1 "x"` 照常走到发送（对照组：护栏不挡本群）；
  6. `plan()` 的 `target_not_in_batch` 只在本群 id 里挑；全是别群 id 时失败退出；
  7. 身份解析四档：env > `<state>/wg_conversation.txt` > config 里唯一的群 > 空；
  8. 多群未钉死 → `session.identity_problem()` 非空（拒绝启动），单群 → 空，
     **零配置 → 也必须空且 `ensure()` 照常重建**（对照组：这条踩过，
     `check_issue_16_audit_fixes.py` 的 `ensure()` 用例就是零配置夹具）；
  9. `wg_isolation.describe()` 歧义态说「未钉死」且带「隔离」字样，钉死态报依据；
 10. 判据完整性自检：把 `rows_of` 换成恒空 → 1/2 必须红；换成恒返回全部 → 3 必须红。

**已知边界**（不声称「任何错都红」）：本门禁不覆盖 issue #164 的 PD 分节（采集侧
按群展开，`collector` 仍只采一个群），也不覆盖「同一状态目录下两个群共用一个游标、
高 id 把游标推过别群未读的低 id」——那要按群分状态目录才自愈，属 PD。此外它不
枚举「跨群误发的每一种形状」：`--also` 只覆盖「事件流里能查到的事件」。

    .venv/Scripts/python.exe scripts/check_multigroup_isolation.py
"""
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import check_fixture_support as fs  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import scratch_dir  # noqa: E402
fs.bootstrap_paths(scripts=False, root=False)

DAEMON = ROOT / "water-group" / "daemon"

# 数据根 / 状态目录 / 配置 / 库全部钉进临时目录，且在 import 任何 daemon 模块
# **之前**完成：`wg_paths.state_dir()` / `wg_config.CONFIG` 都是 import 期求值。
SCRATCH = scratch_dir(prefix="wg_multigroup_")
STATE = SCRATCH / "state"
STATE.mkdir(parents=True, exist_ok=True)
CONFIG = SCRATCH / "config.json"
DB = SCRATCH / "none.sqlite3"
os.environ["WATER_GROUP_HOME"] = str(SCRATCH)
os.environ["IDLEJOT_WG_STATE"] = str(STATE)
os.environ["TULPA_WG_STATE"] = str(STATE)
os.environ["IDLEJOT_WG_CONFIG"] = str(CONFIG)
os.environ["IDLEJOT_WG_DB"] = str(DB)
os.environ.pop("IDLEJOT_WG_CONVERSATION", None)
os.environ.pop("TULPA_WG_CONVERSATION", None)

ACCOUNT = "10001"
SELF = ACCOUNT
GROUP_A = "111"
GROUP_B = "222"
CONV_A = f"{ACCOUNT}:group:{GROUP_A}"
CONV_B = f"{ACCOUNT}:group:{GROUP_B}"
# 每条用例一个 id 段，互不干扰（时间戳取此刻：过期唤醒会被 reply.py 直接拒）。
NOW_MS = int(time.time() * 1000)
A_ID, B_ID = 9101, 9102

WATCH = DAEMON / "watch.py"
REPLY = DAEMON / "reply.py"

ID_RE = re.compile(r"^id=(\d+)\b")


def write_config(groups, **extra):
    payload = {"self_account": ACCOUNT, "onebot_token": "multigroup-fixture",
               "self_names": ["小鲸鱼"], "groups": groups}
    payload.update(extra)
    CONFIG.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def both_groups():
    return {GROUP_A: {"enabled": True}, GROUP_B: {"enabled": True}}


def event(event_id, conversation_id, content, sender="群友"):
    return {"id": event_id, "conversation_id": conversation_id, "sender": sender,
            "content": content, "timestamp": NOW_MS, "is_self": False,
            "segments": [{"type": "at", "qq": SELF}],
            "onebot_message_id": f"m{event_id}"}


def seed(rows):
    (STATE / "wg_events.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")


def two_group_events():
    return [event(A_ID, CONV_A, "A 群喊你"), event(B_ID, CONV_B, "B 群喊你")]


def clear_state():
    for name in ("wg_events.jsonl", "wg_replied.jsonl", "wg_watch.txt",
                 "wg_conversation.txt", "wg_session.txt", "wg_state.json"):
        (STATE / name).unlink(missing_ok=True)


def child_env(conversation=None):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["WATER_GROUP_HOME"] = str(SCRATCH)
    env["IDLEJOT_WG_STATE"] = str(STATE)
    env["TULPA_WG_STATE"] = str(STATE)
    env["IDLEJOT_WG_CONFIG"] = str(CONFIG)
    env["IDLEJOT_WG_DB"] = str(DB)
    for name in ("IDLEJOT_WG_CONVERSATION", "TULPA_WG_CONVERSATION"):
        env.pop(name, None)
    if conversation:
        env["IDLEJOT_WG_CONVERSATION"] = conversation
    return env


def run(script, *args, conversation=None, timeout=120):
    import subprocess
    return subprocess.run([sys.executable, str(script), *args], cwd=str(ROOT),
                          env=child_env(conversation), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def listed_ids(out):
    """唤醒块里的 id 集合。正则捕获组，不用子串（#151 教训）。"""
    ids = set()
    for line in out.splitlines():
        match = ID_RE.match(line.strip())
        if match:
            ids.add(int(match.group(1)))
    return ids


def ledger_ids():
    path = STATE / "wg_replied.jsonl"
    if not path.exists():
        return set()
    return {json.loads(line)["id"]
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# ---------- PA：读侧按群过滤 ----------


@case
def watch_只出本群():
    """钉死群 111：只有 111 那条。**111 必须在**——否则「一律清空」也会绿。"""
    clear_state()
    write_config(both_groups())
    seed(two_group_events())
    proc = run(WATCH, "--timeout", "3", "--replay", conversation=CONV_A)
    ids = listed_ids(proc.stdout or "")
    ok = (A_ID in ids and B_ID not in ids and "WAKE" in (proc.stdout or ""))
    return ok or f"ids={ids}\n{proc.stdout}\n{proc.stderr}"


@case
def watch_另一侧对称():
    """对照组：钉死群 222 时结果反过来，证明过滤按会话的群走而不是按固定群号。"""
    clear_state()
    write_config(both_groups())
    seed(two_group_events())
    proc = run(WATCH, "--timeout", "3", "--replay", conversation=CONV_B)
    ids = listed_ids(proc.stdout or "")
    return (B_ID in ids and A_ID not in ids) or f"ids={ids}\n{proc.stdout}"


@case
def list_只出本群():
    clear_state()
    write_config(both_groups())
    seed(two_group_events())
    (STATE / "wg_session.txt").write_text("s1", encoding="utf-8")
    proc = run(REPLY, "--list", conversation=CONV_A)
    ids = listed_ids(proc.stdout or "")
    ok = (proc.returncode == 0 and A_ID in ids and B_ID not in ids)
    return ok or f"rc={proc.returncode} ids={ids}\n{proc.stdout}\n{proc.stderr}"


@case
def 未钉死时两个群都看得到():
    """对照组（改前行为不变）：未钉死 + 多群 → 不过滤，且明说隔离没生效。

    只断言「看不到别群」的实现会红在这里：它把未钉死也当成一个群来处理。
    """
    clear_state()
    write_config(both_groups())
    seed(two_group_events())
    (STATE / "wg_session.txt").write_text("s1", encoding="utf-8")
    proc = run(REPLY, "--list")
    ids = listed_ids(proc.stdout or "")
    out = proc.stdout or ""
    ok = (A_ID in ids and B_ID in ids and "未钉死" in out and "WARN" in out)
    return ok or f"ids={ids}\n{out}"


@case
def 未钉死时watch也放行():
    """同一对照组的另一个入口：watch 不许把未钉死当成「空群」而一条都不出。"""
    clear_state()
    write_config(both_groups())
    seed(two_group_events())
    proc = run(WATCH, "--timeout", "3", "--replay")
    ids = listed_ids(proc.stdout or "")
    return (A_ID in ids and B_ID in ids) or f"ids={ids}\n{proc.stdout}"


# ---------- PB：发送前校验目标群 ----------


@case
def 跨群回复被拒且不记账():
    """P0：钉死群 111 时回群 222 的 id 必须报错退出，且不能记成「已回」。

    「不记账」是本 issue 最贵的半条：改前它会把群 222 那条记成已回，另一个会话
    看到 [已回] 就永远不再回它——两个会话都以为对方处理了。
    """
    clear_state()
    write_config(both_groups())
    seed(two_group_events())
    (STATE / "wg_session.txt").write_text("s1", encoding="utf-8")
    proc = run(REPLY, str(B_ID), "x", "--no-wait", conversation=CONV_A)
    out = proc.stdout or ""
    ok = (proc.returncode == 1 and "拒绝" in out
          and B_ID not in ledger_ids())
    return ok or f"rc={proc.returncode} ledger={ledger_ids()}\n{out}\n{proc.stderr}"


@case
def 本群回复不被误挡():
    """对照组：同一份夹具里回群 111 的 id 必须放行（走到「没有可用批次」）。"""
    clear_state()
    write_config(both_groups())
    seed(two_group_events())
    (STATE / "wg_session.txt").write_text("s1", encoding="utf-8")
    proc = run(REPLY, str(A_ID), "x", "--no-wait", "--no-quote", conversation=CONV_A)
    out = proc.stdout or ""
    ok = ("拒绝" not in out and "没有可用批次" in out)
    return ok or f"rc={proc.returncode}\n{out}\n{proc.stderr}"


@case
def 未钉死时不拦跨群():
    """对照组：未钉死时护栏不生效（与改前一致），否则单群老机器会被挡。"""
    clear_state()
    write_config(both_groups())
    seed(two_group_events())
    (STATE / "wg_session.txt").write_text("s1", encoding="utf-8")
    proc = run(REPLY, str(B_ID), "x", "--no-wait", "--no-quote")
    out = proc.stdout or ""
    return ("拒绝" not in out) or f"rc={proc.returncode}\n{out}"


@case
def plan重定向只挑本群():
    """`target_not_in_batch` 的 `allowed` 里混着别群 id 时不许挑它。

    直接调 `reply.plan`，把 `mcp_client.call_tool` 换成「第一次报 target_not_in_batch，
    allowed=[本群旧 id, 别群新 id]」的桩：坏实现会挑别群那个（allowed[-1]），
    好实现只在本群里挑。
    """
    import reply

    clear_state()
    write_config(both_groups())
    seed(two_group_events())
    calls = []

    class Fake:
        """第一次拒绝并给出 allowed，第二次按 target 回 READY。"""

        def call_tool(self, name, args, timeout=None):
            calls.append(args.get("target_event_id"))
            if len(calls) == 1:
                return {"state": "REJECTED", "error_code": "target_not_in_batch",
                        "valid_event_ids": [A_ID, B_ID]}
            return {"state": "READY", "plan_id": "p1"}

    real = reply.mcp_client
    real_iso = reply.wg_isolation.group_id
    reply.mcp_client = Fake()
    reply.wg_isolation.group_id = lambda: GROUP_A
    try:
        plan_id, final_target = reply.plan("s1", "b1", 9999, "回复群友", True)
    finally:
        reply.mcp_client = real
        reply.wg_isolation.group_id = real_iso
    ok = (plan_id == "p1" and final_target == A_ID)
    return ok or f"plan_id={plan_id} final={final_target} calls={calls}"


@case
def plan全是他群时失败退出():
    """`allowed` 里只有别群 id → 不许回落到「本批最新那条」，直接失败。"""
    import reply

    clear_state()
    write_config(both_groups())
    seed(two_group_events())

    class Fake:
        def call_tool(self, name, args, timeout=None):
            return {"state": "REJECTED", "error_code": "target_not_in_batch",
                    "valid_event_ids": [B_ID]}

    real = reply.mcp_client
    real_iso = reply.wg_isolation.group_id
    reply.mcp_client = Fake()
    reply.wg_isolation.group_id = lambda: GROUP_A
    try:
        plan_id, final_target = reply.plan("s1", "b1", 9999, "回复群友", True)
    finally:
        reply.mcp_client = real
        reply.wg_isolation.group_id = real_iso
    return (plan_id is None) or f"plan_id={plan_id} final={final_target}"


# ---------- PC：身份收口 ----------


@case
def 身份四档优先级():
    """env > 文件 > config 唯一的群 > 空。每一档单独造，不叠加。"""
    import wg_isolation

    results = []
    clear_state()
    write_config(both_groups())

    saved_env = {k: os.environ.pop(k, None)
                 for k in ("IDLEJOT_WG_CONVERSATION", "TULPA_WG_CONVERSATION")}
    try:
        # 档 1：env 压过文件与 config
        (STATE / "wg_conversation.txt").write_text(CONV_B, encoding="utf-8")
        os.environ["IDLEJOT_WG_CONVERSATION"] = CONV_A
        results.append(wg_isolation.group_id() == GROUP_A)
        os.environ.pop("IDLEJOT_WG_CONVERSATION")
        # 档 2：文件压过 config（config 是多群，本身无解）
        results.append(wg_isolation.group_id() == GROUP_B)
        # 档 3：config 里只有一个启用群
        (STATE / "wg_conversation.txt").unlink()
        write_config({GROUP_A: {"enabled": True}})
        results.append(wg_isolation.group_id() == GROUP_A)
        # 档 4：多群且无 env/文件 → 空（不按书写顺序挑）
        write_config(both_groups())
        results.append(wg_isolation.group_id() == "")
    finally:
        for key, value in saved_env.items():
            if value is not None:
                os.environ[key] = value
        (STATE / "wg_conversation.txt").unlink(missing_ok=True)
    return results == [True, True, True, True] or f"results={results}"


@case
def 多群未钉死拒绝启动():
    """多群未钉死 → 拒绝原因说清；单群 → 空；**零配置 → 也必须空**（对照组）。

    零配置那档是踩出来的：第一版把「config 里一个群都没有」也算成「没有群身份」，
    于是 `ensure()` 一进门就返回空——`check_issue_16_audit_fixes.py` 的
    `test_ensure_rebuilds_dead_session` 用的正是零配置夹具（`os.environ.clear()`
    之后没有任何 groups），它当场红在 `session.ensure() == "fresh-sid"`。
    「多群所以选谁都是替用户选」与「一个群都没配」是两件事：后者改前也照样起，
    由 `wg_config.problems()`（issue #51）单独报。所以这里必须把零配置钉成**不拦**。
    """
    from unittest.mock import patch

    import mcp_client
    import session

    saved_group, saved_refused = session.GROUP, session._REFUSED
    results = []
    try:
        clear_state()
        write_config(both_groups())
        session.GROUP = ""
        problem = session.identity_problem()
        results.append(bool(problem) and "未钉死" in problem)

        write_config({GROUP_A: {"enabled": True}})
        session.GROUP = CONV_A          # 单群档已解析出群号
        results.append(session.identity_problem() == "")

        # 对照组 1：零配置（groups 为空）不许被当成「没身份」。
        write_config({})
        session.GROUP = ""
        results.append(session.identity_problem() == "")
        results.append(session.refuse_if_unidentified() is False)

        # 对照组 2：零配置下 `ensure()` 必须照常走重建（issue #16 用例的原形）。
        sid_file = SCRATCH / "wg_session_probe.txt"
        sid_file.write_text("stale-sid", encoding="utf-8")
        calls = []

        def fake_call(name, arguments, timeout=None):
            calls.append(name)
            if name == "get_chat_session":
                return {}
            if name == "list_chat_sessions":
                return {"sessions": []}
            return {"session_id": "fresh-sid"}

        with patch.object(session, "SID_FILE", sid_file), \
                patch.object(mcp_client, "call_tool", fake_call):
            rebuilt = session.ensure()
        results.append(rebuilt == "fresh-sid")
    finally:
        session.GROUP, session._REFUSED = saved_group, saved_refused
        (SCRATCH / "wg_session_probe.txt").unlink(missing_ok=True)
    return results == [True] * 5 or f"results={results} calls={calls}"


@case
def describe_歧义态点名隔离无效():
    """状态行不许把「多群未钉死」印成正常：必须带「未钉死」与「隔离」。"""
    import wg_isolation

    clear_state()
    write_config(both_groups())
    ambiguous = wg_isolation.describe()
    (STATE / "wg_conversation.txt").write_text(CONV_A, encoding="utf-8")
    try:
        pinned = wg_isolation.describe()
    finally:
        (STATE / "wg_conversation.txt").unlink(missing_ok=True)
    ok = ("未钉死" in ambiguous and "隔离" in ambiguous
          and CONV_A in pinned and "file:" in pinned)
    return ok or f"ambiguous={ambiguous!r}\npinned={pinned!r}"


@case
def 配置覆盖对session生效():
    """`IDLEJOT_WG_CONFIG` 指夹具时 `session.GROUP` 必须是夹具里的群。

    改前 `session._config()` 直读 `ROOT/water-group/config.json`——本机跑门禁读到
    的是实机配置，`session.GROUP` 变成实机群号，门禁测的对象是另一台机器。
    """
    import importlib

    import session

    clear_state()
    write_config({GROUP_A: {"enabled": True, "persona": "fixture_persona"}},
                 default_persona="default_persona")
    reloaded = importlib.reload(session)
    ok = (reloaded.GROUP == CONV_A and reloaded.PERSONA == "fixture_persona")
    return ok or f"GROUP={reloaded.GROUP!r} PERSONA={reloaded.PERSONA!r}"


@case
def 过滤把自己滤空要说出来():
    """钉死群 111 而事件流全是群 222 → 明说「一条都不属于本群」。

    防的是过滤自己的静默失效：群号/self_account/状态目录配错时，「过滤生效」与
    「群里没人说话」在输出里完全一样，Agent 会一直等一个不会来的唤醒。
    """
    clear_state()
    write_config(both_groups())
    seed([event(B_ID, CONV_B, "B 群喊你")])
    (STATE / "wg_session.txt").write_text("s1", encoding="utf-8")
    proc = run(REPLY, "--list", conversation=CONV_A)
    out = proc.stdout or ""
    ok = ("WARN" in out and "没有一条属于本群" in out and GROUP_B in out)
    return ok or out


@case
def 正常空流不吵():
    """对照组：事件流为空时不许报「一条都不属于本群」（排除型断言带对照）。"""
    clear_state()
    write_config(both_groups())
    seed([])
    (STATE / "wg_session.txt").write_text("s1", encoding="utf-8")
    proc = run(REPLY, "--list", conversation=CONV_A)
    out = proc.stdout or ""
    return ("没有一条属于本群" not in out) or out


# ---------- 判据完整性自检 ----------


def inprocess_list():
    """进程内跑一次 `reply.list_wakes()`，返回 (退出码, 输出)。

    自检必须在进程内：`rows_of` 的退化实现是打在本进程的模块全局上的，子进程
    读不到（`check_now_clock` 的 `watch_唤醒块先给现在` 同样是进程内跑 `main()`）。
    """
    import reply

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = reply.list_wakes()
    return code, buf.getvalue()


def inprocess_watch():
    """进程内跑一次 `watch.main()`（`--replay` 让游标归零），返回 (退出码, 输出)。"""
    import watch

    saved = sys.argv
    sys.argv = ["watch.py", "--timeout", "3", "--replay"]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = watch.main()
    finally:
        sys.argv = saved
    return code, buf.getvalue()


@case
def 自检_rows_of恒空必须翻转():
    """把 `rows_of` 换成恒空：两条「只出本群」用例必须红。

    这是「判据抓得住」的自检——只看 `B_ID not in ids` 的实现会在这里露馅：
    恒空时 B 当然不在，但 A 也不在，`A_ID in ids` 把它挡下来。
    """
    import wg_isolation

    clear_state()
    write_config(both_groups())
    seed(two_group_events())
    (STATE / "wg_session.txt").write_text("s1", encoding="utf-8")

    real = wg_isolation.rows_of
    wg_isolation.rows_of = lambda rows, group=None: []
    try:
        _code, watch_out = inprocess_watch()
        _code2, list_out = inprocess_list()
    finally:
        wg_isolation.rows_of = real
    watch_ok = A_ID in listed_ids(watch_out) and B_ID not in listed_ids(watch_out)
    list_ok = A_ID in listed_ids(list_out) and B_ID not in listed_ids(list_out)
    return (not watch_ok and not list_ok) or f"watch_ok={watch_ok} list_ok={list_ok}"


@case
def 自检_rows_of恒全量必须翻转():
    """把 `rows_of` 换成恒返回全部：钉死态用例必须红。

    与上一条合起来证明过滤真的跟着群身份走，而不是「反正只有一个群所以看不出」。
    """
    import wg_isolation

    clear_state()
    write_config(both_groups())
    seed(two_group_events())

    real = wg_isolation.rows_of
    wg_isolation.rows_of = lambda rows, group=None: list(rows)
    try:
        _code, watch_out = inprocess_watch()
    finally:
        wg_isolation.rows_of = real
    ids = listed_ids(watch_out)
    pinned_ok = A_ID in ids and B_ID not in ids
    return (not pinned_ok) or f"ids={ids}"


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    bad = 0
    for fn in CASES:
        try:
            got = fn()
        except Exception as exc:  # noqa: BLE001
            got = False
            print(f"✗ {fn.__name__}: {type(exc).__name__}: {exc}")
            bad += 1
            continue
        if got is True:
            print(f"✓ {fn.__name__}")
        else:
            bad += 1
            print(f"✗ {fn.__name__}")
            if isinstance(got, str):
                print(f"    {got}")
    print("全部通过" if not bad else f"{bad} 项失败")
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
