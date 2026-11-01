import ast
import contextlib
import io
import os
import re
import subprocess
import sys
import time

from ._common import (
    case, ROOT, DAEMON, fs, temp_dir, isolated_env,
    run_script, fixture_home, write_events, event, lines, index_of,
    child_env, status_listener_line, child_budget,
    DOC_SAMPLES, DAEMON_SOURCES,
    listen, reply, up, watch, wg_budget, wg_up_probe,
    kill_tree,
)


def capture_wait(seconds):
    """跑一次 `reply.wait_for_next`，返回 (stdout, 它交给子监听的命令行)（issue #97）。

    两个外部依赖打桩：进程探针返回空（否则直接走「已有监听在跑」），
    `run_passthrough` 只记下命令行并返回 0（不真的 spawn `listen.py`，也不等）。
    """
    originals = (up.listener_pids, reply.winconsole.run_passthrough)
    seen = []
    up.listener_pids = lambda *args, **kwargs: []
    reply.winconsole.run_passthrough = lambda cmd, cwd=None: (seen.append(list(cmd)), 0)[1]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            reply.wait_for_next(seconds)
    finally:
        up.listener_pids, reply.winconsole.run_passthrough = originals
    return buf.getvalue(), (seen[0] if seen else [])


def _arg_default(rel, flag):
    """`add_argument(flag, default=…)` 的**实际**取值（字面量 / 模块常量都解）。

    只断言模块常量不够：把 `default=240` 直接写死、常量仍留 90 时，「常量 ≤ 预算」
    那条照样绿（变异实测）。所以解析 argparse 自己的 default，再要求它与常量一致。

    解出来的常量值一律与**本进程里的真实取值**对账（issue #313）：`X = wg_budget.WAIT_BUDGET`
    这种写法在环境变量放宽预算后不再是 90，AST 里那个名字必须指回同一个数，
    否则「常量 ≤ 预算」测的是另一份快照。`X = wg_budget.WAIT_BUDGET - 10` 这类
    带字面量的推导也按真实值解，同样对账。
    """
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    consts = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, int):
                consts[node.targets[0].id] = value.value
            elif (_is_budget_ref(value) and hasattr(wg_budget, value.attr)):
                consts[node.targets[0].id] = getattr(wg_budget, value.attr)
            elif _is_budget_arith(value):
                consts[node.targets[0].id] = _budget_arith(value)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            continue
        flags = [a.value for a in node.args
                 if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if flag not in flags:
            continue
        for kw in node.keywords:
            if kw.arg != "default":
                continue
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int):
                return kw.value.value
            if isinstance(kw.value, ast.Name):
                return consts.get(kw.value.id)
            if _is_budget_arith(kw.value):
                return _budget_arith(kw.value)
    return None


def _is_budget_ref(node):
    """`wg_budget.<名字>` 这种引用（issue #313：取值随环境变量变，必须现算）。"""
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "wg_budget")


def _is_budget_arith(node):
    """`wg_budget.<名字> (+|-|*) <整数常量>`，只支持这一种形状（多一层就自己加）。"""
    return (isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult))
            and _is_budget_ref(node.left)
            and isinstance(node.right, ast.Constant) and isinstance(node.right.value, int))


def _budget_arith(node):
    """按**本进程**的真实预算解上面那种形状，不去推源码里的字面量。"""
    base = getattr(wg_budget, node.left.attr)
    return {ast.Add: base + node.right.value,
            ast.Sub: base - node.right.value,
            ast.Mult: base * node.right.value}[type(node.op)]


def _quoted_timeouts(rel):
    """某脚本里字符串字面量中的 `--timeout N` 取值（跳过注释与 docstring）。

    只看字符串常量是有意的：注释与 docstring 会写「300s 会怎样」这类解释，
    按原文扫会把解释当成推荐值判红（复核 agent 指出的假阳性风险）。
    """
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        body = getattr(node, "body", None) or []
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            docs.add(id(body[0].value))
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs):
            found += re.findall(r"--timeout[ =](\d+)", node.value)
    return found


def inside_probe():
    """透传区间内的样本值；预算装不下区间时返回 None（issue #95）。

    透传条件是 `SHORT_WAIT <= timeout <= WAIT_BUDGET`（listen.restart_hint），
    所以样本取 `max(WAIT_BUDGET - 10, SHORT_WAIT + 10)` 夹进区间；但裸夹会在
    `WAIT_BUDGET <= SHORT_WAIT + 20` 时恰好夹到 `WAIT_BUDGET` 本身——#86 的
    教训是探针恰好等于常量时，「永远报常量」的坏实现区分不出，所以再往里收
    一格。两个前提都不成立（`WAIT_BUDGET < SHORT_WAIT + 10`）时返回 None，
    由 `预算装得下透传区间` 那条契约点名，这里不硬测区间外的值。
    """
    if wg_budget.WAIT_BUDGET < listen.SHORT_WAIT + 10:
        return None
    return min(max(wg_budget.WAIT_BUDGET - 10, listen.SHORT_WAIT + 10),
               wg_budget.WAIT_BUDGET - 1)


