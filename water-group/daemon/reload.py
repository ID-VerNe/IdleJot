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
  sharing one user-data-dir.  A deferral is reported as a failure (exit 5), not
  as a success: the changed code is still the old code (issue #229).
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
import wg_budget  # noqa: E402  (issue #230：--wait 默认走预算口径)
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
    line = (f"watch {watch}（间隔 {state.get('interval', 2)}s，"
            f"上次 {stamp} 重启 {restarted} {elapsed}）")
    # issue #229：推迟 = 改动的代码没生效。只报「重启 -」等于说一切正常，
    # 所以把还欠着的 target 单独说出来。
    deferred = [str(name) for name in (state.get("last_deferred") or [])]
    if deferred:
        at = state.get("last_deferred_at")
        when = time.strftime("%H:%M", time.localtime(at)) if at else stamp
        line += f"；{when} 有 {len(deferred)} 个 target 推迟未生效：{','.join(deferred)}"
    return line


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


def _defer_reasons(result: dict) -> dict:
    """把 warnings 里的 ``target: why`` 还原成 target → why（issue #229）。"""
    reasons = {}
    for item in result.get("warnings") or []:
        name, sep, why = str(item).partition(": ")
        if sep:
            reasons[name] = why
    return reasons


def _print_result(result: dict):
    if not result:
        print("等待超时：guard 没有写结果文件")
        return
    if result.get("rejected"):
        for item in result["rejected"]:
            print(f"拒绝: {item.get('path') or '-'} {item.get('why')}")
    deferred = [str(name) for name in (result.get("deferred") or [])]
    print(f"ok={result.get('ok')} elapsed={result.get('elapsed')}s "
          f"restarted={','.join(result.get('restarted') or []) or '-'} "
          f"deferred={','.join(deferred) or '-'}")
    # issue #229：推迟是「这次没做」，改动的代码一行没生效。必须单独一行说清，
    # 否则 `ok=True deferred=watchdog restarted=-` 读起来跟成功一样。
    if deferred:
        reasons = _defer_reasons(result)
        for name in deferred:
            print(f"未生效（推迟）: {name} —— {reasons.get(name) or '原因未记录'}")
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
    parser.add_argument("--wait", type=float, default=wg_budget.WAIT_BUDGET,
                        help=f"等待结果秒数（默认 {wg_budget.WAIT_BUDGET}，= WAIT_BUDGET，issue #230）")
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
    # 超预算的显式取值自己说一声（issue #230）。reload 与 listen/reply 不同：等待是「等
    # 满 / 不等」二选一，没有「压回预算后继续等」的语义——静默夹断会把用户「我就是要
    # 等 120s」改成另一个数，是谎言；所以这里**不 clamp**，只把事实说在前头。WARN 必须
    # 打在等待之前：被宿主在 AGENT_TOOL_TIMEOUT 处截断进程树时，_wait_result 的超时
    # 分支（:151-152、:233-234）来不及执行，Agent 看到的最后一行就停在这里，分不清被
    # 杀还是还在等。默认值 = WAIT_BUDGET，不打 WARN。逃生口是 --wait 0（只投递）+
    # --status 轮询，与 reply.py:713-717 的 shorter_hint 同一个思路。
    if wg_budget.over_budget(args.wait):
        print(f"WARN: --wait {args.wait}s 加上发送余量 {wg_budget.SEND_BUDGET}s 已超过常见工具调用"
              f"超时 {wg_budget.AGENT_TOOL_TIMEOUT}s；guard 可能在写回结果前被宿主截断进程树，"
              f"这条路径上不会打印结果。要只投递不等待：--wait 0（再 --status 轮询）",
              flush=True)
    result = _wait_result(request["nonce"], args.wait, guard)
    _print_result(result)
    if result is None:
        return 3
    # 退出码即 guard 给的 code：2 拒绝 / 4 失败 / 5 推迟未生效（issue #229）。
    # reload.ps1 末行 exit $LASTEXITCODE 直接透出，调用方按非 0 处理即可。
    return int(result.get("code", 0))


if __name__ == "__main__":
    sys.exit(main())
