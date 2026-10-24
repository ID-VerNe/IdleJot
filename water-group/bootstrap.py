import os
import sys
import time
import socket
import subprocess
import argparse
import json
from pathlib import Path
import requests

ROOT_DIR = Path(__file__).resolve().parent.parent

# A console child of a console-less parent gets a brand-new console window on
# Windows, which is the black box that flashes whenever the watchdog shells out.
# Every probe here runs from a background daemon with no console, so each one
# would otherwise pop a window; winconsole.run_hidden suppresses it without
# touching the redirected stdout we actually read.

# Win32 SetErrorMode flags. SEM_NOGPFAULTERRORBOX is the one that suppresses the
# "X has stopped working" dialog; SEM_FAILCRITICALERRORS keeps the related
# critical-error box away too.
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOGPFAULTERRORBOX = 0x0002

# When launched as a script (`python water-group/bootstrap.py`) this file is
# __main__, so the repository root is not on sys.path yet and the bridge
# package cannot be found. Importing through the bridge is what keeps every
# water-group file loaded under exactly one module name.
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT_DIR))
# 本地数据根（~/.water-group）的单一事实来源在 daemon/wg_paths.py，这边 insert
# 后 flat import，避免第二份路径规则各自漂移。
sys.path.insert(0, str(Path(__file__).resolve().parent / "daemon"))
import winconsole  # noqa: E402  (探针开窗抑制，一次收口)
from wg_config import config_path  # noqa: E402  (config.json 路径单一规则，issue #51)
from wg_up_wake import login_info  # noqa: E402  (登录态探针，与 --status 同一实现，issue #50)
from wg_paths import logs_dir, personas_dir, state_dir

NAPCAT_DIR = ROOT_DIR / "tools" / "napcat-shell"
# 与 wg_config 同一份路径规则（env 覆盖同族）：本文件自己读 config.json 的三处
# （configured_account / qq_extra_args / ensure_config）不许各写一份路径。
CONFIG_FILE = config_path()
CONFIG_EXAMPLE = ROOT_DIR / "water-group" / "config.example.json"
STATUS_FILE = state_dir() / "status.json"

# 最近一次注入的启动器标识（issue #46）。命令行指纹在「多实例并存」时不可靠：
# 实测重拉出来的主进程只有 `--disable-gpu`，没有 `-q <账号>`，所以按进程表 + 命令行
# 判定会恒报 0，上一实例就这么活到了下一次注入。记下「这次我拉起的到底是谁」，
# 下一轮才有得比对。
LAUNCH_FILE = state_dir() / "napcat_launch.json"
# 本进程最近一次真正 spawn 的启动器记录；ensure_napcat 因「已在线」提前返回时不写，
# 这样 watchdog 日志里的 pid 一定对得上本次注入，而不是上一轮的残留。
LAST_LAUNCH = None

def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

def ensure_config():
    if not CONFIG_FILE.exists():
        if CONFIG_EXAMPLE.exists():
            import shutil
            shutil.copy2(CONFIG_EXAMPLE, CONFIG_FILE)
            print(f"[Init] 已从 config.example.json 创建初始配置文件: {CONFIG_FILE}")
        else:
            print(f"[Warn] 未找到配置文件模板: {CONFIG_EXAMPLE}")

LOGS_DIR = logs_dir()

def get_python_exe():
    venv_py = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    if venv_py.exists():
        return str(venv_py)
    return sys.executable

