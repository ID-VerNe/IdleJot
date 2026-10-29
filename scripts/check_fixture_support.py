"""Shared isolated fixtures, independent of the optional terminal UI."""
import atexit
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
DAEMON = ROOT / "water-group" / "daemon"
LOOPBACK = frozenset({'127.0.0.1', 'localhost', '::1'})
_SCRATCH_HOME = None

# 数据根必须在进程级钉住，不能只靠 main() 里的 patch.dict。
# memory_store.py 的 WG_HOME / DEFAULT_DB_PATH 是 import 期求值的（issue #11 PA 搬家），
# 而 chatlocal/mcp_chat.py 的 get_memory_engine() 全是函数内延迟导入
# （399/547/868/1005/1018/1042 行），事件处理又跑在 onebot_events.py:169 的
# 后台线程里：补丁退出后线程才第一次 import，环境已恢复成「补丁前的样子」，
# 常量于是落回真实 ~/.water-group，夹具档案就这样写进了实机记忆库
# （issue #89，#79 的第二例）。
#
# 这里在模块顶层把 WATER_GROUP_HOME 钉进临时目录：patch.dict 进入时会把这
# 个值当作「原值」保存，退出时恢复的也是它，所以后台线程无论多晚才首次
# import，读到的都是临时路径。刻意**不**在这里 import memory_engine：有些
# 门禁（check_sixty.py、check_text_truncation.py）会在 import 本模块之后才
# 设自己的 IDLEJOT_WG_MEMORY_DB，提前冻结会把它们的临时库顶掉。
_PINNED_HOME = Path(tempfile.mkdtemp(prefix='tulpa-fixture-home-')) / 'home'
_PINNED_HOME.mkdir(parents=True, exist_ok=True)
atexit.register(shutil.rmtree, _PINNED_HOME.parent, True)
_LIVE_HOME = (Path.home() / '.water-group').resolve()


def _inside_live_home(value):
    """True when `value` resolves into the real ~/.water-group tree."""
    if not value:
        return True  # unset -> memory_engine falls back to the live root
    try:
        path = Path(value).resolve()
    except OSError:
        return False
    return path == _LIVE_HOME or _LIVE_HOME in path.parents


_preferred_home = os.environ.get('WATER_GROUP_HOME')
os.environ['WATER_GROUP_HOME'] = (
    str(_PINNED_HOME) if _inside_live_home(_preferred_home) else _preferred_home
)


def isolated_env(home=None):
    """Clear user configuration while retaining Windows TLS/runtime roots.

    The data root (``WATER_GROUP_HOME``) is always pinned. ``home`` says where;
    when omitted a throwaway directory is created and removed at exit.

    Pinning it is not optional: the SQLite stores resolve the data root at import
    or first use, so an unset variable silently falls back to the real
    ``~/.water-group``. That is exactly how fixture rows used to land in the live
    memory database (issue #14) -- the hardcoded repo path merely masked it.

    Note this context manager only covers the **calling thread's main flow**.
    Import-time constants and background threads (event receivers, timers) can
    outlive it, which is why the data root is also pinned at module level above.
    A suite that spawns threads which import chatlocal lazily is safe either way.
    """
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {'SYSTEMROOT', 'WINDIR'}}
    env['WATER_GROUP_HOME'] = str(home if home is not None else _scratch_home())
    return env


def _scratch_home():
    """Lazily created data root for suites that do not already have a temp dir."""
    global _SCRATCH_HOME
    if _SCRATCH_HOME is None:
        _SCRATCH_HOME = Path(tempfile.mkdtemp(prefix='tulpa-home-'))
        atexit.register(shutil.rmtree, _SCRATCH_HOME, True)
    return _SCRATCH_HOME


def bootstrap_paths(*, daemon=True, scripts=True, root=True):
    """One-time ``sys.path`` setup for a check script (issue #169 PE).

    Every ``scripts/check_*.py`` opened with the same three ``sys.path.insert``
    calls -- this directory (so it can ``import check_fixture_support``), the
    repo root (so it can ``import chatlocal`` / ``import water_group``), and
    ``water-group/daemon`` (so it can ``import reply`` / ``import wg_text``).
    The three were inlined 154 times across 85 files; this is the single home.

    Returns ``ROOT`` so the caller does not redefine it. ``scripts`` is on
    ``sys.path[0]`` already when a check runs as its own process
    (``python scripts/check_x.py``), so the ``scripts`` insert is a safety net
    for in-process imports (``check_mcp_chat`` is imported by siblings, and a
    sibling's process may have a different ``sys.path[0]``); keep it on by
    default. Pass ``daemon=False`` for suites that only touch ``chatlocal/``.
    """
    if scripts and str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    if root and str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if daemon and str(DAEMON) not in sys.path:
        sys.path.insert(0, str(DAEMON))
    return ROOT


