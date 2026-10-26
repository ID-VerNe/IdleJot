"""issue #132 gate: 发送失败必须说出真实原因，禁言要说得出解禁时刻。

issue #132 的现场：机器人被禁言，`reply.py ... --at <花名>` 打出

    send: retcode=200 message_id=None
    SEND_FAILED: 带 @ 的消息只能走 OneBot，计划通道会把它降级成纯文本

第二句说的是「为什么不退回计划通道」，被读成「是 @ 害的」，Agent 去掉 @ 重发一次
照样失败。OneBot 响应体里的 `message`（禁言是 `EventChecker Failed ... result 120`）
和 `get_group_member_info().shut_up_timestamp`（解禁时刻）一个字都没进输出。

本门禁用假 OneBot 造四类响应，钉住：

  1. 三类失败（禁言 / 参数错 / 连接失败）文案**互不相同**，都带原始 retcode 与
     message 首行；「未退回计划通道」是单独一句，排在原因之后；
  2. 禁言档含「禁言至 HH:MM:SS」与剩余秒数；未禁言档不得出现解禁时刻；
     探测失败档说「未知」，不许冒充「未检出禁言」（带对照组）；
  3. `--wait-unmute`：预算内不真睡（打桩 `SLEEP` 记录调用）并补发一次，
     超预算不睡、如实报「需等 N 秒」、退出 1；
  4. 成功路径文案不变；首条成功第二条失败时不重发（原有语义不许被重试破坏）。

    .venv/Scripts/python.exe scripts/check_reply_send_failure.py
"""
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_fixture_support  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)

sys.path.insert(0, str(ROOT / "water-group" / "daemon"))
sys.path.insert(0, str(ROOT))

# 数据根 / 配置 / 库全部钉进临时目录，import 任何 daemon 模块之前完成：
# memory_store 与 wg_paths 的常量是 import 期求值的（与 check_alias_visibility 同款）。
_SCRATCH = Path(tempfile.mkdtemp(prefix="wg_send_failure_"))
os.environ["WATER_GROUP_HOME"] = str(_SCRATCH / "home")
os.environ["TULPA_WG_STATE"] = str(_SCRATCH / "state")
os.environ["IDLEJOT_WG_DB"] = str(_SCRATCH / "chats.sqlite3")
os.environ["IDLEJOT_WG_CONFIG"] = str(_SCRATCH / "config.json")
for path in ("home", "state"):
    (_SCRATCH / path).mkdir(parents=True, exist_ok=True)

from fixture_server import FixtureHandler, fixture_server  # noqa: E402
import reply  # noqa: E402
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
FIX = {"sends": [], "replies": [], "member": {}, "group": {}, "member_fails": False}


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
            if FIX["member_fails"]:
                self.send_json({"status": "failed", "retcode": 1200, "message": "查询失败"})
            else:
                self.send_json({"status": "ok", "retcode": 0, "data": FIX["member"]})
        elif action == "get_group_info":
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


def _reset(now, shut_up=None, all_shut=False, member_fails=False):
    _write_state()
    wg_send_failure.clear()
    FIX["sends"].clear()
    FIX["replies"].clear()
    FIX["member_fails"] = member_fails
    FIX["member"] = {"user_id": int(SELF), "card": "机器人", "nickname": "bot",
                     "role": "member", "shut_up_timestamp": shut_up or 0}
    FIX["group"] = {"group_id": int(GROUP), "group_all_shut": all_shut}


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def 禁言档_带原文与解禁时刻():
    now = time.time()
    until = int(now) + 300
    _reset(now, shut_up=until)
    FIX["replies"].append(_mute_send())
    code, out = _run(["1", TEXT, "--at", "群友A", "--no-wait"])
    reason = out.index("SEND_FAILED: OneBot retcode=200 message=EventChecker Failed")
    separate = out.index("未退回计划通道：带 @ 的消息只能走 OneBot")
    return (code == 1
            and 'EventRet: {"result": 120}' in out
            and f"机器人被禁言至 {wg_clock.clock(until)}" in out
            and any(f"还剩 {n} 秒" in out for n in (298, 299, 300))
            and "单人禁言" in out
            and reason < separate        # 先原因，后「为什么不降级」
            and "需等 30" not in out)     # 没传 --wait-unmute 就别说等着补发


