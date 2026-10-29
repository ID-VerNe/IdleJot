"""Hot-reload execution: process restarts, ports, health, the reload loop.

The execution half of the former ``reload.py`` (issue #11, section PD).
Everything here touches processes or the loopback network; the analysis half
lives in ``reload_plan`` and the CLI in ``reload``.

Design notes carried over from the original module:

* ``watchdog`` is only restarted while NapCat answers; otherwise the request is
  deferred.  Killing an injector mid-relaunch is how two QQ instances end up
  sharing one user-data-dir.  A deferral is not a success: it exits non-zero
  (code 5) so nobody reads "reloaded" while the old code is still running
  (issue #229).
* ``db_keeper`` is the one child that may briefly have two instances: start the
  new one, wait, then stop the old one.  There must never be a window with no
  keeper.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import time
import urllib.request
from pathlib import Path
from typing import Callable

from wg_daemon import read_json, write_json  # noqa: E402  (issue #11 PB: 助手收口)
import wg_pause  # noqa: E402  (暂停标记的判定：有主 + 有时限，issue #189)
from reload_plan import (  # noqa: E402  (issue #11 PD: 分析层)
    FAILED_NAME,
    INFLIGHT_NAME,
    REQUEST_NAME,
    STATE_NAME,
    WATCH_NAME,
    WATER_GROUP,
    Planner,
    ROOT,
    _inflight_path,
    _log,
    _request_path,
    _result_path,
    _soft_path,
    _state,
    build_stamp,
    guard_info,
)

def _terminate(proc, timeout: float = 10.0) -> bool:
    """Stop one process only.  Never uses a tree kill: QQ is a grandchild."""
    if proc is None:
        return True
    try:
        if proc.poll() is not None:
            return True
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001  TimeoutExpired and fake-process variants
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.4):
            return True
    except OSError:
        return False


def _wait_ports_free(ports, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_port_open(port) for port in ports):
            return True
        time.sleep(0.2)
    return not any(_port_open(port) for port in ports)


def _http_json(url: str, timeout: float = 2.0):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def napcat_healthy() -> bool:
    """OneBot answers on 127.0.0.1:43000.  Used before bouncing watchdog."""
    config = read_json(WATER_GROUP / "config.json") or {}
    token = str(config.get("onebot_token") or "")
    body = b"{}"
    request = urllib.request.Request(
        "http://127.0.0.1:43000/get_login_info", data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("status") == "ok"
    except Exception:  # noqa: BLE001
        return False


def _heartbeat_age(path: Path):
    try:
        return time.time() - int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


class Reloader:
    """Guard-side executor.  One instance, one thread, one writer."""

    def __init__(self, spawn: Callable, *, healthy: Callable | None = None,
                 quarantined=None, config=None, root: Path = ROOT,
                 verify: bool = True, guard_pid_value: int | None = None):
        self.spawn = spawn
        self.healthy = healthy or (lambda: True)
        self.quarantined = quarantined if quarantined is not None else set()
        self.config = dict(config or {})
        self.planner = Planner(root=root)
        self.verify = verify
        self.guard_pid_value = int(guard_pid_value or os.getpid())
        self.watch = bool(self.config.get("watch", False))
        watch_file = _state(WATCH_NAME)
        try:
            if watch_file.exists():
                self.watch = watch_file.read_text(encoding="utf-8").strip() == "1"
        except OSError:
            pass
        self.interval = float(self.config.get("interval", 2.0) or 2.0)
        self.debounce = float(self.config.get("debounce", 1.5) or 0)
        self.cooldown = float(self.config.get("cooldown", 10) or 0)
        self.verify_timeout = float(self.config.get("verify_timeout", 30) or 0)
        self.defer_when_busy = bool(self.config.get("defer_when_busy", True))
        # The guard has just imported the code it is running, so the current
        # tree is the correct baseline.  Reusing a snapshot written by an older
        # guard would make the first --reload after a merge see guard-owned
        # files (up.py/supervisor.py) as changed and reject every request.
        self.snapshot = self.planner.scan()
        self.planner.save(self.snapshot)
        self.pending: set[str] = set()
        self.pending_since = 0.0
        self.last_reload = 0.0
        self.failures: dict[str, int] = {}
        self.quarantine_until: dict[str, float] = {}
        self.quarantine_delay: dict[str, float] = {}
        self.deferred: set[str] = set()
        self.state = read_json(_state(STATE_NAME)) or {}
        # A guard restart is a fresh start: quarantine does not survive it.
        # The failed file is still overwritten by _write_failed on a new failure.
        self.recover_inflight()
        self._write_state()

    # ---- mailbox -----------------------------------------------------
    def recover_inflight(self):
        path = _inflight_path()
        raw = read_json(path)
        if raw is None:
            return
        nonce = str(raw.get("nonce") or "")
        if nonce:
            self._write_result(nonce, {
                "nonce": nonce, "ok": False, "elapsed": 0,
                "restarted": [], "deferred": [], "skipped": [],
                "rejected": [{"path": "", "why": "上次重载未完成（guard 中断）"}],
                "warnings": [], "build": build_stamp(self.snapshot),
                "guard_sandboxed": bool(guard_info().get("sandbox")), "code": 4,
            })
            _log(f"[reload] recovered unfinished request nonce={nonce}")
        try:
            path.unlink()
        except OSError:
            pass

    def _write_result(self, nonce: str, payload: dict):
        write_json(_result_path(nonce), payload)

    def _claim_requests(self):
        if _inflight_path().exists():
            return []
        paths = []
        base = _request_path()
        if base.exists():
            paths.append(base)
        try:
            paths.extend(sorted(base.parent.glob(REQUEST_NAME + ".*")))
        except OSError:
            pass
        claimed = []
        for path in paths:
            if path.name.endswith(".inflight") or path.name == INFLIGHT_NAME:
                continue
            try:
                os.replace(path, _inflight_path())
            except OSError:
                continue
            claimed.append(path)
            break  # one request per tick; the rest stay queued
        return claimed

    def _read_inflight(self):
        raw = read_json(_inflight_path())
        return raw if isinstance(raw, dict) else None

    def _finish_inflight(self):
        try:
            _inflight_path().unlink()
        except OSError:
            pass

    def _handle_request(self, request, procs, spawn):
        nonce = str(request.get("nonce") or secrets.token_hex(8))
        mode = str(request.get("mode") or "reload")
        only = request.get("only") or []
        started = time.monotonic()
        if mode == "watch-on":
            self.watch = True
            self._persist_watch()
            result = self._result(nonce, mode, started, [], [], [], [], [])
        elif mode == "watch-off":
            self.watch = False
            self._persist_watch()
            result = self._result(nonce, mode, started, [], [], [], [], [])
        elif mode == "full":
            result = self._result(nonce, mode, started, [], [], [], [{
                "path": "", "why": "P0 不支持 --full；请从 explorer 重启 start-bot.pyw",
            }], [], code=2)
        else:
            force = only if only else None
            changed = self.planner.diff(self.snapshot)
            plan = self.planner.plan(changed, only=only or None, force_targets=force, mode=mode)
            if request.get("dry_run"):
                result = self._result(nonce, mode, started, [], [], plan.skipped,
                                      plan.rejected, plan.warnings, plan=plan)
            else:
                try:
                    result = self._execute(nonce, mode, plan, procs, spawn, started)
                except Exception as exc:  # noqa: BLE001  never leave a request stuck
                    result = self._result(
                        nonce, mode, started, [], [], plan.skipped,
                        plan.rejected + [{"path": "", "why": f"reload crashed: {type(exc).__name__}: {exc}"}],
                        plan.warnings, plan=plan, code=4)
                if plan.ok:
                    self.snapshot = self.planner.scan()
                    self.planner.save(self.snapshot)
                    self.last_reload = time.monotonic()
                    self.pending.clear()
        self._write_result(nonce, result)
        self._write_state(last_result=result)
        self._finish_inflight()
        _log(f"[reload] {mode} nonce={nonce} ok={result.get('ok')} "
             f"restarted={','.join(result.get('restarted') or []) or '-'} "
             f"deferred={','.join(result.get('deferred') or []) or '-'} "
             f"{result.get('elapsed')}s")
        return result

    def _result(self, nonce, mode, started, restarted, deferred, skipped, rejected,
                warnings, plan=None, code=None):
        if code is None:
            code = 0 if not rejected else 2
        payload = {
            "nonce": nonce,
            "mode": mode,
            "ok": not rejected and code == 0,
            "elapsed": round(time.monotonic() - started, 2),
            "restarted": restarted,
            "deferred": deferred,
            "skipped": skipped,
            "rejected": rejected,
            "warnings": warnings,
            "build": build_stamp(self.snapshot),
            "guard_sandboxed": bool(guard_info().get("sandbox")),
            "code": code,
        }
        if plan is not None:
            payload["changed"] = plan.changed
            payload["targets"] = plan.targets
        return payload

    # ---- execution ----------------------------------------------------
    def _execute(self, nonce, mode, plan, procs, spawn, started):
        if not plan.ok:
            return self._result(nonce, mode, started, [], [], plan.skipped,
                                plan.rejected, plan.warnings, plan=plan, code=2)
        restarted, deferred, warnings = [], [], list(plan.warnings)
        failed = []
        for target in plan.targets:
            if self._is_quarantined(target):
                deferred.append(target)
                warnings.append(f"{target}: quarantined")
                continue
            status, why = self._restart_target(target, procs, spawn)
            if status == "ok":
                restarted.append(target)
                self._clear_failure(target)
            elif status == "deferred":
                deferred.append(target)
                warnings.append(f"{target}: {why or 'deferred'}")
                self.deferred.add(target)
            else:
                failed.append(target)
                warnings.append(f"{target}: {why or 'failed'}")
                self._record_failure(target, why or "failed")
        # issue #229：deferred 是「这次没做」，不能与成功同形。给它自己的非 0
        # 码（5），于是 ok=False、CLI 退出 5、reload.ps1 透传 5——调用方一眼能
        # 看出改动的代码一行没生效。4 仍然只表示真的失败（起不来）。
        code = 4 if failed else (5 if deferred else 0)
        return self._result(nonce, mode, started, restarted, deferred, plan.skipped,
                            plan.rejected, warnings, plan=plan, code=code)

    def _restart_target(self, target, procs, spawn):
        try:
            if target == "db_keeper":
                return self._restart_db_keeper(procs, spawn)
            if target == "app":
                return self._restart_app(procs, spawn)
            if target == "collector":
                return self._restart_collector(procs, spawn)
            if target == "commands":
                return self._restart_commands(procs, spawn)
            if target == "watchdog":
                return self._restart_watchdog(procs, spawn)
        except Exception as exc:  # noqa: BLE001
            return "failed", f"{type(exc).__name__}: {exc}"
        return "failed", "unknown target"

    def _spawn_one(self, target, spawn):
        try:
            proc = spawn(target)
        except Exception as exc:  # noqa: BLE001
            return None, f"spawn raised {type(exc).__name__}: {exc}"
        if proc is None:
            return None, "spawn returned None"
        return proc, ""

    def _restart_db_keeper(self, procs, spawn):
        old = procs.get("db_keeper")
        new, why = self._spawn_one("db_keeper", spawn)
        if new is None:
            return "failed", why
        # Keep the old keeper alive until the new one is definitely running.
        time.sleep(2)
        if new.poll() is not None:
            return "failed", "new db_keeper exited before old was stopped"
        if old is not None and old is not new:
            _terminate(old)
        procs["db_keeper"] = new
        return "ok", ""

    def _restart_app(self, procs, spawn):
        old = procs.get("app")
        _terminate(old)
        if not _wait_ports_free((47860, 48777), 10):
            return "failed", "ports 47860/48777 still busy; not spawning"
        new, why = self._spawn_one("app", spawn)
        if new is None:
            return "failed", why
        procs["app"] = new
        if self.verify and self.verify_timeout > 0:
            deadline = time.monotonic() + self.verify_timeout
            while time.monotonic() < deadline:
                body = _http_json("http://127.0.0.1:47860/")
                if isinstance(body, dict) and body.get("mcp_port") == 48777:
                    return "ok", ""
                time.sleep(0.5)
            return "ok", "started, but GET / did not verify"
        return "ok", ""

    def _restart_collector(self, procs, spawn):
        old = procs.get("collector")
        _terminate(old)
        new, why = self._spawn_one("collector", spawn)
        if new is None:
            return "failed", why
        procs["collector"] = new
        if self.verify and self.verify_timeout > 0:
            deadline = time.monotonic() + min(self.verify_timeout, 10)
            while time.monotonic() < deadline:
                age = _heartbeat_age(_state("wg_heartbeat"))
                if age is not None and age <= 30:
                    return "ok", ""
                if new.poll() is not None:
                    return "failed", "collector exited during verify"
                time.sleep(0.5)
            return "ok", "started, but heartbeat not verified"
        return "ok", ""

    def _restart_commands(self, procs, spawn):
        old = procs.get("commands")
        soft = _soft_path("commands")
        try:
            soft.write_text(str(int(time.time())), encoding="utf-8")
        except OSError:
            pass
        deadline = time.monotonic() + 5
        while old is not None and old.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if old is not None and old.poll() is None:
            _terminate(old)
        try:
            soft.unlink()
        except OSError:
            pass
        new, why = self._spawn_one("commands", spawn)
        if new is None:
            return "failed", why
        procs["commands"] = new
        if self.verify and self.verify_timeout > 0:
            deadline = time.monotonic() + min(self.verify_timeout, 10)
            while time.monotonic() < deadline:
                age = _heartbeat_age(_state("wg_commands_heartbeat"))
                if age is not None and age <= 30:
                    return "ok", ""
                if new.poll() is not None:
                    return "failed", "commands exited during verify"
                time.sleep(0.5)
            return "ok", "started, but heartbeat not verified"
        return "ok", ""

    def _restart_watchdog(self, procs, spawn):
        if not self.healthy():
            return "deferred", "NapCat not healthy; injector may be in flight"
        old = procs.get("watchdog")
        _terminate(old)
        new, why = self._spawn_one("watchdog", spawn)
        if new is None:
            return "failed", why
        procs["watchdog"] = new
        if self.verify and self.verify_timeout > 0:
            time.sleep(2)
            if not self.healthy():
                return "failed", "watchdog restarted but NapCat is not healthy"
        return "ok", ""

    # ---- watch / quarantine ------------------------------------------
    def _persist_watch(self):
        try:
            path = _state(WATCH_NAME)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("1" if self.watch else "0", encoding="utf-8")
        except OSError:
            pass
        self._write_state()

    def _busy(self):
        if not self.defer_when_busy:
            return False
        # issue #189：只看「文件在不在」会把一个没人认领的遗留标记当成正在进行的
        # 发送，热重载从此被永久挡住。判定交给 wg_pause（owner 存活 + 有时限）。
        return wg_pause.active()

    def _tick_watch(self, procs, spawn):
        if not self.watch:
            return
        changed = self.planner.diff(self.snapshot)
        if changed:
            self.pending.update(changed)
            self.pending_since = time.monotonic()
            return
        if not self.pending:
            return
        now = time.monotonic()
        if now - self.pending_since < self.debounce:
            return
        if now - self.last_reload < self.cooldown:
            return
        if self._busy():
            return
        plan = self.planner.plan(self.pending)
        result = self._execute("watch", "watch", plan, procs, spawn, now)
        self.last_reload = now
        if plan.ok:
            self.pending.clear()
            self.snapshot = self.planner.scan()
            self.planner.save(self.snapshot)
            _log(f"[reload] watch {len(plan.changed)} files -> "
                 f"{','.join(plan.targets) or '-'} | ok {result['elapsed']}s")
        else:
            _log(f"[reload] watch rejected: "
                 f"{'; '.join(item.get('why','') for item in plan.rejected)}")
        self._write_state(last_result=result)

    def _retry_deferred(self, procs, spawn):
        if not self.deferred:
            return
        for target in list(self.deferred):
            if self._is_quarantined(target):
                continue
            if target == "watchdog" and not self.healthy():
                continue
            status, why = self._restart_target(target, procs, spawn)
            if status == "ok":
                self.deferred.discard(target)
                self._clear_failure(target)
                # 补上了就不再欠着：状态文件里那条「推迟未生效」要跟着撤掉，
                # 否则 --status 会永远说上一次没做完（issue #229）。
                self._clear_deferred(target)
                _log(f"[reload] deferred retry ok {target}")
            elif status == "failed":
                self.deferred.discard(target)
                self._record_failure(target, why or "deferred retry failed")
                self._clear_deferred(target)

    def tick(self, procs, spawn=None):
        spawn = spawn or self.spawn
        try:
            if self._claim_requests():
                request = self._read_inflight()
                if request is None:
                    self._finish_inflight()
                else:
                    try:
                        self._handle_request(request, procs, spawn)
                    except Exception as exc:  # noqa: BLE001  answer even on planner bugs
                        nonce = str(request.get("nonce") or "")
                        if nonce:
                            self._write_result(nonce, self._result(
                                nonce, str(request.get("mode") or "reload"),
                                time.monotonic(), [], [], [], [{
                                    "path": "",
                                    "why": f"request failed: {type(exc).__name__}: {exc}",
                                }], [], code=4))
                        self._finish_inflight()
                        _log(f"[reload] request error {type(exc).__name__}: {exc}")
            self._tick_watch(procs, spawn)
            self._retry_deferred(procs, spawn)
        except Exception as exc:  # noqa: BLE001  a bad reloader must not kill the guard
            _log(f"[reload] tick error {type(exc).__name__}: {exc}")

    def _is_quarantined(self, target):
        if target not in self.quarantined:
            return False
        until = self.quarantine_until.get(target, 0)
        if time.monotonic() >= until:
            self.quarantined.discard(target)
            self.failures[target] = 0
            self._write_failed()
            _log(f"[reload] half-open {target}")
            return False
        return True

    def _record_failure(self, target, why):
        count = self.failures.get(target, 0) + 1
        self.failures[target] = count
        if count >= 3:
            delay = min(self.quarantine_delay.get(target, 300), 3600)
            self.quarantine_delay[target] = min(delay * 2, 3600)
            self.quarantine_until[target] = time.monotonic() + delay
            self.quarantined.add(target)
            self.failures[target] = 0
            self._write_failed()
            _log(f"[reload] quarantine {target}: {why} (retry in {delay}s)")
        else:
            _log(f"[reload] {target} failed ({count}/3): {why}")

    def _clear_failure(self, target):
        if self.failures.get(target) or target in self.quarantined:
            self.failures[target] = 0
            self.quarantined.discard(target)
            self.quarantine_until.pop(target, None)
            self._write_failed()

    def _write_failed(self):
        targets = {}
        for name in self.quarantined:
            targets[name] = {
                "count": self.failures.get(name, 0),
                "until": self.quarantine_until.get(name, 0),
            }
        write_json(_state(FAILED_NAME), {"v": 1, "at": time.time(), "targets": targets})

    def _write_state(self, **extra):
        self.state.update({
            "v": 1,
            "watch": self.watch,
            "interval": self.interval,
            "last_at": self.state.get("last_at"),
            "last_elapsed": self.state.get("last_elapsed"),
            "last_files": self.state.get("last_files"),
            "last_restarted": self.state.get("last_restarted"),
            # issue #229：推迟的那次必须落盘，guard 重启后 self.deferred 是空的，
            # 这是唯一还能说出「上一次有 target 没生效」的地方。
            "last_deferred": self.state.get("last_deferred") or [],
            "last_deferred_at": self.state.get("last_deferred_at"),
            "build": build_stamp(self.snapshot),
        })
        if extra.get("last_result"):
            result = extra["last_result"]
            self.state.update({
                "last_at": time.time(),
                "last_elapsed": result.get("elapsed"),
                "last_files": result.get("changed") or [],
                "last_restarted": result.get("restarted") or [],
            })
            self._note_deferred(result)
        write_json(_state(STATE_NAME), self.state)

    def _note_deferred(self, result):
        """Remember which targets did not take effect (issue #229).

        A deferred target means the changed code is still running the old
        version.  ``self.deferred`` retries it inside a live guard; the state
        file is what survives a guard restart, so ``--status`` can report the
        fact instead of showing ``重启 -``.
        """
        deferred = [str(name) for name in (result.get("deferred") or [])]
        if deferred:
            self.state["last_deferred"] = deferred
            self.state["last_deferred_at"] = time.time()
        elif result.get("ok"):
            # 一次干净的成功才清账；失败结果里的 target 已经由 failed 报出，
            # 不该顺手把上一轮的推迟记录抹掉。
            self.state["last_deferred"] = []
            self.state["last_deferred_at"] = None

    def _clear_deferred(self, target):
        """Drop one target from the persisted deferral record (issue #229)."""
        pending = [name for name in (self.state.get("last_deferred") or [])
                   if name != target]
        if pending != list(self.state.get("last_deferred") or []):
            self.state["last_deferred"] = pending
            if not pending:
                self.state["last_deferred_at"] = None
            self._write_state()