@case
def 默认等待短于宿主超时():
    """默认调用必须在宿主失去耐心之前自己返回（issue #79 候选 4）。

    截断路径上父进程与子监听同组同死，且 `run_passthrough` 只在子进程正常返回后
    才回写输出——Agent 看到的最后一行是「等下一次唤醒…」，分不清是被杀还是还在
    等。所以默认值要留出发送余量：不是「小于宿主超时」，而是「加上发送仍有余」。
    谁把 `DEFAULT_WAIT` 调大，或者把宿主超时估小，都在这里红。
    """
    return (0 < reply.DEFAULT_WAIT
            and reply.DEFAULT_WAIT + reply.SEND_BUDGET <= reply.AGENT_TOOL_TIMEOUT)


@case
def 等待前说清被杀边界():
    """等待开始前的输出必须给出可据以改参的东西（issue #79 候选 2）。

    这条路径上唯一来得及说的话就是等待之前那几行：一旦被截断，之后不会再有任何
    输出。所以断言不是「有提示」，而是「提示里有实际秒数、合计值和两个出口」；
    显式把 `--wait` 开到宿主超时以上时，还要多一行 `WARN` 点名被压回的实际秒数
    （issue #97：超预算不再等于「会被截断」，WARN 也不再这么说）。
    """
    # 用显式的 60 而不是 DEFAULT_WAIT 当正常样本：默认值是否合规由
    # `默认等待短于宿主超时` 管，这条只管「提示在不在、内容够不够改参」，
    # 否则两个契约会一起红、分不清坏在哪。
    too_long = max(reply.AGENT_TOOL_TIMEOUT, 60) + 60
    normal, _ = capture_wait(60)
    over, _ = capture_wait(too_long)

    return ("最多 60s" in normal
            and f"约 {60 + reply.SEND_BUDGET}s" in normal
            and "被杀" in normal and "--no-wait" in normal
            and "WARN" not in normal
            and "WARN" in over and f"{too_long}s" in over)


@case
def 等待秒数与合计值都是实际值():
    """`--wait` 的打印与传参都必须是**实际**等待值（issue #97）。

    请求 240s 时子监听只会等 `clamp_wait(240, 0)` = 90s。父进程按请求值打印，
    就会虚报「合计约 270s」并宣称「很可能被截断」——而 #88 之后实际合计 120s，
    仍在预算内。同一次调用里父子两行因此互相矛盾。

    断言取的是**实际值**（90 ≠ 240）：只断言「有秒数、有合计值」时，把打印改回
    请求值照样绿（#86 复核模板：探针不能等于被测的坏实现会输出的那个值）。预算内
    的 60s 另测一遍：原样透传，不许被折半或压回。
    """
    over = wg_budget.AGENT_TOOL_TIMEOUT * 2
    actual = wg_budget.clamp_wait(over, 0)
    loud, cmd = capture_wait(over)
    inside, inside_cmd = capture_wait(60)
    total = actual + reply.SEND_BUDGET
    loud_cmd = " ".join(cmd)
    inside_cmd_text = " ".join(inside_cmd)
    return (actual != over
            and f"最多 {actual}s" in loud
            and f"约 {total}s" in loud
            and total <= wg_budget.AGENT_TOOL_TIMEOUT
            and f"--timeout {actual}" in loud_cmd
            and f"--timeout {over}" not in loud_cmd
            and "最多 60s" in inside
            and f"约 {60 + reply.SEND_BUDGET}s" in inside
            and "--timeout 60" in inside_cmd_text)


@case
def 父子超预算措辞不矛盾():
    """`reply.py` 与 `listen.py` 对超预算取值必须说同一件事（issue #97）。

    #88 把子监听那句改成「已压回等待预算 90s，不会因此被截断」，父进程仍写
    「很可能在拿到 WAKE 之前被截断」——同一次调用里相邻两行互相打架，Agent 无从
    判断该信哪句，于是白白放弃 `--wait` 改用 `--no-wait`（#69 想消除的空窗）。

    两边都钉：必须有「不会因此被截断」，且两边都不许再出现「很可能…被截断」。
    `listen.py` 那次预置一条唤醒事件，`watch.py` 立刻返回，不真等 90s。
    """
    over = wg_budget.AGENT_TOOL_TIMEOUT * 2
    loud, _ = capture_wait(over)
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        write_events(home, [event(1, 5, "@机器人 在吗")])
        child = run_script(home, "listen.py", "--cycles", "1", "--timeout",
                           str(over), "--no-digest", "--probe")
    both = loud + child.stdout
    return (child.returncode == 0
            and "不会因此被截断" in loud
            and "不会因此被截断" in child.stdout
            and "很可能" not in both)


