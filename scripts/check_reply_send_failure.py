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
  4. 成功路径文案不变；首条成功第二条失败时不重发（原有语义不许被重试破坏）；
  5. 只读入口的禁言复查（issue #145）：账本 `mute_until` 已到点 + 现查
     `shut_up_timestamp=0` 时必须说「实测解禁」并把 `recovered` 落盘，不得再打
     「禁言至 …」；未到点不查也不改口径；现查仍禁言 / 全员禁言 / 探测失败都不许
     冒充解禁（对照组）；未到点、已有结论、无账本三种前置零请求。

    .venv/Scripts/python.exe scripts/check_reply_send_failure.py
"""
import contextlib
import io
import json
import os
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def _has(out, *needles):
    """逐条检查子串，缺哪条就打印哪条——只报 True/False 的断言在 CI 上没法看。"""
    missing = [needle for needle in needles if needle not in out]
    if missing:
        print(f"    缺：{missing}")
    return not missing


@case
def 禁言档_带原文与解禁时刻():
    now = time.time()
    until = int(now) + 300
    _reset(now, shut_up=until)
    FIX["replies"].append(_mute_send())
    code, out = _run(["1", TEXT, "--at", "群友A", "--no-wait"])
    reason_text = "SEND_FAILED: OneBot retcode=200 message=EventChecker Failed"
    separate_text = "未退回计划通道：带 @ 的消息只能走 OneBot"
    ok = (code == 1
          and _has(out, reason_text, 'EventRet: {"result": 120}',
                   f"机器人被禁言至 {wg_clock.clock(until)}", "单人禁言", separate_text)
          and any(f"还剩 {n} 秒" in out for n in (298, 299, 300))
          and "需等 30" not in out)   # 没传 --wait-unmute 就别说等着补发
    if ok:
        ok = out.index(reason_text) < out.index(separate_text)   # 先原因，后「不降级」
        if not ok:
            print("    顺序错：原因行不在「未退回计划通道」之前")
    return ok


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
    return (code == 1
            and "未检出禁言（shut_up_timestamp=0，group_all_shut=False）" in out
            and "禁言至" not in out)


@case
def 全员禁言_个体时间戳为0也不算没禁言():
    """NapCat 在全员禁言时可能不给成员写 shut_up_timestamp（独立复核抓到的假事实）。"""
    _reset(time.time(), shut_up=0, all_shut=True)
    FIX["replies"].append(_mute_send())
    code, out = _run(["1", TEXT, "--at", "群友A", "--no-wait"])
    return (code == 1 and "全员禁言：整个群都发不出去" in out
            and "未检出禁言" not in out and "禁言状态未知" not in out)


@case
def 群信息查不到_说未知不说未禁言():
    """成员未禁言 + 群信息查询失败：只能说「状态未知」，不许判「与禁言无关」。"""
    _reset(time.time(), shut_up=0, group_fails=True)
    FIX["replies"].append(_mute_send())
    code, out = _run(["1", TEXT, "--at", "群友A", "--no-wait"])
    return (code == 1 and "禁言状态未知：读不到 group_all_shut" in out
            and "未检出禁言" not in out)


@case
def 群信息缺字段_同样算未知():
    """响应里没有 group_all_shut 时不能当 False（老版本 / 裁剪过的响应）。"""
    _reset(time.time(), shut_up=0)
    FIX["group"] = {"group_id": int(GROUP)}
    FIX["replies"].append(_mute_send())
    code, out = _run(["1", TEXT, "--at", "群友A", "--no-wait"])
    return (code == 1 and "禁言状态未知" in out and "未检出禁言" not in out)


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
    """85+5=90 正好等于预算 → 允许；86+5=91 → 拒绝（预算是 wg_budget 的唯一出口）。

    issue #224 的钉子：`wait_plan` 的「等不等得起」判定走 `remaining_budget()` 的
    原始浮点，不走 `clamp_wait()` 的 ceil——`clamp_wait(90, 0.4)` 会回 90（ceil 89.6），
    拿它当 `allowance < need` 的操作数就把 `90 > 89.6` 读成「90 不大于 90」，
    判成等得起、睡 90s，叠加 0.4s 已用 + 30s 发送就超出宿主 120s。
    """
    allowed, _ = wg_send_failure.wait_plan(wg_budget.WAIT_BUDGET - wg_send_failure.UNMUTE_MARGIN)
    refused, lines = wg_send_failure.wait_plan(
        wg_budget.WAIT_BUDGET - wg_send_failure.UNMUTE_MARGIN + 1)
    # 形参 budget 必须真的参与判定，不能只进文案（复核遗留 2）；elapsed 同理。
    tight, _ = wg_send_failure.wait_plan(30, budget=30)   # need=35 > 预算 30
    spent, _ = wg_send_failure.wait_plan(60, elapsed=30)
    # issue #224：已用 0.4s 时真实剩余 89.6，need=90 不该塞进去。
    # 85+5=90（need=90）在剩余 89.6 内塞不进 → None；84+5=89（need=89）塞得进 → 89。
    ceil_skipped, _ = wg_send_failure.wait_plan(
        wg_budget.WAIT_BUDGET - wg_send_failure.UNMUTE_MARGIN, elapsed=0.4)
    one_short, _ = wg_send_failure.wait_plan(
        wg_budget.WAIT_BUDGET - wg_send_failure.UNMUTE_MARGIN - 1, elapsed=0.4)
    return (allowed == wg_budget.WAIT_BUDGET and refused is None and tight is None
            and spent is None
            and ceil_skipped is None            # need=90 > remaining=89.6 → 不硬等
            and one_short == wg_budget.WAIT_BUDGET - 1   # mute=84 → need=89 塞得进，返回 need
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
    shut_up = int(now) + 20
    _reset(now, shut_up=shut_up)
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
            and (ledger.get("reason") or "").startswith("OneBot retcode=200")
            # 解禁时刻是绝对时刻：睡完再记账也不许往后漂（复核提的第二处）
            and ledger.get("mute_until") is not None
            and abs(int(ledger["mute_until"]) - shut_up) <= 2)


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
def 开窗失败不拖垮已发出的回复():
    """issue #227：开窗写盘失败不许让一次成功的发送表现为失败。

    现场是「窗口父目录建不出来」（磁盘/权限/占位文件的真实形态）：`reply.main()`
    在 `print("SENT")` 之后调 `open_followup()`，`followup.save()` 的 `STATE.mkdir` /
    `WINDOW.write_text` 抛 `OSError` 系，旧实现一路冒泡到 CLI，进程带 traceback 退出、
    退出码非 0——而群里那条已经发出去。Agent 按 `SENT`+traceback + 非零退出码读成
    「发送可能失败」，于是同一条消息再回一遍（重复回复）；`--wait` 续挂也被整段跳过。

    用 monkeypatch `followup.save` 抛 `OSError` 站在 issue 复现里「窗口父目录不可建」
    的位置（避开 Windows mkdir-on-file 的 OS 特异性，CI matrix 可移植）。断言输出里
    **有** `SENT`、**有** `[追问] 开窗失败`、**有** `消息已发送`，**没有** `Traceback`、
    **没有** `SEND_FAILED`、**没有** `窗口已开`（没开成就不许说开了），且 `main()` 正常
    返回 0——`SENT` 与退出码只由发送结果决定。

    对照组：门控关（`--no-followup`）时 `wanted=False`，连 `plugins.emit` 都不调，
    不出失败行——这条不能被「开窗失败」的 monkeypatch 误染（门控不开是合法的静默 None）。
    """
    _reset(time.time(), shut_up=0)
    real = followup.save
    followup.save = lambda _state: (_ for _ in ()).throw(
        OSError("窗口父目录建不出来（issue #227 复现形态）"))
    try:
        code, out = _run(["1", TEXT, "--at", "群友A", "--no-wait"])
    finally:
        followup.save = real
    ok = (code == 0 and "SENT" in out and "[追问] 开窗失败" in out
          and "消息已发送" in out and "Traceback" not in out
          and "SEND_FAILED" not in out and "窗口已开" not in out)
    if not ok:
        print(f"    退出码={code} 输出={out.strip()!r}")
    if not ok:
        return False
    # 对照组：门控关时不走 emit，不该有失败行
    _reset(time.time(), shut_up=0)
    code2, out2 = _run(["1", TEXT, "--at", "群友A", "--no-wait", "--no-followup"])
    return (code2 == 0 and "SENT" in out2 and "开窗失败" not in out2
            and "窗口已开" not in out2)


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


# ---------- issue #145：禁言账本必须复查，不许继续报「禁言至」 ----------


@case
def 提前解禁_禁言时刻在未来也必须复查():
    """**issue #145 的实机现场**（本文件里唯一直接对应该症状的用例）。

    issue 的证据链：账本 `mute_until` 在 **未来** 53 分钟（11:18:29），而此刻实测
    `shut_up_timestamp=0`——管理员提前解禁了。旧实现（`if rec.get("mute_until")`）
    照样打「禁言至 11:18:29」，Agent 据此放弃回待回的唤醒。

    所以复查的触发条件不能是「`mute_until` 已到点」：提前解禁时它必然还在未来。
    判据必须是「这份预测还新不新」（账本 `at` 距今超过 `RECHECK_AFTER`）。

    断言：① 不再出现「禁言至」；② 说「实测解禁」；③ `recovered` / `recovered_at`
    真的落盘（#132 预留的字段第一次有写者）；④ 原判时刻留在账本里可追溯；
    ⑤ `recovered_at` 必须是**这次复查的时刻**，不许拿 `mute_until`（预测）冒充，
    也不许落在未来——否则输出会变成「已于未来某刻实测解禁」，与 #145 要消灭的
    假事实同类（变异 N10/N16 实测逃逸后补的断言）。
    """
    now = time.time()
    until = int(now) + 3180                     # issue 现场：未来 53 分钟
    _reset(now, shut_up=0, all_shut=False)
    _ledger(until, at=int(now) - 7200)
    out = _list()
    rec = wg_send_failure.read() or {}
    recovered_at = rec.get("recovered_at")
    ok = ("禁言至" not in out
          and "实测解禁" in out
          and rec.get("recovered") is True
          and rec.get("recheck_result") == "released"
          and int(rec.get("mute_until") or 0) == until
          and recovered_at is not None
          # ⑤ 实测时刻 ≈ now，且绝不等于预测时刻、不在未来
          and abs(int(recovered_at) - int(now)) <= 5
          and int(recovered_at) != until
          and int(recovered_at) <= int(time.time()) + 1)
    if not ok:
        print(f"    输出={out.strip()!r} 账本 recovered={rec.get('recovered')!r} "
              f"recovered_at={recovered_at!r} until={until} "
              f"recheck_result={rec.get('recheck_result')!r} "
              f"请求数={FIX['member_queries']}")
    return ok


@case
def 补发未成功也要复查():
    """`recovered=False` 的语义是「那次补发没成功」，**不是**「禁言没解除」。

    把它当成「已有结论」跳过复查，issue 的一半场景仍然无解。变异实测：把
    `recovered is not True` 放宽成 `recovered is not None`，其余用例全绿——只有
    这条能抓。
    """
    now = time.time()
    until = int(now) + 600
    _reset(now, shut_up=0, all_shut=False)
    _ledger(until, at=int(now) - 3600, recovered=False)
    out = _list()
    rec = wg_send_failure.read() or {}
    return (rec.get("recovered") is True
            and rec.get("recheck_result") == "released"
            and rec.get("recovered_at") is not None
            and "实测解禁" in out)


@case
def 过期禁言_现查已解禁就说实测解禁():
    """issue「现象」段的复现命令场景：`mute_until` 已过期 60 秒。

    与上一条同一条修复路径，但它只覆盖「已到点」这一半；单靠它会让「只判已到点」
    的错误实现假绿（#145 的实机现场是未来时刻）。
    """
    now = time.time()
    until = int(now) - 60
    _reset(now, shut_up=0, all_shut=False)
    _ledger(until, at=int(now) - 7200)
    out = _list()
    rec = wg_send_failure.read() or {}
    recovered_at = rec.get("recovered_at")
    ok = ("禁言至" not in out
          and "实测解禁" in out
          and rec.get("recovered") is True
          and rec.get("recheck_result") == "released"
          # 原判时刻不许被抹掉：复查是「改写结论」，不是「假装没失败过」
          and int(rec.get("mute_until") or 0) == until
          and recovered_at is not None
          # 实测时刻 ≈ now：不许把「原判时刻」写成「实测解禁时刻」（变异 N10）
          and abs(int(recovered_at) - int(now)) <= 5
          and int(recovered_at) != until)
    if not ok:
        print(f"    输出={out.strip()!r} 账本 recovered={rec.get('recovered')!r} "
              f"recovered_at={recovered_at!r} recheck_result={rec.get('recheck_result')!r}")
    return ok


@case
def 账本刚写不复查():
    """对照组：账本写入不到 `RECHECK_AFTER` 秒时零请求。

    `diagnose()` 在失败那一刻已经查过 `shut_up_timestamp`，紧接着再问一遍只是白打
    接口。恒查的实现红在 `member_queries == 0`。
    """
    now = time.time()
    until = int(now) + 300
    _reset(now, shut_up=until, all_shut=False)
    _ledger(until, at=int(now) - 10)             # 刚写 10 秒
    out = _list()
    return (_has(out, f"禁言至 {wg_clock.clock(until)}")
            and FIX["member_queries"] == 0)


@case
def 复查间隔常量被钉住():
    """`RECHECK_AFTER` 本身要有用例守，且**不许读模块常量来自指**。

    `账本刚写不复查` 用 `at=now-10`、`提前解禁…` 用 `at=now-7200`，10s–7200s 之间
    是断言盲区——把 `RECHECK_AFTER` 从 60 改到 600 时其余用例全绿（变异实测）。
    这里用**字面值** 90 秒卡：`at = now - 90` 必须复查（若阈值被放大到 90 以上就会
    零请求而红），`at = now - 20` 必须零请求（若阈值被改小就会复查而红）。
    """
    now = time.time()
    until = int(now) + 300
    _reset(now, shut_up=0, all_shut=False)
    _ledger(until, at=int(now) - 90)             # 字面 90 秒：必须已过阈值
    _list()
    over = FIX["member_queries"]
    _reset(now, shut_up=0, all_shut=False)
    _ledger(until, at=int(now) - 20)             # 字面 20 秒：必须未过阈值
    _list()
    inside = FIX["member_queries"]
    if over != 1 or inside != 0:
        print(f"    RECHECK_AFTER={wg_send_failure.RECHECK_AFTER} "
              f"90 秒前={over} 请求（应为 1）、20 秒前={inside} 请求（应为 0）")
    return over == 1 and inside == 0


@case
def 未复查的禁言时刻标明来源():
    """账本刚写（还没复查）时，禁言尾巴必须说清这个时刻是**失败时探测**来的。

    对照组：把「禁言至 X」直接当成此刻结论的实现红在「据失败时探测」这句缺失上
    （#142 的纪律：推断要说成推断）。
    """
    now = time.time()
    until = int(now) + 300
    _reset(now, shut_up=until, all_shut=False)
    _ledger(until, at=int(now) - 10)
    out = _list()
    return (_has(out, f"禁言至 {wg_clock.clock(until)}", "据失败时探测")
            and any(f"还剩 {n} 秒" in out for n in (298, 299, 300)))


@case
def 复查仍禁言_以现查为准():
    """对照组：账本说未来某时刻，现查 `shut_up_timestamp` 是**另一个**未来时刻
    （管理员重新禁了一次）→ 不许说解禁，要换成现查到的时刻并标明复查。"""
    now = time.time()
    still = int(now) + 240
    _reset(now, shut_up=still, all_shut=False)
    _ledger(int(now) - 60, at=int(now) - 3600)
    out = _list()
    rec = wg_send_failure.read() or {}
    stamps = [wg_clock.clock(still + n) for n in range(-2, 3)]
    return (rec.get("recovered") is None
            and rec.get("recheck_result") == "muted"
            and any(f"禁言至 {stamp}" in out for stamp in stamps)
            and "复查仍禁言" in out
            and "实测解禁" not in out)


@case
def 全员禁言说未知解禁时刻():
    """全员禁言时 `shut_up_timestamp` 常常是 0：这时不许说解禁（#132 的假事实）。"""
    now = time.time()
    _reset(now, shut_up=0, all_shut=True)
    _ledger(int(now) - 60, at=int(now) - 3600)
    out = _list()
    rec = wg_send_failure.read() or {}
    return (rec.get("recovered") is None
            and "全员禁言，解禁时刻未知" in out
            and "实测解禁" not in out)


