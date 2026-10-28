"""钉住采集暂停标记：有主、有时限、失效能自愈、状态行说得出来（issue #189）。

2026-10-28 的现场：交接时有人手工塞了一个 `state/wg_pause`，内容只有
`placeholder`。`collector` 的老实现只看「文件在不在」（`collector.py:135`），
于是每 2 秒空转、**不写心跳**；`supervisor` 按 `STALE_AFTER=300` 判死重启，
重启后第一轮又看见同一个文件——进程在、端口在、监听也挂着，事件流水却永久停住。
而全仓没有任何写入方，`up.py --status` 一个字都不提这件事。

这条门禁钉下面这些契约，任一处退回旧行为都红：

1. **没有标记就是没有暂停**：`probe()` 三态之 `none`、`active()==False`、
   `describe()` 空串、`clear_stale()` 空串。排除型对照：恒说「暂停中」的实现红在这里。
2. **有效暂停受保护**：规范记录 + owner 进程活着 + 未到时限 → `active`，
   `clear_stale()` 不删，`describe()` 带原因与「后自动恢复」。
3. **owner 死了立刻失效**：pid 指向一个已经退出的进程 → `stale`（不等 TTL），
   `clear_stale()` 真删掉并给出说明。写它的进程没了，它保护的发送不可能还在进行。
4. **时限是硬上限**：`at=now-299` → `active`，`at=now-300` → `stale`；
   `until` 只能提前收工，写成 `now+10000` 也在 `at+TTL` 处失效；`at` 写成未来时刻
   按现在算，不会换来无限期暂停。
5. **非规范内容按遗留处理**：`placeholder`、坏 JSON、JSON 但缺 `reason`、
   1 MiB 大文件——都不抛异常，宽限内 `active`、超宽限 `stale`，且只读前 4 KiB。
6. **状态行照实报**：有标记时出现 `采集` 行，原因/失效依据取自正则捕获组（不是子串）；
   没有标记时**不出现**（排除型对照）。采集器不在时明说「没人会去清它」。
7. **暂停期间照写心跳**：同一夹具跑 4 轮，`wait_chat_messages` 0 次、
   心跳写入次数 = 启动 2 次 + 4 轮。对照组：把 `gate` 换回「存在即停」的旧实现，
   「心跳推进」这条判据必须翻假——否则它绿的只是「两处用了同一份实现」。
8. **失效标记当轮自愈**：遗留标记超宽限后，同一进程下一轮就恢复采集
   （`wait_chat_messages` 被调用、`wg_events.jsonl` 出现新行），不需要重启。
   反例对照：换回旧 `gate` 后同一夹具 0 次调用、0 行事件。
9. **热重载不被永久挡住**：`reload_exec.Reloader._busy()` 对有效暂停 `True`、
   对遗留标记 `False`（旧实现是 `exists()`，一个哑文件能挡到天荒地老）。
10. **各群互不影响**：两个 state 目录，A 的暂停不影响 B 的 `active()`。
11. **写入方与读取方同源**：`acquire()` 写出的文件被 `probe()` 认成 `active`、
    `release()` 之后回到 `none`；空原因拒绝写入。
12. **宽限与预算挂钩**：`TTL > SEND_BUDGET`（否则正常发送期间暂停就先过期了）、
    `LEGACY_GRACE >= 2 × SEND_BUDGET`。

全程临时 `WATER_GROUP_HOME` / `IDLEJOT_WG_STATE`，不碰实机状态：

    .venv/Scripts/python.exe scripts/check_wg_pause.py
"""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 数据根在 import 前隔离：collector / up / reload_exec 都在模块级求 state_dir()。
import check_fixture_support as fs  # noqa: E402  (issue #169 PE: path 注入收口)
from check_fixture_support import scratch_dir  # noqa: E402

HOME = scratch_dir(prefix="tulpa-wg-pause-gate-")
STATE = HOME / "state"
STATE.mkdir(parents=True, exist_ok=True)
os.environ["WATER_GROUP_HOME"] = str(HOME)
os.environ["IDLEJOT_WG_STATE"] = str(STATE)

fs.bootstrap_paths(scripts=False, root=False)

import collector  # noqa: E402
import reload_exec  # noqa: E402
import up  # noqa: E402
import wg_budget  # noqa: E402
import wg_pause  # noqa: E402