def silence_crash_dialogs():
    """Stop Windows from putting a "QQ has stopped working" box on the desktop.

    Every crash of an injected instance used to leave one of these behind, and
    because the watchdog re-injects on its own schedule the boxes piled up faster
    than anyone dismissed them -- ten were sitting on screen at once. Error mode
    is inherited by child processes, so setting it here covers QQ and everything
    it starts, while leaving the user's own applications reporting errors as usual.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)
    except Exception:  # noqa: BLE001
        pass


def sandbox_dll():
    """Name of the agent-sandbox DLL injected into this process, or "".

    The command sandbox injects ``tsbx.dll`` into every process it spawns, and the
    injection is inherited down the whole chain: up.py -> supervisor -> watchdog ->
    NapCatWinBootMain -> QQ.exe. QQ does not survive it. Every injected instance
    dies roughly twenty seconds after launch with 0xC06D007F -- the delay-load
    helper's ERROR_PROC_NOT_FOUND -- because the hook answers one of its imports
    with "no such export". The visible symptom is a crash dialog per instance and a
    watchdog that relaunches forever.

    Nothing in the repository can undo that, so the stack has to be started from
    Explorer (start-bot.vbs); this check exists so the logs say so instead of
    leaving the next reader to rediscover it from a stack trace.
    """
    if os.name != "nt":
        return ""
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetModuleHandleW("tsbx.dll")
        return "tsbx.dll" if handle else ""
    except Exception:  # noqa: BLE001
        return ""


def start_process_detached(cmd, cwd=None, log_name=None, env=None):
    """Spawn a long-lived child with a windowless console, appending its raw output.

    The flag is NO_WINDOW (``winconsole.NO_WINDOW``), not DETACHED_PROCESS. Both
    keep the child from inheriting the caller's console (the 936 window from issue
    #4), but only one of them also keeps the *grandchildren* quiet:

      NO_WINDOW          the child gets its own console with no window. Anything
                         it starts later inherits that console and stays invisible
                         even without a flag of its own.
      DETACHED_PROCESS   Windows gives the child no console, and a console program
                         (cmd.exe, python.exe) allocates one on startup -- with a
                         window. Once per launch, on the desktop (issue #39's
                         residue).

    Measured 2026-10-23 with a window hook: `cmd /c ping` under DETACHED flashes
    Windows Terminal, under NO_WINDOW it is silent, and an unflagged `cmd` started
    *from* a NO_WINDOW child is silent too. A probe child reports
    `(GetConsoleCP(), GetConsoleWindow())` as `(936, 0)` under NO_WINDOW and
    `(936, <hwnd>)` under DETACHED -- the console exists either way, only the
    window differs.

    DETACHED never bought anything else here: it does not help a child outlive the
    shell that started it (job objects do, see up.py's header), and nothing in the
    launcher chain needs "no console handle" specifically.

    The log handle is opened in binary on purpose. `Popen` only takes the
    handle's `fileno()`; the child writes bare bytes into that descriptor, so
    `encoding="utf-8"` on the Python file object promised something it could not
    keep. It also lied in a way that hid a real problem: `cmd.exe` (the .bat
    launcher's echo/pause) writes 936 while NapCat writes UTF-8, so one file ends
    up with two encodings (issue #4). Read it back with
    `wg_logs.decode_log_line`, which sniffs per line.
    """
    silence_crash_dialogs()
    out_file = subprocess.DEVNULL
    opened = None
    if log_name:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        opened = open(LOGS_DIR / log_name, "ab")
        out_file = opened
    try:
        # popen_hidden owns the window flag; passing only the process-group bit
        # keeps this call site free of hand-rolled constants (see
        # scripts/check_no_console_child.py).
        return winconsole.popen_hidden(
            cmd,
            cwd=cwd or str(ROOT_DIR),
            env=env,
            stdout=out_file,
            stderr=out_file,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
    finally:
        # The child owns a duplicate of the handle; keeping ours open would leak
        # one descriptor per watchdog relaunch.
        if opened is not None:
            opened.close()

QQ_INSTALL_CANDIDATES = (
    Path(r"C:\Program Files\Tencent\QQNT\QQ.exe"),
    Path(r"C:\Program Files (x86)\Tencent\QQNT\QQ.exe"),
)


def configured_account():
    """Bot account from config.json, used for NapCat quick login."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            account = str(json.load(f).get("self_account", "")).strip()
    except (OSError, ValueError):
        return ""
    return account if account.isdigit() else ""


def registry_qq_exe():
    """QQ.exe from the uninstall entry; empty when the registry is unavailable."""
    try:
        import winreg
    except ImportError:
        return None
    key_path = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            raw, _ = winreg.QueryValueEx(key, "UninstallString")
    except OSError:
        return None
    candidate = Path(str(raw).strip().strip('"')).with_name("QQ.exe")
    return candidate if candidate.exists() else None


def resolve_qq_exe():
    """Locate QQ.exe without requiring the registry (it can be policy-blocked)."""
    found = registry_qq_exe()
    if found:
        return found
    for candidate in QQ_INSTALL_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def wait_napcat_ports(seconds=12):
    for _ in range(seconds):
        time.sleep(1)
        if is_port_open(43000) and is_port_open(43001):
            return True
    return False


def wait_napcat_login(seconds=30):
    """端口就绪之后再确认登录态：端口在从来不是登录态在（issue #50）。

    get_login_info 是唯一能区分「桥活着」与「账号登上去了」的探针——端口由
    NapCat 的 node sidecar 占着，QQ 主进程登不上世（扫码被拒、同账号冲突）时
    端口照样在。这里与 up.py --status 用同一个实现（wg_up_wake.login_info），
    判定标准不许有第二份。
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if login_info():
            return True
        time.sleep(2)
    return False


def ports_up_login_missing():
    """端口起来了但登录态没确认时的统一说辞（issue #50）。

    三种启动路径（直接注入 / bat / bat 宽限）共用：文案必须指向可动作的下一步
    ——扫码或冲突端，以及能看到 Login Error 的日志。
    """
    return ("端口已就绪但登录态未确认（可能需要扫码，或同账号冲突/授权被拒；"
            "看 ~/.water-group/logs/napcat.log 的 Login Error，"
            "排查步骤见 doc/NAPCAT_TROUBLESHOOTING.md）")


LAUNCHER_OLD_SWITCH = "--enable-logging"
LAUNCHER_NEW_SWITCH = "--disable-gpu"


def ensure_launcher_patch():
    """Make the injected QQ start with --disable-gpu.

    NapCatWinBootMain.exe builds QQ.exe's command line itself and only reads its
    first three arguments (QQ.exe, hook.dll, uin). Anything appended after them is
    dropped -- proven by passing --tulpa-probe-xyz, which never showed up in the
    child command line and also cost the `-q <uin>` quick-login switch, so a
    `qq_args` entry can never reach QQ.

    The single switch the launcher does inject is "--enable-logging". Rewriting
    that literal inside the binary is therefore the only way to hand QQ a flag it
    will actually receive. Both strings are 16 and 13 characters, so the
    replacement is space-padded and the file keeps its exact size:

        QQ.exe --enable-logging  -q <self_account>   (stock)
        QQ.exe --disable-gpu     -q <self_account>   (patched)

    Why the GPU has to go: the bot instance and the user's own QQ both resolve
    user-data-dir to %APPDATA%\\QQ. QQ obtains that path through the Shell API, so
    overriding the APPDATA env var does nothing (an isolated .qqprofile stayed
    empty through a full launch), and Chromium's crash report lists both accounts
    under one profile. Two instances on one GPUCache kill the second GPU process
    within minutes -- "GPU process isn't usable. Goodbye." -- and QQ pops a crash
    dialog each time. With --disable-gpu no GPU process is spawned at all, and
    NapCat is pure Node logic that never needed one.

    Runs on every launch because a NapCat update restores the stock binary.
    """
    exe = NAPCAT_DIR / "NapCatWinBootMain.exe"
    if not exe.exists():
        return False
    try:
        data = exe.read_bytes()
    except OSError:
        return False
    old = LAUNCHER_OLD_SWITCH.encode("utf-16le")
    new = LAUNCHER_NEW_SWITCH.ljust(len(LAUNCHER_OLD_SWITCH)).encode("utf-16le")
    if old not in data:
        # Already patched, or a build that words the switch differently.
        return new in data
    backup = NAPCAT_DIR / "NapCatWinBootMain.exe.orig"
    if not backup.exists():
        try:
            backup.write_bytes(data)
        except OSError:
            pass
    try:
        exe.write_bytes(data.replace(old, new, 1))
    except OSError as exc:
        # A running launcher holds the file; the next relaunch retries.
        print(f"[Warn] 无法写入启动器补丁: {exc}")
        return False
    print("[启动] 已让注入实例走 --disable-gpu（避免与自己的 QQ 抢同一份 GPUCache）")
    return True


def launch_napcat_direct(qq_exe, account=""):
    """Inject NapCat without the .bat launcher, which hard-depends on reg query."""
    ensure_launcher_patch()
    env = os.environ.copy()
    env.update(
        NAPCAT_PATCH_PACKAGE=str(NAPCAT_DIR / "qqnt.json"),
        NAPCAT_LOAD_PATH=str(NAPCAT_DIR / "loadNapCat.js"),
        NAPCAT_INJECT_PATH=str(NAPCAT_DIR / "NapCatWinBootHook.dll"),
        NAPCAT_LAUNCHER_PATH=str(NAPCAT_DIR / "NapCatWinBootMain.exe"),
        NAPCAT_MAIN_PATH=str(NAPCAT_DIR / "napcat.mjs"),
    )
    cmd = [str(NAPCAT_DIR / "NapCatWinBootMain.exe"), str(qq_exe), str(NAPCAT_DIR / "NapCatWinBootHook.dll")]
    if account:
        cmd.append(account)
    cmd.extend(qq_extra_args())
    proc = start_process_detached(cmd, cwd=str(NAPCAT_DIR), log_name="napcat.log", env=env)
    return _record_launch(proc, account, "direct")


def qq_extra_args():
    """Extra switches handed to QQ.exe.

    Read from config.json (`qq_args`) first so the setting survives a restart and
    is not lost when a fresh agent runs up.py without the environment it was first
    started with. `TULPA_QQ_ARGS` stays as an override for one-off experiments.

    Why this exists: the injected instance dies in a loop on this box with
    "GPU process exited unexpectedly: exit_code=-1073741819" followed by
    "GPU process isn't usable", about every ten minutes. --disable-gpu stops
    Chromium from spawning a GPU process at all, which removes the failure mode
    entirely. A bot does not need hardware acceleration.

    Keep the list short: NapCatWinBootMain reads argv[3] as the account and is not
    documented to forward anything beyond it, so each extra switch is a gamble that
    quick login still works. Verify get_login_info reports the bot account after
    changing this.
    """
    raw = os.environ.get("TULPA_QQ_ARGS", "")
    if raw.strip():
        return [part for part in raw.split() if part]
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            args = json.load(f).get("qq_args") or []
    except (OSError, ValueError):
        return []
    return [str(a) for a in args if str(a).strip()]


def _record_launch(proc, account, mode):
    """记下本次 spawn 的启动器，供下一轮按身份找出「这次拉起的实例」（issue #46）。

    `proc.pid` 是 NapCatWinBootMain（bat 降级时是 cmd.exe）。QQ 主进程是它的
    子进程，父进程先退时 WMI 里的 ParentProcessId 仍是这个 pid，所以这份记录
    就是钥匙。pid 缺失（门禁替身、spawn 抛错）时不写：没有记录好过一份对不上
    号的记录。
    """
    global LAST_LAUNCH

    pid = getattr(proc, "pid", None)
    if not pid:
        return None
    record = {
        "launcher_pid": int(pid),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "started_epoch": int(time.time()),
        "account": account or "",
        "mode": mode,
    }
    try:
        LAUNCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LAUNCH_FILE.with_name(LAUNCH_FILE.name + ".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, LAUNCH_FILE)
    except OSError:
        pass
    LAST_LAUNCH = record
    return record


def last_launch():
    """本进程最近一次真正 spawn 的启动器记录；None = 这一轮没有注入。

    `ensure_napcat()` 因「已在线」提前返回时不写记录，日志里的 pid 因此一定是
    本次注入的，而不是上一轮留在 state/ 里的残影。
    """
    return LAST_LAUNCH


def _qq_processes(launcher_pid=None):
    """一次 WMI 查询取出全部 QQ.exe（以及记录里的启动器本身）。

    返回 `[{pid, parent, name, created, command_line}]`；`created` 是进程创建
    时刻（epoch）。它和父 pid 一起用，挡的是 pid 回收：只有创建时间不早于本次
    注入的 QQ 才算本次实例。查询失败回空列表，调用方据此说「没找到」，不猜。
    """
    filt = "Name='QQ.exe'" if launcher_pid is None else f"Name='QQ.exe' or ProcessId={launcher_pid}"
    script = (
        f"Get-CimInstance Win32_Process -Filter \"{filt}\" | "
        "ForEach-Object { \"$($_.ProcessId)`t$($_.ParentProcessId)`t"
        "$($_.CreationDate.ToString('yyyy-MM-dd HH:mm:ss'))`t$($_.Name)`t$($_.CommandLine)\" }"
    )
    try:
        out = winconsole.run_hidden(["powershell", "-NoProfile", "-NonInteractive",
                                     "-Command", script], capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for line in out.stdout.decode("utf-8", "ignore").splitlines():
        parts = line.split("\t", 4)
        if len(parts) < 4 or not parts[0].strip().isdigit():
            continue
        try:
            created = time.mktime(time.strptime(parts[2].strip(), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            created = 0.0
        rows.append({
            "pid": int(parts[0]),
            "parent": int(parts[1]) if parts[1].strip().isdigit() else 0,
            "created": created,
            "name": parts[3].strip(),
            "command_line": parts[4] if len(parts) > 4 else "",
        })
    return rows


def _launch_instance_pids(record, rows):
    """记录里那次注入还活着的 QQ 主进程（父 pid + 创建时间双重比对）。"""
    launcher_pid = record["launcher_pid"]
    started = record.get("started_epoch") or 0
    return [str(row["pid"]) for row in rows
            if row["parent"] == launcher_pid and row["pid"] != launcher_pid
            and (not started or row["created"] >= started - 60)]


def _load_launch_record():
    """只读 state/ 里的启动记录；文件缺失或字段不完整时返回 None。"""
    try:
        record = json.loads(LAUNCH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    try:
        record["launcher_pid"] = int(record.get("launcher_pid"))
    except (TypeError, ValueError):
        return None
    return record


def read_launch_record():
    """最近一次注入的记录，附带现读的进程事实（issue #46）。

    `instance_pids` 是本次注入还活着的 QQ 主进程，`launcher_alive` 只作为日志
    提示（不参与杀谁的决定）。返回 None 表示「没有记录可比对」，与「记录在、
    实例已死」是两件事——连续 killed 0 时，watchdog 靠这个区分同源失败。
    """
    record = _load_launch_record()
    if not record:
        return None
    rows = _qq_processes(record["launcher_pid"])
    record["instance_pids"] = _launch_instance_pids(record, rows)
    expected = {"direct": "napcatwinbootmain.exe", "bat": "cmd.exe"}.get(record.get("mode"), "")
    record["launcher_alive"] = any(
        row["pid"] == record["launcher_pid"]
        and (not expected or row["name"].lower() == expected)
        for row in rows
    )
    return record


def injected_instance_pids():
    """现在活着的注入实例 pid：本次注入的 ∪ `-q <账号>` 认出来的，去重。

    身份优先（issue #46）：`-q` 只在主进程命令行确实带着它时才有效，实测重拉
    出来的进程只带 `--disable-gpu`，所以只认标记会漏掉正抢同一份 user-data-dir
    的那一个。两个判据共用一次 WMI 查询。
    """
    record = _load_launch_record()
    rows = _qq_processes(record["launcher_pid"] if record else None)
    pids = _launch_instance_pids(record, rows) if record else []
    account = configured_account()
    if account:
        marker = f" -q {account}"
        for row in rows:
            pid = str(row["pid"])
            if marker in (row["command_line"] or "") and pid not in pids:
                pids.append(pid)
    return pids


def count_injected_instances():
    """只读：现在有几个注入实例（`--stop` 收前的计数口径，issue #46）。"""
    return len(injected_instance_pids())


def kill_stale_instances():
    """Kill every QQ instance this launcher started, whole tree.

    Every boot attempt spawns a QQ process. When a boot stalls — waiting for a QR
    scan, for example — the old process stays alive, the next attempt adds one
    more, and several instances end up sharing one user-data-dir. Their GPU
    processes then fight, crash in a loop, and QQ dies with "GPU process isn't
    usable", which reads like a network outage from the outside.

    Identification used to be `--enable-logging`, because that was the one switch
    NapCatWinBootMain injected. Rewriting that literal to `--disable-gpu` silently
    broke the match: nothing was recognised, nothing was killed, and ten orphan
    instances piled up at ~190 MB each while every watchdog restart added another.

    `-q <uin>` was the next marker and it is not durable either: an instance the
    watchdog relaunched carries only `--disable-gpu` on its command line (observed
    2026-10-24, issue #46), so the same `killed 0` came back while two instances
    coexisted. The launch record is what actually identifies them: the pid we
    spawned, matched against QQ's ParentProcessId, with a creation-time check so a
    recycled pid cannot drag in a stranger.
    """
    pids = injected_instance_pids()
    killed = 0
    for pid in pids:
        try:
            # /T takes the whole tree: killing only the main process leaves the
            # renderer and network utilities running against the same profile,
            # which is how a "cleaned" instance kept holding locks.
            winconsole.run_hidden(["taskkill", "/F", "/T", "/PID", pid],
                                  capture_output=True, timeout=15)
            killed += 1
        except Exception:
            continue
    if killed:
        time.sleep(4)
    return killed


def ensure_napcat():
    """Start NapCat, trying direct injection before the .bat launcher.

    The order used to be reversed, and it was costing a process on every boot:
    `launcher-user.bat` resolves QQ.exe through `reg query`, which is denied on
    this box, so the launcher printed "QQ installation was not found" and exited
    (53 hits in napcat.log) and we injected directly right after. The .bat
    contributed nothing but a `cmd.exe` that had to be started first -- and that
    cmd is the black box that flashed on the desktop (#39).

    `resolve_qq_exe()` is pure winreg plus the documented install paths, no
    subprocess, so it cannot flash anything. The .bat is now only reached when
    that returns nothing (its one remaining value *is* the registry lookup) or
    when `TULPA_NAPCAT_LAUNCHER=bat` asks for the old order explicitly.

    A failed direct injection does not fall back to the .bat: two launchers
    racing for the same account share one user-data-dir and their GPU processes
    fight until QQ dies (see kill_stale_instances), so a second attempt is worse
    than an honest error.

    issue #51: a missing config.json used to degrade the whole chain silently --
    an empty account simply dropped the quick-login argument, NapCat fell back
    to a QR code nobody could scan (50x Login Error ErrCode:3 in issue #50), and
    nothing on this path said why. The account is now checked before anything is
    spawned: no account, no injection, and the note names what to restore.

    issue #50: "在线" used to mean nothing but two bound ports. The node sidecar
    keeps 43000/43001 while the QQ main behind it never gets past the login
    screen, so the early exit reported a healthy chain through 99 Login Error
    lines and the watchdog log showed `launch -> True 在线`. Both the early exit
    and every post-injection return now ask get_login_info first.
    """
    # 一次 ensure 只认自己这次拉起的：进来先清掉上一轮，跳过注入的提前返回
    # （「在线」/ 守护接管）就不会把旧 pid 报成本次注入（issue #46）。
    global LAST_LAUNCH
    LAST_LAUNCH = None
    if is_port_open(43000) and is_port_open(43001):
        # 端口在≠登录态在：QQ 登不上世时 sidecar 仍然占着端口。三问登录态
        # （15s，与 wg_up_stack.ensure_napcat 的早退同一节奏），都不答才算失败。
        for _ in range(3):
            if login_info():
                return True, "在线"
            time.sleep(5)
        return False, ("端口在但 OneBot 无响应（登录态未确认，可能正在重连或登录被拒；"
                       "看 ~/.water-group/logs/napcat.log 的 Login Error）")

    # 文件不在时先从模板重建，让「占位符状态」可见；ensure_config 幂等，
    # 文件在时零动作。少了这一步，配置丢失后会一路静默（issue #51）。
    ensure_config()

    account = configured_account()
    if not account:
        # 少传账号参数 = NapCat 只能弹二维码等人扫，而「没配账号」与「扫码被拒」
        # 之间隔着整条链路。拒绝注入并说清缺什么，不起任何进程。
        return False, ("缺 self_account：water-group/config.json 缺失或未设置"
                       "（占位符不算），NapCat 无法快速登录、只能弹二维码。"
                       "请按 config.example.json 恢复配置后重试。")

    stale = kill_stale_instances()
    if stale:
        print(f"[启动] 已清理 {stale} 个残留 QQ 实例，避免多实例争用同一 user-data-dir")

    qq_exe = resolve_qq_exe()
    force_bat = os.environ.get("TULPA_NAPCAT_LAUNCHER", "").strip().lower() == "bat"

    if qq_exe and not force_bat:
        print(f"[启动] 直接注入 NapCat (QQ: {qq_exe})...")
        launch_napcat_direct(qq_exe, account)
        if not wait_napcat_ports():
            return False, "注入超时，请检查 ~/.water-group/logs/napcat.log 或是否需要扫码 (WebUI: http://127.0.0.1:46099)"
        if not wait_napcat_login():
            return False, ports_up_login_missing()
        return True, "启动成功（直接注入）"

    bat_path = NAPCAT_DIR / "launcher-user.bat"
    if not bat_path.exists():
        return False, "未找到 QQ.exe（注册表不可用且常见安装路径不存在），也没有 launcher-user.bat 可退回"

    print("[启动] 改用 launcher-user.bat 启动 NapCat（降级）...")
    cmd = ["cmd.exe", "/c", "launcher-user.bat"]
    if account:
        cmd.append(account)
    cmd.extend(qq_extra_args())
    launcher = start_process_detached(cmd, cwd=str(NAPCAT_DIR), log_name="napcat.log")
    _record_launch(launcher, account, "bat")
    if wait_napcat_ports():
        if not wait_napcat_login():
            return False, ports_up_login_missing()
        return True, "启动成功（bat 降级）"
    # A still-running launcher is booting QQ, not failing: never inject a
    # second instance behind its back, just give the boot a longer window.
    if launcher.poll() is None:
        if wait_napcat_ports(18):
            if not wait_napcat_login():
                return False, ports_up_login_missing()
            return True, "启动成功（bat 降级）"
        return False, "启动超时，请检查 NapCat 日志或是否需要扫码 (WebUI: http://127.0.0.1:46099)"
    return False, "启动器已退出且端口未就绪，请检查 ~/.water-group/logs/napcat.log"

def check_tulpa_mcp_health(port: int = 48777, host: str = "127.0.0.1", token: str = None, timeout: float = 1.5) -> tuple[bool, str]:
    """Probes the Tulpa MCP HTTP endpoint with a JSON-RPC POST request.
    
    Verifies that the ASGI application on port 48777 is not only listening on TCP
    but is actively processing JSON-RPC MCP requests (returns 200 on authorized ping
    or 401 on protocol endpoint verification). Bypasses system proxies for clean loopback probing.
    """
    url = f"http://{host}:{port}/mcp"
    headers = {
        "Host": f"{host}:{port}",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {
        "jsonrpc": "2.0",
        "id": "healthcheck",
        "method": "ping",
    }
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=timeout, proxies={"http": None, "https": None})
        # 200 means full JSON-RPC response; 401 means MCP endpoint is active and guarded
        if resp.status_code in (200, 401):
            return True, f"HTTP POST 握手成功 (状态码: {resp.status_code})"
        return False, f"HTTP 返回异常状态码: {resp.status_code}"
    except Exception as e:
        return False, f"HTTP POST 握手失败: {e}"

def wait_tulpa_mcp_ready(port: int = 48777, host: str = "127.0.0.1", token: str = None, timeout_seconds: float = 15.0) -> tuple[bool, str]:
    """Wait barrier ensuring Tulpa MCP HTTP POST JSON-RPC endpoint is fully ready.
    
    Prevents race conditions where an Agent invokes MCP right after TCP socket opens
    but before ASGI middleware finishes initialization, causing one-way client deadlock.
    """
    start_time = time.time()
    last_err = ""
    while time.time() - start_time < timeout_seconds:
        ok, msg = check_tulpa_mcp_health(port, host=host, token=token)
        if ok:
            return True, msg
        last_err = msg
        time.sleep(0.5)
    return False, f"Tulpa MCP 就绪等待超时 ({timeout_seconds}s): {last_err}"

def ensure_tulpa():
    if is_port_open(48777) and is_port_open(47860):
        ready, msg = check_tulpa_mcp_health(48777)
        if ready:
            return True, f"在线且 MCP 握手就绪 ({msg})"
    
    print("[启动] 正在后台启动 Tulpa 主服务 (Web: 47860, MCP: 48777)...")
    server_script = os.environ.get("TULPA_WG_SERVER", "server_wg.py")
    start_process_detached([get_python_exe(), server_script], cwd=str(ROOT_DIR), log_name="tulpa.log")
    
    ports_ok = False
    for _ in range(12):
        time.sleep(1)
        if is_port_open(48777) and is_port_open(47860):
            ports_ok = True
            break

    if not ports_ok:
        return False, "端口监听超时，请查看 Tulpa 运行日志 (~/.water-group/logs/tulpa.log)"

    ready, msg = wait_tulpa_mcp_ready(48777, timeout_seconds=12.0)
    if ready:
        return True, f"启动成功且 MCP 握手就绪 ({msg})"
    return False, f"Tulpa 启动就绪屏障未通过: {msg}"

def run_sync():
    try:
        from water_group.sync_groups import inspect_and_sync
        return inspect_and_sync()
    except Exception as e:
        print(f"[Warn] 同步群聊状态失败: {e}")
        return None

def main():
    parser = argparse.ArgumentParser(description="Water-Group 一键环境就绪与后台启动器")
    parser.add_argument("--group", "-g", help="指定目标 QQ 群号")
    parser.add_argument("--status-only", action="store_true", help="仅检查服务状态，不自动拉起")
    parser.add_argument("--allow-injected", action="store_true",
                        help="明知本进程带沙箱注入仍然起栈（会把 QQ 拖进崩溃循环，仅调试用）")
    args = parser.parse_args()

    # 从这里起的栈会把 tsbx.dll 一路继承到 QQ，二十秒后必崩。宁可现在就拒，
    # 也不要给群里留一个每 20 秒崩一次、还不断重注入的实例——那条路今天已经
    # 把桌面堆满过崩溃框。AGENTS.md §0 有正确入口。
    # --status-only 只探端口和健康检查，不拉起任何进程，放行。
    injected = sandbox_dll()
    if injected and not (args.allow_injected or args.status_only):
        print(f"[ABORT] 本进程带着 {injected}（代理沙箱注入）。")
        print("        从这里起的栈会把注入继承给 QQ：约 20 秒后崩 0xC06D007F，")
        print("        watchdog 会无限重注入，桌面上每个实例留一个崩溃框。")
        print("        改用 explorer 代启动：")
        print('          .venv/Scripts/python.exe -c "import subprocess,pathlib; '
              "subprocess.Popen(['explorer.exe', "
              "str(pathlib.Path('water-group/start-bot.pyw').resolve())])\"")
        print("        只查状态不受影响：bootstrap.py --status-only")
        print("        确需在当前进程里继续，加 --allow-injected。")
        return 2

    print("========================================================================")
    print("           [WATER-GROUP] 一键后台服务自检与自动启动")
    print("========================================================================")

    ensure_config()

    if args.status_only:
        napcat_ok = is_port_open(43000)
        tulpa_port_ok = is_port_open(48777) and is_port_open(47860)
        tulpa_ready, tulpa_msg = check_tulpa_mcp_health(48777) if tulpa_port_ok else (False, "端口未开放")
        tulpa_ok = tulpa_port_ok and tulpa_ready
    else:
        napcat_ok, napcat_msg = ensure_napcat()
        tulpa_ok, tulpa_msg = ensure_tulpa()

    print(f"1. NapCat OneBot  : {'[OK] 在线' if napcat_ok else '[FAIL] 未运行'} (HTTP: 43000, WS: 43001, WebUI: 46099)")
    print(f"2. Tulpa Backend  : {'[OK] ' + tulpa_msg if tulpa_ok else '[FAIL] 未就绪 (' + tulpa_msg + ')'} (Web: 47860, MCP: 48777)")

    if not (napcat_ok and tulpa_ok):
        print("\n[ERROR] 核心后台服务未完全就绪，请检查上述报错。")
        sys.exit(1)

    if args.status_only:
        # 真的只查状态：下面的群列表同步、人设卡装载和「MCP 极速启动指南」都属于
        # 接管流程，跑在这里既多余又会把 Agent 引向已经不用的 wait_chat_messages。
        return 0

    print("\n4. 正在同步群列表与角色卡...")
    sync_res = run_sync()

    print("\n5. 正在装载群友动态小本本记忆库...")
    try:
        from water_group.memory_engine import get_memory_engine
        mem_engine = get_memory_engine()
        mem_engine.seed_from_persona()
        mem_count = mem_engine.get_member_count()
        print(f"✓ 群友动态记忆库就绪 : 已建立 {mem_count} 位核心群友档案，支持小本本自动记录与检索")
    except Exception as e:
        print(f"[Warn] 加载群友记忆库异常: {e}")

    ready_groups = []
    if STATUS_FILE.exists():
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                sdata = json.load(f)
                ready_groups = sdata.get("ready_groups", [])
        except Exception:
            pass

    target_sess = None
    if args.group:
        for g in ready_groups:
            if str(g.get("group_id")) == str(args.group):
                target_sess = g
                break
    elif ready_groups:
        target_sess = ready_groups[0]

    print("------------------------------------------------------------------------")
    if target_sess:
        gid = target_sess.get("group_id")
        gname = target_sess.get("group_name")
        sid = target_sess.get("session_id")
        persona_name = target_sess.get("persona")
        persona_preset = target_sess.get("persona_preset") or "little_whale"
        mode = target_sess.get("trigger_mode", "mention_only")
        reply_addressing = target_sess.get("reply_addressing", "quote")
        mention_only_bool = (mode == "mention_only")

        # 查找对应人设卡文件（私有卡在 ~/.water-group/personas，仓库只留公开模板）
        persona_file = None
        for cand in [
            personas_dir() / f"{persona_preset}.md",
            personas_dir() / f"{persona_name}.md",
            ROOT_DIR / "water-group" / "personas" / f"{persona_preset}.md",
            ROOT_DIR / "water-group" / "personas" / f"{persona_name}.md",
        ]:
            if cand.exists():
                persona_file = cand
                break

        persona_content = ""
        if persona_file and persona_file.exists():
            try:
                persona_content = persona_file.read_text(encoding="utf-8")
            except Exception:
                pass

        print(f"✓ 目标群聊      : {gid} ({gname})")
        print(f"✓ 生效 Session : {sid}")
        print(f"✓ 当前装载人设 : {persona_name} ({persona_preset})")
        if persona_file:
            try:
                shown = persona_file.relative_to(ROOT_DIR)
            except ValueError:
                shown = persona_file  # 私有卡在 ~/.water-group 下，不在仓库里
            print(f"✓ 人设卡路径   : {shown}")
        print(f"✓ 触发唤醒模式 : {mode} ({'仅被@或点名唤醒，大群防烧 Token' if mention_only_bool else '自然插话'})")
        print(f"✓ 回复寻址模式 : {reply_addressing} ({'首个气泡带 quote: true 开启 QQ 原生引用回复，精准定位原话' if reply_addressing == 'quote' else '普通回复'})")
        member_mappings = target_sess.get("member_mappings") or {}
        if member_mappings:
            annotated_summary = ", ".join(f"{v.get('name')}({k})" for k, v in member_mappings.items())
            print(f"✓ 核心群友标注 : {annotated_summary} (号码与名片已绑定，发言自动精准识别)")
        print("------------------------------------------------------------------------")
        if persona_content:
            print("【已加载人设卡核心内容 - 主脑必须严格以此设定进行思考与发言】：")
            print(persona_content.strip())
            print("------------------------------------------------------------------------")
        print("【Agent 极速启动行动指南 - 严禁进行任何多余探索，直接调用 MCP】：")
        print("  1. 必须牢记上述人设卡中的所有群友档案（花名、外号、梗、雷点）及说话风格，严禁凭空瞎猜胡编事实！")
        print("  2. 发起长轮询 wait_chat_messages 前向用户输出一句就绪提示")
        print("  3. 调用 MCP 工具 wait_chat_messages 挂起监听：")
        print(f"     MCP Server : \"tulpa\"")
        print(f"     MCP Tool   : \"wait_chat_messages\"")
        print(f"     Arguments  : {{")
        print(f"       \"session_id\": \"{sid}\",")
        print(f"       \"timeout_seconds\": 150,")
        print(f"       \"mention_only\": {str(mention_only_bool).lower()}")
        print(f"     }}")
        print("  4. 动态小本本记忆已挂载：发言群友的精简画像会自动出现在 chat_guidance.member_dossiers 中；群友说“记在小本本上”会自动入库；随时可调用 query_member_dossier 查询群友档案与专属八卦；回复时多结合对方外号与历史黑料调侃！")
        print("  5. QQ 原生引用回复：回复具体群友时，send_chat_reply 的首个文字气泡必须带上 \"quote\": true，自动开启 QQ 原生引用回复卡片精准定位原话！")
        print("  6. 群成员精准标注：核心群友号码与名片已强绑定，上下文画像已透传 QQ/名片，务必结合其专属外号与核心狠料精准怼人！群里发“@机器人 标注 <QQ/@> 是 <人名>”可秒级入库新群友！")
        print("------------------------------------------------------------------------")
        
        output_payload = {
            "status": "ready",
            "target_group": gid,
            "session_id": sid,
            "persona": persona_name,
            "persona_preset": persona_preset,
            "persona_file": str(shown) if persona_file else None,
            "trigger_mode": mode,
            "reply_addressing": reply_addressing,
            "member_mappings": member_mappings,
            "next_call": {
                "server": "tulpa",
                "tool": "wait_chat_messages",
                "arguments": {
                    "session_id": sid,
                    "timeout_seconds": 150,
                    "mention_only": mention_only_bool
                }
            }
        }
        print(f"\n<!-- BOOTSTRAP_RESULT: {json.dumps(output_payload, ensure_ascii=False)} -->")
    else:
        print("[Warn] 暂无就绪会话。请检查 config.json 中的 groups 配置与 QQ 加入的群。")
        sys.exit(1)

    print("========================================================================")

if __name__ == "__main__":
    sys.exit(main())
