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
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import secrets
import socket
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
WATER_GROUP = ROOT / "water-group"
WATCH_DIRS = (ROOT / "chatlocal", WATER_GROUP, ROOT / "water_group")
WATCH_FILES = (ROOT / "server_wg.py", ROOT / "start-bot.pyw", ROOT / "stop-bot.pyw")
WATCH_EXTRA = (ROOT / "pyproject.toml", ROOT / "uv.lock")

# Runtime data, virtualenvs and the generated NapCat tree are not code.  The
# exclusion table is a constant on purpose: tests assert that these names are
# covered, so a future scanner cannot quietly walk 2,000 JSON files.
EXCLUDE_DIRS = frozenset({
    ".git", ".venv", "__pycache__", "data", "tools", "imports", "reports",
    "build", ".cache", "node_modules", "state", "logs", "tmp",
})

TARGETS = ("db_keeper", "app", "collector", "commands", "watchdog")

ENTRY_FILES = {
    "db_keeper": WATER_GROUP / "daemon" / "db_keeper.py",
    "app": ROOT / "server_wg.py",
    "collector": WATER_GROUP / "daemon" / "collector.py",
    "commands": WATER_GROUP / "daemon" / "commands.py",
    "watchdog": WATER_GROUP / "daemon" / "watchdog.py",
}

# The guard's own code cannot be replaced by a child restart.  ``bootstrap.py``
# is here too: changing the injection path needs a full stop/start and a fresh
# NapCat injection, not a watchdog bounce.
GUARD_OWNED = frozenset({
    "water-group/daemon/up.py",
    "water-group/daemon/supervisor.py",
    "water-group/daemon/reload.py",
    "water-group/daemon/winconsole.py",
    "water-group/bootstrap.py",
    "start-bot.pyw",
    "stop-bot.pyw",
    "water-group/start-bot.pyw",
    "water-group/stop-bot.pyw",
})

# Code that is loaded by a one-shot CLI, or is not in any live daemon closure.
# A changed CLI file is naturally fresh on its next invocation; restarting a
# daemon would be pointless.
NO_RESTART = {
    "chatlocal/posix_child.py": "one-shot/dead module, not in a daemon closure",
    "chatlocal/tulpa_model.py": "one-shot/dead module, not in a daemon closure",
    "chatlocal/watch_check.py": "one-shot/dead module, not in a daemon closure",
    "water-group/daemon/dossier.py": "one-shot CLI",
    "water-group/daemon/listen.py": "one-shot CLI",
    "water-group/daemon/look.py": "one-shot CLI",
    "water-group/daemon/reply.py": "one-shot CLI",
    "water-group/daemon/watch.py": "one-shot CLI",
    "water-group/daemon/wg_logs.py": "one-shot log reader",
}

# Shared with the guard.  A child restart gets the new copy; the guard keeps
# the copy it imported until the next full start.  ``wg_paths`` is special:
# every child reads it, even the ones whose import closure does not mention it
# (server_wg gets its data root through chatlocal.config).
SHARED_GUARD = {
    "water-group/daemon/wg_paths.py": "all",
}

REQUEST_NAME = "wg_reload.request"
RESULT_PREFIX = "wg_reload.result."
INFLIGHT_NAME = "wg_reload.request.inflight"
SNAPSHOT_NAME = "wg_reload_snapshot.json"
STATE_NAME = "wg_reload_state.json"
FAILED_NAME = "wg_reload_failed.json"
WATCH_NAME = "wg_watch"
GUARD_NAME = "wg_guard.json"
SOFT_PREFIX = "wg_reload.soft."
LOG_NAME = "wg-reload.log"


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _abs(rel: str) -> Path:
    return ROOT / Path(rel)


def _state(name: str) -> Path:
    from wg_paths import state_dir

    return state_dir() / name


def _logs(name: str) -> Path:
    from wg_paths import logs_dir

    return logs_dir() / name


def _log(line: str) -> None:
    try:
        path = _logs(LOG_NAME)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _write_json(path: Path, payload) -> bool:
    """Write one JSON line atomically, leaving no half file for a reader."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def pid_alive(pid) -> bool:
    """True when *pid* names a live process.  Never sends a signal."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def sandbox_dll() -> str:
    """Name of an injected agent-sandbox DLL, or an empty string."""
    if os.name != "nt":
        return ""
    try:
        import ctypes

        return "tsbx.dll" if ctypes.windll.kernel32.GetModuleHandleW("tsbx.dll") else ""
    except Exception:  # noqa: BLE001
        return ""


