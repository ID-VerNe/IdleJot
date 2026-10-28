import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ._common import (
    case, ROOT, DAEMON, fs, temp_dir,
    run_script, fixture_home, child_env,
    status_report_line, status_listener_line,
    listen, up, wg_budget, wg_clock, wg_listener_state,
)


def ledger_at(home):
    """把账本指到 `home`，返回还原函数（issue #112 / #119）。

    `wg_listener_state` 的路径绑定与 `STATE` 同款，在 import 期求值。要让本
    进程读到**子进程**写的那一份账本，只能临时改写这些模块级绑定——与
    `status_listener_line` 打桩同款，只是这次桩在账本模块自己身上。返回还原函数
    而不是用上下文管理器：用例里的 finally 本来就要收树，多一层不划算。

    #119 之后账本是 `wg_listeners/<pid>.json` 一槽一份；`STATE` 只在旧布局上
    存在（基线复跑要用**新门禁**跑未打补丁的模块），两套路径都指到 `home`，
    否则旧模块会读写实机账本。`OWNER_ENV` 也一并还原，避免上一个用例的 pid
    泄漏到下一个。
    """
    names = ("STATE", "STATE_DIR", "LEGACY_STATE")
    saved = {name: getattr(wg_listener_state, name) for name in names
             if hasattr(wg_listener_state, name)}
    owner_env = getattr(wg_listener_state, "OWNER_ENV", "WG_LISTENER_PID")
    owner = os.environ.get(owner_env)
    if "STATE" in saved:
        wg_listener_state.STATE = home / "state" / "wg_listener.json"
    if "STATE_DIR" in saved:
        wg_listener_state.STATE_DIR = home / "state" / "wg_listeners"
    if "LEGACY_STATE" in saved:
        wg_listener_state.LEGACY_STATE = home / "state" / "wg_listener.json"

    def restore():
        for name, value in saved.items():
            setattr(wg_listener_state, name, value)
        if owner is None:
            os.environ.pop(owner_env, None)
        else:
            os.environ[owner_env] = owner

    return restore


def write_ledger(home, record):
    """把一条记录写回账本（夹具用；新旧两种布局都认）。

    过期用例要把心跳推到窗口之外，等于自己改写一条记录——这是 `stop()` 不会
    走到的「硬杀」形态，只能直接落盘。
    """
    if hasattr(wg_listener_state, "STATE_DIR"):
        path = wg_listener_state.STATE_DIR / f"{record['pid']}.json"
    else:
        path = home / "state" / "wg_listener.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def write_raw_slot(home, name, text):
    """直接落一个原始槽文件（夹具用，issue #124）。

    `write_ledger()` 要拿 `record["pid"]` 拼文件名，写不出坏 pid 的槽——坏槽
    只能原样落盘。路径取模块当前绑定的 `STATE_DIR`，基线复跑时同样落在 `home`。
    """
    base = getattr(wg_listener_state, "STATE_DIR", home / "state" / "wg_listeners")
    path = Path(base) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@case
def 退出后账本留下可读的无人值守():
    """单轮监听退出后，「现在没有人值守」必须是一个可读事实（issue #112）。

    issue 现象段的验收基线：同一条复现命令跑完之后，修复前只有 `rc=0` 和一个
    空状态目录；修复后要有 `state/wg_listener.json`，且只读入口（模块函数，不
    问进程表）就能报出「无人值守」与退出时刻——不需要谁恰好想起来跑一次
    `--status`。

    这条**不带** `--probe`：探针监听不占「值守」语义（契约 33），拿它做基线就
    测不到 `describe()` 的空窗分支。
    """
    with temp_dir(prefix="wg-ledger-") as home:
        fixture_home(home)
        restore = ledger_at(home)
        try:
            proc = run_script(home, "listen.py", "--cycles", "1", "--timeout", "1",
                              "--no-digest", timeout=60)
            record = wg_listener_state.read()
            on_watch = wg_listener_state.on_watch()
            note = wg_listener_state.describe()
        finally:
            restore()
    stopped = wg_clock.clock(record.get("stopped_at")) if record else ""
    return (proc.returncode == 0
            and record is not None
            and record.get("state") == "exited"
            and record.get("reason") == "NO_WORK"
            and isinstance(record.get("started_at"), int)
            and isinstance(record.get("stopped_at"), int)
            and record["stopped_at"] >= record["started_at"]
            and not on_watch
            and "无人值守" in note and bool(stopped) and stopped in note)