PAUSE_LINE = re.compile(r"^采集\s*:\s*(?P<body>.*)$", re.M)
REASON_FIELD = re.compile(r"原因：(?P<reason>[^；]+)")
PROBES = ("listener_specs", "listener_pids", "supervisor_pids", "commands_pids",
          "port_open", "config_problems", "collector_pids", "heartbeat_age",
          "heartbeat_time")


class Stop(BaseException):  # noqa: N818  循环里的裸 except 会吞 Exception
    """停止泵的哨兵：必须继承 BaseException，否则被 collector 的兜底 except 吃掉。"""


def fresh_dir(name):
    """门禁用的一次性 state 目录（不碰实机，也不复用彼此）。"""
    target = HOME / name
    target.mkdir(parents=True, exist_ok=True)
    for leftover in target.glob(wg_pause.NAME):
        leftover.unlink()
    return target


def write_record(root, *, reason="发送中", pid=None, at=None, until=None, raw=None):
    """落一份标记。`raw` 给了就原样写（用于构造非规范内容）。"""
    target = wg_pause.path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        target.write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
        return target
    record = {"reason": reason}
    if pid is not None:
        record["pid"] = pid
    if at is not None:
        record["at"] = at
    if until is not None:
        record["until"] = until
    target.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return target


def dead_pid():
    """一个确定已经退出的 pid（本轮跑完就退的子进程）。"""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid


# ---------- 契约 1：没有标记就是没有暂停 ----------


def test_no_marker():
    root = fresh_dir("no-marker")
    info = wg_pause.probe(root)
    assert info.state == "none", f"没有标记时不该有状态：{info}"
    assert not wg_pause.active(root), "没有标记时 active() 必须是假"
    assert wg_pause.describe(info) == "", wg_pause.describe(info)
    assert wg_pause.clear_stale(root) == "", "没有标记时 clear_stale 不该有话说"
    print("✓ 1. 没有标记 → none / 不暂停 / 状态行不加字")


# ---------- 契约 2：有效暂停受保护 ----------


def test_active_pause_is_protected():
    root = fresh_dir("active")
    now = time.time()
    write_record(root, reason="发送中", pid=os.getpid(), at=now - 5, until=now + 60)
    info = wg_pause.probe(root, now)
    assert info.state == "active", f"活着的 owner + 未到时限应为 active：{info}"
    assert info.reason == "发送中", info.reason
    text = wg_pause.describe(info)
    assert "暂停中" in text and "原因：发送中" in text, text
    assert "后自动恢复" in text, text
    assert wg_pause.clear_stale(root, now) == "", "有效暂停被清掉了"
    assert wg_pause.path(root).exists(), "有效暂停的文件被删了"
    print("✓ 2. 有效暂停（owner 活着 + 未到时限）受保护")


# ---------- 契约 3：owner 死了立刻失效 ----------


def test_dead_owner_is_stale():
    root = fresh_dir("dead-owner")
    now = time.time()
    pid = dead_pid()
    assert not wg_pause.pid_alive(pid), f"夹具前提不成立：pid {pid} 还活着"
    write_record(root, reason="发送中", pid=pid, at=now - 1, until=now + 60)
    info = wg_pause.probe(root, now)
    assert info.state == "stale", f"owner 已退出却没判 stale：{info}"
    assert f"pid {pid} 已退出" in info.why, info.why
    note = wg_pause.clear_stale(root, now)
    assert "清掉失效的暂停标记" in note, note
    assert not wg_pause.path(root).exists(), "失效标记没被删掉"
    assert wg_pause.probe(root, now).state == "none", "清完还是 stale"
    print("✓ 3. owner 已退出 → 立刻 stale，clear_stale 真删")


# ---------- 契约 4：时限是硬上限 ----------


def test_deadline_boundaries():
    root = fresh_dir("deadline")
    now = time.time()
    write_record(root, at=now - 299)
    assert wg_pause.probe(root, now).state == "active", "at=now-299 应该还在时限内"
    write_record(root, at=now - 300)
    assert wg_pause.probe(root, now).state == "stale", "at=now-300 应该判失效"

    # `until` 只能提前收工
    write_record(root, at=now, until=now + 10)
    assert wg_pause.probe(root, now + 5).state == "active"
    assert wg_pause.probe(root, now + 11).state == "stale", "until 到点没失效"

    # `until` 写得再远也不能突破 TTL
    write_record(root, at=now, until=now + 10000)
    assert wg_pause.probe(root, now + 1).state == "active"
    assert wg_pause.probe(root, now + wg_pause.TTL + 1).state == "stale", (
        "until 越过了 TTL：任何标记都必须有硬上限")

    # `at` 写成未来时刻按现在算（坏时间戳不换来无限期暂停）
    write_record(root, at=now + 10 ** 6)
    assert wg_pause.probe(root, now + 1).state == "active"
    assert wg_pause.probe(root, now + wg_pause.TTL + 2).state == "stale", (
        "未来的 at 把暂停拖成了无限期")
    print("✓ 4. TTL 边界、until 收工、until/at 都不能突破上限")


