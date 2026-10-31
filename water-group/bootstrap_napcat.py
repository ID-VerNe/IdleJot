import os
import sys
import time
import json
from pathlib import Path

import winconsole  # noqa: E402  (探针开窗抑制，一次收口)
from wg_paths import state_dir
from wg_up_wake import login_info  # noqa: E402  (登录态探针，与 --status 同一实现，issue #50)

# bootstrap_napcat.py 是 bootstrap.py 的私有拆分（issue #277 R1）：QQ.exe 解析 +
# launcher patch + 直/bat 启动 + WMI 进程跟踪 + kill_stale + ensure_napcat。
# LAST_LAUNCH 全局在本模块内闭合（_record_launch 写、last_launch/ensure_napcat 读），
# 不跨模块 re-export 名字——见 plan 追评的 LAST_LAUNCH 陷阱分析。
#
# 低层叶子（ROOT_DIR）从 bootstrap_win32 取。共享工具（is_port_open / ensure_config /
# configured_account / start_process_detached）留在 bootstrap.py，本模块函数体内经
# `_shared()` 延迟取——避免模块顶层 `from water_group import bootstrap` 造成循环导入
# 与脚本态双装载（bootstrap.py 作为 __main__ 运行时，顶层反向 import 会触发 bridge
# 把 bootstrap.py 再装一次为 water_group.bootstrap，撕裂模块级状态）。
from water_group.bootstrap_win32 import ROOT_DIR

NAPCAT_DIR = ROOT_DIR / "tools" / "napcat-shell"


def _shared():
    """延迟取 bootstrap.py 模块对象；调用时已完全装载，且门禁 monkeypatch 打在
    bootstrap.<name> 上时此处读到的是桩。"""
    from water_group import bootstrap
    return bootstrap


# 最近一次注入的启动器标识（issue #46）。命令行指纹在「多实例并存」时不可靠：
# 实测重拉出来的主进程只有 `--disable-gpu`，没有 `-q <账号>`，所以按进程表 + 命令行
# 判定会恒报 0，上一实例就这么活到了下一次注入。记下「这次我拉起的到底是谁」，
# 下一轮才有得比对。
LAUNCH_FILE = state_dir() / "napcat_launch.json"
# 本进程最近一次真正 spawn 的启动器记录；ensure_napcat 因「已在线」提前返回时不写，
# 这样 watchdog 日志里的 pid 一定对得上本次注入，而不是上一轮的残留。
LAST_LAUNCH = None

QQ_INSTALL_CANDIDATES = (
    Path(r"C:\Program Files\Tencent\QQNT\QQ.exe"),
    Path(r"C:\Program Files (x86)\Tencent\QQNT\QQ.exe"),
)


def resolve_qq_exe():
    """Locate QQ.exe without requiring the registry (it can be policy-blocked)."""
    found = registry_qq_exe()
    if found:
        return found
    for candidate in QQ_INSTALL_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


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


def wait_napcat_ports(seconds=12):
    for _ in range(seconds):
        time.sleep(1)
        if _shared().is_port_open(43000) and _shared().is_port_open(43001):
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
    proc = _shared().start_process_detached(cmd, cwd=str(NAPCAT_DIR), log_name="napcat.log", env=env)
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
        with open(_shared().CONFIG_FILE, "r", encoding="utf-8") as f:
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
    # issue #169 PD：合流到 wg_daemon.write_json（唯一临时名 + os.replace）。
    # 原固定名 `.tmp` 并发写互踩；失败不抛，与原 `except OSError: pass` 一致。
    import wg_daemon
    wg_daemon.write_json(LAUNCH_FILE, record)
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
    account = _shared().configured_account()
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
    b = _shared()
    # 一次 ensure 只认自己这次拉起的：进来先清掉上一轮，跳过注入的提前返回
    # （「在线」/ 守护接管）就不会把旧 pid 报成本次注入（issue #46）。
    global LAST_LAUNCH
    LAST_LAUNCH = None
    if b.is_port_open(43000) and b.is_port_open(43001):
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
    b.ensure_config()

    account = b.configured_account()
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
    launcher = b.start_process_detached(cmd, cwd=str(NAPCAT_DIR), log_name="napcat.log")
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