def run_cases(cases, *, setup=None, prefix=True, catch=True):
    """Run a list of self-test callables, print one line each, return exit code.

    The ``check_*.py`` suites collected cases into ``CASES`` and looped them
    with one of three near-identical ``main()`` bodies (issue #169 PE). The
    only real differences are whether failures are caught, the pass/fail
    marker wording, and a per-suite ``setup()`` call before the loop. Those
    three are the parameters; the loop is this function.

    Each case returns truthy = pass, falsy = fail. With ``catch=True`` a case
    that raises is recorded as a failure (with the exception) instead of
    aborting the suite -- a broken gate must count as red, not silently skip
    the rest (issue #62). ``prefix=True`` prints ``pass_marker``/``fail_marker``
    in the ``checkmark`` style (``✓``/``✗``) used by the gates that report in
    Chinese; ``prefix=False`` uses the ``PASS``/``FAIL`` English wording.
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if setup is not None:
        setup()
    pass_marker = "✓ " if prefix else "PASS "
    fail_marker = "✗ " if prefix else "FAIL "
    tail_ok = "全部通过" if prefix else "OK"
    bad = 0
    for fn in cases:
        try:
            ok = fn()
        except Exception as exc:  # noqa: BLE001  门禁自己坏了也算红
            if not catch:
                raise
            print(f"{fail_marker}{fn.__name__}: {type(exc).__name__}: {exc}")
            bad += 1
            continue
        if ok:
            print(f"{pass_marker}{fn.__name__}")
        else:
            print(f"{fail_marker}{fn.__name__}")
            bad += 1
    print(tail_ok if not bad else f"{bad} 项失败" if prefix else f"{bad} case(s) failed")
    return 1 if bad else 0


# `run_python` owns the text-decoding arguments, so a caller passing one is a
# mistake: silently honouring it would put the locale back in charge (issue #240).
_DECODING_KWARGS = ('text', 'universal_newlines', 'encoding', 'errors')


def run_python(args, *, cwd=None, env=None, timeout=90, **kwargs):
    """Run a Python child here and read its output as UTF-8 text (issue #240).

    Every daemon entry point calls ``winconsole.ensure_utf8_stdio()`` and so
    writes UTF-8 bytes no matter what the host locale is (AGENTS.md §2.5). A
    bare ``subprocess.run(..., text=True)`` decodes with
    ``locale.getpreferredencoding()`` -- cp936 on a Chinese Windows box -- and on
    the first non-ASCII byte the reader thread raises and ``stdout`` comes back
    as ``None``. The child had succeeded; the assertion then died on
    ``"..." in None``.

    That is a gate that is green on CI (UTF-8 locale) and red on the developer's
    machine, and the red says nothing about what the gate is checking: three
    check scripts had this shape (#240). Pinning the encoding here is what makes
    "本地绿" mean the same thing as "CI 绿". ``errors="replace"`` is deliberate:
    one stray byte should not throw away the whole output a failure report needs.

    ``args`` are the child's argv *after* the interpreter; the interpreter is
    prepended. Returns the ``CompletedProcess``.
    """
    for taken in _DECODING_KWARGS:
        if taken in kwargs:
            raise TypeError(
                f'run_python owns {taken!r} (fixed to UTF-8); '
                'pass it to subprocess.run instead of routing it through here')
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=timeout,
        **kwargs,
    )


@contextmanager
def temp_dir(prefix='tulpa-test-', dir=None):
    """Scratch directory for fixtures, isolated from the working tree.

    ``dir`` lands the temp dir under a parent (``ROOT/'.tmp'``) when a suite
    cares about that location; ``None`` (default) uses the OS temp root.
    """
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=dir))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def scratch_dir(prefix='tulpa-scratch-'):
    """Module-level scratch dir that outlives any ``with`` block.

    For suites that pin ``WATER_GROUP_HOME`` / ``IDLEJOT_WG_STATE`` at module
    top so background threads importing chatlocal lazily (after ``patch.dict``
    exits) read the temp root (issue #79/#89). ``with temp_dir()`` would close
    before those threads import; this registers atexit cleanup instead. The
    dir lives until interpreter shutdown. Returns ``Path``.
    """
    path = Path(tempfile.mkdtemp(prefix=prefix))
    atexit.register(shutil.rmtree, path, True)
    return path


def network_guard(ports):
    """Patch helper: allow loopback connections to ``ports`` only.

    ``ports`` is kept by reference so suites that bind an extra port after
    installing the guard stay allowed. Returns ``(restrict, calls)``.
    """
    allowed = ports if isinstance(ports, set) else set(ports)
    real_connect = socket.create_connection
    calls = []

    def restrict(address, *args, **kwargs):
        try:
            permitted = address[0] in LOOPBACK and int(address[1]) in {int(port) for port in allowed}
        except (TypeError, ValueError):
            permitted = False
        assert permitted, ('non-fixture connection blocked', address)
        calls.append(address)
        return real_connect(address, *args, **kwargs)

    return restrict, calls