@case
def 探测失败说未能确认():
    """探测失败只能降级措辞（「复查未能确认」），不许当解禁、也不许继续断言禁言。"""
    now = time.time()
    _reset(now, member_fails=True)
    _ledger(int(now) - 60, at=int(now) - 3600)
    out = _list()
    rec = wg_send_failure.read() or {}
    return (rec.get("recovered") is None
            and "复查未能确认" in out
            and "实测解禁" not in out
            and "禁言至" not in out)


@case
def 探测失败不吃上一轮结论():
    """上一轮复查留下 `group_shut=True`，这一轮探测直接失败：不许拿旧值顶着**本次**
    复查的时刻说话（那是把两次不同的探测缝成一句假事实）。"""
    now = time.time()
    _reset(now, member_fails=True)
    _ledger(int(now) - 60, at=int(now) - 3600,
            recheck_result="muted", group_shut=True,
            rechecked_at=int(now) - 600)
    out = _list()
    rec = wg_send_failure.read() or {}
    return (rec.get("group_shut") is None
            and rec.get("recheck_result") == "unknown"
            and "全员禁言" not in out
            and "复查未能确认" in out)


@case
def 群信息查不到_复查不冒充解禁():
    """成员探测成功（`shut_up_timestamp=0`）但 `group_all_shut` 读不到：**不许**当解禁。

    `scope is None` 是「不知道」，不是「没全员禁言」。变异实测：把判定从
    `scope is False` 放宽成 `scope is not True`，其余 28 条全绿——只有这条能抓。
    """
    now = time.time()
    _reset(now, shut_up=0, group_fails=True)
    _ledger(int(now) - 60, at=int(now) - 3600)
    out = _list()
    rec = wg_send_failure.read() or {}
    return (rec.get("recovered") is None
            and rec.get("recheck_result") == "unknown"
            and "实测解禁" not in out
            and "复查未能确认" in out)


