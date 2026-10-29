"""issue #192 gate: 禁言解禁必须叫醒值守的监听，而不是等下一句话。

issue #192 的现场：机器人被禁言 10 分钟，`watch.py` 的轮询循环照旧在等 @——
禁言结束这件事没有任何一句话触发它。`wg_send_failure.recheck()`（issue #145）早就
会现查 `shut_up_timestamp`，但入口只有 `reply.py --list` 的表头：**只读入口会复查，
值守循环不会**，而禁言期间 Agent 等的正是后者。恢复输出只有那句 `TIMEOUT`，与
「群里没人说话」完全同形。

本门禁用假 OneBot + 临时 state 跑真实子进程，钉住：

  1. 提前解禁（账本预测 +600s，实测 `shut_up_timestamp=0` / `group_all_shut=False`）
     与自然到期（预测已过）都必须在**没人说话**的轮次里返回 0、打印 `UNMUTED:`、
     末行仍是 `WAKE`，并把 `recovered=True` / `recheck_result=released` 写回账本；
  2. 仍禁言 / 探测失败 / `group_all_shut` 读不到时**不许**冒充解禁：返回 2、
     无 `UNMUTED`、结论分别是 `muted` / `unknown`，且 `recovered` 不被写成 True；
  3. 全员禁言（成员 `shut_up_timestamp=0`）没有解禁时刻，账本只记 `group_shut=True`
     ——这条记录同样必须进探测（前置放宽），解除时同样唤醒。对照组：账本
     `group_shut=None`（不知道）时零请求，不把参数错 / 网络错变成轮询对象；
  4. 节流：账本刚写 5 秒、上次复查 5 秒都零请求；把 `rechecked_at` 推到 27 秒前
     才探一次（`PROBE_INTERVAL=15`）。对照组把「永远不探」与「每秒都探」都排掉；
  5. 只读入口的口径没被监听拖快：`reply.py --list` 在「上次复查 20 秒前」仍不探
     （`RECHECK_AFTER=60`）；
  6. 端到端接线：`listen.py` 起 `watch.py`（`run_passthrough`）时 `UNMUTED` 块要被
     转述出来，`RESTART:` 仍是末行——只测 `watch.py` 会漏掉「参数没传下去 /
     输出被吞掉」这一类（issue #162 的教训）；
  7. 禁言期间不扫追问窗口（issue #197）：账本压着未解除的禁言时，窗口里的闲聊
     **不许**把监听叫醒——那一轮一个字都发不出去，叫醒只是白烧一轮。判据是
     `rc=2` + 无 `[追问]` + 无 `WAKE` + 一行「暂停追问窗口扫描」，并且窗口的
     `after_id` / `budget` 一个都没动（跳过 ≠ 扫了再丢，后者会推进游标、扣额度）。
     两条对照组钉住「抑制不能把追问关死」：`recovered=True`（真实时序里
     `UNMUTED` 之后重挂的监听就是这个状态）与**完全没有账本**都必须照常
     `[追问]` + `WAKE`。

已知边界：本门禁不覆盖「解禁与有人 @ 同时发生」的先后顺序——那时普通唤醒先走，
`handle()` 的成功发送会写 `recovered=True`，`UNMUTED` 不出现是设计（`watch.py`
把探测放在 fresh 之后）。也不覆盖真实 QQ 侧 NapCat 的字段抖动。

    .venv/Scripts/python.exe scripts/check_unmute_probe.py
"""
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import check_fixture_support as fs  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import scratch_dir  # noqa: E402
fs.bootstrap_paths(scripts=False)

# 数据根 / 配置 / 库全部钉进临时目录，import 任何 daemon 模块之前完成：`wg_config`
# 的 CONFIG 与 `wg_paths` 的 state_dir 都是 import 期求值的，晚一步就会指向实机。
# `IDLEJOT_WG_STATE` 必须显式覆盖：`wg_paths.state_dir()` 的优先级是
# `IDLEJOT_WG_STATE or TULPA_WG_STATE`，只钉后者会被外部环境穿透——那正是
# 「测试写生产状态」的口子（穿透就意味着向实机 OneBot 发查询）。
_SCRATCH = scratch_dir(prefix="wg_unmute_")
os.environ["WATER_GROUP_HOME"] = str(_SCRATCH / "home")
os.environ["TULPA_WG_STATE"] = str(_SCRATCH / "state")
os.environ["IDLEJOT_WG_STATE"] = str(_SCRATCH / "state")
os.environ["IDLEJOT_WG_DB"] = str(_SCRATCH / "chats.sqlite3")
os.environ["IDLEJOT_WG_CONFIG"] = str(_SCRATCH / "config.json")
for path in ("home", "state"):
    (_SCRATCH / path).mkdir(parents=True, exist_ok=True)