@case
def 参数错档_与禁言档文案不同():
    _reset(time.time(), shut_up=0)
    FIX["replies"].append({"status": "failed", "retcode": 1400,
                           "message": PARAM_MESSAGE, "wording": "参数错误", "data": None})
    code, out = _run(["1", TEXT, "--at", "群友A", "--no-wait"])
    return (code == 1
            and "SEND_FAILED: OneBot retcode=1400 message=参数错误：group_id 无效" in out
            and "禁言至" not in out          # 对照组：不是禁言就不许说禁言
            and "未检出禁言" in out)


@case
def 连接失败档_原文是异常且不探测禁言():
    _reset(time.time(), shut_up=0)
    alive = _BASE[0]
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead = probe.getsockname()[1]
    probe.close()
    _point(f"http://127.0.0.1:{dead}")
    try:
        code, out = _run(["1", TEXT, "--at", "群友A", "--no-wait"])
    finally:
        _point(alive)
    return (code == 1
            and "SEND_FAILED: OneBot 请求失败" in out
            and "未探测禁言：请求在拿到响应之前就失败了" in out
            and "禁言至" not in out)


@case
def 未禁言档_不编解禁时刻():
    _reset(time.time(), shut_up=0)
    FIX["replies"].append(_mute_send())
    code, out = _run(["1", TEXT, "--at", "群友A", "--no-wait"])
    return code == 1 and "未检出禁言（shut_up_timestamp=0）" in out and "禁言至" not in out


@case
def 探测失败档_说未知不说未禁言():
    _reset(time.time(), member_fails=True)
    FIX["replies"].append(_mute_send())
    code, out = _run(["1", TEXT, "--at", "群友A", "--no-wait"])
    return (code == 1 and "禁言状态未知" in out
            and "未检出禁言" not in out and "禁言至" not in out)   # 对照组：未知≠没禁言


@case
def 全员禁言与单人禁言分开说():
    now = time.time()
    _reset(now, shut_up=int(now) + 120, all_shut=True)
    FIX["replies"].append(_mute_send())
    _, out = _run(["1", TEXT, "--at", "群友A", "--no-wait"])
    all_shut = "全员禁言" in out and "单人禁言" not in out
    _reset(now, shut_up=int(now) + 120, all_shut=False)
    FIX["replies"].append(_mute_send())
    _, out2 = _run(["1", TEXT, "--at", "群友A", "--no-wait"])
    return all_shut and "单人禁言" in out2 and "全员禁言" not in out2


@case
def 等待预算_边界与唯一出处():
    """85+5=90 正好等于预算 → 允许；86+5=91 → 拒绝（预算是 wg_budget 的唯一出口）。"""
    allowed, _ = wg_send_failure.wait_plan(wg_budget.WAIT_BUDGET - wg_send_failure.UNMUTE_MARGIN)
    refused, lines = wg_send_failure.wait_plan(
        wg_budget.WAIT_BUDGET - wg_send_failure.UNMUTE_MARGIN + 1)
    return (allowed == wg_budget.WAIT_BUDGET and refused is None
            and any(f"超出单次调用等待预算 {wg_budget.WAIT_BUDGET}s" in ln for ln in lines))


@case
def 补发_超预算不硬等():
    now = time.time()
    _reset(now, shut_up=int(now) + 300)
    FIX["replies"].append(_mute_send())
    sleeps = []
    real = wg_send_failure.SLEEP
    wg_send_failure.SLEEP = sleeps.append
    try:
        code, out = _run(["1", TEXT, "--at", "群友A", "--no-wait", "--wait-unmute"])
        sends = len(FIX["sends"])
    finally:
        wg_send_failure.SLEEP = real
    return (code == 1 and sleeps == [] and sends == 1
            and "超出单次调用等待预算 90s" in out)