@case
def 复查限流_一分钟内只查一次():
    """同一分钟内被反复叫起（`--list` 连跑）不该每次都打接口。

    必须用**探测失败**场景：成功复查会写下 `recovered=True`，第二次就被「已有结论」
    那条前置挡住了——那样的用例测不到限流（变异实测：删掉限流代码它照样全绿）。
    失败复查只写 `rechecked_at`，`recovered` 仍是 `None`，第二次会不会再查完全取决
    于限流这一条。
    """
    now = time.time()
    _reset(now, member_fails=True)
    _ledger(int(now) - 60, at=int(now) - 3600)
    _list()
    first = FIX["member_queries"]
    _list()
    _list()
    return first == 1 and FIX["member_queries"] == 1


@case
def 复查前置零请求():
    """无账本 / 非禁言类失败 / 已有结论：三种前置都零请求（防「每次 --list 都查」）。"""
    now = time.time()
    _reset(now, shut_up=0, all_shut=False)
    wg_send_failure.clear()
    _list()
    none_ledger = FIX["member_queries"]
    # 参数错类失败（没有 mute_until）：复查没有对象
    _ledger(0, at=int(now) - 3600)
    _list()
    not_mute = FIX["member_queries"]
    _ledger(int(now) + 60, at=int(now) - 3600, recovered=True,
            recovered_at=int(now) - 30)                # 已有结论
    _list()
    settled = FIX["member_queries"]
    return none_ledger == 0 and not_mute == 0 and settled == 0