def guard_info():
    return _read_json(_state(GUARD_NAME)) or {}


def write_guard_state(pid=None, build="", sandbox=None):
    payload = {
        "v": 1,
        "pid": int(pid or os.getpid()),
        "started_at": time.time(),
        "sandbox": sandbox_dll() if sandbox is None else sandbox,
        "build": build,
    }
    _write_json(_state(GUARD_NAME), payload)
    return payload


def guard_pid():
    info = guard_info()
    try:
        return int(info.get("pid") or 0)
    except (TypeError, ValueError):
        return 0


def _request_path() -> Path:
    return _state(REQUEST_NAME)


def _inflight_path() -> Path:
    return _state(INFLIGHT_NAME)


def _result_path(nonce: str) -> Path:
    return _state(f"{RESULT_PREFIX}{nonce}.json")


def _soft_path(target: str) -> Path:
    return _state(f"{SOFT_PREFIX}{target}")


def soft_requested(target: str = "commands", max_age: float = 60.0) -> bool:
    """Cooperative-exit checkpoint for a child.

    A stale marker (left by a guard that died mid-reload) is ignored and
    removed, otherwise the next commands start would exit immediately.
    """
    path = _soft_path(target)
    try:
        if not path.exists():
            return False
        if time.time() - path.stat().st_mtime > max_age:
            path.unlink()
            return False
        return True
    except OSError:
        return False


def build_stamp(files=None) -> str:
    """Short deterministic stamp of the code snapshot (no git subprocess)."""
    if files is None:
        files = Planner().scan()
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(files[rel].get("sha") or "").encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()[:12]


def _file_info(path: Path):
    try:
        stat = path.stat()
        data = path.read_bytes()
        return {
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "sha": hashlib.sha256(data).hexdigest(),
        }
    except OSError:
        return None


def _iter_watch_files(root: Path = ROOT):
    seen = set()
    for base in (root / "chatlocal", root / "water-group", root / "water_group"):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.suffix not in (".py", ".pyw") or not path.is_file():
                continue
            rel = _rel(path)
            if any(part in EXCLUDE_DIRS for part in Path(rel).parts):
                continue
            if rel not in seen:
                seen.add(rel)
                yield path
    for path in (root / "server_wg.py", root / "start-bot.pyw", root / "stop-bot.pyw"):
        if path.is_file():
            rel = _rel(path)
            if rel not in seen:
                seen.add(rel)
                yield path
    # Tracked so a change shows up as skipped, not invisible: config.json is
    # re-read by commands every 30s and by every CLI at its next invocation.
    for path in (root / "water-group" / "config.json",
                 root / "pyproject.toml", root / "uv.lock"):
        if path.is_file():
            rel = _rel(path)
            if rel not in seen:
                seen.add(rel)
                yield path


def _module_files(name: str, root: Path = ROOT):
    """Resolve an import name to every plausible source file.

    Three import shapes are live in this repository: package imports
    (``chatlocal.config``), flat daemon imports (``import sixty``), and the
    ``water_group`` bridge that maps ``water_group.bootstrap`` onto the
    hyphenated ``water-group/bootstrap.py``.
    """
    if not name:
        return []
    parts = [part for part in name.split(".") if part]
    if not parts:
        return []
    out = []
    if parts[0] == "chatlocal":
        base = root.joinpath(*parts)
        out.extend([base.with_suffix(".py"), base / "__init__.py"])
        # importing a submodule executes its parent packages
        for i in range(1, len(parts)):
            pkg = root.joinpath(*parts[:i]) / "__init__.py"
            if pkg.is_file():
                out.append(pkg)
    elif parts[0] == "water_group":
        if len(parts) == 1:
            out.append(root / "water_group" / "__init__.py")
        else:
            target = root / "water-group" / Path(*parts[1:])
            out.extend([target.with_suffix(".py"), target / "__init__.py"])
            bridge = root / "water_group" / Path(*parts[1:])
            out.extend([bridge.with_suffix(".py"), bridge / "__init__.py"])
            out.append(root / "water_group" / "__init__.py")
    else:
        base = root / "water-group" / "daemon" / Path(*parts)
        out.extend([base.with_suffix(".py"), base / "__init__.py"])
        rootbase = root / Path(*parts)
        out.extend([rootbase.with_suffix(".py"), rootbase / "__init__.py"])
    return [path for path in out if path.is_file()]


