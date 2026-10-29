"""Hot-reload gate: closure coverage, planning, debounce, quarantine, no spawn.

Run with the repo venv:

    .venv/Scripts/python.exe scripts/check_hot_reload.py

Everything uses a throwaway WATER_GROUP_HOME; no real port or child is touched.
The fake child is a small Popen-shaped object so ordering assertions (db_keeper
new-before-old, watchdog deferred) do not need a real process.
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from check_fixture_support import scratch_dir  # noqa: E402
TMP = scratch_dir(prefix="wg_hot_reload_")
os.environ["WATER_GROUP_HOME"] = str(TMP)
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

import reload  # noqa: E402
import reload_exec  # noqa: E402  (issue #11 PD: 执行层；补丁打到这里才到真实调用点)
import reload_plan  # noqa: E402  (issue #11 PD: 分析层；补丁打到这里才到真实调用点)
import wg_budget  # noqa: E402  (issue #230：reload.py --wait 默认走预算口径)
import wg_daemon  # noqa: E402  (issue #11 PB: JSON 助手搬到 wg_daemon)


class FakeProc:
    def __init__(self, name="fake", events=None, alive=True):
        self.name = name
        self.events = events if events is not None else []
        self.alive = alive
        self.pid = 1000 + len(self.events)

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.events.append(f"terminate:{self.name}")
        self.alive = False

    def kill(self):
        self.events.append(f"kill:{self.name}")
        self.alive = False

    def wait(self, timeout=None):
        self.alive = False
        return 0


def check_coverage():
    planner = reload.Planner()
    files = planner.scan()
    py = [rel for rel in files if rel.endswith((".py", ".pyw"))]
    assert len(py) >= 100, f"scan too small: {len(py)}"
    assert "water-group/daemon/reload.py" in py
    assert "water-group/daemon/commands.py" in py
    for rel in py:
        assert not any(part in reload.EXCLUDE_DIRS for part in Path(rel).parts), rel
        targets, why, _ = planner.targets_for(rel)
        if targets is None and why != "guard-owned":
            raise AssertionError(f"unclassified file: {rel} ({why})")
    print(f"  ok  闭包覆盖：{len(py)} 个 Python 文件全部有归属")


def check_mapping():
    planner = reload.Planner()
    assert planner.plan(["water-group/daemon/commands.py"]).targets == ["commands"]
    assert planner.plan(["chatlocal/config.py"]).targets == ["app"]
    assert planner.plan(["water-group/daemon/wg_paths.py"]).targets == list(reload.TARGETS)
    rejected = planner.plan(["water-group/daemon/supervisor.py"]).rejected
    assert rejected and "guard-owned" in rejected[0]["why"], rejected
    assert planner.plan(["water-group/daemon/reload.py"]).rejected
    skipped = planner.plan(["water-group/daemon/reply.py"])
    assert skipped.targets == [] and skipped.skipped, skipped
    dep = planner.plan(["pyproject.toml"])
    assert dep.rejected and "uv sync" in dep.rejected[0]["why"], dep
    cfg = planner.plan(["water-group/config.json"])
    assert cfg.targets == [] and cfg.skipped, cfg
    print("  ok  映射：commands/app/wg_paths/guard-owned/CLI/deps/config")


def check_syntax():
    bad = TMP / "bad_syntax.py"
    bad.write_text("def broken(:\n    pass\n", encoding="utf-8")
    error = reload._compile_with_retry(bad, attempts=1)
    assert error and "SyntaxError line" in error, error

    original = reload_plan._compile_with_retry
    reload_plan._compile_with_retry = lambda path, attempts=3: "SyntaxError line 1: bad"
    try:
        plan = reload.Planner().plan(["water-group/daemon/commands.py"])
        assert plan.rejected and plan.targets == [], plan
    finally:
        reload_plan._compile_with_retry = original
    print("  ok  语法错整批拒绝，targets 清空")


def check_debounce():
    calls = []
    reloader = reload.Reloader(
        spawn=lambda name: None,
        config={"watch": True, "debounce": 1.5, "cooldown": 0},
        verify=False,
    )
    changed = {"files": ["water-group/daemon/commands.py"]}
    reloader.planner.diff = lambda old=None: list(changed["files"])
    reloader.planner.scan = lambda: dict(reloader.snapshot)

    def fake_execute(nonce, mode, plan, procs, spawn, started):
        calls.append(plan.targets)
        return {"elapsed": 0.01, "changed": plan.changed, "restarted": plan.targets}

    reloader._execute = fake_execute
    reloader._tick_watch({}, lambda name: None)
    assert reloader.pending, "first change should be pending"
    reloader._tick_watch({}, lambda name: None)
    assert not calls, "debounce window must not execute"
    changed["files"] = []
    reloader.pending_since -= 2.0
    reloader._tick_watch({}, lambda name: None)
    assert len(calls) == 1, calls
    print("  ok  debounce：窗口内多次变化合并成一次")


def check_single_flight():
    reloader = reload.Reloader(spawn=lambda name: None, verify=False)
    first = reload._request_path()
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text(json.dumps({"nonce": "one"}), encoding="utf-8")
    second = first.with_name("wg_reload.request.two")
    second.write_text(json.dumps({"nonce": "two"}), encoding="utf-8")
    claimed = reloader._claim_requests()
    assert len(claimed) == 1, claimed
    assert reloader._claim_requests() == [], "inflight must block a second claim"
    reloader._finish_inflight()
    claimed = reloader._claim_requests()
    assert len(claimed) == 1 and claimed[0].name.endswith("two"), claimed
    reloader._finish_inflight()
    print("  ok  单飞：第二个 nonce 排队，不覆盖 inflight")


def check_watchdog_deferred():
    old = FakeProc("watchdog")
    spawned = []
    reloader = reload.Reloader(
        spawn=lambda name: spawned.append(name),
        healthy=lambda: False,
        verify=False,
    )
    status, why = reloader._restart_watchdog({"watchdog": old}, lambda name: None)
    assert status == "deferred" and "not healthy" in why, (status, why)
    assert not old.events and not spawned, (old.events, spawned)
    print("  ok  watchdog：NapCat 不健康时 deferred，不 terminate")


def check_deferred_reports_failure():
    """issue #229：推迟不是成功——code/ok/输出/state 四层都要说「未生效」。

    老用例只测了 ``_restart_watchdog`` 的返回值，``_execute`` 里那个
    ``code = 0 if not failed and not deferred else ...`` 从来没被跑过，
    于是 deferred 一直拿到 code=0、ok=True，reload.ps1 退出 0。
    """
    reloader = reload.Reloader(
        spawn=lambda name: None, healthy=lambda: False, verify=False)
    plan = reload_plan.RestartPlan(
        changed=["water-group/daemon/watchdog.py"], targets=["watchdog"])
    result = reloader._execute(
        "deferred-nonce", "reload", plan, {}, lambda name: None, time.monotonic())
    assert result["deferred"] == ["watchdog"], result
    assert result["restarted"] == [], result
    assert result["ok"] is False, result
    assert result["code"] == 5, result

    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        reload._print_result(result)
    out = printed.getvalue()
    assert "未生效（推迟）: watchdog" in out, out
    assert "not healthy" in out, out

    reloader._write_state(last_result=result)
    state = wg_daemon.read_json(reload._state(reload.STATE_NAME)) or {}
    assert state.get("last_deferred") == ["watchdog"], state
    assert state.get("last_deferred_at"), state
    assert "推迟未生效：watchdog" in reload.status_line(), reload.status_line()

    # 欠账不是「上一次结果快照」：别的 target 干净成功、或只切 watch 开关，
    # 都不能把还欠着的 watchdog 抹掉——否则 --status 又回到「重启 -」，而
    # 跑的还是旧代码（复核意见 1）。
    other_ok = reloader._result(
        "other-nonce", "reload", time.monotonic(), ["commands"], [], [], [], [])
    assert other_ok["code"] == 0 and other_ok["ok"] is True, other_ok
    reloader._write_state(last_result=other_ok)
    state = wg_daemon.read_json(reload._state(reload.STATE_NAME)) or {}
    assert state.get("last_deferred") == ["watchdog"], state

    watch_on = reloader._result(
        "watch-nonce", "watch-on", time.monotonic(), [], [], [], [], [])
    reloader._write_state(last_result=watch_on)
    state = wg_daemon.read_json(reload._state(reload.STATE_NAME)) or {}
    assert state.get("last_deferred") == ["watchdog"], state
    assert "推迟未生效：watchdog" in reload.status_line(), reload.status_line()

    # 该 target 自己后来正常重载成功，欠账要消——这条钉住 `_note_deferred` 里的
    # `difference_update(restarted)`，否则欠账再也清不掉，--status 永远假报警
    # （第二轮复核的 M1 变异：删掉那行原本仍全绿）。
    recovered = reloader._result(
        "recover-nonce", "reload", time.monotonic(), ["watchdog"], [], [], [], [])
    reloader._write_state(last_result=recovered)
    state = wg_daemon.read_json(reload._state(reload.STATE_NAME)) or {}
    assert not state.get("last_deferred"), state
    assert "推迟未生效" not in reload.status_line(), reload.status_line()

    # 补做失败：代码还是旧的，账不能清（复核意见 2）。
    failing = reload.Reloader(spawn=lambda name: None, healthy=lambda: True, verify=False)
    failing._write_state(last_result=result)
    failing.deferred.add("watchdog")
    failing._retry_deferred({"watchdog": FakeProc("old")}, lambda name: None)
    assert "watchdog" not in failing.deferred, failing.deferred
    state = wg_daemon.read_json(reload._state(reload.STATE_NAME)) or {}
    assert state.get("last_deferred") == ["watchdog"], state

    # 补做成功后要把「还欠一次」的记录撤掉，否则 --status 永远说上次推迟；
    # 同时把这次重启记进 last_restarted，免得账清了却读成「什么都没重启」。
    flip = {"healthy": False}
    retry = reload.Reloader(
        spawn=lambda name: FakeProc(name), healthy=lambda: flip["healthy"], verify=False)
    retry._write_state(last_result=result)
    retry.deferred.add("watchdog")
    flip["healthy"] = True
    retry._retry_deferred({"watchdog": FakeProc("old")}, lambda name: FakeProc(name))
    assert "watchdog" not in retry.deferred, retry.deferred
    state = wg_daemon.read_json(reload._state(reload.STATE_NAME)) or {}
    assert not state.get("last_deferred"), state
    assert "watchdog" in (state.get("last_restarted") or []), state
    assert "推迟未生效" not in reload.status_line(), reload.status_line()

    # 对照组：全部成功仍然必须是 code=0 / ok=True，成功路径没被改坏。
    ok_reloader = reload.Reloader(
        spawn=lambda name: FakeProc(name), verify=False)
    ok_plan = reload_plan.RestartPlan(
        changed=["water-group/daemon/watchdog.py"], targets=["watchdog"])
    ok_result = ok_reloader._execute(
        "ok-nonce", "reload", ok_plan, {"watchdog": FakeProc("old")},
        lambda name: FakeProc(name), time.monotonic())
    assert ok_result["ok"] is True and ok_result["code"] == 0, ok_result
    assert ok_result["restarted"] == ["watchdog"], ok_result
    ok_reloader._write_state(last_result=ok_result)
    state = wg_daemon.read_json(reload._state(reload.STATE_NAME)) or {}
    assert not state.get("last_deferred"), state
    assert "推迟未生效" not in reload.status_line(), reload.status_line()
    print("  ok  推迟：code=5 / ok=False / 输出「未生效」/ 欠账只被该 target 的成功清掉")


def check_db_keeper_order():
    events = []
    old = FakeProc("old", events)
    new = FakeProc("new", events)

    def spawn(name):
        events.append("spawn:new")
        return new

    reloader = reload.Reloader(spawn=spawn, verify=False)
    original_sleep = reload.time.sleep
    reload.time.sleep = lambda seconds: None
    try:
        status, _ = reloader._restart_db_keeper({"db_keeper": old}, spawn)
    finally:
        reload.time.sleep = original_sleep
    assert status == "ok", status
    assert events[0] == "spawn:new", events
    assert events.index("terminate:old") > events.index("spawn:new"), events
    assert old.alive is False
    print("  ok  db_keeper：先起新的，再停旧的（任意时刻至少一个 keeper）")


def check_quarantine():
    quarantined = set()
    reloader = reload.Reloader(
        spawn=lambda name: None, quarantined=quarantined, verify=False)
    for _ in range(3):
        reloader._record_failure("app", "boom")
    assert "app" in quarantined, quarantined
    reloader.quarantine_until["app"] = time.monotonic() - 1
    assert not reloader._is_quarantined("app")
    assert "app" not in quarantined
    print("  ok  quarantine：3 次失败后熔断，到期半开")


def check_no_port_spawn():
    old = FakeProc("app")
    spawned = []
    reloader = reload.Reloader(spawn=lambda name: spawned.append(name), verify=False)
    original = reload_exec._wait_ports_free
    reload_exec._wait_ports_free = lambda ports, timeout: False
    try:
        status, why = reloader._restart_app({"app": old}, lambda name: spawned.append(name))
    finally:
        reload_exec._wait_ports_free = original
    assert status == "failed" and "busy" in why, (status, why)
    assert not spawned, spawned
    print("  ok  端口未释放：不 spawn 新 app")


def check_cli_no_spawn():
    source = (ROOT / "water-group" / "daemon" / "reload.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    assert "subprocess" not in names, names
    assert "Popen" not in names, names
    assert (ROOT / "reload.ps1").exists()
    print("  ok  CLI：reload.py 无 subprocess/Popen，reload.ps1 存在")


def check_wait_budget():
    """reload.py 的 --wait 默认值必须 = wg_budget.WAIT_BUDGET，且 reload.ps1 不补发
    字面量 --wait（单点定义）。issue #230：默认 120 吃光 AGENT_TOOL_TIMEOUT，宿主在
    120s 处杀进程树时 _wait_result 的超时分支来不及执行，Agent 看到的最后一行停在
    「已投递 nonce=…」，分不清被杀还是还在等。
    """
    source = (ROOT / "water-group" / "daemon" / "reload.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wait_default = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
            if any(getattr(a, "value", None) == "--wait" for a in node.args):
                for kw in node.keywords:
                    if kw.arg == "default":
                        wait_default = kw.value
    assert wait_default is not None, "reload.py --wait 没有找到 default 关键字参数"
    # default=wg_budget.WAIT_BUDGET → Attribute(value=Name('wg_budget'), attr='WAIT_BUDGET')
    ok_attr = (isinstance(wait_default, ast.Attribute)
               and isinstance(wait_default.value, ast.Name)
               and wait_default.value.id == "wg_budget"
               and wait_default.attr == "WAIT_BUDGET")
    # 不接受任何字面量默认值（120 / 90 / 数字一律不行——必须走预算口径，单点定义）
    not_literal = not isinstance(wait_default, ast.Constant)
    assert ok_attr and not_literal, \
        f"reload.py --wait default 必须是 wg_budget.WAIT_BUDGET，实际是 {ast.dump(wait_default)}"

    ps1 = (ROOT / "reload.ps1").read_text(encoding="utf-8")
    # reload.ps1 里 --wait 只允许出现在注释里（解释/示例），不允许作为补发的参数值。
    # 抓「补发」形态：`@('--wait', '<数字>')` 或 `--wait` 后跟一个裸数字字面量。
    code_lines = [ln for ln in ps1.splitlines() if ln.strip()
                  and not ln.strip().startswith("#")]
    code = "\n".join(code_lines)
    assert not re.search(r"'--wait'\s*,\s*'\d+'", code), \
        "reload.ps1 不得补发字面量 --wait（单点定义，让 reload.py 默认值生效，issue #230）"
    assert "$hasWait" not in code and "$cliArgs +=" not in code, \
        "reload.ps1 不应再有 --wait 补发逻辑（已删，issue #230）"

    # 反例：旧默认 120 必须被判超预算（钉住复发——任何把默认改回 120 的改动会让这条变红，
    # 而 reload.py 的 default 断言会先一步挡住）。
    assert wg_budget.over_budget(120) is True, "120 应被判超预算（issue #230 反例）"
    assert wg_budget.over_budget(wg_budget.WAIT_BUDGET) is False, \
        "WAIT_BUDGET 不应被判超预算"
    print(f"  ok  等待预算：reload.py --wait 默认 = wg_budget.WAIT_BUDGET "
          f"({wg_budget.WAIT_BUDGET})，reload.ps1 不补发字面量，over_budget(120) 为真")


def check_exclusions():
    planner = reload.Planner()
    files = planner.scan()
    for prefix in ("data/", "tools/", ".venv/", ".git/", "imports/", "reports/"):
        assert not any(rel.startswith(prefix) for rel in files), prefix
    print("  ok  排除表：data/tools/.venv/.git/imports/reports 不进扫描")


def check_soft_marker():
    soft = reload._soft_path("commands")
    soft.parent.mkdir(parents=True, exist_ok=True)
    soft.write_text("1", encoding="utf-8")
    os.utime(soft, (time.time() - 120, time.time() - 120))
    assert not reload.soft_requested("commands")
    assert not soft.exists(), "stale soft marker must be removed"
    print("  ok  soft 标记：陈旧标记被忽略并清理")


def check_mailbox_dry_run():
    spawned = []
    reloader = reload.Reloader(spawn=lambda name: spawned.append(name), verify=False)
    request = reload._request_payload("reload", ["commands"], "gate", dry_run=True)
    path = reload._request_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(request), encoding="utf-8")
    reloader.tick({"commands": FakeProc("commands")}, lambda name: spawned.append(name))
    result = wg_daemon.read_json(reload._result_path(request["nonce"]))
    assert isinstance(result, dict), result
    assert result["ok"] is True and result["targets"] == ["commands"], result
    assert not spawned, spawned
    assert not reload._request_path().exists()
    assert not reload._inflight_path().exists()
    print("  ok  信箱：dry-run 请求写结果，不 spawn，不留下 inflight")


def main():
    check_coverage()
    check_mapping()
    check_syntax()
    check_debounce()
    check_single_flight()
    check_watchdog_deferred()
    check_deferred_reports_failure()
    check_db_keeper_order()
    check_quarantine()
    check_no_port_spawn()
    check_cli_no_spawn()
    check_wait_budget()
    check_exclusions()
    check_soft_marker()
    check_mailbox_dry_run()
    print("PASS: hot reload suite passed.")


if __name__ == "__main__":
    main()
