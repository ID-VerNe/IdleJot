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
import wg_budget  # noqa: E402  (等待预算：重挂提示不推荐撑不住的取值，issue #83)
import wg_clock  # noqa: E402  (「现在」的唯一出口，issue #95)
import wg_gap  # noqa: E402  (停机窗口缺口账本：漏了什么事后说得出来，issue #114)
import wg_images  # noqa: E402  (图片标记渲染，与 reply/look 同一份实现)
import wg_listener_state  # noqa: E402  (监听存亡账本：「无人值守」的可读事实，issue #112)
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
    collector_pids,
    commands_pids,
    listener_pids,
    listener_specs,
    parse_timeout,
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
    HISTORY_COUNT,
    _forward_at,
    _forward_word,
    app_plugins_line,
    command_prefix,
    config,
    config_problems,
    events,
    group_msg_history,
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

    account, nickname = (None, None)
    info = login_info()
    if info:
        account, nickname = info

    napcat = "离线"
    napcat_online = all(port_open(p) for p in NAPCAT_PORTS)
    if napcat_online:
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
        guard_alive = True
    else:
        pids = supervisor_pids(exclude=exclude_pids)
        guard = f"运行中 pid={','.join(pids)}" if pids else "未运行"
        guard_alive = bool(pids)
    age = heartbeat_age()
    beat_at = heartbeat_time()
    at = wg_clock.clock(beat_at) if beat_at else "时刻不可读"
    if age is None:
        beat = "无心跳"
    elif age > 300:
        # issue #142：这句以前是无条件断言。栈整体退出后没有任何进程会执行重启，
        # 「supervisor 应会重启」于是成了状态行里最贵的一句假话——值守者据此等待。
        # 只有真的有一个守护在跑时才允许说它会把栈拉回来。
        if guard_alive:
            beat = f"{age:.0f}s 前（{at}，已卡死，supervisor 应会重启）"
        else:
            beat = (f"{age:.0f}s 前（{at}，已卡死；没有 supervisor 在跑——不会自愈，"
                    f"需要重新拉起整栈）")
    else:
        beat = f"{age:.0f}s 前（{at}，wait 周期 170s，正常）"
    # issue #51：配置缺失曾经整条链路静默退化（注入少传账号参数只能弹二维码、
    # token 读空让 NapCat 行误报、@ 判定永假让监听永远唤不醒人）。problems() 把
    # 「缺什么」说出来——与 bootstrap 注入前的拒绝判定同一份实现。
    problems = config_problems()
    # issue #86：监听行必须说清每个监听**实际的** `--timeout`。只报 pid 时，
    # 修复前起的超预算监听（`--timeout 300`）和修复后起的（`--timeout 90`）
    # 在状态里长得一模一样——而前者下一轮等待必被宿主截断，且截断路径零输出。
    specs = listener_specs(exclude=exclude_pids)
    if specs:
        pieces = []
        for spec in specs:
            timeout = spec.get("timeout")
            if timeout is None:
                # 不猜：命令行里没写就是没写，它跑的是启动时那份代码的默认值。
                # 当前代码的默认就是等待预算，但老监听跑的是老代码——所以两半都写。
                detail = (f"--timeout 未显式指定（生效值取该监听启动时那版代码的默认；"
                          f"当前默认 {wg_budget.WAIT_BUDGET}s）")
            elif wg_budget.over_budget(timeout):
                detail = (f"--timeout {timeout} ⚠ 超预算：等待 {timeout}s + 发送余量 "
                          f"{wg_budget.SEND_BUDGET}s 已超过宿主常见工具调用超时 "
                          f"{wg_budget.AGENT_TOOL_TIMEOUT}s，这轮等待会被截断且不留输出"
                          f"——重挂 listen.py --timeout {wg_budget.WAIT_BUDGET}")
            else:
                detail = f"--timeout {timeout}"
            pieces.append(f"pid={spec.get('pid')}（{detail}）")
        listener = "已挂 " + "，".join(pieces)
    else:
        listener = "未挂（此刻没有进程值守）"
        # issue #142：「消息照收」曾经是无条件写死的。NapCat 离线或 collector 不在时
        # 这句话是假的——消息连本地都到不了，而这正是栈静默死亡时最误导人的一格。
        # 只有两边都在线才允许说收到；否则点名是哪一环断了。
        silent = []
        if not napcat_online:
            silent.append("NapCat 离线，消息到不了本机")
        if not collector_pids(exclude=exclude_pids):
            silent.append("collector 未运行，消息不会被写入本地事件")
        if silent:
            listener += "，" + "；".join(silent) + "——收不到任何消息，也没人会叫醒你"
        else:
            listener += "，消息照收，但没人会叫醒你"
        # issue #112：这一行以前只有现算的进程结论。空窗期里没人跑 --status 时，
        # 「刚才有人值守过、现在没了」在磁盘上没有任何痕迹；账本把上一轮的起止
        # 时刻带进来，状态行才回答「空窗多长」而不只是「现在没人」。
        # issue #119：账本按 pid 分槽，describe() 汇总所有记录——并行监听时不会
        # 把「还有人在值守」说成「此后 0 秒无人值守」。
        # issue #142：账本只补历史，历史段自带「仅供参考」，不与上面的现刻结论混写。
        note = wg_listener_state.describe()
        if note:
            listener += f"；账本：{note}"
    if specs and any("self_account" in p for p in problems):
        # 一个永远唤不醒的监听比没有监听更糟：它让人以为在值守（issue #51）。
        listener += "（⚠ self_account 不可用，@ 与关键词都判不中——这个监听唤不醒任何人，见「配置」行）"
    cmds = commands_pids(exclude=exclude_pids)
    command = (f"运行中 pid={','.join(cmds)}（# 指令直回，不过 Agent）" if cmds
               else "未运行（# 指令无自动回复）")
    rows = events()
    ledger = replied_ids()
    wakes = [e for e in rows if wake_reason(e)][-6:]

    print()
    print(wg_clock.now_line())
    print(f"NapCat    : {napcat}")
    print(f"配置      : {'；'.join(problems) if problems else 'ok（self_account / onebot_token 均在位）'}")
    print(f"后端      : {backend}  (47860 / 48777)")
    print(f"守护      : {guard}  心跳 {beat}")
    print(f"监听      : {listener}")
    # issue #114：监听在不在与「这段时间消息有没有漏收」是两件事。后端停机跨过
    # 一段时间再拉起，窗口内的群消息不进任何本地存储，只读入口（--status / --list /
    # listen NO_WORK）此前一律报「一切正常」。「缺口」行读 `state/wg_gap.json`——
    # `collector` 启动时算一次「本地 chat_inbox 最后一条 → 拉起时刻」与
    # `get_group_msg_history` 的差。无记录或缺口为空时不加字（排除型对照，与 #112
    # 监听行同口径）——只读 `wg_gap.json`，不联网、不写，保住 --status「只看不动」。
    gap = wg_gap.describe()
    if gap:
        print(f"缺口      : {gap}")
    print(f"#指令     : {command}")
    try:
        import commands as wg_commands  # noqa: PLC0415  只读：拿插件状态与设置
        settings = wg_commands.load_settings()
        plugin_line = wg_commands.PLUGINS.status_line(settings)
        # 追问行走 window_status 事件（issue #4 PD2a）：与插件行同一次 import，
        # 门控口径也一致——插件行说「停用」而追问行说「进行中」是自相矛盾。
        follow = (wg_commands.PLUGINS.emit("window_status", {"settings": settings})
                  or "未开窗（只等 @ 和关键词）")
    except Exception as exc:  # noqa: BLE001  status must not fail because a plugin is broken
        plugin_line = f"状态不可读（{type(exc).__name__}: {exc}）"
        follow = "状态不可读"
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
    print(f"            回完立刻重挂 listen.py --timeout {wg_budget.WAIT_BUDGET}，"
          "它是唯一会叫醒你的东西。")
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