from fixture_server import FixtureHandler, fixture_server  # noqa: E402
import followup  # noqa: E402  (追问窗口机件：窗口由它开，不手写 JSON)
import reply  # noqa: E402
import wg_send_failure  # noqa: E402

GROUP = "777000111"
SELF = "99999"
STATE = _SCRATCH / "state"
LEDGER = STATE / "wg_send_failure.json"
EVENTS = STATE / "wg_events.jsonl"
WINDOW = STATE / "wg_followup.json"
DAEMON = ROOT / "water-group" / "daemon"

# 夹具状态：每个用例自己写，Handler 只读。
# `member_queries` 数的是 `get_group_member_info` 次数——「没探」与「探了但结论一样」
# 在输出上完全同形，只能靠请求计数区分（#145 的教训）。
FIX = {"member": {}, "group": {}, "member_fails": False, "group_fails": False,
       "member_queries": 0}


class Handler(FixtureHandler):
    def do_POST(self):
        self.read_json()
        action = self.path.strip("/")
        if action == "get_group_member_info":
            FIX["member_queries"] += 1
            if FIX["member_fails"]:
                self.send_json({"status": "failed", "retcode": 1200, "message": "查询失败"})
            else:
                self.send_json({"status": "ok", "retcode": 0, "data": FIX["member"]})
        elif action == "get_group_info":
            if FIX["group_fails"]:
                self.send_json({"status": "failed", "retcode": 1200, "message": "查询失败"})
            else:
                self.send_json({"status": "ok", "retcode": 0, "data": FIX["group"]})
        else:
            self.send_json({"status": "failed", "retcode": 1404, "message": action})


def _write_config(base):
    (_SCRATCH / "config.json").write_text(json.dumps({
        "self_account": SELF, "onebot_token": "fixture-token",
        "onebot_http_url": base, "groups": {},
    }, ensure_ascii=False), encoding="utf-8")


def _reset(now, shut_up=0, all_shut=False, member_fails=False, group_fails=False):
    """清账本 / 清事件 / 关窗口 / 复位假 OneBot 的应答。"""
    wg_send_failure.clear()
    try:
        EVENTS.unlink()
    except OSError:
        pass
    # 窗口不关会串到下一个用例：追问唤醒（rc=0）与「禁言期静默」（rc=2）互斥，
    # 残留窗口会让下一条用例红在与判据无关的地方（issue #197）。
    try:
        WINDOW.unlink()
    except OSError:
        pass
    FIX["member_fails"] = member_fails
    FIX["group_fails"] = group_fails
    FIX["member_queries"] = 0
    FIX["member"] = {"user_id": int(SELF), "card": "机器人", "nickname": "bot",
                     "role": "member", "shut_up_timestamp": shut_up or 0}
    FIX["group"] = {"group_id": int(GROUP), "group_all_shut": all_shut}


def _followup_window(after_id=5, session="fixture-session"):
    """开一个真实的追问窗口，并往 chat_inbox 塞一条窗口之后的闲聊（issue #197）。

    与 `check_followup.py` 同一套口径：窗口走 `followup.open()`，不手写 JSON——
    字段一改就跟着变，否则夹具与实现会各自漂。`after_id` 那条是被回复的消息
    （`open()` 从它往后看），它的下一条是群友接话，也就是追问唤醒的燃料。
    """
    db = sqlite3.connect(followup.DB)
    db.execute("CREATE TABLE IF NOT EXISTS chat_sessions "
               "(id TEXT, conversation_id TEXT, active INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS chat_inbox "
               "(id INTEGER PRIMARY KEY, session_id TEXT, payload TEXT,"
               " at REAL NOT NULL, media TEXT NOT NULL DEFAULT '[]')")
    db.execute("DELETE FROM chat_sessions")
    db.execute("DELETE FROM chat_inbox")
    db.execute("INSERT INTO chat_sessions VALUES (?, ?, 1)",
               (session, f"111:group:{GROUP}"))
    now = time.time()
    for eid, who, text in ((after_id, "甲", "@机器人 在吗"),
                           (after_id + 1, "乙", "在的在的")):
        payload = {"sender": who, "content": text, "timestamp": now * 1000,
                   "conversation_id": f"111:group:{GROUP}",
                   "onebot_message_id": str(7000 + eid)}
        db.execute("INSERT INTO chat_inbox VALUES (?, ?, ?, ?, '[]')",
                   (eid, session, json.dumps(payload, ensure_ascii=False),
                    now - 10 + eid))
    db.commit()
    db.close()
    return followup.open(after_id, max_messages=10, idle_seconds=600, session=session)


