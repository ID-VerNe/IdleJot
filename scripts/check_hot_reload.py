"""Hot-reload gate: closure coverage, planning, debounce, quarantine, no spawn.

Run with the repo venv:

    .venv/Scripts/python.exe scripts/check_hot_reload.py

Everything uses a throwaway WATER_GROUP_HOME; no real port or child is touched.
The fake child is a small Popen-shaped object so ordering assertions (db_keeper
new-before-old, watchdog deferred) do not need a real process.
"""
from __future__ import annotations

import ast
import atexit
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="wg_hot_reload_"))
atexit.register(shutil.rmtree, TMP, True)
os.environ["WATER_GROUP_HOME"] = str(TMP)
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

import reload  # noqa: E402
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

    original = reload._compile_with_retry
    reload._compile_with_retry = lambda path, attempts=3: "SyntaxError line 1: bad"
    try:
        plan = reload.Planner().plan(["water-group/daemon/commands.py"])
        assert plan.rejected and plan.targets == [], plan
    finally:
        reload._compile_with_retry = original
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
    original = reload._wait_ports_free
    reload._wait_ports_free = lambda ports, timeout: False
    try:
        status, why = reloader._restart_app({"app": old}, lambda name: spawned.append(name))
    finally:
        reload._wait_ports_free = original
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
    check_db_keeper_order()
    check_quarantine()
    check_no_port_spawn()
    check_cli_no_spawn()
    check_exclusions()
    check_soft_marker()
    check_mailbox_dry_run()
    print("PASS: hot reload suite passed.")


if __name__ == "__main__":
    main()