@case
def 值守中与空窗两侧都判定得出():
    """「值守中」与「退出后无人值守」两侧都要判定得出（issue #112）。

    只钉退出后为 False 时，`on_watch()` 恒返回 False 的坏实现照样绿；只钉在跑时
    为 True 时，恒 True 的实现会在下一半被抓。所以起一个真监听（不带 `--probe`：
    探针不算值守），轮询到 True 再等它自己退出（`--timeout 6 --cycles 1` 会自己
    收手），断言翻面且记录里落下 `stopped_at`。
    """
    with temp_dir(prefix="wg-live-") as home:
        fixture_home(home)
        restore = ledger_at(home)
        proc = None
        seen_watch, seen_note = False, ""
        try:
            proc = subprocess.Popen(
                [sys.executable, str(DAEMON / "listen.py"), "--cycles", "1",
                 "--timeout", "6", "--no-digest"],
                cwd=str(ROOT), env=child_env(home),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.time() + 30
            while time.time() < deadline:
                if wg_listener_state.on_watch():
                    seen_watch = True
                    seen_note = wg_listener_state.describe()
                    break
                time.sleep(0.5)
            code = proc.wait(timeout=60)
            record = wg_listener_state.read()
            after_watch = wg_listener_state.on_watch()
            after_note = wg_listener_state.describe()
        finally:
            restore()
            if proc is not None and proc.poll() is None:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               capture_output=True)
    return (seen_watch and "值守中" in seen_note
            and code == 0
            and record is not None and bool(record.get("stopped_at"))
            and not after_watch and "无人值守" in after_note)


@case
def 状态行报出上一轮监听与空窗():
    """`up.py --status` 的「监听」行在无人值守时带出上一轮起止（issue #112）。

    只读入口里 Agent 最常跑的就是这个：修复前它只会说「现在没人」（而且这个结论
    还是现算的），说不出「刚才有、几分钟前没的」。预置一条已退出记录后，行里必须
    出现退出时刻与「无人值守」；**没有账本时不得凭空加字**——恒加注脚的实现红在
    后半条（排除型对照）。
    """
    with temp_dir(prefix="wg-status-") as home:
        fixture_home(home)
        restore = ledger_at(home)
        try:
            blank = status_listener_line([])
            wg_listener_state.start(timeout=wg_budget.WAIT_BUDGET)
            wg_listener_state.stop("WAKE")
            record = wg_listener_state.read()
            line = status_listener_line([])
        finally:
            restore()
    stopped = wg_clock.clock(record.get("stopped_at")) if record else ""
    return ("未挂" in blank and "账本" not in blank
            and "未挂" in line and "账本" in line
            and "无人值守" in line and bool(stopped) and stopped in line)


@case
def 栈全下线不得说消息照收():
    """NapCat 或 collector 不在时，监听行不许再说「消息照收」（issue #142）。

    这句以前是无条件写死的：整栈静默死亡后，`--status` 一边报 `NapCat: 离线`，
    一边在同一屏说「消息照收」——自相矛盾，而且是最误导人的那一格。三档各自
    点名缺的那一环；两环都在线时仍要说「消息照收」（对照组，防止实现把整句
    删掉、从此什么都不说）。
    """
    nobody = status_report_line("监听", [], port_open=lambda *a, **k: False,
                               collector_pids=lambda *a, **k: [])
    napcat_missing = status_report_line(
        "监听", [], port_open=lambda *a, **k: False,
        collector_pids=lambda *a, **k: ["4321"])
    collector_missing = status_report_line(
        "监听", [], port_open=lambda *a, **k: True,
        collector_pids=lambda *a, **k: [])
    healthy = status_report_line(
        "监听", [], port_open=lambda *a, **k: True,
        collector_pids=lambda *a, **k: ["4321"])
    return ("消息照收" not in nobody
            and "NapCat 离线" in nobody and "collector 未运行" in nobody
            and "消息照收" not in napcat_missing
            and "collector 未运行" not in napcat_missing
            and "消息照收" not in collector_missing
            and "NapCat 离线" not in collector_missing
            and "消息照收" in healthy
            and "收不到任何消息" not in healthy)


