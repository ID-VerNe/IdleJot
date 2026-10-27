"""钉住「现在」：值守工具链必须告诉 Agent 当前时刻（issue #95）。

2026-10-23 20:51 的真实翻车：Agent 把官方公告的「今晚八点开播」照搬进群里，
而前瞻当时已经开播快一小时。资料是对的，错的是整条工具链只输出消息时间戳、
相对量（`19s 前`）和未来截止时刻（`静默至 20:55:15`）——没有一处给出「现在」，
时间相关的判断只能猜。

这条门禁钉四类输出，任何一处把「现在」弄丢都会红：

  1. `wg_clock.now_line()` 的格式，且解析出来的时刻与本机当前时间对得上；
  2. `up.py --status` 的首个非空行就是 now line（真实子进程，钉 CLI 表面）；
  3. `reply.py --list` / `--context`、`followup.py --show` 的表头带当前时刻；
  4. `watch.py` 的唤醒块先打 now line 再打 id 行，`WAKE` 仍是最后一行。

全程临时 `WATER_GROUP_HOME` / `IDLEJOT_WG_DB`，不碰实机状态：

    .venv/Scripts/python.exe scripts/check_now_clock.py

issue #51 之后 `watch.py` 启动就要 `self_account`（没有就拒绝启动）：本门禁
进程内跑 `watch.main()` 的用例与若干子进程 CLI 因此带一份最小身份夹具
（`FIXTURE_CONFIG`，`IDLEJOT_WG_CONFIG` 指过去），否则会红在「监听拒绝启动」
上，而它测的是唤醒块的时间锚。
"""
import atexit
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "water-group" / "daemon"

# 数据根必须在 import 之前钉死：STATE / DB 都是 import 期求值的。
HOME = Path(tempfile.mkdtemp(prefix="wg-clock-gate-"))
atexit.register(shutil.rmtree, HOME, True)
(HOME / "data").mkdir(parents=True, exist_ok=True)
os.environ["WATER_GROUP_HOME"] = str(HOME)
os.environ["IDLEJOT_WG_DB"] = str(HOME / "data" / "mcp-access.sqlite3")
# 外部测试变量会把 state 指回实机，这里清掉，只留临时数据根。
for _name in ("IDLEJOT_WG_STATE", "TULPA_WG_STATE"):
    os.environ.pop(_name, None)

# #51 之后 watch.py 启动就要 self_account（缺身份拒绝启动）：watch 用例进程内
# 跑 main()、子进程 CLI 也可能校验身份。写一份最小配置到临时数据根，用
# IDLEJOT_WG_CONFIG 指过去——不读实机配置（门禁不许依赖机器状态）。
FIXTURE_CONFIG = HOME / "config.json"
FIXTURE_CONFIG.write_text(json.dumps({"self_account": "10001",
                                      "onebot_token": "clock-fixture"},
                                     ensure_ascii=False), encoding="utf-8")
os.environ["IDLEJOT_WG_CONFIG"] = str(FIXTURE_CONFIG)

import check_fixture_support as fs  # noqa: E402  (issue #169 PE: path 注入收口)
fs.bootstrap_paths(scripts=False, root=False)

import followup  # noqa: E402
import reply  # noqa: E402
import up  # noqa: E402
import watch  # noqa: E402
import wg_clock  # noqa: E402
import wg_config  # noqa: E402

DB = Path(os.environ["IDLEJOT_WG_DB"])
SID = "session-clock"
GROUP = "1:group:123"

NOW_RE = re.compile(r"现在 (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
                    r"\((UTC[+-]\d{1,2}(?::\d{2})?|UTC)\)")


def moment_of(text):
    """从一段输出里取 now line 的时刻（epoch 秒）；取不到返回 None。"""
    found = NOW_RE.search(text or "")
    if not found:
        return None
    return datetime.strptime(found.group(1), "%Y-%m-%d %H:%M:%S").timestamp()


def is_now(text, tolerance=120):
    """这段输出里的 now line 是否就是此刻（容忍执行耗时）。"""
    moment = moment_of(text)
    return moment is not None and abs(moment - time.time()) <= tolerance


def build_db():
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE chat_sessions (id TEXT, conversation_id TEXT, active INTEGER)")
    db.execute("CREATE TABLE chat_inbox "
               "(id INTEGER PRIMARY KEY, session_id TEXT, payload TEXT, "
               "media TEXT NOT NULL DEFAULT '[]')")
    db.execute("INSERT INTO chat_sessions VALUES (?, ?, 1)", (SID, GROUP))
    now = time.time()
    messages = [
        (100, {"sender": "甲", "content": "早上好", "timestamp": (now - 600) * 1000}),
        (101, {"sender": "乙", "content": "早", "timestamp": (now - 500) * 1000}),
        (102, {"sender": "小鲸鱼", "content": "早啊", "timestamp": (now - 400) * 1000,
               "is_self": True}),
    ]
    for event_id, payload in messages:
        db.execute("INSERT INTO chat_inbox (id, session_id, payload) VALUES (?, ?, ?)",
                   (event_id, SID, json.dumps(payload, ensure_ascii=False)))
    db.commit()
    db.close()