# ---------- 契约 5：非规范内容按遗留处理 ----------


def test_legacy_content():
    root = fresh_dir("legacy")
    for raw, label in ((b"placeholder", "纯文本"),
                       (b"{not json", "坏 JSON"),
                       (b'["a"]', "JSON 但不是对象"),
                       (b'{"reason": ""}', "空 reason"),
                       (b'{"reason": 5}', "reason 不是字符串")):
        target = write_record(root, raw=raw)
        stamp = target.stat().st_mtime
        fresh = wg_pause.probe(root, stamp + 1)
        assert fresh.state == "active", f"{label}：宽限内应判 active，实际 {fresh}"
        late = wg_pause.probe(root, stamp + wg_pause.LEGACY_GRACE + 1)
        assert late.state == "stale", f"{label}：超宽限应判 stale，实际 {late}"
        assert not wg_pause.active(root, stamp + wg_pause.LEGACY_GRACE + 1), label

    # 宽限恰好落在边界上：< LEGACY_GRACE 算有效，>= 算失效（时间从文件自己的 mtime 起算）
    target = write_record(root, raw=b"placeholder")
    stamp = target.stat().st_mtime
    assert wg_pause.probe(root, stamp + wg_pause.LEGACY_GRACE - 1).state == "active", (
        "宽限内 1 秒就判失效了")
    assert wg_pause.probe(root, stamp + wg_pause.LEGACY_GRACE).state == "stale", (
        "宽限到点还判有效")

    # 1 MiB 的哑文件：只读前 4 KiB，不崩、不把整个文件塞进状态行
    target = write_record(root, raw=b"x" * (1024 * 1024))
    info = wg_pause.probe(root, target.stat().st_mtime + 1)
    assert info.state == "active", info
    assert len(info.reason) <= wg_pause.REASON_CHARS + 1, (
        f"原因字段没截断：{len(info.reason)} 字")
    print("✓ 5. 遗留内容（纯文本/坏 JSON/缺 reason/1 MiB）都不崩且按期失效")


# ---------- 契约 6：状态行照实报 ----------


def status_text(collectors=("4321",)):
    """跑一次 `up.report()`（只桩机器层探针），返回整段输出。"""
    saved = {name: getattr(up, name) for name in PROBES}
    saved_info = up.login_info
    up.listener_specs = lambda *a, **k: []
    up.listener_pids = lambda *a, **k: []
    up.supervisor_pids = lambda *a, **k: ["1111"]
    up.commands_pids = lambda *a, **k: []
    up.port_open = lambda *a, **k: True
    up.config_problems = lambda *a, **k: []
    up.collector_pids = lambda *a, **k: list(collectors)
    up.heartbeat_age = lambda *a, **k: None
    up.heartbeat_time = lambda *a, **k: None
    up.login_info = lambda *a, **k: None
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            up.report()
    finally:
        for name, value in saved.items():
            setattr(up, name, value)
        up.login_info = saved_info
    return buf.getvalue()


def pause_body(text):
    """`采集` 行的正文；没有这一行时返回 None（排除型对照靠它判）。"""
    match = PAUSE_LINE.search(text)
    return match.group("body") if match else None


def test_status_line():
    now = time.time()
    body = pause_body(status_text())
    assert body is None, f"没有暂停标记时不该有采集行：{body!r}"

    # 状态行读的是模块级 state 根（`up.report()` 用默认 state 目录），所以夹具写在
    # STATE 上；跑完自己收拾，别让后面的泵测试看见上一组的标记。
    write_record(STATE, reason="发送中", pid=os.getpid(), at=now - 12,
                 until=now + 288)
    try:
        body = pause_body(status_text())
        assert body is not None, "有效暂停没有出现在状态行里"
        assert "暂停中" in body, body
        assert REASON_FIELD.search(body).group("reason") == "发送中", body
        assert "后自动恢复" in body, body
        assert "这一轮不采集" in body, body
        # 采集器不在时不能说「在跑」：暂停没有任何人执行，得说清它只是个躺着的文件
        body = pause_body(status_text(collectors=()))
        assert body is not None and "没有任何人执行" in body, body

        # 失效标记：明说是谁去清；采集器不在时不许假装会有人清
        write_record(STATE, raw=b"placeholder")
        old = now - wg_pause.LEGACY_GRACE - 5
        os.utime(wg_pause.path(STATE), (old, old))
        body = pause_body(status_text())
        assert body is not None and "标记已失效" in body, body
        assert "下一轮会删掉它并恢复采集" in body, body
        body = pause_body(status_text(collectors=()))
        assert body is not None and "没人会去清它" in body, body
    finally:
        wg_pause.release(STATE)
    print("✓ 6. 状态行：有标记才出现、原因/失效依据/清理人各就各位")