def _ledger(mute_until=None, at=None, recovered=None, group_shut=None, **extra):
    """直接写一条账本（模拟上一次失败留下的冻结记录），不经过发送路径。"""
    now = time.time()
    entry = {"at": int(now if at is None else at), "group_id": GROUP,
             "kind": "onebot", "retcode": 200, "reason": "EventChecker Failed",
             "mute_until": int(mute_until) if mute_until else None,
             "recovered": recovered, "recovered_at": None,
             "rechecked_at": None, "group_shut": group_shut,
             "recheck_result": None}
    entry.update(extra)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    return entry


def _read_ledger():
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _read_window():
    """当前追问窗口；被关掉 / 没开都是 {}（两者靠「文件在不在」区分，见调用点）。"""
    try:
        return json.loads(WINDOW.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _run(script, *args, timeout=90):
    """起真实子进程（env 继承本进程已钉住的数据根）。"""
    return subprocess.run(
        [sys.executable, str(DAEMON / script), *args],
        cwd=str(ROOT), env=dict(os.environ), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout)


def _last_line(out):
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def 提前解禁叫醒监听():
    """账本预测 +600s、实测已解禁：没人说话的轮次里也要返回 0 并打印 UNMUTED。"""
    now = int(time.time())
    _reset(now, shut_up=0, all_shut=False)
    _ledger(now + 600, at=now - 3600)
    r = _run("watch.py", "--timeout", "5")
    rec = _read_ledger()
    return (r.returncode == 0 and "UNMUTED: 禁言已于" in r.stdout
            and _last_line(r.stdout) == "WAKE"
            and rec.get("recovered") is True
            and rec.get("recheck_result") == "released"
            and FIX["member_queries"] == 1)


@case
def 自然到期也叫醒():
    """预测时刻已过、复查过一次仍禁言（`muted`）：实测解禁同样要唤醒。"""
    now = int(time.time())
    _reset(now, shut_up=0, all_shut=False)
    _ledger(now - 30, at=now - 900, recheck_result="muted")
    r = _run("watch.py", "--timeout", "5")
    rec = _read_ledger()
    return (r.returncode == 0 and "UNMUTED:" in r.stdout
            and _last_line(r.stdout) == "WAKE"
            and rec.get("recheck_result") == "released")


@case
def 仍禁言静默值守():
    """现查仍禁言：返回 2、一个字都不多说，账本按现查时刻更新。"""
    now = int(time.time())
    until = now + 600
    _reset(now, shut_up=until, all_shut=False)
    _ledger(until, at=now - 3600)
    r = _run("watch.py", "--timeout", "2")
    rec = _read_ledger()
    return (r.returncode == 2 and "TIMEOUT" in r.stdout
            and "UNMUTED" not in r.stdout
            and rec.get("recheck_result") == "muted"
            and rec.get("mute_until") == until
            and FIX["member_queries"] == 1)


@case
def 探测失败不冒充解禁():
    """成员查询整条失败：结论只能是 `unknown`，不能说「解禁」也不能说「仍禁言」。"""
    now = int(time.time())
    _reset(now, member_fails=True)
    _ledger(now + 600, at=now - 3600)
    r = _run("watch.py", "--timeout", "2")
    rec = _read_ledger()
    return (r.returncode == 2 and "UNMUTED" not in r.stdout
            and rec.get("recheck_result") == "unknown"
            and rec.get("recovered") is None)


@case
def 全员禁言仍静默但确实复查():
    """全员禁言没有解禁时刻：账本只有 `group_shut=True`，也必须进探测。

    旧前置是「有 `mute_until` 才查」，全群禁言的账本永远探不到——管理员解禁后
    仍然没人知道。
    """
    now = int(time.time())
    _reset(now, shut_up=0, all_shut=True)
    _ledger(None, at=now - 3600, group_shut=True)
    r = _run("watch.py", "--timeout", "2")
    rec = _read_ledger()
    return (r.returncode == 2 and "UNMUTED" not in r.stdout
            and FIX["member_queries"] == 1
            and rec.get("recheck_result") == "muted"
            and rec.get("recovered") is None)


@case
def 全员禁言解除叫醒():
    """同一份 `group_shut=True` 账本，`group_all_shut` 翻成 False 就是实测解禁。"""
    now = int(time.time())
    _reset(now, shut_up=0, all_shut=False)
    _ledger(None, at=now - 3600, group_shut=True)
    r = _run("watch.py", "--timeout", "5")
    rec = _read_ledger()
    return (r.returncode == 0 and "UNMUTED:" in r.stdout
            and _last_line(r.stdout) == "WAKE"
            and rec.get("recovered") is True
            and rec.get("recheck_result") == "released")


@case
def 读不到全员禁言不算解禁():
    """`get_group_info` 失败时 `group_all_shut` 是 None，不是 False——不许当解禁。"""
    now = int(time.time())
    _reset(now, shut_up=0, all_shut=False, group_fails=True)
    _ledger(now + 600, at=now - 3600)
    r = _run("watch.py", "--timeout", "2")
    rec = _read_ledger()
    return (r.returncode == 2 and "UNMUTED" not in r.stdout
            and rec.get("recheck_result") == "unknown"
            and rec.get("recovered") is None)


@case
def 没有禁言账本时零请求():
    """对照组：无账本 / 非禁言类失败 / 已有结论，三种前置都不许打接口。

    这一条防的是「把监听变成轮询器」：短了看不出症状，长了就是每分钟一次无谓请求。
    """
    now = int(time.time())
    _reset(now, shut_up=0, all_shut=False)
    wg_send_failure.clear()
    r1 = _run("watch.py", "--timeout", "1")
    none_queries = FIX["member_queries"]
    _ledger(None, at=now - 3600)                       # 参数错 / 网络错：没有对象
    r2 = _run("watch.py", "--timeout", "1")
    other_queries = FIX["member_queries"] - none_queries
    _ledger(now + 600, at=now - 3600, recovered=True,  # 已有结论
            recovered_at=now - 30)
    r3 = _run("watch.py", "--timeout", "1")
    settled_queries = FIX["member_queries"] - none_queries - other_queries
    return (r1.returncode == 2 and r2.returncode == 2 and r3.returncode == 2
            and none_queries == 0 and other_queries == 0 and settled_queries == 0)


@case
def 账本刚写不重复探测():
    """失败那一刻 `diagnose()` 已经查过：40 秒内再挂监听不许再打一次接口。"""
    now = int(time.time())
    _reset(now, shut_up=0, all_shut=False)
    _ledger(now + 600, at=now - 5)          # 若探就会判 released
    r = _run("watch.py", "--timeout", "2")
    return (r.returncode == 2 and "UNMUTED" not in r.stdout
            and FIX["member_queries"] == 0)


@case
def 复查间隔内不重复探测():
    """上次复查 5 秒前（< PROBE_INTERVAL 15s）：本轮零请求。"""
    now = int(time.time())
    _reset(now, shut_up=0, all_shut=False)
    _ledger(now + 600, at=now - 3600, rechecked_at=now - 5)
    r = _run("watch.py", "--timeout", "2")
    return (r.returncode == 2 and "UNMUTED" not in r.stdout
            and FIX["member_queries"] == 0)


@case
def 间隔到期后会再探():
    """对照组：同一份账本把 `rechecked_at` 推到 27 秒前，必须探一次并唤醒。

    与上一条配对：只有「刚查过不查」而没有「过一会儿要查」，等于永远不查。
    """
    now = int(time.time())
    _reset(now, shut_up=0, all_shut=False)
    _ledger(now + 600, at=now - 3600, rechecked_at=now - 27)
    r = _run("watch.py", "--timeout", "5")
    return (r.returncode == 0 and "UNMUTED:" in r.stdout
            and FIX["member_queries"] == 1)


@case
def 只读入口仍是六十秒口径():
    """`reply.py --list` 在「上次复查 20 秒前」不该重探：监听的 15s 不许传染过去。"""
    now = int(time.time())
    _reset(now, shut_up=0, all_shut=False)
    _ledger(now + 600, at=now - 3600, rechecked_at=now - 20)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        reply.list_wakes()
    return FIX["member_queries"] == 0 and "UNMUTED" not in buf.getvalue()


@case
def 禁言期间不扫追问窗口():
    """账本压着未解除的禁言时，窗口里的闲聊不许把监听叫醒（issue #197）。

    改前：`wake_scan` 照扫，`consume()` 扣额度、推游标，返回 0 让 `listen.py`
    重挂一轮——可那一轮里一个字都发不出去，Agent 白起一轮还被算作「有人叫」。
    改后：跳过扫描并留一行说明；窗口本身不动（不关、不推游标、不扣额度），
    解禁后照旧可用、过期照旧自关。
    """
    now = int(time.time())
    _reset(now, shut_up=now + 600, all_shut=False)
    _ledger(now + 600, at=now - 3600)
    _followup_window()
    before = _read_window()
    r = _run("watch.py", "--timeout", "2")
    after = _read_window()
    return (r.returncode == 2 and "TIMEOUT" in r.stdout
            and "[追问]" not in r.stdout and "WAKE" not in r.stdout
            and "暂停追问窗口扫描" in r.stdout
            and before.get("after_id") == 5
            and after.get("after_id") == 5          # 游标没被推
            and after.get("budget") == before.get("budget"))


@case
def 禁言期间不回退未回唤醒游标():
    """禁言期 BACKFILL 暂停（issue #269）：游标之前积压未回 @ 时，禁言未解除
    不许 `rewind_for_backlog()` 把游标回退并 WAKE 退出。

    改前：watch.py 每轮启动立刻 BACKFILL + WAKE + rc=0，listen.py 据此立即重挂，
    再 BACKFILL+WAKE——每秒数次死循环刷屏，禁言几百秒里整屏都是这两行。
    改后：禁言期跳过 `rewind_for_backlog()`，与 #197 暂停追问窗口扫描同构；
    游标不关、`start_cursor` 不动，解禁后由 `poll_unmute()` 触发重挂，重挂首轮
    的 `check_backlog` 在 `recovered=True` 的账本上（`muted_now()` 为 False）放行，
    照旧补扫积压。对照组：解禁后（`recovered=True`）同一份积压必须 BACKFILL+WAKE。
    """
    now = int(time.time())
    _reset(now, shut_up=now + 600, all_shut=False)
    _ledger(now + 600, at=now - 3600)
    # 游标 12、之前积压 id=10/11 两条新鲜未回 @（与 issue 现象段同形：11433/11434）
    (STATE / "wg_watch.txt").write_text("12", encoding="utf-8")
    fresh_ms = (now - 30) * 1000
    rows = [
        {"id": 10, "timestamp": fresh_ms, "is_self": False, "sender": "甲",
         "content": "@机器人 在吗", "segments": [{"type": "at", "qq": SELF}],
         "conversation_id": f"111:group:{GROUP}", "onebot_message_id": "7010"},
        {"id": 11, "timestamp": fresh_ms + 1000, "is_self": False, "sender": "乙",
         "content": "@机器人 说话呀", "segments": [{"type": "at", "qq": SELF}],
         "conversation_id": f"111:group:{GROUP}", "onebot_message_id": "7011"},
    ]
    EVENTS.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                      encoding="utf-8")
    before_cursor = int((STATE / "wg_watch.txt").read_text(encoding="utf-8").strip())
    r = _run("watch.py", "--timeout", "2")
    after_cursor = int((STATE / "wg_watch.txt").read_text(encoding="utf-8").strip())
    return (r.returncode == 2 and "TIMEOUT" in r.stdout
            and "BACKFILL:" not in r.stdout
            and "WAKE" not in r.stdout
            and "暂停未回唤醒 BACKFILL" in r.stdout
            and before_cursor == 12
            and after_cursor == 12)              # 游标没被回退到 9