@case
def 逃生口真的更短():
    """边界行推荐的「更短」取值必须真的更短（issue #97）。

    `max(seconds // 2, 1)` 对 `--wait 200` 给出 100——100 同样被夹回 90s，与
    200 的行为完全一样，照着改也改不掉任何东西。断言从输出里解析推荐值，要求它
    严格小于**实际**等待值（因此也小于 `WAIT_BUDGET`）。

    三个样本：超预算 240 → 实际 90 → 推荐必须 < 90（修复后 45，修复前 120）；
    预算内 60 → 推荐 30；`--wait 1` → 实际 1s，折半不足一秒，**只留 `--no-wait`**
    （契约第 25 条后半句，独立复核抓出的缺口：不钉这一条时，去掉 `shorter > 0`
    守卫、推荐出 `--wait 0` 也照样全绿，而 `--wait 0` 等于 `--no-wait`，是个假出口）。
    """
    def shorter_for(seconds):
        out, _ = capture_wait(seconds)
        match = re.search(r"--wait (\d+)；立即返回", out)
        return int(match.group(1)) if match else None

    over = wg_budget.AGENT_TOOL_TIMEOUT * 2
    actual = wg_budget.clamp_wait(over, 0)
    short_over = shorter_for(over)
    short_inside = shorter_for(60)
    tiny, _ = capture_wait(1)
    return (short_over is not None and short_over < actual
            and short_over < wg_budget.WAIT_BUDGET
            and short_inside is not None and short_inside < 60
            and "--no-wait" in tiny and shorter_for(1) is None)


@case
def 续挂不许留下孤儿子进程():
    """`--wait` 的子监听必须和父进程同生共死（issue #79 候选 3 的反向断言）。

    候选 3 是「让子监听在父进程被杀后活下来」。它看着能救回监听，实际更坏：
    `watch.py` 打完唤醒块立刻推进游标（watch.py:188），管道已断的监听会把唤醒
    消费掉而没人读到；`reply.py` 的 `up.listener_pids()` 还会把孤儿当成「已有监听
    在跑」而拒绝重挂。两条都是静默失败——所以这里钉住这条路径不许出现
    `start_new_session` / `creationflags` / `DETACHED_PROCESS`，也钉住它确实还有
    那一次 `run_passthrough` 调用，免得上面的断言在空集合上白绿。
    """
    tree = ast.parse((DAEMON / "reply.py").read_text(encoding="utf-8"))
    func = next((node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name == "wait_for_next"),
                None)
    if func is None:
        return False
    escaped = False
    calls = 0
    for node in ast.walk(func):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run_passthrough"):
            calls += 1
            names = {kw.arg for kw in node.keywords if kw.arg}
            if names & {"start_new_session", "creationflags", "process_group"}:
                escaped = True
        if isinstance(node, ast.Attribute) and "DETACH" in node.attr:
            escaped = True
    return calls == 1 and not escaped


@case
def 已有监听时不重复挂():
    originals = (up.listener_pids, reply.winconsole.run_passthrough)
    up.listener_pids = lambda *args, **kwargs: ["4321"]

    def forbid(*args, **kwargs):
        raise AssertionError("已有监听时 wait_for_next 不该再 spawn listen.py")

    reply.winconsole.run_passthrough = forbid
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = reply.wait_for_next(reply.DEFAULT_WAIT)
    finally:
        up.listener_pids, reply.winconsole.run_passthrough = originals
    return code == 0 and "已有监听在跑" in buf.getvalue()


@case
def no_wait_优先于_wait():
    """`--no-wait` 是「马上要做别的事」的出口，不能被默认值盖过去。"""
    tree = ast.parse((DAEMON / "reply.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Attribute)
                and node.test.attr == "no_wait"):
            continue
        for stmt in node.body:
            if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Attribute)
                    and stmt.targets[0].attr == "wait"
                    and isinstance(stmt.value, ast.Constant)
                    and stmt.value.value == 0):
                return True
    return False


@case
def 常驻周期不长于等待预算():
    """一次挂机周期必须装得进一次工具调用（issue #83）。

    宿主工具调用默认 120s。周期取 300 时，只要群里安静，调用必被截断；而截断路径上
    `run_passthrough` 不回写任何输出，`listen.py` 连「我挂上了」都说不出来。周期
    与 `reply.py --wait` 用同一套预算（`wg_budget.WAIT_BUDGET`），谁把它调回去都在
    这里红。
    """
    return (0 < listen.DEFAULT_TIMEOUT <= wg_budget.WAIT_BUDGET
            and _arg_default("water-group/daemon/listen.py", "--timeout")
            == listen.DEFAULT_TIMEOUT)


@case
def 默认路径自己返回():
    """默认形态（不传 `--cycles`）必须在预算内自己返回（issue #88）。

    #88 之前 `--cycles` 的 argparse default 是 0 = 不限轮，`while True` 永远
    转下去：宿主在 120s 处截断这次调用，进程树连同 `watch.py` 一起被杀，监听
    就此静默消失——而 `--status` 在那之前一直报「已挂」。本门禁里调 `listen.py`
    的契约全都显式带 `--cycles 1`，默认路径一次都没被测过，这里补上。

    断言不是「有输出」，而是「自己回来了」：rc=0、只跑了 1 轮、末行 `NO_WORK`
    + `RESTART:`。timeout 给 30s 而不是默认的 120s：回归时要快速变红，而不是把
    整条门禁挂到超时。
    """
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        proc = run_script(home, "listen.py", "--timeout", "1", "--probe",
                          timeout=30)
    out = proc.stdout
    return (proc.returncode == 0
            and len(re.findall(r"第 \d+ 轮", out)) == 1
            and index_of(out, "NO_WORK") >= 0
            and index_of(out, "RESTART:") > index_of(out, "NO_WORK"))