def _resolve_import(node, current: Path, root: Path = ROOT):
    """Return (files, unknown_dynamic) for one import AST node."""
    if isinstance(node, ast.Import):
        files = []
        for alias in node.names:
            files.extend(_module_files(alias.name, root))
        return files, False
    if isinstance(node, ast.ImportFrom):
        base = node.module or ""
        if node.level:
            pkg = current.parent
            for _ in range(node.level - 1):
                pkg = pkg.parent
            try:
                prefix = pkg.relative_to(root).parts
            except ValueError:
                prefix = ()
            base = ".".join((*prefix, *([base] if base else [])))
        files = list(_module_files(base, root))
        for alias in node.names:
            if alias.name == "*":
                continue
            qualified = f"{base}.{alias.name}" if base else alias.name
            files.extend(_module_files(qualified, root))
        return files, False
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id == "__import__":
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                return _module_files(node.args[0].value, root), False
            return [], True
    return [], False


@dataclass
class RestartPlan:
    changed: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unknown: bool = False

    @property
    def ok(self) -> bool:
        return not self.rejected


class Planner:
    """Pure-ish scanner/classifier.  No process is touched here."""

    def __init__(self, root: Path = ROOT, snapshot_path: Path | None = None):
        self.root = Path(root)
        self.snapshot_path = snapshot_path or _state(SNAPSHOT_NAME)
        self._closure_cache: dict[str, tuple[set[str], bool]] = {}

    def scan(self):
        files = {}
        for path in _iter_watch_files(self.root):
            info = _file_info(path)
            if info is not None:
                files[_rel(path)] = info
        return files

    def load(self):
        raw = _read_json(self.snapshot_path)
        if not isinstance(raw, dict):
            return None
        files = raw.get("files")
        if not isinstance(files, dict):
            return None
        return {str(k): v for k, v in files.items() if isinstance(v, dict)}

    def save(self, files):
        return _write_json(self.snapshot_path, {
            "v": 1,
            "at": time.time(),
            "files": files,
        })

    def diff(self, old=None):
        old = self.load() if old is None else old
        current = self.scan()
        if old is None:
            return sorted(current)
        changed = []
        for rel in sorted(set(old) | set(current)):
            if rel not in old or rel not in current:
                changed.append(rel)
            elif (old[rel].get("sha") != current[rel].get("sha")
                  or old[rel].get("size") != current[rel].get("size")):
                changed.append(rel)
        return changed

    def closure(self, target: str):
        if target in self._closure_cache:
            return self._closure_cache[target]
        entry = ENTRY_FILES.get(target)
        if entry is None or not entry.is_file():
            self._closure_cache[target] = (set(), True)
            return self._closure_cache[target]
        seen = set()
        unknown = False
        stack = [entry]
        while stack:
            path = stack.pop()
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    found, dynamic = _resolve_import(node, path, self.root)
                    unknown = unknown or dynamic
                    stack.extend(found)
                elif isinstance(node, ast.Call):
                    found, dynamic = _resolve_import(node, path, self.root)
                    unknown = unknown or dynamic
                    stack.extend(found)
        result = ({_rel(path) for path in seen}, unknown)
        self._closure_cache[target] = result
        return result

    def targets_for(self, rel: str):
        """Return (targets, why, warning) for one changed file."""
        rel = rel.replace("\\", "/").lstrip("./")
        if rel in GUARD_OWNED:
            return None, "guard-owned", None
        if rel in NO_RESTART:
            return set(), NO_RESTART[rel], None
        if rel in SHARED_GUARD:
            mode = SHARED_GUARD[rel]
            if mode == "all":
                targets = set(TARGETS)
            else:
                targets = {target for target in TARGETS if rel in self.closure(target)[0]}
            return targets, "shared-with-guard", (
                f"{rel} 也被 guard 引用；子进程会拿到新副本，guard 侧仍是旧副本")
        if rel.endswith((".md", ".json")):
            return set(), "data-only", None
        targets = {target for target in TARGETS if rel in self.closure(target)[0]}
        if targets:
            return targets, "", None
        return None, "not in any daemon closure", None

    def plan(self, changed: Iterable[str], only=None, force_targets=None, mode="reload"):
        changed = sorted({str(rel).replace("\\", "/").lstrip("./") for rel in changed})
        skipped = []
        rejected = []
        warnings = []
        targets = set()
        unknown = False

        if mode == "config":
            force_targets = ["collector", "commands"]
            changed = []
        if force_targets is not None:
            requested = [str(name) for name in force_targets if name]
            bad = [name for name in requested if name not in TARGETS]
            if bad:
                rejected.append({"path": "", "why": f"unknown target(s): {', '.join(bad)}"})
            targets.update(name for name in requested if name in TARGETS)

        for rel in changed:
            if rel in ("pyproject.toml", "uv.lock"):
                rejected.append({
                    "path": rel,
                    "why": "依赖文件变化；先 uv sync，再从 explorer 整栈启动",
                })
                continue
            found, why, warning = self.targets_for(rel)
            if warning:
                warnings.append(warning)
            if found is None and why == "guard-owned":
                rejected.append({
                    "path": rel,
                    "why": "guard-owned；普通 --reload 不替换 guard，请从 explorer 重启 start-bot.pyw",
                })
            elif found is None:
                unknown = True
                warnings.append(f"{rel} 无法映射到入口闭包；按 fail-closed 重启全部")
                targets.update(TARGETS)
            elif not found:
                skipped.append({"path": rel, "why": why or "data-only"})
            else:
                targets.update(found)

        if only:
            only_set = {str(name) for name in only if name}
            bad = [name for name in only_set if name not in TARGETS]
            if bad:
                rejected.append({"path": "", "why": f"unknown target(s): {', '.join(bad)}"})
            filtered = targets & only_set
            for target in sorted(targets - filtered):
                skipped.append({"path": "", "why": f"{target} filtered by --only"})
            targets = filtered

        # A syntax error in any changed Python file rejects the whole batch.
        for rel in changed:
            if not rel.endswith((".py", ".pyw")):
                continue
            path = _abs(rel)
            error = _compile_with_retry(path)
            if error:
                rejected.append({"path": rel, "why": error})

        if rejected:
            targets = set()
        ordered = [target for target in TARGETS if target in targets]
        return RestartPlan(changed=changed, targets=ordered, skipped=skipped,
                           rejected=rejected, warnings=warnings, unknown=unknown)