def child_env():
    env = dict(os.environ)
    env["WATER_GROUP_HOME"] = str(HOME)
    env["IDLEJOT_WG_DB"] = str(DB)
    env["IDLEJOT_WG_CONFIG"] = str(FIXTURE_CONFIG)
    env["PYTHONUTF8"] = "1"
    for name in ("IDLEJOT_WG_STATE", "TULPA_WG_STATE"):
        env.pop(name, None)
    return env


def run(script, *args, timeout=180):
    return subprocess.run([sys.executable, str(DAEMON / script), *args],
                          cwd=str(ROOT), env=child_env(), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# ---------- wg_clock：格式与语义 ----------


@case
def 时间戳格式可解析回原值():
    # 固定时刻（2026-10-23 20:52:07 附近）经 stamp() 再 parse 必须回到同一秒，
    # 证明用的是本地墙钟而不是 UTC 或毫秒。
    fixed = 1792500000
    parsed = datetime.strptime(wg_clock.stamp(fixed), "%Y-%m-%d %H:%M:%S")
    return abs(parsed.timestamp() - fixed) < 1


@case
def now_line_就是此刻():
    line = wg_clock.now_line()
    return (line.startswith("现在 ") and NOW_RE.fullmatch(line) is not None
            and is_now(line, tolerance=2))


@case
def 时区标签跟随本地偏移():
    # 不写死 UTC+8：标签必须和本机 utcoffset 一致（CI 在别的时区也要对）。
    offset = wg_clock._local().utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset else 0
    label = wg_clock.tz_label()
    if minutes == 0:
        return label == "UTC"
    sign = "+" if minutes > 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return label == (f"UTC{sign}{hours}" if not rest else f"UTC{sign}{hours}:{rest:02d}")


@case
def 剩余秒数不为负():
    now = time.time()
    return (wg_clock.seconds_left(now + 30, now=now) == 30
            and wg_clock.seconds_left(now - 30, now=now) == 0
            and wg_clock.seconds_left(None) is None)


# ---------- up.py --status：首行锚点 ----------


@case
def status_首个非空行是现在():
    proc = run("up.py", "--status")
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if proc.returncode != 0 or not lines:
        return False
    first = lines[0]
    return (NOW_RE.match(first) is not None and is_now(first)
            and f"({wg_clock.tz_label()})" in first)


# ---------- reply.py --list / --context：表头锚点 ----------


@case
def list_表头带现在():
    proc = run("reply.py", "--list")
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if proc.returncode != 0 or not lines:
        return False
    return lines[0].startswith("# 唤醒记录 · 现在 ") and is_now(lines[0])


@case
def context_表头带现在():
    proc = run("reply.py", "--context", "5")
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if proc.returncode != 0 or not lines:
        return False
    head = lines[0]
    return (re.match(r"^# 最近 \d+ 条（含闲聊，不只是 @）· 现在 ", head) is not None
            and is_now(head))


# ---------- followup.py --show：首行锚点 + 剩余量 ----------


@case
def followup_show_带现在和剩余():
    window = {"after_id": 99, "session": SID, "not_before": time.time() - 60,
              "opened_at": time.time() - 30, "until": time.time() + 90,
              "budget": 3, "max_messages": 10, "idle_seconds": 180, "wakes": []}
    (HOME / "state").mkdir(parents=True, exist_ok=True)
    (HOME / "state" / "wg_followup.json").write_text(
        json.dumps(window, ensure_ascii=False), encoding="utf-8")
    proc = run("followup.py", "--show")
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if proc.returncode != 0 or len(lines) < 2:
        return False
    return (NOW_RE.match(lines[0]) is not None and is_now(lines[0])
            and "还剩" in lines[1])


# ---------- watch.py：唤醒块先给基准 ----------


@case
def watch_唤醒块先给现在():
    rows = [{"id": 1, "timestamp": (time.time() - 30) * 1000, "is_self": False,
             "sender": "甲", "content": "@机器人 在吗", "conversation_id": GROUP}]
    saved = (up.events, up.replied_ids, up.wake_reason, up.wake_expired,
             up.replied_after, watch.read_cursor, watch.write_cursor, sys.argv)
    up.events = lambda: list(rows)
    up.replied_ids = lambda: set()
    up.wake_reason = lambda event: "at"
    up.wake_expired = lambda event: False
    up.replied_after = lambda *args, **kwargs: False
    watch.read_cursor = lambda: 0
    watch.write_cursor = lambda value: None
    sys.argv = ["watch.py", "--timeout", "5", "--replay"]
    # #51 后 main() 启动即校验 self_account：本用例在进程内跑，桩一份最小身份
    # （wg_config.CONFIG 是 import 期求值的模块全局，子进程那套 env 覆盖管不到）。
    real_config = wg_config.CONFIG
    wg_config.CONFIG = FIXTURE_CONFIG
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = watch.main()
    finally:
        wg_config.CONFIG = real_config
        (up.events, up.replied_ids, up.wake_reason, up.wake_expired,
         up.replied_after, watch.read_cursor, watch.write_cursor, sys.argv) = saved
    lines = [ln.strip() for ln in buf.getvalue().splitlines() if ln.strip()]
    return (code == 0 and len(lines) >= 3
            and NOW_RE.match(lines[0]) is not None and is_now(lines[0])
            and lines[1].startswith("id=") and lines[-1] == "WAKE")


def main():
    return fs.run_cases(CASES, setup=build_db)


if __name__ == "__main__":
    sys.exit(main())