@case
def 补发_预算内睡到解禁并发出():
    now = time.time()
    _reset(now, shut_up=int(now) + 20)
    FIX["replies"].extend([_mute_send(), _ok_send(), _ok_send()])
    sleeps = []
    real = wg_send_failure.SLEEP
    wg_send_failure.SLEEP = sleeps.append
    try:
        code, out = _run(["1", TEXT, "--at", "群友A", "--no-wait", "--wait-unmute"])
        sends = len(FIX["sends"])
    finally:
        wg_send_failure.SLEEP = real
    ledger = wg_send_failure.read()
    return (code == 0 and len(sleeps) == 1 and 20 <= sleeps[0] <= 25
            and sends == 3     # 1 次失败 + 2 条补发
            and "补发：2/2 条已发出" in out and "SENT" in out
            # 账本记的是原始原因，不是补发那一轮的空 dict
            and ledger.get("recovered") is True
            and (ledger.get("reason") or "").startswith("OneBot retcode=200"))


@case
def 未传_wait_unmute_不睡也不补发():
    now = time.time()
    _reset(now, shut_up=int(now) + 20)
    FIX["replies"].extend([_mute_send(), _ok_send()])
    sleeps = []
    real = wg_send_failure.SLEEP
    wg_send_failure.SLEEP = sleeps.append
    try:
        code, out = _run(["1", TEXT, "--at", "群友A", "--no-wait"])
        sends = len(FIX["sends"])
    finally:
        wg_send_failure.SLEEP = real
    return code == 1 and sleeps == [] and sends == 1 and "SENT" not in out


@case
def 成功路径文案不变():
    _reset(time.time(), shut_up=0)
    code, out = _run(["1", TEXT, "--at", "群友A", "--no-wait"])
    return (code == 0 and "send: retcode=0 message_id=4242" in out
            and "SENT" in out and "SEND_FAILED" not in out)


@case
def 中途失败不重发():
    now = time.time()
    _reset(now, shut_up=0)
    FIX["replies"].extend([_ok_send(), {"status": "failed", "retcode": 200,
                                        "message": "消息被撤回", "data": None}])
    code, out = _run(["1", TEXT, "--at", "群友A", "--no-wait", "--wait-unmute"])
    return (code == 1 and "前 1 条已发出，第 2 条失败，不再重试" in out
            and "消息被撤回" in out and len(FIX["sends"]) == 2)   # 没有第三次


@case
def list_表头带出上次失败():
    now = time.time()
    _reset(now, shut_up=int(now) + 300)
    FIX["replies"].append(_mute_send())
    _run(["1", TEXT, "--at", "群友A", "--no-wait"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        reply.list_wakes()
    out = buf.getvalue()
    # 解禁时刻允许秒级抖动：夹具的 shut_up 用 int(now)+300 造，落盘的是「记录时刻+mute」。
    stamps = [wg_clock.clock(int(now) + n) for n in range(296, 302)]
    return ("# 上次发送失败：" in out and "retcode=200" in out
            and f"group={GROUP}" in out
            and any(f"禁言至 {stamp}" in out for stamp in stamps))


@case
def 没有失败记录时不出声():
    """对照组：恒加注脚的实现红在这里（一次都没失败过也报一句 = 假事实）。"""
    _reset(time.time(), shut_up=0)
    wg_send_failure.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        reply.list_wakes()
    return "# 上次发送失败" not in buf.getvalue()


_BASE = [""]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    bad = 0
    with fixture_server(Handler) as server:
        base = f"http://127.0.0.1:{server.server_port}"
        _point(base)
        reply.onebot_config = lambda: (_BASE[0], "fixture-token")
        for fn in CASES:
            try:
                ok = fn()
            # argparse 拒绝未知参数时抛 SystemExit：那是「这条用例的输入没被支持」，
            # 记成这一条红就好，别让整个门禁在第一条卡住、后面 13 条一条都不跑。
            except (Exception, SystemExit) as exc:  # noqa: BLE001
                print(f"✗ {fn.__name__}: {type(exc).__name__}: {exc}")
                bad += 1
                continue
            print(("✓ " if ok else "✗ ") + fn.__name__)
            bad += 0 if ok else 1
    print("全部通过" if not bad else f"{bad} 项失败")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
