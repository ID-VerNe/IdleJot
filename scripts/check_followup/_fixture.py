"""check_followup 包的夹具层（issue #284）。

从旧 `scripts/check_followup.py` 整体搬运，按 #169 PF-2（`scripts/check_listener_rehang/`）
与 #247（`scripts/check_dossier_cadence/`）的先例拆出。本模块只放钉住临时库 / 临时
状态目录 / `WATCH` 路径的环境层，加上 `MESSAGES` / `FOREIGN` / `GROUP` / `ONEBOT`
常量与 `append` / `build` / `run_watch` 三个夹具函数。24 条 `check_*` 用例与 `main()`
留主模块 `__init__.py`。

与 `check_qr_login/_fixture.py` 的关键差别：check_followup 的 runner 不是 R4 的
`check()` / `FAILED` / `全部通过` 形态（grep 证实原文件 0 处 `check(` / `FAILED` /
`全部通过` / `assert_runner`），而是更早的裸 `assert` + 每条用例末尾
`print("  ok  <desc>")` + `main()` 末尾 `print("PASS")`。故本模块**不**委派
`assert_runner`——委派会把输出从 `  ok  <desc>` / `PASS` 改成 `ok  <name>` /
`全部通过`（ok+3 空格 vs 2 空格+ok+2 空格，尾行 PASS vs 全部通过），破 byte-identity。
拆包只搬夹具层，runner 形态原样留在 `__init__.py`。issue #284 正文「runner 是 R4
形态」一句是误述，已在追评点明。

与 `check_qr_login/_fixture.py` 同口径的路径收口：包式门禁经
`python -m scripts.check_followup` 调起，此时 `sys.path[0]` 是仓库根（cwd）而非
`scripts/`——后者才是 `check_fixture_support` 所在地。无法先调
`bootstrap_paths`（它自己要 import fs），故手动把 `scripts/` 插进 `sys.path`，
再进口 `check_fixture_support`，随后 `bootstrap_paths()` 收口 root + daemon。

    .venv/Scripts/python.exe -m scripts.check_followup
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 包式门禁经 `python -m scripts.check_followup` 调起，此时 `sys.path[0]` 是
# 仓库根（cwd）而非 `scripts/`——后者才是 `check_fixture_support` 所在地。无法先调
# `bootstrap_paths`（它自己要 import fs），故手动把 `scripts/` 插进 `sys.path`，
# 再进口 `check_fixture_support`，随后 `bootstrap_paths()` 收口 root + daemon。
_scripts = Path(__file__).resolve().parents[1]
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))

import check_fixture_support as fs  # noqa: E402  (issue #169 PE: path 注入收口)
from check_fixture_support import scratch_dir  # noqa: E402
fs.bootstrap_paths(scripts=False, root=False)

TMP = scratch_dir(prefix="wg_followup_")
DB = TMP / "fixture.sqlite3"
SID = "session-fixture"
WATCH = ROOT / "water-group" / "daemon" / "watch.py"

# 两个旧名都要钉：state_dir()/DB 先读 IDLEJOT_*，只设 TULPA_* 时外面若已有
# IDLEJOT_* 就会落回实机状态——本文件会 close() 窗口，等于删掉真实窗口文件。
os.environ["TULPA_WG_DB"] = str(DB)
os.environ["IDLEJOT_WG_DB"] = str(DB)
os.environ["TULPA_WG_STATE"] = str(TMP)
os.environ["IDLEJOT_WG_STATE"] = str(TMP)

sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

import followup  # noqa: E402
import up  # noqa: E402  (`#` 指令闸门与 wake_reason 同源，issue #70)
import wg_text  # noqa: E402  (追问行正文渲染，issue #138)
import wg_up_wake  # noqa: E402  (issue #37 PC: config 的真实调用点；up.config 只是门面引用)

# 5 是被回复的那条；6/9 是群友接话；7 是机器人自己；8 是系统提示；
# 10 属于另一个会话；11 是稍后机器人自己补的一句。第三个字段是入库时刻 at。
MESSAGES = [
    (5, {"sender": "甲", "content": "@机器人 在吗", "timestamp": 1792500000000}, 1792500000.0),
    (6, {"sender": "乙", "content": "在的在的", "timestamp": 1792500001000}, 1792500001.0),
    (7, {"sender": "小鲸鱼", "content": "在呢", "timestamp": 1792500002000,
         "is_self": True}, 1792500002.0),
    (8, {"sender": "系统", "content": "提示", "timestamp": 1792500003000,
         "synthetic": True}, 1792500003.0),
    (9, {"sender": "丙", "content": "问作业吗", "timestamp": 1792500004000}, 1792500004.0),
    (11, {"sender": "小鲸鱼", "content": "刚那句当我没说", "timestamp": 1792500006000,
          "is_self": True}, 1792500006.0),
]
FOREIGN = (10, {"sender": "外人", "content": "别的会话", "timestamp": 1792500005000},
           1792500005.0)
GROUP = "123"
ONEBOT = {5: "775", 6: "776", 7: "777", 8: "778", 9: "779", 10: "7710", 11: "7711"}


def append(rows):
    """往临时库里追加消息：(id, payload, at)，at 是入库时刻。"""
    db = sqlite3.connect(DB)
    for event_id, payload, at in rows:
        data = dict(payload)
        data["conversation_id"] = f"111:group:{GROUP}"
        data["onebot_message_id"] = ONEBOT.get(event_id, str(7000 + event_id))
        db.execute("INSERT INTO chat_inbox(id,session_id,payload,at,media) VALUES (?, ?, ?, ?, '[]')",
                   (event_id, SID, json.dumps(data, ensure_ascii=False), at))
    db.commit()
    db.close()


def build():
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE chat_sessions (id TEXT, conversation_id TEXT, active INTEGER)")
    db.execute("CREATE TABLE chat_inbox "
               "(id INTEGER PRIMARY KEY, session_id TEXT, payload TEXT, at REAL NOT NULL,"
               " media TEXT NOT NULL DEFAULT '[]')")
    db.execute("INSERT INTO chat_sessions VALUES (?, ?, 1)", (SID, f"111:group:{GROUP}"))
    db.execute("INSERT INTO chat_sessions VALUES ('dead-session', '100:group:999', 0)")
    foreign = dict(FOREIGN[1])
    foreign["conversation_id"] = "100:group:999"
    foreign["onebot_message_id"] = ONEBOT[FOREIGN[0]]
    db.execute("INSERT INTO chat_inbox(id,session_id,payload,at,media) VALUES (?, ?, ?, ?, '[]')",
               (FOREIGN[0], "dead-session", json.dumps(foreign, ensure_ascii=False), FOREIGN[2]))
    db.commit()
    db.close()
    append(MESSAGES)


def run_watch():
    """跑一次 `watch.py --timeout 5`，返回 (returncode, 合并后的输出)。

    #51 之后 watch.py 启动就要 self_account；config.json 被 gitignore，CI 干净
    checkout 没有它——写份最小配置用 IDLEJOT_WG_CONFIG 指过去（只进子进程 env，
    不动本进程 environ：本文件其他用例对 config 有自己的桩）。
    """
    fixture = TMP / "watch-fixture-config.json"
    fixture.write_text(json.dumps({"self_account": "10001",
                                   "onebot_token": "followup-fixture"},
                                  ensure_ascii=False), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    for name in ("WATER_GROUP_HOME", "IDLEJOT_WG_STATE", "TULPA_WG_STATE",
                 "IDLEJOT_WG_DB", "TULPA_WG_DB"):
        env.pop(name, None)
    env["IDLEJOT_WG_STATE"] = str(TMP)
    env["TULPA_WG_STATE"] = str(TMP)
    env["IDLEJOT_WG_DB"] = str(DB)
    env["TULPA_WG_DB"] = str(DB)
    env["IDLEJOT_WG_CONFIG"] = str(fixture)
    result = subprocess.run([sys.executable, str(WATCH), "--timeout", "5"],
                            cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", timeout=120)
    return result.returncode, result.stdout or ""