@case
def 守护为空不得断言会自愈():
    """没有守护在跑时，心跳过期不得再说「supervisor 应会重启」（issue #142）。

    栈整体静默死亡后没有任何进程会执行那次重启，这句断言让值守者一直等一个
    不会来的自愈。两档都要：守护为空时只许说「不会自愈、需要重新拉起整栈」；
    真有守护在跑时仍要说「应会重启」（对照组，防止把这句话整体删掉）。
    """
    stale = 39_000
    beat = time.time() - stale
    alone = status_report_line("守护", [], supervisor_pids=lambda *a, **k: [],
                               heartbeat_age=lambda *a, **k: stale,
                               heartbeat_time=lambda *a, **k: beat)
    guarded = status_report_line("守护", [], supervisor_pids=lambda *a, **k: ["7777"],
                                 heartbeat_age=lambda *a, **k: stale,
                                 heartbeat_time=lambda *a, **k: beat)
    return ("supervisor 应会重启" not in alone
            and "不会自愈" in alone and "重新拉起整栈" in alone
            and "supervisor 应会重启" in guarded)


@case
def 陈旧账本槽必须标明仅供参考():
    """账本的历史分支必须自带「仅供参考」，不许与现刻结论同形（issue #142）。

    监听行前半句是现刻事实（「未挂」+ 缺哪一环），后半句「账本：…」是上一轮的
    历史。修复前两段同形，一个早就过期的槽被无限复述成「疑似被硬杀」，读起来
    像刚刚发生。两档历史分支（心跳过期没退出记录 / 已退出）都要带标记；仍在
    值守的账本不得带（对照组，防止实现把标记加在每一档上）。
    """
    with temp_dir(prefix="wg-stale-note-") as home:
        fixture_home(home)
        restore = ledger_at(home)
        try:
            now = int(time.time())
            wg_listener_state.start(pid=111, timeout=wg_budget.WAIT_BUDGET)
            stale = wg_listener_state.read(111)
            stale["beat_at"] = now - wg_listener_state.STALE_AFTER - 5
            write_ledger(home, stale)
            stale_note = wg_listener_state.describe()
            write_ledger(home, {"pid": 222, "started_at": now - 500,
                                "beat_at": now - 400, "stopped_at": now - 100,
                                "reason": "NO_WORK", "timeout": wg_budget.WAIT_BUDGET,
                                "probe": False, "state": "exited"})
            # 只留已退出那条：两条都在时 `_last_moment` 会选心跳过期那条。
            (wg_listener_state.STATE_DIR / "111.json").unlink()
            exited_note = wg_listener_state.describe()
            line = status_listener_line([])
            wg_listener_state.start(pid=333, timeout=wg_budget.WAIT_BUDGET)
            live_note = wg_listener_state.describe()
        finally:
            restore()
    return ("失去心跳" in stale_note and "仅供参考" in stale_note
            and "无人值守" in exited_note and "仅供参考" in exited_note
            and "账本" in line and "仅供参考" in line
            and "值守中" in live_note and "仅供参考" not in live_note)