@case
def 不限轮数也在额度内收手():
    """`--cycles 0`（不限轮数）也得在额度内收手（issue #88）。

    「不限轮数」在 #88 之前是字面意思上的无限：整次调用永不返回，等宿主来砍，
    进程树连同子监听一起死。这里把预算压到 1s、每轮只花 0.2s，断言它自己收手
    （`NO_WORK` + `RESTART:` + 一行说明），而不是把整次调用拖到宿主超时。

    压预算、假扮 `watch.py`（返回 2 = 静默超时）是刻意的：真实额度 90s，CI 跑
    满要 90s，而这条要测的恰恰是「自己收手」这个分支。假扮的 `watch.py` 另有
    一个 50 轮上限：回归时（循环没有收手点）要在第 51 轮抛错变红，而不是把整条
    门禁挂死——挂死的门禁比红的门禁难查得多。
    """
    originals = (wg_budget.WAIT_BUDGET, listen.run, sys.argv)
    calls = []

    def fake_run(script, *args):
        calls.append(script)
        if len(calls) > 50:
            raise RuntimeError("轮数没有收手：50 轮之后仍在循环")
        time.sleep(0.2)
        return 2

    buf = io.StringIO()
    try:
        wg_budget.WAIT_BUDGET = 1
        listen.run = fake_run
        sys.argv = ["listen.py", "--timeout", "1", "--cycles", "0", "--no-digest"]
        with contextlib.redirect_stdout(buf):
            code = listen.main()
    finally:
        wg_budget.WAIT_BUDGET, listen.run, sys.argv = originals
    out = buf.getvalue()
    return (code == 0 and "NO_WORK" in out and "RESTART:" in out
            and "等待额度" in out)


@case
def 轮数上限由等待预算决定():
    """整次调用的等待额度是所有轮次共享的**总额**（issue #88）。

    `AGENTS.md` 写的是「单次调用最多能等 WAIT_BUDGET=90」，所以这一轮能等多少
    要减掉已经过去的部分，而不是每轮都领一份新的额度。这里钉住四件事：额度用尽
    时返回 0（调用方该收手）、已经过去的时间要扣掉、超预算的取值压回额度内、
    以及 `listen.py --cycles` 的 argparse **实际** default 必须是有界的 1。

    最后一条用 `_arg_default` 解析 argparse 自己的 `default=`：只断言模块常量
    不够，写死 `default=0` 而别处仍留 1 时，「常量有界」照样绿（#83 的同类坑）。
    """
    wait_budget = wg_budget.WAIT_BUDGET
    return (wg_budget.clamp_wait(10, 0) == 10
            and wg_budget.clamp_wait(wait_budget, 0) == wait_budget
            and wg_budget.clamp_wait(10, wait_budget) == 0
            and wg_budget.clamp_wait(10, wait_budget + 5) == 0
            and wg_budget.clamp_wait(10, wait_budget - 5) == 5
            and wg_budget.clamp_wait(wait_budget * 2, 0) == wait_budget
            # 已经过去的时间不能是负数：负 elapsed 会让额度凭空多出一截
            # （clamp_wait(300, -5) 曾等于 95 > WAIT_BUDGET），负值按 0 算。
            and wg_budget.clamp_wait(wait_budget * 2, -5) == wait_budget
            and _arg_default("water-group/daemon/listen.py", "--cycles") == 1)


@case
def 传给watch的秒数被额度夹住():
    """`clamp_wait()` 的结果必须真的用在 `watch.py` 上（issue #88）。

    只断言「`clamp_wait()` 算得对」不够：把 `run("watch.py", "--timeout", str(wait))`
    改回 `str(args.timeout)`，前十九条照样全绿（变异实测），而 `--timeout 300` 时
    `watch.py` 实等 300s > 宿主 120s —— 进程树连同子监听一起被杀、监听静默消失，
    正是 #88 的症状。所以这里钉两条：**实收秒数 ≤ 预算**，且**打印的秒数 == 实收
    秒数**（后者挡住「打印按夹住的值、实等按传入的值」这种半改）。

    样本必须用**超预算**的取值：`--timeout 90` 恰好等于额度，压不出差异，测了
    等于没测。

    `listen.run` 被换成探针（返回 2 = 静默超时），所以这条不真的等 90s；断言的是
    「算出来的秒数被交出去」而不是「等够了那么久」。
    """
    originals = (listen.run, sys.argv)
    seen = []

    def spy(script, *args):
        seen.append((script, args))
        return 2

    buf = io.StringIO()
    try:
        listen.run = spy
        sys.argv = ["listen.py", "--timeout", "300", "--cycles", "1", "--no-digest"]
        with contextlib.redirect_stdout(buf):
            code = listen.main()
    finally:
        listen.run, sys.argv = originals

    out = buf.getvalue()
    passed = [int(args[args.index("--timeout") + 1])
              for script, args in seen
              if script == "watch.py" and "--timeout" in args]
    printed = next((ln for ln in lines(out) if ln.startswith("等最多")), "")
    return (code == 0 and passed
            and max(passed) <= wg_budget.WAIT_BUDGET
            and f"等最多 {max(passed)}s" in printed)


