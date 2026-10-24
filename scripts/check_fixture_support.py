"""Shared isolated fixtures, independent of the optional terminal UI."""
import atexit
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
LOOPBACK = frozenset({'127.0.0.1', 'localhost', '::1'})
_SCRATCH_HOME = None

# 数据根必须在进程级钉住，不能只靠 main() 里的 patch.dict。
# memory_engine.py:31,34-36 的 WG_HOME / DEFAULT_DB_PATH 是 import 期求值的，
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


@contextmanager
def temp_dir(prefix='tulpa-test-'):
    """Scratch directory for fixtures, isolated from the working tree."""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


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


@contextmanager
def management_http(app):
    import uvicorn
    sock = socket.socket(); sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    # TestClient owns application startup/shutdown in the caller.
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic()+5
        while not server.started and time.monotonic() < deadline: time.sleep(.02)
        if not server.started: raise AssertionError('Management HTTP did not start')
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        thread.join(5); sock.close()
        if thread.is_alive(): raise AssertionError('Management HTTP did not stop')