def _compile_with_retry(path: Path, attempts: int = 3):
    last = None
    for attempt in range(attempts):
        try:
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")
            return None
        except SyntaxError as exc:
            return f"SyntaxError line {exc.lineno}: {exc.msg}"
        except OSError as exc:
            last = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < attempts:
                time.sleep(0.2)
    return last or "FileNotFound"


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
    config = _read_json(WATER_GROUP / "config.json") or {}
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
        self.state = _read_json(_state(STATE_NAME)) or {}
        # A guard restart is a fresh start: quarantine does not survive it.
        # The failed file is still overwritten by _write_failed on a new failure.
        self.recover_inflight()
        self._write_state()

    # ---- mailbox -----------------------------------------------------
    def recover_inflight(self):
        path = _inflight_path()
        raw = _read_json(path)
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
        _write_json(_result_path(nonce), payload)

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
        raw = _read_json(_inflight_path())
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
        code = 0 if not failed and not deferred else (4 if failed else 0)
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
        try:
            return _state("wg_pause").exists()
        except OSError:
            return False

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
            elif status == "failed":
                self.deferred.discard(target)
                self._record_failure(target, why or "deferred retry failed")

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
        _write_json(_state(FAILED_NAME), {"v": 1, "at": time.time(), "targets": targets})

    def _write_state(self, **extra):
        self.state.update({
            "v": 1,
            "watch": self.watch,
            "interval": self.interval,
            "last_at": self.state.get("last_at"),
            "last_elapsed": self.state.get("last_elapsed"),
            "last_files": self.state.get("last_files"),
            "last_restarted": self.state.get("last_restarted"),
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
        _write_json(_state(STATE_NAME), self.state)


def status_line() -> str:
    state = _read_json(_state(STATE_NAME)) or {}
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
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(request, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def _wait_result(nonce: str, wait: float, guard: int):
    path = _result_path(nonce)
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        raw = _read_json(path)
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
        failed = _read_json(_state(FAILED_NAME)) or {}
        if failed.get("targets"):
            print("quarantine: " + ", ".join(sorted(failed["targets"])))
        return 0 if _read_json(_state(STATE_NAME)) else 2

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
