"""check_reply_send_failure 包的夹具层（issue #287）。

从旧 `scripts/check_reply_send_failure.py` 整体搬运，按 #282（`scripts/check_qr_login/`）
与 #186（`scripts/check_listener_rehang/`）的先例拆出。本模块只放假 OneBot 的 `Handler`
夹具、8 个 fixture-state helper（`_ok_send`/`_mute_send`/`_write_config`/`_point`/
`_write_state`/`_run`/`_reset`/`_ledger`/`_list`）与模块级状态（`_SCRATCH`/`FIX`/
`_BASE`/`GROUP`/`SELF`/`PEER`/`MUTE_MESSAGE`/`PARAM_MESSAGE`/`TEXT`）。37 个 `@case`
用例、`CASES`/`case` 收集器、`_has` 断言 helper 与 `main()` 留主模块 `__init__.py`。

runner 归属：本文件**没有** R4 的 `check()`/`FAILED` 族（grep 零命中），主收集器是
`@case`/`CASES`（dedup §6 特殊形态，不归 R4）——故 `assert_runner` 委派不适用，
本 PR 只做夹具层拆包。

与 `check_qr_login/_fixture.py` 同口径：包式门禁经
`python -m scripts.check_reply_send_failure` 调起，此时 `sys.path[0]` 是仓库根（cwd）而非
`scripts/`——后者才是 `check_fixture_support` 所在地。无法先调
`bootstrap_paths`（它自己要 import fs），故手动把 `scripts/` 插进 `sys.path`，
再进口 `check_fixture_support`，随后 `bootstrap_paths()` 收口 root + daemon。
"""
import contextlib
import io
import json
import os
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 包式门禁经 `python -m scripts.check_reply_send_failure` 调起，此时 `sys.path[0]` 是
# 仓库根（cwd）而非 `scripts/`——后者才是 `check_fixture_support` 所在地。无法先调
# `bootstrap_paths`（它自己要 import fs），故手动把 `scripts/` 插进 `sys.path`，
# 再进口 `check_fixture_support`，随后 `bootstrap_paths()` 收口 root + daemon。
_scripts = Path(__file__).resolve().parents[1]
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))

import check_fixture_support as fs  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import scratch_dir  # noqa: E402
fs.bootstrap_paths(scripts=False)

# 数据根 / 配置 / 库全部钉进临时目录，import 任何 daemon 模块之前完成：
# memory_store 与 wg_paths 的常量是 import 期求值的（与 check_alias_visibility 同款）。
# `IDLEJOT_WG_STATE` 必须**显式覆盖**：`wg_paths.state_dir()` 的优先级是
# `IDLEJOT_WG_STATE or TULPA_WG_STATE`，只钉后者会被外部环境穿透——那正是
# 「测试写生产状态」的口子（#145 的账本复查上线后，穿透就意味着向实机 OneBot
# 发查询、把结论写进实机账本）。
_SCRATCH = scratch_dir(prefix="wg_send_failure_")
os.environ["WATER_GROUP_HOME"] = str(_SCRATCH / "home")
os.environ["TULPA_WG_STATE"] = str(_SCRATCH / "state")
os.environ["IDLEJOT_WG_STATE"] = str(_SCRATCH / "state")
os.environ["IDLEJOT_WG_DB"] = str(_SCRATCH / "chats.sqlite3")
os.environ["IDLEJOT_WG_CONFIG"] = str(_SCRATCH / "config.json")
for path in ("home", "state"):
    (_SCRATCH / path).mkdir(parents=True, exist_ok=True)

from fixture_server import FixtureHandler, fixture_server  # noqa: E402
import reply  # noqa: E402
import followup  # noqa: E402  (open_followup 写盘护栏的 monkeypatch 点，issue #227)
import wg_budget  # noqa: E402
import wg_clock  # noqa: E402
import wg_send_failure  # noqa: E402

GROUP = "777000111"
SELF = "99999"
PEER = "88888"
MUTE_MESSAGE = ("EventChecker Failed: NTEvent serviceAndMethod:"
                "NodeIKernelMsgService/sendMsg EventRet: {\"result\": 120}")
PARAM_MESSAGE = "参数错误：group_id 无效"
TEXT = "第一句\\n第二句"

# 夹具状态：每个用例自己写，Handler 只读。
# `member_queries` 数的是 `get_group_member_info` 次数——#145 的「未到点不查」
# 只能靠请求计数判定，看输出是看不出少发了一次查询的。
FIX = {"sends": [], "replies": [], "member": {}, "group": {},
       "member_fails": False, "group_fails": False, "member_queries": 0}


