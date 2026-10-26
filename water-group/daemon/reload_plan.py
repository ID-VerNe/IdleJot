"""Hot-reload analysis: paths, state, scanning, import closure, planning.

The analysis half of the former ``reload.py`` (issue #11, section PD).  Nothing
here touches a process: it snapshots the watched files, maps changed files onto
child processes through an AST import closure, and classifies every change.
Restart execution lives in ``reload_exec``; the CLI and the historical import
surface stay in ``reload``.

Design notes carried over from the original module:

* Changes are mapped to child processes by an AST import closure, not by a
  hand-written file table.  A new file that nobody imports fails the
  ``check_hot_reload.py`` coverage gate, forcing an explicit classification.
* A syntax error rejects the whole batch.  The old processes keep running.
"""
from __future__ import annotations

import ast
import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from wg_daemon import read_json, write_json  # noqa: E402  (issue #11 PB: 助手收口)

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
    # issue #37: up.py is only a facade now; the three modules it imports at module
    # level are the guard's own code just the same. A child restart cannot swap the
    # copy the resident guard already imported, so changing them needs the same
    # full stop/start as up.py itself.
    "water-group/daemon/wg_up_probe.py",
    "water-group/daemon/wg_up_wake.py",
    "water-group/daemon/wg_up_stack.py",
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
    # PD2a 后 commands 在插件注册期 import 到它（plugins.load_all →
    # followup_plugin），但守护进程从不派发窗口事件——reply_sent / wake_scan /
    # window_status 只从 reply / watch / up 三个一次性 CLI 发出，改了下次调用
    # 就是新副本；守护进程里那份旧代码是死的，重启它没有意义。
    "water-group/daemon/followup.py": "CLI-only at runtime (reply/watch/up); daemon copy never invoked",
    "water-group/daemon/wg_clock.py": "shared clock helper for guard/CLI, no daemon imports it",
    "water-group/daemon/wg_quotes.py": "shared quote renderer for guard/CLI, no daemon imports it",
    "water-group/daemon/wg_text.py": "shared body renderer for guard/CLI, no daemon imports it",
    "water-group/daemon/wg_aliases.py": "shared alias renderer for CLI (watch/reply), no daemon imports it",
    "water-group/daemon/wg_budget.py": "shared wait-budget constants for CLI (reply/listen), no daemon imports it",
    "water-group/daemon/wg_bubbles.py": "shared bubble-cap/split helper for CLI (reply), no daemon imports it",
    # issue #132：发送失败的诊断 / 禁言探测 / 失败账本，只被 reply.py（一次性 CLI）
    # import；守护进程里没有副本，改了下次调用自然生效。
    "water-group/daemon/wg_send_failure.py": "shared send-failure diagnostics for CLI (reply), no daemon imports it",
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
    return read_json(_state(GUARD_NAME)) or {}


def write_guard_state(pid=None, build="", sandbox=None):
    payload = {
        "v": 1,
        "pid": int(pid or os.getpid()),
        "started_at": time.time(),
        "sandbox": sandbox_dll() if sandbox is None else sandbox,
        "build": build,
    }
    write_json(_state(GUARD_NAME), payload)
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
        raw = read_json(self.snapshot_path)
        if not isinstance(raw, dict):
            return None
        files = raw.get("files")
        if not isinstance(files, dict):
            return None
        return {str(k): v for k, v in files.items() if isinstance(v, dict)}

    def save(self, files):
        return write_json(self.snapshot_path, {
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