@case
def 等待开始前声明边界():
    """进入 `watch.py` 之前必须说一句「在等、等多久」（issue #83）。

    这条是 issue 的决定性一条：`listen.py` 按设计有界（issue #88 之前是「按设计
    不返回」），但调用方仍可能显式要一个装不下的取值——那种情况下宿主迟早会截断
    这次调用，而截断路径上一个字都不打印。所以断言不是「有输出」，而是「等待开始
    那一句在 `TIMEOUT` 之前就出来了」——被砍掉时它是唯一的痕迹。

    锚点必须是 `TIMEOUT`（`watch.py` 等完之后才打的那行），不能用 `NO_WORK`：
    两者都在等待之后，把这句话挪到 `run()` 下面仍然会排在 `NO_WORK` 前面，
    只有排在 `TIMEOUT` 之前才证明「等待开始前」。变异实测：把打印挪到
    `run()` 之后，这条红。
    """
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        proc = run_script(home, "listen.py", "--cycles", "1", "--timeout", "1",
                          "--no-digest", "--probe")
    out = proc.stdout
    waiting = index_of(out, "等最多 1s（第 1 轮）")
    timed_out = index_of(out, "TIMEOUT")
    return (proc.returncode == 0 and waiting >= 0 and timed_out >= 0
            and timed_out > waiting and index_of(out, "NO_WORK") > timed_out)


@case
def 预算装得下透传区间():
    """预算合法收缩不许把门禁变成绊脚石（issue #95 候选 2）。

    透传样本要落在 `[SHORT_WAIT, WAIT_BUDGET]` 且不等于常量本身，前提是预算
    装得下「短等待」与「预算内」两个区间。预算小到装不下时，那是**预算本身**
    要重新设计（`SHORT_WAIT` 是给 `reply.py --wait` 这类有界等待用的），红在
    预算不自洽上，而不是让 `超预算的取值不写进重挂命令` 以无关的签名假红。
    """
    return listen.SHORT_WAIT + 10 <= wg_budget.WAIT_BUDGET


@case
def 超预算的取值不写进重挂命令():
    """RESTART 行不推荐撑不住的取值（issue #83）。

    `restart_hint` 原来把任何 ≥60s 的取值原样透传，于是 `--timeout 300` 被推荐
    回来、被截断、再被推荐回来。现在三个区间：短等待回到常驻周期、超预算压回
    预算、其余原样透传。

    透传样本由 `inside_probe()` 取（issue #95）：直接用 `WAIT_BUDGET - 10` 的话，
    预算合法收缩到 ≤70 时样本落进短等待夹回区间，断言以与被测行为无关的理由
    假红——预算装不下区间时这里跳过透传样本，红交给 `预算装得下透传区间`。
    """
    def hint(seconds):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            listen.restart_hint(seconds)
        return buf.getvalue()

    inside = inside_probe()
    if inside is None:
        print(f"  WAIT_BUDGET {wg_budget.WAIT_BUDGET} 装不下透传区间"
              f"（需 >= SHORT_WAIT+10 = {listen.SHORT_WAIT + 10}），透传样本跳过，"
              f"由『预算装得下透传区间』点名")
        passes_through = True
    else:
        passes_through = f"--timeout {inside}" in hint(inside)
    return (f"--timeout {wg_budget.WAIT_BUDGET}" in hint(300)
            and f"--timeout {wg_budget.WAIT_BUDGET}" in hint(wg_budget.AGENT_TOOL_TIMEOUT * 2)
            and f"--timeout {wg_budget.WAIT_BUDGET}" in hint(wg_budget.WAIT_BUDGET)
            and f"--timeout {wg_budget.WAIT_BUDGET}" in hint(1)
            and passes_through)


