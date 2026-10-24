"""daemon 公共助手：日志、心跳、进程探测、JSON 状态读写（issue #11 PB）。

六个常驻脚本（collector / commands / db_keeper / sixty / supervisor /
watchdog）各自手写了一份 `log()` / `beat()`，两份 `pid_alive()`，reload 又
自带一对 `_read_json` / `_write_json`。同一语义多个真相的代价已经现实验证：
commands 版 `pid_alive` 以「OpenProcess 成功」判活，对已退出但句柄未关的
进程会误判活；reload 版多 `int()` 容错与 `GetExitCodeProcess == STILL_ACTIVE`
判定。本模块把四类助手各收成一份定义，判活以更严格的 reload 版为基准。

为什么是 flat import 而不是包级模块：这些脚本都以
`python water-group/daemon/xxx.py` 直接跑，sys.path[0] 是 daemon 目录自身，
所以 `from wg_daemon import logger` 即可——与 `wg_paths.py` 同一模式
（见其 docstring）。经 bridge 以 `water_group.daemon.wg_daemon` 访问会把这个
文件加载两遍，不要那样用。
"""
import json
import os
import secrets
import time
from pathlib import Path


def logger(path, *, tag=None):
    """返回向 *path* 追加一行的 ``log(line)``。

    ``tag`` 生成 ``[tag] `` 前缀（watchdog 用自己的日志 tag 区分来源）。
    父目录恒建：collector 旧版缺这一步，日志目录被清掉后它静默丢日志，
    而另外四家本来就会建。写失败一律吞掉——日志通道不该拖垮守护本体
    （sixty.py 旧注释：「日志写不出去不该让指令失败」）。
    """
    def log(line):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                prefix = f"[{tag}] " if tag else ""
                fh.write(f"{prefix}{time.strftime('%m-%d %H:%M:%S')} {line}\n")
        except OSError:
            pass
    return log


def beater(path):
    """返回写心跳时间戳的 ``beat()``（父目录恒建，写失败吞掉）。"""
    def beat():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(int(time.time())), encoding="utf-8")
        except OSError:
            pass
    return beat


def pid_alive(pid) -> bool:
    """True when *pid* names a live process.  Never sends a signal.

    以 reload 版为基准（issue #11 PB）：``int()`` 容错 + Windows 上
    ``GetExitCodeProcess == STILL_ACTIVE`` 判定。只查句柄能否打开会把
    已退出、句柄未关的进程误判成活——commands 的锁接管判据因此可能
    永远等不到锁释放。
    """
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
        except Exception:  # noqa: BLE001  探测失败按死处理，让锁可被接管
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_json(path: Path):
    """Read one JSON document; ``None`` when missing or malformed."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def write_json(path: Path, payload) -> bool:
    """Write one JSON document atomically, leaving no half file for a reader."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False