@case
def 解禁后未回唤醒照旧回退补扫():
    """对照组（issue #269）：`recovered=True` 之后同一份积压必须 BACKFILL+WAKE。

    真实时序：`poll_unmute()` 解禁唤醒 → Agent 重挂监听 → 新监听读到账本
    `recovered=True`（`muted_now()` 为 False），`rewind_for_backlog()` 放行，
    游标回退到最老未回 @ 之前并重放。断言 `id=10` 被打印、`BACKFILL:` 出现、
    rc=0 + WAKE——只摘「暂停」那句不是修复。
    """
    now = int(time.time())
    _reset(now, shut_up=0, all_shut=False)
    _ledger(now + 600, at=now - 3600, recovered=True, recovered_at=now - 5,
            recheck_result="released")
    (STATE / "wg_watch.txt").write_text("12", encoding="utf-8")
    fresh_ms = (now - 30) * 1000
    rows = [
        {"id": 10, "timestamp": fresh_ms, "is_self": False, "sender": "甲",
         "content": "@机器人 在吗", "segments": [{"type": "at", "qq": SELF}],
         "conversation_id": f"111:group:{GROUP}", "onebot_message_id": "7010"},
        {"id": 11, "timestamp": fresh_ms + 1000, "is_self": False, "sender": "乙",
         "content": "@机器人 说话呀", "segments": [{"type": "at", "qq": SELF}],
         "conversation_id": f"111:group:{GROUP}", "onebot_message_id": "7011"},
    ]
    EVENTS.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                      encoding="utf-8")
    r = _run("watch.py", "--timeout", "5")
    return (r.returncode == 0
            and "BACKFILL:" in r.stdout
            and "id=10 " in r.stdout
            and _last_line(r.stdout) == "WAKE")