@case
def 补发成功后不再重复禁言尾巴():
    """`recovered=True` 但没有 `recovered_at`（#132 的补发路径）→ 尾巴只剩补发结论。"""
    now = time.time()
    _reset(now, shut_up=0)
    _ledger(int(now) + 60, at=int(now) - 600, recovered=True)
    out = _list()
    return ("补发成功" in out and "禁言至" not in out
            and "实测解禁" not in out)


# ---------- issue #207：OneBot 直发失败不许回退计划通道 ----------


def _append_wake(event_id, *, at_self=True):
    """向事件流追加一条本群唤醒（`@ 机器人`），供「还有 N 条没回」用例用。"""
    state = _SCRATCH / "state"
    segments = [{"type": "at", "qq": SELF}] if at_self else []
    row = {"id": event_id, "conversation_id": f"1:group:{GROUP}",
           "onebot_message_id": f"msg-{event_id}", "sender": "群友A",
           "content": "在吗", "segments": segments,
           "timestamp": int(time.time() * 1000)}
    path = state / "wg_events.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


@case
def 直发失败带引用也不回退():
    """issue #207 的核心现场：带引用（事件有 onebot_message_id）、不带 `--at`、
    不带图，首条 OneBot 发送失败。

    改前 `reply.py:931` 的 `if sent or has_at: return 1` 在 `sent=0` 且 `has_at=False`
    时穿过去，落到 `:937` 的 `falling back to the plan path`——把带 `reply` 段的
    消息交给计划通道，而 `send()` 的气泡写死 `kind=text`，引用段整个丢掉。断言：
    不回退、失败真实原因在、未记账、计划通道零输出、报账行照旧（不删）。
    """
    now = time.time()
    _reset(now, shut_up=0)
    FIX["replies"].append({"status": "failed", "retcode": 1400,
                           "message": PARAM_MESSAGE, "wording": "参数错误",
                           "data": None})
    code, out = _run(["1", TEXT, "--no-wait"])
    replied = _SCRATCH / "state" / "wg_replied.jsonl"
    ok = (code == 1
          and "falling back to the plan path" not in out
          and "没有可用批次" not in out
          and "PLAN_FAILED" not in out
          and "no OneBot id on record" not in out
          and "SEND_FAILED: OneBot retcode=1400" in out
          and "未退回计划通道" in out
          and "NOTE 还有" not in out
          and "cost:" in out                     # 报账行照旧（#88/#136，不因失败删）
          and not replied.exists())              # 未记账
    if not ok:
        print(f"    输出={out.strip()!r} 记账文件存在={replied.exists()}")
    return ok