# ---------- 契约 7 / 8：采集器的心跳与自愈 ----------


def pump(rounds=4, batch=None):
    """跑 `collector.main()` 的循环 `rounds` 轮后退出，返回 (wait, beat, sleep) 计数。

    只桩三样：MCP 客户端、会话建立、缺口计算（它会联网）。其余是真 loop；
    collector 的路径在 import 期就钉在 IDLEJOT_WG_STATE 上，所以夹具只写在 STATE。
    """
    calls = {"wait": 0, "beat": 0, "sleep": 0}
    saved = (collector.mcp_client, collector.bootstrap_session, collector.beat,
             collector.time.sleep, collector.wg_gap.compute_and_record)
    real_beat = collector.beat

    class FakeMCP:
        @staticmethod
        def call_tool_raw(*a, **k):
            calls["wait"] += 1
            messages = list(batch or []) if calls["wait"] == 1 else []
            return {"messages": messages, "read_through_id": 100 + calls["wait"]}, []

    def fake_beat():
        calls["beat"] += 1
        real_beat()

    def fake_sleep(seconds):
        calls["sleep"] += 1
        if calls["sleep"] >= rounds:
            raise Stop()

    collector.mcp_client = FakeMCP
    collector.bootstrap_session = lambda: "gate-session"
    collector.wg_gap.compute_and_record = lambda **k: {}
    collector.beat = fake_beat
    collector.time.sleep = fake_sleep
    try:
        try:
            collector.main()
        except Stop:
            pass
    finally:
        (collector.mcp_client, collector.bootstrap_session, collector.beat,
         collector.time.sleep, collector.wg_gap.compute_and_record) = saved
    return calls["wait"], calls["beat"], calls["sleep"]


def test_pause_keeps_heartbeat():
    for leftover in STATE.glob("*"):
        if leftover.is_file():
            leftover.unlink()
    now = time.time()
    write_record(STATE, reason="发送中", pid=os.getpid(), at=now, until=now + 120)
    waits, beats, sleeps = pump(rounds=4)
    assert waits == 0, f"暂停期间还在采集（wait 调用 {waits} 次）"
    assert sleeps == 4, sleeps
    assert beats == 2 + 4, (
        f"暂停期间没写心跳：写入 {beats} 次，应为启动 2 次 + 4 轮（issue #189 的现场是 2 次）")
    print("✓ 7. 暂停期间 0 次采集、每轮照写心跳")


def resume_scenario():
    """遗留标记超宽限 → 同一进程下一轮恢复采集。返回 (wait 次数, 落盘事件行数)。"""
    for leftover in STATE.glob("*"):
        if leftover.is_file():
            leftover.unlink()
    write_record(STATE, raw=b"placeholder")
    old = time.time() - wg_pause.LEGACY_GRACE - 30
    os.utime(wg_pause.path(STATE), (old, old))
    waits, beats, sleeps = pump(rounds=3, batch=[{"id": 9001, "is_self": False,
                                                 "content": "@bot 在吗"}])
    rows = 0
    if collector.EVENTS.exists():
        rows = len([ln for ln in collector.EVENTS.read_text(encoding="utf-8").splitlines()
                    if ln.strip()])
    return waits, rows, beats


def test_stale_marker_self_heals():
    waits, rows, beats = resume_scenario()
    assert waits >= 1, "遗留标记挡住了采集：wait_chat_messages 一次都没调用"
    assert rows == 1, f"恢复采集后事件没有落盘：{rows} 行"
    assert not wg_pause.path(STATE).exists(), "遗留标记没被清掉"
    print("✓ 8. 遗留标记超宽限 → 当轮清理并恢复采集（不用重启）")