@case
def 解禁后追问窗口照旧唤醒():
    """对照组：`recovered=True` 之后同一份窗口必须照常唤醒（抑制不能关死追问）。

    这也是真实时序：`poll_unmute()` 返回 0 → Agent 重挂监听 → 新监听在账本上
    读到「已解禁」，窗口里积压的接话照旧叫醒它。断言 `after_id` 推到 6 才说明
    真的扫了，不只是把「暂停」那句话摘掉。
    """
    now = int(time.time())
    _reset(now, shut_up=0, all_shut=False)
    _ledger(now + 600, at=now - 3600, recovered=True, recovered_at=now - 5,
            recheck_result="released")
    _followup_window()
    r = _run("watch.py", "--timeout", "5")
    after = _read_window()
    return (r.returncode == 0 and "[追问]" in r.stdout
            and "乙" in r.stdout
            and _last_line(r.stdout) == "WAKE"
            and "UNMUTED" not in r.stdout
            and after.get("after_id") == 6)


@case
def 无禁言账本时追问正常唤醒():
    """对照组：完全没有禁言账本（日常值守）时，追问窗口一行都不能少。

    与上一条配对排掉「`muted_now()` 恒 True」——那种实现下前一条与这一条都红。
    `_reset()` 已经 `clear()` 过账本，这里不再写任何记录。
    """
    now = int(time.time())
    _reset(now, shut_up=0, all_shut=False)
    _followup_window()
    r = _run("watch.py", "--timeout", "5")
    return (r.returncode == 0 and "[追问]" in r.stdout
            and _last_line(r.stdout) == "WAKE"
            and "暂停追问窗口扫描" not in r.stdout)