@case
def 环境变量能放宽等待预算():
    """宿主工具超时比 120s 宽时，预算必须能按宿主放宽（issue #313）。

    四条一起钉，缺一条就只剩假绿：

    1. **默认不变**：`WG_AGENT_TOOL_TIMEOUT` 未设时仍是 120/90；
    2. **穿透到默认值**：`reply.py:94/101`、`listen.py:63`、`watch.py:104` 都是
       import 期快照——只让运行时 `--wait` 生效、`DEFAULT_WAIT` 不动的话，不传参
       永远拿不到大窗口，而门禁照样全绿（plan §1.2 实测过）；所以断言的是
       **argparse 里的 default**，不是常量本身；
    3. **非法值不静默**：`abc` / 越界 / 非数字都必须回落 120 **且** stderr 有 WARN
       ——「我配了 300」被悄悄读成 120，下一个故障就是「为什么还是 90s」；
    4. **反例对照**：把 `_env_seconds` 换成「永远返回默认」的坏实现，第 2 组必须
       红。没有这一组，第 2 组红不红只取决于子进程起没起来。

    子进程 env 只用 `isolated_env()` 那套白名单再补一条 `WG_*`：开发机设了这个
    变量时，门禁不该跟着漂。判定在子进程里做、只回一行真值，不在这儿 mock 源码。
    """
    probe = (
        "import sys; from pathlib import Path\n"
        "sys.path.insert(0, 'water-group/daemon')\n"
        "import wg_budget, reply, listen, watch\n"
        "consts = (wg_budget.AGENT_TOOL_TIMEOUT, wg_budget.WAIT_BUDGET)\n"
        "defaults = (reply.DEFAULT_WAIT, listen.DEFAULT_TIMEOUT, watch.DEFAULT_TIMEOUT)\n"
        # `python -c` 下 sys.argv[0] 是 `-c`，参数从 [1] 起：用 `> 1` 判分支，
        # 写成 `> 2` 会让反例组静默走「无桩」路径、恒读到 300（#313 实测踩过）。
        "if 'stub' in sys.argv[1:]:\n"
        "    src = Path('water-group/daemon/wg_budget.py').read_text(encoding='utf-8')\n"
        # 整段替换 `def _env_seconds` 到 `WAIT_BUDGET = …` 之间的两块常量定义，
        # 换成「永远返回默认」的坏实现；**按函数定义与常量行定位**，别按裸
        # `AGENT_TOOL_TIMEOUT = `——模块 docstring 与注释里都有这串字（#313 实测）。
        "    head = src[:src.index('def _env_seconds(')]\n"
        "    tail = src[src.index('WAIT_BUDGET = AGENT_TOOL_TIMEOUT'):]\n"
        "    stub = 'def _env_seconds(*a, default=120, **k):' + chr(10) + "
        "'    return default' + chr(10)\n"
        "    consts = ('AGENT_TOOL_TIMEOUT = _env_seconds()' + chr(10) + "
        "'WAIT_BUDGET = AGENT_TOOL_TIMEOUT - SEND_BUDGET' + chr(10))\n"
        "    src = head + stub + consts + tail\n"
        # 三个入口都是 `import wg_budget`：只 exec 进 `wg_budget.__dict__` 不动
        # `sys.modules` 的话，reload 它们拿到的仍是真正那份（反例永不翻转，#313）。
        "    exec(compile(src, 'wg_budget-stub', 'exec'), wg_budget.__dict__)\n"
        "    sys.modules['wg_budget'] = wg_budget\n"
        "    importlib = __import__('importlib'); importlib.reload(reply)\n"
        "    importlib.reload(listen); importlib.reload(watch)\n"
        "    consts = (wg_budget.AGENT_TOOL_TIMEOUT, wg_budget.WAIT_BUDGET)\n"
        "    defaults = (reply.DEFAULT_WAIT, listen.DEFAULT_TIMEOUT, watch.DEFAULT_TIMEOUT)\n"
        "cli = reply._build_parser().parse_args([]).wait\n"
        "print(consts[0], consts[1], *defaults, cli)\n"
    )
    with temp_dir(prefix="wg-budget-") as home:
        env = isolated_env(home)

        def run(value, stub=False):
            child = dict(env)
            if value is not None:
                child["WG_AGENT_TOOL_TIMEOUT"] = value
            args = [sys.executable, "-c", probe] + (["stub"] if stub else [])
            return subprocess.run(args, cwd=str(ROOT), env=child, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=120)

        default = run(None)
        wide = run("300")
        bad = run("abc")
        stub = run("300", stub=True)

    def numbers(result):
        return [int(v) for v in result.stdout.split()] if result.returncode == 0 else None

    base, wide_n, bad_n, stub_n = (numbers(r) for r in (default, wide, bad, stub))
    default_ok = base == [120, 90, 90, 90, 90, 90]
    wide_ok = wide_n == [300, 270, 270, 270, 270, 270]
    # 非法值：回落 120，WARN 必须在 stderr 点名变量与回落后的值（stdout 不掺噪音）
    bad_ok = (bad_n == [120, 90, 90, 90, 90, 90]
              and "WARN" in bad.stderr and "WG_AGENT_TOOL_TIMEOUT" in bad.stderr
              and "120" in bad.stderr and "WARN" not in bad.stdout)
    # 反例：坏实现下放宽**不起作用**（常量与三个默认值全停在 120/90）——
    # 若仍然读到 270，说明这套探针根本没在测 `_env_seconds`。
    counter_ok = stub_n is not None and stub_n[0] == 120 and stub_n[1] == 90
    if not (default_ok and wide_ok and bad_ok and counter_ok):
        print(f"  默认={base} 放宽={wide_n} 非法={bad_n} 反例={stub_n}；"
              f"wide.stderr={wide.stderr.strip()[:200]}"
              f" stub.stderr={stub.stderr.strip()[-300:]}")
    return default_ok and wide_ok and bad_ok and counter_ok


