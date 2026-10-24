"""Bring the whole water-group stack up with one command, and keep it up.

An agent told to "go water the group" runs this and then only has to write replies.

Why this process stays in the foreground instead of detaching: on Windows a child
started with DETACHED_PROCESS still dies with the job object of the shell that
created it, and this host kills the whole tree when a command finishes. Measured
here: both DETACHED_PROCESS and CREATE_BREAKAWAY_FROM_JOB children were gone before
they could write a single line. The only thing that reliably keeps the stack alive
is a parent that never exits, so up.py *becomes* the supervisor: it ensures NapCat,
starts the children, reports, and then blocks in the watch loop.

issue #37: this file used to mix four jobs in 1063 lines -- probing the stack,
starting/stopping it, reading config, and deciding whether a message wakes an agent.
The three mechanical quarters now live next door and this file is the facade:

* ``wg_up_probe``  (PA) -- which pids/ports are ours; nothing else.
* ``wg_up_wake``   (PC) -- config, OneBot account probe, wake/ledger judgement.
* ``wg_up_stack``  (PB) -- taskkill, injection, stop/spawn; depends on the other two.

What stays here is what needs the whole machine view or this process's own identity:
``report`` (every layer's answer on one screen), ``main``/CLI, and the two helpers
only the entry point uses (``strip_proxy`` / ``python_exe``).

**Patching contract**: the names below are re-exports, so ``up.wake_reason`` *is*
``wg_up_wake.wake_reason``. A test that stubs one must stub it on the module that
calls it -- stubbing ``up.config`` no longer reaches ``wg_up_wake.is_command``.
``scripts/check_up_facade.py`` pins the identity so this cannot drift silently.

Usage:
  up.py            bring everything up and stay resident (run it in the background)
  up.py --status   report only, touch nothing
  up.py --stop     take the whole stack down (the only stop path; stop.ps1 delegates)
  up.py --detach   legacy: spawn the supervisor and return (unreliable under a job)
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
import winconsole  # noqa: E402  (stdout 编码 + 控制台代码页，一次收口)
import wg_clock  # noqa: E402  (「现在」的唯一出口，issue #95)
import wg_images  # noqa: E402  (图片标记渲染，与 reply/look 同一份实现)
import wg_text  # noqa: E402  (正文渲染：段占位不占额度，issue #32)
from wg_daemon import read_json  # noqa: E402
from wg_paths import logs_dir, state_dir  # noqa: E402

# 门面（issue #37）：外部一律经 up.<name> 消费，切分后零改动。名字按归属模块排列；
# 新模块里的公开名字必须在这里同步导出，否则 check_up_facade 会红。
from wg_up_probe import (  # noqa: E402,F401  (分节 PA：进程/端口探测)
    BACKEND_PORTS,
    KILLABLE_NAMES,
    NAPCAT_PORTS,
    STACK_SCRIPTS,
    STOP_PORTS,
    _match_pids,
    _process_names,
    commands_pids,
    listener_pids,
    port_open,
    port_owners,
    self_pids,
    stack_pids,
    supervisor_pids,
    transient_pids,
    wait_ports_free,
    watchdog_pids,
)
from wg_up_wake import (  # noqa: E402,F401  (分节 PC：配置/账号/唤醒判定)
    DEFAULT_PENDING_REPLY_TTL,
    FORWARD_WAKE_CHARS,
    FORWARD_WAKE_NODES,
    _forward_at,
    _forward_word,
    app_plugins_line,
    command_prefix,
    config,
    events,
    heartbeat_age,
    heartbeat_time,
    is_command,
    ledger_started,
    login_info,
    onebot_token,
    pending_reply_ttl,
    read_text,
    replied_after,
    replied_ids,
    self_account,
    wake_expired,
    wake_reason,
    wake_state,
    wake_words,
)
from wg_up_stack import (  # noqa: E402,F401  (分节 PB：启停链路)
    StopResult,
    _clear_stale_commands_lock,
    _kill_instances,
    _taskkill_tree,
    ensure_napcat,
    load_reload,
    load_supervisor,
    log_stop,
    spawn,
    stop_stack,
    wait_ports,
)

STATE = state_dir()

PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


def strip_proxy():
    """The host proxy intercepts loopback and NapCat only answers on 127.0.0.1.

    Mutating os.environ instead of only building a child env means every module
    imported afterwards — mcp_client, session, bootstrap — inherits a clean one.
    """
    for key in list(os.environ):
        if key.upper() in PROXY_KEYS:
            os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"


def python_exe():
    candidate = ROOT / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.exists() else sys.executable


def report(exclude_pids=None, resident_pid=None):
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import followup

    account, nickname = (None, None)
    info = login_info()
    if info:
        account, nickname = info

    napcat = "离线"
    if all(port_open(p) for p in NAPCAT_PORTS):
        if info:
            napcat = f"在线 ({account} {nickname})"
        else:
            # Ports bound but OneBot silent is the issue #45 outage signature:
            # QQ keeps delivering to NapCat while the bridge emits no events, so
            # @ and # commands get no answer at all. "暂未响应" read like NapCat
            # itself was still healthy -- say what it actually means.
            napcat = ("端口在，OneBot 无响应（消息可达 NapCat，但事件未发出"
                      "——@ 与 # 指令都不会有响应）")
            halt = read_json(STATE / "napcat_halt.json")
            if halt:
                napcat += (f"；watchdog 已 halt（{halt.get('kind')} ×{halt.get('fails')}，"
                           f"{halt.get('halted_at')} 起，每 10 分钟重试一次）")
    backend = "在线" if all(port_open(p) for p in BACKEND_PORTS) else "离线"
    if resident_pid:
        guard = f"运行中 pid={resident_pid}（本进程常驻）"
    else:
        pids = supervisor_pids(exclude=exclude_pids)
        guard = f"运行中 pid={','.join(pids)}" if pids else "未运行"
    age = heartbeat_age()
    beat_at = heartbeat_time()
    at = wg_clock.clock(beat_at) if beat_at else "时刻不可读"
    if age is None:
        beat = "无心跳"
    elif age > 300:
        beat = f"{age:.0f}s 前（{at}，已卡死，supervisor 应会重启）"
    else:
        beat = f"{age:.0f}s 前（{at}，wait 周期 170s，正常）"
    listeners = listener_pids(exclude=exclude_pids)
    listener = (f"已挂 pid={','.join(listeners)}" if listeners
                else "未挂 —— 消息照收，但没人会叫醒你")
    cmds = commands_pids(exclude=exclude_pids)
    command = (f"运行中 pid={','.join(cmds)}（# 指令直回，不过 Agent）" if cmds
               else "未运行（# 指令无自动回复）")
    window = followup.load()
    if window and not followup.stale(window):
        until = window.get("until") or 0
        left = wg_clock.seconds_left(until)
        left_text = f"，还剩 {left}s" if left is not None else ""
        follow = (f"进行中（回复后还可跟 {window.get('budget')} 条，"
                  f"静默至 {wg_clock.clock(until)}{left_text}）")
    else:
        follow = "未开窗（只等 @ 和关键词）"
    rows = events()
    ledger = replied_ids()
    wakes = [e for e in rows if wake_reason(e)][-6:]

    print()
    print(wg_clock.now_line())
    print(f"NapCat    : {napcat}")
    print(f"后端      : {backend}  (47860 / 48777)")
    print(f"守护      : {guard}  心跳 {beat}")
    print(f"监听      : {listener}")
    print(f"#指令     : {command}")
    try:
        import commands as wg_commands  # noqa: PLC0415  只读：拿插件状态与设置
        plugin_line = wg_commands.PLUGINS.status_line(wg_commands.load_settings())
    except Exception as exc:  # noqa: BLE001  status must not fail because a plugin is broken
        plugin_line = f"状态不可读（{type(exc).__name__}: {exc}）"
    print(f"插件      : {plugin_line}")
    try:
        app_line = app_plugins_line()
    except Exception as exc:  # noqa: BLE001  status must not fail because a plugin is broken
        app_line = f"状态不可读（{type(exc).__name__}: {exc}）"
    print(f"app 插件  : {app_line}")
    try:
        reload_line = load_reload().status_line()
    except Exception as exc:  # noqa: BLE001  status must not fail because reload is broken
        reload_line = f"状态不可读（{type(exc).__name__}: {exc}）"
    print(f"热重载    : {reload_line}")
    print(f"追问      : {follow}")
    print(f"会话      : {read_text(STATE / 'wg_session.txt') or '（尚未建立）'}")
    print(f"游标      : {read_text(STATE / 'wg_ack.txt') or '0'}   事件 {len(rows)} 条")
    if wakes:
        print("最近唤醒  :")
        for e in wakes:
            stamp = wg_clock.clock_ms(e.get("timestamp"))
            state = wake_state(e, rows, ledger)
            images = wg_images.render(e)
            # status 行与唤醒行同一份渲染口径（issue #32）：40 字额度按正文
            # 算，段占位不占，截断了带 --show 指引。
            body, _, _ = wg_text.render_body(e.get("content"), wg_text.STATUS_BODY_CHARS,
                                              e.get("id"))
            print(f"  id={e.get('id')} {stamp} [{state}] [{wake_reason(e)}] "
                  f"{e.get('sender')}: {body}"
                  f"{('  ' + images) if images else ''}")
    else:
        print("最近唤醒  : 无")
    print()
    print("下一步    : 对上面 id 里还没回过的，用 reply.py <id> \"文本\" 回复"
          "（内部走 plan_chat_reply + send_chat_reply）；")
    print("            [过期] 是超过待回时效的老唤醒，默认不再回，确实要回加 --force；")
    print("            回完立刻重挂 listen.py --timeout 300，它是唯一会叫醒你的东西。")
    print("            # 开头的指令由 commands.py 直接回复，Agent 不参与；增删关键词改 "
          "water-group/config.json 的 slash_commands.commands（30 秒热生效）。")
    return 0


def main():
    parser = argparse.ArgumentParser(description="一键接管水群：拉起 NapCat、后端与守护并常驻")
    parser.add_argument("--status", action="store_true", help="只报告状态，不启动任何东西")
    parser.add_argument("--detach", action="store_true",
                        help="旧行为：后台拉起 supervisor 后立即返回（沙箱内不可靠）")
    parser.add_argument("--stop", action="store_true",
                        help="停掉常驻守护、看护与注入实例后退出")
    args = parser.parse_args()

    winconsole.ensure_utf8_stdio()
    strip_proxy()

    if args.stop:
        result = stop_stack()
        print(result.summary())
        if result.ports_left:
            print("      仍被占用说明有进程不受脚本指纹管辖，按 pid 精准清后重跑；"
                  "详见 " + str(logs_dir() / "stopper.log"))
        return 0 if result.ok else 1

    if args.status:
        return report(exclude_pids=self_pids())

    if args.detach:
        ok_napcat, note_napcat = ensure_napcat()
        if not all(port_open(p) for p in BACKEND_PORTS):
            spawn([python_exe(), str(HERE / "supervisor.py")], "supervisor.log")
        ok = wait_ports(BACKEND_PORTS, 120)
        report()
        print(f"[结果] NapCat: {note_napcat} | 后端: {'启动成功' if ok else '未就绪'} | "
              f"守护: 已脱离（随启动它的进程一起死）")
        return 0 if (ok_napcat and ok) else 1

    ok_napcat, note_napcat = ensure_napcat()

    supervisor = load_supervisor()
    hot_reload = load_reload()
    procs = supervisor.start_all()
    hot_reload.write_guard_state(os.getpid(), build=hot_reload.build_stamp())
    reloader = hot_reload.Reloader(
        spawn=supervisor.spawn,
        healthy=lambda: bool(login_info()),
        quarantined=supervisor.QUARANTINED,
        config=(config().get("reload") or {}),
    )
    print("[2/3] 后端启动中…", flush=True)
    ok_backend = wait_ports(BACKEND_PORTS, 180)
    print("[3/3] 守护循环接管中…", flush=True)

    report(resident_pid=os.getpid())
    print(f"[结果] NapCat: {note_napcat} | 后端: {'启动成功' if ok_backend else '未就绪'} | "
          f"守护: 本进程 pid={os.getpid()} 常驻")
    print("[常驻] 不要关闭这个任务；关掉它等于关掉整个水群。", flush=True)

    try:
        supervisor.supervise(procs, on_tick=reloader.tick,
                             quarantined=supervisor.QUARANTINED)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