@case
def 探针监听留痕但不算值守():
    """门禁/测试的临时监听留痕，但不占「值守」语义（issue #112 / #94 PB）。

    事后归因需要它的起止（跑了多久、什么时候退的），但「现在有没有人值守」不能被
    一次跑门禁的临时监听顶掉——那与进程探针跳过 `--probe` 是同一口径。恒 False
    的坏实现由「值守中与空窗两侧都判定得出」抓；这里钉的是「留痕 ≠ 计数」。
    """
    with temp_dir(prefix="wg-probe-ledger-") as home:
        fixture_home(home)
        restore = ledger_at(home)
        try:
            proc = run_script(home, "listen.py", "--cycles", "1", "--timeout", "1",
                              "--no-digest", "--probe", timeout=60)
            record = wg_listener_state.read()
            on_watch = wg_listener_state.on_watch()
        finally:
            restore()
    return (proc.returncode == 0
            and record is not None and record.get("probe") is True
            and bool(record.get("stopped_at"))
            and not on_watch)


@case
def 心跳过期的值守记录不算值守():
    """被硬杀写不了退出记录：心跳过期必须与「还在等」分开（issue #112 / #88）。

    `stop()` 在宿主截断那条路径上根本跑不到（进程整棵被杀），记录会永远停在
    `watching`。只按 state 判定时，一个早就死掉的监听在账本里一直是「值守中」
    ——与 #88 的那种静默同形。所以：窗口内算值守（对照组，恒 False 的实现不许
    蒙混过关）、窗口外不算，且 `describe()` 要说出「失去心跳、没有退出记录」，
    而不是编一个退出时刻。

    这里直接写记录再把心跳推到窗口之外（不真杀进程）：`stop()` 一次都没被调用
    过，正是硬杀留下的形态。
    """
    with temp_dir(prefix="wg-stale-") as home:
        restore = ledger_at(home)
        try:
            wg_listener_state.start(timeout=wg_budget.WAIT_BUDGET)
            record = wg_listener_state.read()
            record["beat_at"] = record["started_at"] - wg_listener_state.STALE_AFTER - 5
            write_ledger(home, record)
            stale_watch = wg_listener_state.on_watch()
            stale_note = wg_listener_state.describe()
            # 同一份记录、把「现在」拨回窗口内：必须是 True，证明上面的 False
            # 来自窗口判定，而不是这份记录一律不算值守。
            inside_watch = wg_listener_state.on_watch(now=record["beat_at"] + 1)
        finally:
            restore()
    return (not stale_watch and inside_watch
            and "失去心跳" in stale_note and "没有退出记录" in stale_note)


@case
def 过期窗口两入口同一结论():
    """同一条记录、同一时刻，`on_watch()` 与 `describe()` 必须同一答案（issue #118）。

    窗口曾经有两处取值来源：`on_watch()` 的默认参数在 def 期就把 180 绑死，
    `describe()` 每次现读模块常量。两处同值时看不出问题，只改常量一处就同一条
    账本记录两个答案——`--status`（走 `describe()`）说「疑似被硬杀」，按
    `on_watch()` 判的调用方说「值守中」。

    这里只钉行为：同一条 `watching` 记录、同一个 `now`（心跳固定 100s 前），把
    窗口常量分别设到 100s 两侧，布尔值与文案都必须跟着一起翻。恒真、恒假、单个
    入口绑死常量这三种坏实现各红在其中一档；常量 `try/finally` 还原，`now` 显式
    传入避免秒级漂移。
    """
    age = 100
    with temp_dir(prefix="wg-stale-window-") as home:
        restore = ledger_at(home)
        original = wg_listener_state.STALE_AFTER
        rows = []
        try:
            now = int(time.time())
            wg_listener_state.start(timeout=wg_budget.WAIT_BUDGET)
            record = wg_listener_state.read()
            record["started_at"] = now - 500
            record["beat_at"] = now - age
            write_ledger(home, record)
            for window in (age + 20, age - 20, original):
                wg_listener_state.STALE_AFTER = window
                rows.append((window,
                             wg_listener_state.on_watch(now=now),
                             wg_listener_state.describe(now=now)))
        finally:
            wg_listener_state.STALE_AFTER = original
            restore()
    return all(
        watching == (age <= window)
        and ("值守中" in note) == (age <= window)
        and ("失去心跳" in note) == (age > window)
        for window, watching, note in rows)