@case
def 预算缩到极窄时逃生口不留废话():
    """预算被覆盖到比请求值还窄时，提示不能自相矛盾，也不许替没做过的夹断作保。

    预算 32（等待额度 2s）时，`clamp_wait(12)` 若仍回 12（旧写法
    `min(int(timeout), ceil(remaining))` 就是个空夹），`reply.py` 会把请求值打印成
    「已压回 12s」而子进程那边是 2s——正是 #88 要灭的那种假话（issue #313 才让它
    现形：默认 90、`--wait 60` 时夹断从不触发）。同时 `shorter_wait` 在这么窄的
    预算下不该给 `--wait 1` 这种与「最多 Ns」同义的假出口，只剩 `--no-wait`；
    正常预算下那行照旧在（对照组，防「一律不给出口」的坏实现）。
    """
    originals = (wg_budget.AGENT_TOOL_TIMEOUT, wg_budget.WAIT_BUDGET)
    try:
        wg_budget.AGENT_TOOL_TIMEOUT, wg_budget.WAIT_BUDGET = 32, 2
        clamped = wg_budget.clamp_wait(12)
        narrow, _ = capture_wait(12)
        wg_budget.AGENT_TOOL_TIMEOUT, wg_budget.WAIT_BUDGET = originals
        normal, _ = capture_wait(60)
    finally:
        wg_budget.AGENT_TOOL_TIMEOUT, wg_budget.WAIT_BUDGET = originals
    return (clamped == 2 and "最多 2s" in narrow and "要更短" not in narrow
            and "--no-wait" in narrow
            and "要更短：--wait 30" in normal)


@case
def 看护默认超时也在预算内():
    """裸跑 `watch.py` 的等待上限同样不能超预算（issue #83）。

    `watch.py --timeout 240` 是默认值而不是文档示例：Agent 直接敲 `watch.py`
    就走到它，超预算时同样被宿主截断、同样不留输出。数值藏在 argparse 的
    `default=` 里，文档扫描抓不到，所以单独钉。
    """
    return (0 < watch.DEFAULT_TIMEOUT <= wg_budget.WAIT_BUDGET
            and _arg_default("water-group/daemon/watch.py", "--timeout")
            == watch.DEFAULT_TIMEOUT)


@case
def 示例命令不超预算():
    """文档与守护脚本里**给人照抄**的 `--timeout` 取值都得装得进一次调用。

    源码改对了不算修好：Agent 照抄的是 `AGENTS.md` / `README.md` / `SKILL.md` /
    rules 里的示例命令，以及 `up.py --status` 的「下一步」提示串（`up.py:242`
    曾经硬编码 300，比文档还难发现——它是运行时打印的）。

    文档按正则在原文上扫；守护脚本只扫**字符串字面量**（AST，跳过 docstring 与
    注释）：注释里写「300s 会怎样」是解释，不是推荐值，按原文扫会把解释也判红。

    仓外那份私有 SOP 口径不同，由第 26 条单扫（见「私有SOP示例不超预算」）。

    显式抬预算的示例（issue #313：`WG_AGENT_TOOL_TIMEOUT=300 … --timeout 300`）不算
    超预算——它的前提就写在同一条命令里。判据不是「出现 300 就放行」：同一行必须同时
    带 `WG_AGENT_TOOL_TIMEOUT=<n>`，且 `n` 装得下那个取值。写漏变量、或变量比取值小，
    照样红。

    抬预算时的上限是 `AGENT_TOOL_TIMEOUT` 本身，**不是** `WAIT_BUDGET`：`--timeout`
    和 `--wait` 是两回事。`--wait` 是「sleep 完还要发送」的额度，所以默认 90 = 120 - 30；
    `--timeout` 只是这一轮守到多久，300s 的工具调用里挂 300s 的监听是正解，压到 270
    白白少吃 30s。错把发送余量也扣掉，这条示例永远差 30 秒、怎么写都是红的。
    """
    over = []
    for rel in DOC_SAMPLES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for line in text.splitlines():
            cap = re.search(r"WG_AGENT_TOOL_TIMEOUT=(\d+)", line)
            limit = int(cap.group(1)) if cap else wg_budget.WAIT_BUDGET
            over += [f"{rel}: --timeout {value}"
                     for value in re.findall(r"--timeout[ =](\d+)", line)
                     if int(value) > limit]
    for rel in DAEMON_SOURCES:
        over += [f"{rel}: --timeout {value}"
                 for value in _quoted_timeouts(rel)
                 if int(value) > wg_budget.WAIT_BUDGET]
    if over:
        print("  " + "; ".join(over))
    return not over


@case
def 状态行报出监听实际超时():
    """`--status` 的监听行必须带上它实际的 `--timeout`（issue #86）。

    修复前那行只有 pid：修复前起的监听（`--timeout 300`）和修复后起的
    （`--timeout 90`）在状态里长得一模一样，Agent 无从判断自己挂的是不是那个
    能活的。断言不是「有提示」，而是 pid 与秒数同时在行里。

    探针值刻意取 `WAIT_BUDGET - 10` 而不是 `WAIT_BUDGET` 本身（独立复核建议）：
    取值恰好等于模块常量时，「永远报常量」的坏实现输出逐字相同，断言区分不出
    （变异 M4 实测：那样写门禁仍全绿，而一个活着的 60s 监听会被显示成 90）。
    """
    probe = wg_budget.WAIT_BUDGET - 10
    line = status_listener_line([{"pid": "1111", "timeout": probe}])
    return "1111" in line and f"--timeout {probe}" in line