def test_old_gate_fails_these_checks():
    """反例对照：换回「存在即停」的旧 gate，第 8 组的判据必须翻假。

    只替 gate 不替 beat：新 collector 的暂停分支照写心跳（那是第 7 组的契约），
    旧实现的「也不写心跳」由 PR 的基线验证复现输出承担，不在这里假装。
    """
    saved = wg_pause.gate
    wg_pause.gate = lambda root=None, now=None: (wg_pause.path(STATE).exists(), "")
    try:
        waits, rows, _ = resume_scenario()
    finally:
        wg_pause.gate = saved
        wg_pause.release(STATE)
    assert waits == 0, f"旧实现下竟然采到了 {waits} 次——判据抓不住「存在即停」"
    assert rows == 0, f"旧实现下落盘了 {rows} 行事件"
    print("✓ 8b. 反例对照：旧「存在即停」实现下同一判据翻红")


# ---------- 契约 9：热重载不被永久挡住 ----------


def test_reload_busy():
    plan_root = fresh_dir("reload-plan")
    reloader = reload_exec.Reloader(lambda *a, **k: None, verify=False, root=plan_root)
    now = time.time()
    write_record(STATE, reason="发送中", pid=os.getpid(), at=now, until=now + 60)
    try:
        assert reloader._busy() is True, "有效暂停期间不该重载"
        write_record(STATE, raw=b"placeholder")
        old = now - wg_pause.LEGACY_GRACE - 5
        os.utime(wg_pause.path(STATE), (old, old))
        assert reloader._busy() is False, "遗留标记把热重载永久挡住了"
    finally:
        wg_pause.release(STATE)
    print("✓ 9. _busy()：有效暂停 True、遗留标记 False")


# ---------- 契约 10 / 11 / 12：多群、写入方、常量关系 ----------


def test_groups_isolated():
    left = fresh_dir("group-a")
    right = fresh_dir("group-b")
    now = time.time()
    write_record(left, reason="A 群发送中", pid=os.getpid(), at=now, until=now + 60)
    assert wg_pause.active(left, now), "A 群的暂停没生效"
    assert not wg_pause.active(right, now), "A 群的暂停传染到了 B 群"
    assert wg_pause.probe(right, now).state == "none", "B 群看见了 A 群的标记"
    print("✓ 10. 两个 state 目录互不影响")


def test_acquire_release():
    root = fresh_dir("acquire")
    target = wg_pause.acquire("测试发送", root=root)
    assert target == wg_pause.path(root), target
    info = wg_pause.probe(root)
    assert info.state == "active", f"acquire() 写的东西自己读不懂：{info}"
    assert info.reason == "测试发送", info.reason
    assert info.pid == os.getpid(), info.pid
    assert wg_pause.release(root) is True, "release() 没删掉"
    assert wg_pause.probe(root).state == "none", "release() 之后还有状态"
    assert wg_pause.release(root) is False, "没有标记时 release() 应返回 False"
    try:
        wg_pause.acquire("   ", root=root)
    except ValueError:
        pass
    else:
        raise AssertionError("空原因被接受了：写出来的标记没人看得懂")
    assert not wg_pause.path(root).exists(), "空原因失败后留下了文件"
    print("✓ 11. acquire → active、release → none、空原因拒绝")


def test_budget_relation():
    assert wg_pause.TTL > wg_budget.SEND_BUDGET, (
        f"TTL={wg_pause.TTL} 不大于发送预算 {wg_budget.SEND_BUDGET}："
        f"正常发送期间暂停就先过期了")
    assert wg_pause.LEGACY_GRACE >= 2 * wg_budget.SEND_BUDGET, (
        f"遗留宽限 {wg_pause.LEGACY_GRACE} 短于两份发送预算：正在发的任务会被误清")
    print("✓ 12. TTL / LEGACY_GRACE 与发送预算的关系成立")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 72)
    print("        [测试] issue #189 采集暂停标记：有主、有时限、失效自愈")
    print("=" * 72)
    checks = (test_no_marker, test_active_pause_is_protected,
              test_dead_owner_is_stale, test_deadline_boundaries,
              test_legacy_content, test_status_line,
              test_pause_keeps_heartbeat, test_stale_marker_self_heals,
              test_old_gate_fails_these_checks, test_reload_busy,
              test_groups_isolated, test_acquire_release, test_budget_relation)
    bad = 0
    for fn in checks:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            bad += 1
            print(f"✗ {fn.__name__}: {type(exc).__name__}: {exc}")
    print("=" * 72)
    print("✓ 全部 13 组契约测试通过！" if not bad else f"{bad} 组失败")
    print("=" * 72)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