def _ok_send():
    return {"status": "ok", "retcode": 0, "data": {"message_id": 4242}}


def _mute_send():
    return {"status": "failed", "retcode": 200, "message": MUTE_MESSAGE,
            "wording": "发送失败", "data": None}


class Handler(FixtureHandler):
    def do_POST(self):
        self.read_json()
        action = self.path.strip("/")
        if action == "send_group_msg":
            FIX["sends"].append(1)
            self.send_json(FIX["replies"].pop(0) if FIX["replies"] else _ok_send())
        elif action == "get_group_member_info":
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
        elif action == "get_group_member_list":
            self.send_json({"status": "ok", "retcode": 0, "data": [
                {"user_id": int(SELF), "card": "机器人", "nickname": "bot"},
                {"user_id": int(PEER), "card": "群友A", "nickname": "A"}]})
        else:
            self.send_json({"status": "failed", "retcode": 1404, "message": action})


def _write_config(base):
    (_SCRATCH / "config.json").write_text(json.dumps({
        "self_account": SELF, "onebot_token": "fixture-token",
        "onebot_http_url": base, "groups": {},
    }, ensure_ascii=False), encoding="utf-8")


def _point(url):
    """让 `reply.py` 与诊断模块指向同一个夹具地址。

    `reply.py` 的 `onebot_config()` 固定在这里是为了让本门禁只管 #132：未打补丁
    的 base 读的是仓库配置（干净 checkout 里根本没有），不钉住的话「成功路径」
    也会红，红的原因就跟 issue 无关了。诊断模块自己读 `IDLEJOT_WG_CONFIG`。
    """
    _BASE[0] = url
    _write_config(url)


def _write_state():
    """一条可回的 @ + 会话 id，让 `reply.main()` 走真实的取群号/取 message_id 路径。"""
    state = _SCRATCH / "state"
    (state / "wg_session.txt").write_text("s1", encoding="utf-8")
    row = {"id": 1, "conversation_id": f"1:group:{GROUP}", "onebot_message_id": "12345",
           "sender": "群友A", "content": "在吗", "timestamp": int(time.time() * 1000)}
    (state / "wg_events.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n",
                                           encoding="utf-8")
    for name in ("wg_replied.jsonl",):
        path = state / name
        if path.exists():
            path.unlink()


def _run(argv):
    """跑一次 reply.main()，返回 (退出码, 输出)。"""
    buf = io.StringIO()
    real_argv = sys.argv
    sys.argv = ["reply.py"] + argv
    try:
        with contextlib.redirect_stdout(buf):
            try:
                code = reply.main()
            except SystemExit as exc:   # argparse 拒绝未知参数（如旧版不认 --wait-unmute）
                code = int(exc.code or 0)
    finally:
        sys.argv = real_argv
    return code, buf.getvalue()


def _reset(now, shut_up=None, all_shut=False, member_fails=False, group_fails=False):
    _write_state()
    wg_send_failure.clear()
    FIX["sends"].clear()
    FIX["replies"].clear()
    FIX["member_fails"] = member_fails
    FIX["group_fails"] = group_fails
    FIX["member_queries"] = 0
    FIX["member"] = {"user_id": int(SELF), "card": "机器人", "nickname": "bot",
                     "role": "member", "shut_up_timestamp": shut_up or 0}
    FIX["group"] = {"group_id": int(GROUP), "group_all_shut": all_shut}


def _ledger(mute_until, at=None, recovered=None, **extra):
    """直接写一条账本（模拟上一次失败留下的冻结记录），不经过发送路径。"""
    entry = {"at": int(time.time() if at is None else at), "group_id": GROUP,
             "kind": "onebot", "retcode": 200, "reason": "EventChecker Failed",
             "mute_until": int(mute_until), "recovered": recovered,
             "recovered_at": None, "rechecked_at": None, "group_shut": None}
    entry.update(extra)
    wg_send_failure.LEDGER.parent.mkdir(parents=True, exist_ok=True)
    wg_send_failure.LEDGER.write_text(json.dumps(entry, ensure_ascii=False),
                                      encoding="utf-8")
    return entry


def _list():
    """跑一次 `--list` 的真实表头路径，返回输出。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        reply.list_wakes()
    return buf.getvalue()


_BASE = [""]