@case
def 超预算监听在状态行里被点名():
    """超预算的监听必须在状态行里被点名，预算内的不得被误报（issue #86）。

    这是本 issue 的要害：`--status` 是操作者（和 Agent）唯一的只读诊断入口，
    它得能回答「我挂的是不是那个能活的」。断言 ⚠ 与秒数同时在行里。

    同一行还要装得下**多个**监听（独立复核建议）：实机上「一个健康监听 + 一个
    修复前起的旧监听」同时存在是最常见的场景，只报第一个的话旧监听被整条漏掉，
    #86「旧监听在状态里一眼看得出」直接失效（变异 M7 实测）。
    """
    over = wg_budget.AGENT_TOOL_TIMEOUT * 2
    inside = wg_budget.WAIT_BUDGET - 10
    loud = status_listener_line([{"pid": "2222", "timeout": over}])
    quiet = status_listener_line([{"pid": "3333", "timeout": inside}])
    both = status_listener_line([{"pid": "4444", "timeout": inside},
                                 {"pid": "5555", "timeout": over}])
    return ("2222" in loud and f"--timeout {over}" in loud
            and "⚠" in loud and "超预算" in loud
            and "3333" in quiet and "⚠" not in quiet
            and "4444" in both and "5555" in both
            and f"--timeout {inside}" in both and f"--timeout {over}" in both)


@case
def 显式超预算的监听自己告警():
    """`listen.py` 显式传超预算 `--timeout` 时必须自己说一声（issue #86）。

    `reply.py --wait` 早就有这行 WARN（reply.py:788），`listen.py` 没有——同一个
    预算、两种静默，而这里正是那个会被静默截断的进程。两次运行各用一份干净的
    夹具：唤醒会被 watch.py 消费掉，第二次跑就只剩等待了。

    WARN 必须在**等待开始之前**（独立复核建议）：被宿主截断的路径上一个字都不
    会再打印，把 WARN 挪到 `run("watch.py")` 之后它一次都不会输出，而只查
    「stdout 里有没有 WARN」的断言照样绿（变异 M6b 实测）——症状原样复活。

    超预算/预算内的取值按**子进程**的预算派生（`child_budget`，issue #313）：
    `child_env` 剥掉了 `WG_AGENT_TOOL_TIMEOUT`，子进程永远按默认预算判超没超，
    拿父进程放大后的常量来算，在长超时宿主里 `--timeout 270` 会被子进程当成
    「预算内」而这条对照组断言落空。
    """
    agent, wait = child_budget()
    over = agent * 2
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        write_events(home, [event(1, 5, "@机器人 在吗")])
        loud = run_script(home, "listen.py", "--cycles", "1", "--timeout",
                          str(over), "--no-digest", "--probe")
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        write_events(home, [event(2, 5, "@机器人 在吗")])
        quiet = run_script(home, "listen.py", "--cycles", "1", "--timeout",
                           str(wait), "--no-digest", "--probe")
    warn_at = index_of(loud.stdout, "WARN")
    wait_at = index_of(loud.stdout, "等最多")
    return (loud.returncode == 0 and "WARN" in loud.stdout
            and f"--timeout {over}" in loud.stdout
            and quiet.returncode == 0 and "WARN" not in quiet.stdout
            and warn_at >= 0 and wait_at >= 0 and warn_at < wait_at)


@case
def 探针从真实监听读出超时():
    """探针必须从**真实进程**的命令行里读出 `--timeout`（issue #86）。

    纯正则只能证明解析函数本身。这里起一个真的 `daemon/listen.py`（带
    `--timeout 300`），让探针去 WMI 里认它：那条查询曾经只取 pid 不取命令行
    （`wg_up_probe.py:82-84`），正则写得再好也读不到那个数。

    起的是**本仓**的 listen.py 而不是 temp 下的假脚本（issue #94）：探针现在
    按仓库根圈定监听，temp 下的假脚本会被判成异树——那是新用例的活，这条只管
    「本树监听被认出且读得出预算」。

    进程带 `--probe`（issue #94 PB：本树门禁临时监听不进过滤层），所以断言
    从「`listener_specs()` 认出它」调整为「`_match_rows` 原始匹配到它 +
    `parse_timeout` 读出 300」——端到端契约不变（WMI 查询拿到命令行 → 解析
    出秒数，`listener_specs` 的 timeout 字段本来就是这条链的产物），过滤层
    由「本树门禁临时监听不算已有监听」那条钉。
    """
    if os.name != "nt":
        return True  # 本仓只跑 Windows（探针在 posix 上是 ps 兜底）
    if not hasattr(wg_up_probe, "parse_timeout"):
        return False  # 旧代码没有这层解析：读不出命令行里的 --timeout
    over = 300
    with temp_dir(prefix="wg-listener-") as home:
        fixture_home(home)
        proc = subprocess.Popen([sys.executable, str(DAEMON / "listen.py"),
                                 "--timeout", str(over), "--probe"],
                                cwd=str(ROOT), env=child_env(home),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rows = []
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                rows = wg_up_probe._match_rows("daemon[\\\\/]listen\\.py",
                                               exclude=up.self_pids())
                if any(str(pid) == str(proc.pid) for pid, _ in rows):
                    break
                time.sleep(1)
        finally:
            # 收树：venv 的 python.exe 是个转发器，真解释器是它的子进程。
            kill_tree(proc.pid)
    mine = [cmdline for pid, cmdline in rows if str(pid) == str(proc.pid)]
    return bool(mine) and wg_up_probe.parse_timeout(mine[0]) == over