@case
def 抖动后孤立muted不永久停扫():
    """issue #205：全员禁言被复查过一次，那一次 `group_all_shut` 读不到 → 账本只剩
    孤立的 `recheck_result="muted"`（`mute_until=None` + `group_shut=None`）。

    改前死结：`muted_now()` 恒 True（不看 `mute_until` 缺失）→ `watch.py` 每轮跳过
    追问扫描；`probe()` 恒 idle（前置 `if not until and group_shut is not True` 直接
    返回）→ `poll_unmute()` 永不探测、`UNMUTED:` 永不出现。两边都不会自己恢复。

    改后：`muted_now()` 对孤立的 muted 降级成 False（解除停扫），`probe()` 把孤立的
    muted/unknown 当成可复查对象探一次（带节流）。这一轮 `group_all_shut` 又读到了
    → 探到仍禁言，如实留 `group_shut=True` + `muted`，`muted_now()` 回到 True（有
    事实撑着）。下一轮再抖动 → unknown，`muted_now()` 又降级 False，停扫再解除。

    本用例钉的是「死结解开后时序能自洽」：第一次 probe 必须探（不再 idle），探到
    muted 后账本要有 `group_shut=True` 撑着；同一个账本 `muted_now()` 必须与
    `probe()` 结论一致——不允许「仍在禁言」与「无需探测」同时成立。
    """
    now = int(time.time())
    _reset(now, shut_up=0, all_shut=True)           # 这一轮又能读到全员禁言
    # 账本停在「上一轮探测失败、group_shut 被擦掉、只剩孤立 muted」的现场
    _ledger(None, at=now - 3600, group_shut=None,
            recheck_result="muted", rechecked_at=now - 90)   # 节流已过
    r = _run("watch.py", "--timeout", "5")
    rec = _read_ledger()
    # 不变量：muted_now() 与 probe() 对同一账本不得给出「仍在禁言」+「无需探测」
    # 这里 probe 必须探到 muted（夹具 shut_up=0 + all_shut=True），muted_now() 必须为 True
    # （group_shut=True 撑住了）——死结解开，但结论如实。
    return (FIX["member_queries"] == 1               # 不再 idle，探了一次
            and rec.get("recheck_result") == "muted"
            and rec.get("group_shut") is True        # 这一轮读到了，撑住结论
            and rec.get("recovered") is None)        # 没冒充解禁


