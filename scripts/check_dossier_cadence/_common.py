"""check_dossier_cadence 包的公共夹具与数据根（issue #247）。

从旧 `scripts/check_dossier_cadence.py` 整体搬运，按 #169 PF-2（`scripts/check_listener_rehang/`）
的先例拆成子模块。本模块只放常量、夹具函数与 helper，所有 23 条用例按 issue 分组落进
`quiet_rounds` / `clock_fields` / `backlog_cursor` / `floor_reading`。

与 `check_listener_rehang/_common.py` 同口径：包式门禁经 `python -m scripts.check_dossier_cadence`
调起，此时 `sys.path[0]` 是仓库根（cwd）而非 `scripts/`。手动把 `scripts/` 插进
`sys.path`，再进口 `check_fixture_support`，随后 `bootstrap_paths()` 收口 root + daemon。
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]

# 包式门禁经 `python -m scripts.check_dossier_cadence` 调起，此时 `sys.path[0]` 是
# 仓库根（cwd）而非 `scripts/`——后者才是 `check_fixture_support` 所在地。无法先调
# `bootstrap_paths`（它自己要 import fs），故手动把 `scripts/` 插进 `sys.path`，
# 再进口 `check_fixture_support`，随后 `bootstrap_paths()` 收口 root + daemon（issue #247）。
_scripts = Path(__file__).resolve().parents[1]
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))

import check_fixture_support as fs  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import isolated_env, temp_dir  # noqa: E402

fs.bootstrap_paths()

DAEMON = ROOT / "water-group" / "daemon"
DOSSIER = DAEMON / "dossier.py"
LISTEN = DAEMON / "listen.py"
GROUP = "100200300"
ACCOUNT = "10001"
MARK_REL = Path("state") / "wg_dossier.json"

# 外部测试变量会把 state 指回实机，这里清掉，只留临时数据根。
ISOLATING_VARS = ("IDLEJOT_WG_STATE", "TULPA_WG_STATE", "IDLEJOT_WG_DB",
                  "IDLEJOT_WG_MEMORY_DB")


# ---------- 夹具 ----------

# 用例 13 的端到端夹具，也是用例 23 的两档：共用常量才可能「一起调才同时绿」（#215）。
# 这三个常量与 `fixture_floor_is_tight()` 必须留在单一模块（#247 拆分不许松掉 #215 的耦合）：
# 用例 13（`floor_reading.py`）与用例 23（同文件）都 `from ._common import` 它们。
FIXTURE_ROUND_SECONDS = 30      # `--round-seconds` 回推的跨度
FIXTURE_FLOOR_MINUTES = 0.5     # 必须恰好等于上面那 30s，见 fixture_floor_is_tight
BLOCKED_FLOOR_MINUTES = 1.5     # 「未过」档：必须高于合成跨度 2×0.5min


def fixture_floor_is_tight(round_seconds=FIXTURE_ROUND_SECONDS,
                           floor_minutes=FIXTURE_FLOOR_MINUTES):
    """下限必须**恰好**等于单轮回推跨度：偏大 → 又变成机器速度的函数（#196 复发），偏小 → 只是白给（#215）。"""
    return floor_minutes == round_seconds / 60.0


def write_config(home, dossier=None):
    """临时配置：watch.py 要 self_account（#51），阈值走 `dossier` 段。

    阈值必须能覆盖，否则门禁只能吃默认值——开发机的 `config.json` 里一旦出现
    `dossier` 段，门禁就变成依赖机器状态（与 `check_listener_rehang` 同一口径）。
    """
    payload = {"self_account": ACCOUNT, "onebot_token": "cadence-fixture"}
    if dossier:
        payload["dossier"] = dossier
    path = home / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def seed_store(home, messages=0, base_ts=None):
    """最小 mcp-access 库：一个活跃会话 + N 条可提炼的群消息。

    `base_ts` 是毫秒时刻（payload 里 `timestamp` 的单位）。issue #183 的 burst
    判据要看「最近窗口内有没有一波」，所以需要能造「一小时前的积压」与「刚刚的
    爆发」两种夹具；不给就是没有时间戳（旧夹具的形态）。
    """
    data = home / "data"
    data.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(data / "mcp-access.sqlite3"))
    db.executescript(
        """
        CREATE TABLE chat_sessions (id INTEGER PRIMARY KEY, conversation_id TEXT, active INTEGER);
        CREATE TABLE chat_inbox (id INTEGER PRIMARY KEY, session_id INTEGER, payload TEXT);
        INSERT INTO chat_sessions (id, conversation_id, active) VALUES (1, 'group:100200300', 1);
        """
    )
    for index in range(messages):
        payload = {"sender": "甲", "sender_id": "1", "content": f"第 {index + 1} 句闲聊"}
        if base_ts is not None:
            payload["timestamp"] = int(base_ts + index * 1000)
        db.execute("INSERT INTO chat_inbox (id, session_id, payload) VALUES (?, 1, ?)",
                   (index + 1, json.dumps(payload, ensure_ascii=False)))
    db.commit()
    db.close()


def add_message(home, event_id, ts=None):
    """追加一条消息（issue #183 的「有新素材就重新 DUE」用例要用）。"""
    payload = {"sender": "甲", "sender_id": "1", "content": f"第 {event_id} 句闲聊"}
    if ts is not None:
        payload["timestamp"] = int(ts)
    db = sqlite3.connect(str(home / "data" / "mcp-access.sqlite3"))
    db.execute("INSERT INTO chat_inbox (id, session_id, payload) VALUES (?, 1, ?)",
               (event_id, json.dumps(payload, ensure_ascii=False)))
    db.commit()
    db.close()


def run(home, script, *args, timeout=180):
    """跑真实子进程（`dossier.py` / `listen.py`），隔离环境 + 临时配置。"""
    env = isolated_env(home=home)
    env["IDLEJOT_WG_CONFIG"] = str(home / "config.json")
    env["SYSTEMDRIVE"] = os.environ.get("SYSTEMDRIVE", "C:")
    env["PYTHONUTF8"] = "1"
    for name in ISOLATING_VARS:
        env.pop(name, None)
    return subprocess.run([sys.executable, str(script), *args],
                          cwd=str(ROOT), env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def mark(home):
    try:
        return json.loads((home / MARK_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_mark(home, payload):
    path = home / MARK_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_events(home, rows):
    path = home / "state" / "wg_events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")


def mention_event(eid, content="@机器人 在吗"):
    """一条 @ 本机的落盘事件（`watch.py` 据此返回 0 = 唤醒）。"""
    return {"id": eid, "timestamp": time.time() * 1000, "is_self": False,
            "sender": "甲", "content": content,
            "segments": [{"type": "at", "qq": ACCOUNT},
                         {"type": "text", "text": content}],
            "conversation_id": "1:group:" + GROUP}


def tick(home, *extra):
    return run(home, DOSSIER, "--tick", *extra)


def check(home):
    return run(home, DOSSIER, "--check")


def dossier_module():
    """进程内导入 `dossier`（数据根已在 `check_fixture_support` 里钉死）。

    原为旧文件里的 `_dossier_module()`；拆包后多个子模块共用，提成 `_common` 的公共 helper。
    """
    sys.path.insert(0, str(DAEMON))
    import dossier  # noqa: PLC0415
    return dossier