@case
def 直发成功后才报还有N条():
    """对照组：`NOTE 还有 N 条没回` 是「这条答完了、还有 N 条」，必须排在 `SENT` 之后。

    只断言「NOTE 出现」的实现在失败路径也会把它打出来（issue #207 的症状），所以
    这里同时断言：发送成功才说、且说在 `SENT` 之后。
    """
    now = time.time()
    _reset(now, shut_up=0)
    _append_wake(2)                              # 第二条 @ 本机的唤醒，仍待回
    code, out = _run(["1", TEXT, "--no-wait"])
    return (code == 0
            and "SENT" in out
            and "NOTE 还有 1 条没回：2" in out
            and out.index("SENT") < out.index("NOTE 还有 1 条没回"))


@case
def 直发失败不带引用也不回退():
    """`--no-quote` + 事件无引用：带 `--at` 的既有语义不许被回退破坏（#132 的
    对照组）。断言「不回退 + 失败原因」与 `直发失败带引用也不回退` 同构，覆盖
    `has_at=True` 那一路（`handle()` 里那句「未退回计划通道」）。
    """
    now = time.time()
    _reset(now, shut_up=0)
    FIX["replies"].append({"status": "failed", "retcode": 1400,
                           "message": PARAM_MESSAGE, "wording": "参数错误",
                           "data": None})
    code, out = _run(["1", TEXT, "--no-quote", "--at", "群友A", "--no-wait"])
    return (code == 1
            and "falling back to the plan path" not in out
            and "没有可用批次" not in out
            and "SEND_FAILED: OneBot retcode=1400" in out
            and "未退回计划通道：带 @ 的消息只能走 OneBot" in out)


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