@case
def 抖动擦掉group_shut后不再永久停扫():
    """issue #205 第二条时序：上一轮 `group_shut=True` 撑着 muted，这一轮 `group_info`
    抖动读不到 → 账本被擦成 `group_shut=None` + `recheck_result="unknown"`。

    改前：`group_shut=True` 被这一轮探测自己擦掉（`fresh["group_shut"]=None` 先写
    再探），下一轮前置 `if not until and group_shut is not True` 直接 idle，永久
    不再探——「下一次探测的资格」被这次探测自己取消。

    改后：孤立的 `recheck_result="unknown"`（`mute_until=None` + `group_shut=None`）
    同样让 `muted_now()` 降级 False（停扫解除），`probe()` 仍把它当成可复查对象探
    一次（带节流）。这一轮 `group_info` 又抖动 → 仍是 unknown，但 `watch.py` 不会再
    因为孤立的 muted 永久停扫——停扫只在有事实撑着时才发生。

    本用例钉的是「擦掉 group_shut 后能恢复」：probe 必须探（不再 idle），结论是
    unknown，但 `muted_now()` 必须为 False（孤立 unknown 不算仍在禁言）——watch 的
    追问扫描恢复，不再被这一轮探测自己永久关死。
    """
    now = int(time.time())
    _reset(now, shut_up=0, all_shut=False, group_fails=True)   # group_info 这一轮抖动
    # 账本停在「上一轮 group_shut=True 撑着 muted，这一轮刚擦掉」之后的状态
    _ledger(None, at=now - 3600, group_shut=None,
            recheck_result="unknown", rechecked_at=now - 90)   # 节流已过
    r = _run("watch.py", "--timeout", "2")
    rec = _read_ledger()
    # 关键不变量：muted_now() 与 probe() 不得同时给出「仍在禁言」+「无需探测」
    # 这里 probe 探了一次（unknown），muted_now() 必须为 False（孤立 unknown 不算仍在禁言）
    # ——停扫解除，watch 不会再被这条账本永久关死。
    return (FIX["member_queries"] == 1               # 不再 idle，探了一次
            and rec.get("recheck_result") == "unknown"
            and rec.get("group_shut") is None
            and rec.get("recovered") is None         # 没冒充解禁
            and "暂停追问窗口扫描" not in r.stdout)   # 停扫解除


@case
def 监听转述解禁唤醒():
    """端到端接线：`listen.py` 的 `run_passthrough` 要把 UNMUTED 块转述出来，
    `RESTART:` 仍是末行（#69 契约 1），退出码 0 表示「有活」。"""
    now = int(time.time())
    _reset(now, shut_up=0, all_shut=False)
    _ledger(now + 600, at=now - 3600)
    r = _run("listen.py", "--timeout", "5", "--no-digest")
    return (r.returncode == 0 and "UNMUTED:" in r.stdout
            and _last_line(r.stdout).startswith("RESTART:"))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with fixture_server(Handler) as server:
        _write_config(f"http://127.0.0.1:{server.server_port}")
        return fs.run_cases(CASES)


if __name__ == "__main__":
    sys.exit(main())
