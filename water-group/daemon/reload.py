"""Hot reload for the water-group stack: one planner, one writer, one mailbox.

The guard (``up.py``) is the only process allowed to restart a child.  A CLI
invocation writes a request file and waits for a result file; it never spawns
anything, because a process spawned from the agent sandbox inherits ``tsbx.dll``
down to QQ and kills it (see ``AGENTS.md`` §0).

The module is deliberately free of ``subprocess``/``Popen``.  It is imported by
``commands.py`` only for the cooperative-exit checkpoint, so importing it must
stay cheap and side-effect free.

Design notes that matter when editing this file:

* Changes are mapped to child processes by an AST import closure, not by a
  hand-written file table.  A new file that nobody imports fails the
  ``check_hot_reload.py`` coverage gate, forcing an explicit classification.
* A syntax error rejects the whole batch.  The old processes keep running.
* ``watchdog`` is only restarted while NapCat answers; otherwise the request is
  deferred.  Killing an injector mid-relaunch is how two QQ instances end up
  sharing one user-data-dir.
* ``db_keeper`` is the one child that may briefly have two instances: start the
  new one, wait, then stop the old one.  There must never be a window with no
  keeper.

The module keeps its historical import surface as a facade: the analysis
half lives in ``reload_plan`` and the execution half in ``reload_exec``
(issue #11, section PD), and every name that used to live here is
re-exported below, so ``import reload`` consumers are untouched.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time

from wg_daemon import pid_alive, read_json  # noqa: E402  (issue #11 PB: 助手收口)
from reload_exec import (  # noqa: E402  (issue #11 PD: 执行层)
    Reloader,
    _heartbeat_age,
    _http_json,
    _port_open,
    _terminate,
    _wait_ports_free,
    napcat_healthy,
)
from reload_plan import (  # noqa: E402  (issue #11 PD: 分析层，门面全量 re-export)
    ENTRY_FILES,
    EXCLUDE_DIRS,
    FAILED_NAME,
    GUARD_NAME,
    GUARD_OWNED,
    HERE,
    INFLIGHT_NAME,
    LOG_NAME,
    NO_RESTART,
    Planner,
    REQUEST_NAME,
    RESULT_PREFIX,
    ROOT,
    RestartPlan,
    SHARED_GUARD,
    SNAPSHOT_NAME,
    SOFT_PREFIX,
    STATE_NAME,
    TARGETS,
    WATCH_DIRS,
    WATCH_EXTRA,
    WATCH_FILES,
    WATCH_NAME,
    WATER_GROUP,
    _abs,
    _compile_with_retry,
    _file_info,
    _inflight_path,
    _iter_watch_files,
    _log,
    _logs,
    _module_files,
    _rel,
    _request_path,
    _resolve_import,
    _result_path,
    _soft_path,
    _state,
    build_stamp,
    guard_info,
    guard_pid,
    sandbox_dll,
    soft_requested,
    write_guard_state,
)

def status_line() -> str:
    state = read_json(_state(STATE_NAME)) or {}
    if not state:
        return "未启用（无 wg_reload_state.json）"
    watch = "开" if state.get("watch") else "关"
    last = state.get("last_at")
    stamp = time.strftime("%H:%M", time.localtime(last)) if last else "从未"
    restarted = ",".join(state.get("last_restarted") or []) or "-"
    elapsed = state.get("last_elapsed")
    elapsed = f"{elapsed}s" if isinstance(elapsed, (int, float)) else "-"
    return (f"watch {watch}（间隔 {state.get('interval', 2)}s，"
            f"上次 {stamp} 重启 {restarted} {elapsed}）")


def _request_payload(mode, only, requested_by, dry_run=False):
    return {
        "v": 1,
        "nonce": secrets.token_hex(8),
        "mode": mode,
        "only": list(only or []),
        "requested_by": requested_by or "agent",
        "at": int(time.time()),
        "guard_pid": guard_pid(),
        "build": build_stamp(),
        "dry_run": bool(dry_run),
    }


def _enqueue(request: dict):
    path = _request_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path
    if path.exists():
        target = path.with_name(f"{REQUEST_NAME}.{request['nonce']}")
    # issue #169 PD：合流到 wg_daemon.write_json（唯一临时名 pid + 随机后缀）。
    # 原写法已带 pid 但缺随机后缀——同 pid 的并发请求（reload worker 复用进程）
    # 仍会互踩 tmp。助手统一带 token_hex(4)，并发写互不踩。
    from wg_daemon import write_json
    write_json(target, request)
    return target


def _wait_result(nonce: str, wait: float, guard: int):
    path = _result_path(nonce)
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        raw = read_json(path)
        if isinstance(raw, dict):
            return raw
        if wait <= 0:
            return None
        if not pid_alive(guard):
            return {"ok": False, "code": 2, "rejected": [{
                "path": "", "why": f"guard pid={guard} 已退出；检查 WATER_GROUP_HOME 是否一致",
            }]}
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def _print_result(result: dict):
    if not result:
        print("等待超时：guard 没有写结果文件")
        return
    if result.get("rejected"):
        for item in result["rejected"]:
            print(f"拒绝: {item.get('path') or '-'} {item.get('why')}")
    print(f"ok={result.get('ok')} elapsed={result.get('elapsed')}s "
          f"restarted={','.join(result.get('restarted') or []) or '-'} "
          f"deferred={','.join(result.get('deferred') or []) or '-'}")
    for item in result.get("warnings") or []:
        print(f"warning: {item}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="水群全栈热重载：投递请求给 guard，不 spawn")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--watch", action="store_true", help="打开 guard 内常驻监视")
    group.add_argument("--no-watch", action="store_true", help="关闭常驻监视")
    parser.add_argument("--only", metavar="app,commands", help="只处理指定 target")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不执行")
    parser.add_argument("--full", action="store_true", help="P0 未支持：连 guard 一起换")
    parser.add_argument("--wait", type=float, default=120, help="等待结果秒数（默认 120）")
    parser.add_argument("--status", action="store_true", help="读取上次重载状态")
    parser.add_argument("--config", action="store_true", help="只重启吃 config 的进程")
    parser.add_argument("--requested-by", default="agent")
    args = parser.parse_args(argv)

    if args.status:
        print(status_line())
        failed = read_json(_state(FAILED_NAME)) or {}
        if failed.get("targets"):
            print("quarantine: " + ", ".join(sorted(failed["targets"])))
        return 0 if read_json(_state(STATE_NAME)) else 2

    if args.full:
        print("P0 不支持 --full；请从 explorer 重启 water-group/start-bot.pyw")
        return 2

    only = [part.strip() for part in (args.only or "").split(",") if part.strip()]
    if args.dry_run:
        planner = Planner()
        changed = planner.diff()
        plan = planner.plan(changed, only=only or None)
        print(json.dumps({
            "changed": plan.changed,
            "targets": plan.targets,
            "skipped": plan.skipped,
            "rejected": plan.rejected,
            "warnings": plan.warnings,
        }, ensure_ascii=False, indent=2))
        return 0 if plan.ok else 2

    guard = guard_pid()
    if not guard or not pid_alive(guard):
        print("guard 不在线：先双击 water-group/start-bot.pyw（不要从沙箱起栈）")
        return 2

    if args.watch:
        mode = "watch-on"
    elif args.no_watch:
        mode = "watch-off"
    elif args.config:
        mode = "config"
    else:
        mode = "reload"
    request = _request_payload(mode, only, args.requested_by)
    try:
        _enqueue(request)
    except OSError as exc:
        print(f"写请求失败: {type(exc).__name__}: {exc}")
        return 2
    print(f"已投递 nonce={request['nonce']} mode={mode} guard={guard}")
    if args.wait <= 0:
        return 0
    result = _wait_result(request["nonce"], args.wait, guard)
    _print_result(result)
    if result is None:
        return 3
    return int(result.get("code", 0))


if __name__ == "__main__":
    sys.exit(main())
