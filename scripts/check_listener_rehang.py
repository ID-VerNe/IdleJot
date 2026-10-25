"""钉住监听空窗：退出要说下一步，过期唤醒不许静默消失（issue #69）。

2026-10-24 一晚上 `listen.py` 掉了 3 次。它按设计「有事就退出叫人」，但退出
之后没有任何东西保证下一次挂上：Agent 回消息、读图、写 issue 的几十秒里，
@ 落进文件而没人看。三次都是用户主动问「是不是没挂上」才发现的。

这条门禁钉十四个契约，任何一处退回旧行为都红：

1. `listen.py` 的每种退出（WAKE / NO_WORK / WATCH_FAILED）都以一行
   `RESTART: <python> water-group/daemon/listen.py --timeout N` 收尾，且短等待
   （`reply.py --wait 6` 这类）不透传成新的常驻周期——重挂回到常驻周期。
2. `reply.py` 发送成功后默认就地续挂（`--wait` 默认 = 宿主工具超时减发送余量，
   `--no-wait` 关闭），已有监听时不重复 spawn。
3. `watch.py` 超时块把「因过期被丢弃的未回唤醒」计数并列出 id，不再与
   「从没来过」长得一模一样。
4. 冷启动（游标归零）仍不翻历史积压：那是 `check_pending_reply.py` 钉住的
   既有契约，过期统计不得把它变成新的唤醒源。
5. 默认等待必须明显短于宿主的常见工具调用超时
   （`DEFAULT_WAIT + SEND_BUDGET <= AGENT_TOOL_TIMEOUT`）：调用被截断时父进程
   和子监听同组同死，而且那条路径上一个字都不打印（issue #79）。
6. 等待开始前把这条边界印出来：实际秒数、合计值、两个出口（`--wait` 更小 /
   `--no-wait`）；显式取值超限时另给 `WARN`。
7. 续挂不许留下孤儿监听：`watch.py` 打完唤醒块立刻推进游标，没人读的监听会把
   唤醒吃掉，`up.listener_pids()` 还会把孤儿当成「已有监听在跑」拒绝重挂。
8. `listen.py` 的常驻周期不得长于等待预算（`wg_budget.WAIT_BUDGET`，issue #83）。
9. 进入 `watch.py` 之前必须打印「等最多 Ns（第 k 轮）…」，且位置在 `NO_WORK` 之前
   ——被宿主截断时它是唯一的痕迹。
10. `RESTART:` 行不推荐撑不住的取值：短等待回常驻周期、超预算压回预算、其余透传。
11. 文档（`AGENTS.md` / `README.md` / `.agents/rules/water_group.md` / `SKILL.md`）
    里的 `--timeout` 示例不得超预算——Agent 照抄的是它们，不是源码。守护脚本里的
    字符串字面量（`up.py --status` 的提示串）同扫，注释与 docstring 不算推荐值。
    仓外那份私有 SOP 口径不同，由第 13 条单扫。
12. `watch.py` 裸跑时的 `--timeout` 默认值同样不超预算，且 argparse 的实际 default
    必须等于模块常量（写死 `default=240` 而常量仍是 90 会绕过第 8 条）。
13. 仓外的私有 SOP（`~/.water-group/rules/*.md`）里的示例命令同样不得超预算——
    它是 Agent「动手前先读」的最高权威文档，却不在第 11 条的扫描范围里，于是
    权威路径反向：仓库说 90、私有 SOP 说 240 / 300（issue #87）。扫的是**可照抄
    的片段**（围栏代码块的整行 + 行内代码），不是全文：私有文档里有「`--wait 180`
    会被砍掉」这类故意讲反面的解释，全文正则扫会误报。私有库不存在时跳过。
14. 私有 SOP 扫描器必须真能报出漂移：脏样本必被抓，干净样本与反面示例必被放过。
    否则第 13 条在 CI 上（没有私有库）就是永远绿的空壳。

全程临时 `WATER_GROUP_HOME` + `IDLEJOT_WG_CONFIG`，不碰实机状态：

    .venv/Scripts/python.exe scripts/check_listener_rehang.py
"""
import ast
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _live_private_home():
    """真实的私有数据根（**必须在** import `check_fixture_support` 之前调用）。

    `check_fixture_support` 在 import 期把 `WATER_GROUP_HOME` 钉成临时目录（防夹具
    写进实机，issue #79/#89）。之后再取就只剩夹具目录，仓外的私有规则永远扫不到
    （issue #87）。口径与 `scripts/check_publication.py` / `scripts/check_tmp_hygiene.py`
    一致：env 优先，`~/.water-group` 兜底，只有真是目录才算数。
    """
    for cand in (os.environ.get("WATER_GROUP_HOME"),
                 os.environ.get("IDLEJOT_WG_HOME"),
                 Path.home() / ".water-group"):
        if cand and Path(cand).is_dir():
            return Path(cand)
    return None


PRIVATE_HOME = _live_private_home()

import check_fixture_support  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import temp_dir  # noqa: E402

DAEMON = ROOT / "water-group" / "daemon"
sys.path.insert(0, str(DAEMON))

import listen  # noqa: E402
import reply  # noqa: E402
import up  # noqa: E402
import watch  # noqa: E402
import wg_budget  # noqa: E402

# 外部测试变量会把 state 又指回实机，这里必须清掉，只留临时数据根。
ISOLATING_VARS = ("IDLEJOT_WG_STATE", "TULPA_WG_STATE", "IDLEJOT_WG_DB",
                  "IDLEJOT_WG_MEMORY_DB")
ACCOUNT = "10001"
GROUP = "1:group:123"

# 文档里挂着 `--timeout N` 示例的四处（issue #83）：Agent 照抄的就是这些命令。
DOC_SAMPLES = ("AGENTS.md", "README.md", ".agents/rules/water_group.md",
               ".agents/skills/water-group/SKILL.md")
# 守护脚本里也有会被人照抄的提示串（`up.py --status` 的「下一步」）。
DAEMON_SOURCES = tuple(sorted(p.relative_to(ROOT).as_posix()
                              for p in (DAEMON.glob("*.py"))))

# 私有规则文档在仓外（`~/.water-group/rules/`），但它是 Agent「动手前先读」的最高
# 权威文档（`AGENTS.md` §0 与 `SKILL.md` 都这么写），示例命令同样不得超预算
# （issue #87）。脚本名单不硬编码：daemon 脚本会增删，名单从目录实时取。
DAEMON_NAMES = tuple(sorted(Path(rel).name for rel in DAEMON_SOURCES))
_DAEMON_ALTS = "|".join(re.escape(name) for name in DAEMON_NAMES)
DAEMON_HINT_RE = re.compile(r"daemon/" + (f"|{_DAEMON_ALTS}" if _DAEMON_ALTS else ""))
TIMEOUT_FLAG_RE = re.compile(r"--(?:timeout|wait)[ =](\d+)")
FENCE_RE = re.compile(r"^\s*```")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def fixture_home(home):
    """最小身份配置：watch.py 启动就校验 self_account（issue #51）。"""
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"self_account": ACCOUNT, "onebot_token": "rehang-fixture"},
                   ensure_ascii=False), encoding="utf-8")


def child_env(home):
    env = dict(os.environ)
    env["WATER_GROUP_HOME"] = str(home)
    env["IDLEJOT_WG_CONFIG"] = str(home / "config.json")
    env["PYTHONUTF8"] = "1"
    for name in ISOLATING_VARS:
        env.pop(name, None)
    return env


def run_script(home, script, *args, timeout=120):
    return subprocess.run([sys.executable, str(DAEMON / script), *args],
                          cwd=str(ROOT), env=child_env(home), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def write_events(home, rows):
    path = home / "state" / "wg_events.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")


def event(eid, age_seconds, content, mention=True):
    """一条落盘事件。age_seconds 是「多少秒前」，与 wake_expired 的口径一致。"""
    segments = ([{"type": "at", "qq": ACCOUNT}] if mention else []) + \
               [{"type": "text", "text": content}]
    return {"id": eid, "timestamp": (time.time() - age_seconds) * 1000,
            "is_self": False, "sender": "甲", "content": content,
            "segments": segments, "conversation_id": GROUP}


def lines(out):
    return [ln.strip() for ln in (out or "").splitlines() if ln.strip()]


def index_of(out, prefix):
    for idx, ln in enumerate(lines(out)):
        if ln.startswith(prefix):
            return idx
    return -1


def _arg_default(rel, flag):
    """`add_argument(flag, default=…)` 的**实际**取值（字面量 / 模块常量都解）。

    只断言模块常量不够：把 `default=240` 直接写死、常量仍留 90 时，「常量 ≤ 预算」
    那条照样绿（变异实测）。所以解析 argparse 自己的 default，再要求它与常量一致。
    """
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    consts = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, int):
                consts[node.targets[0].id] = value.value
            elif (isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)
                    and value.value.id == "wg_budget" and hasattr(wg_budget, value.attr)):
                consts[node.targets[0].id] = getattr(wg_budget, value.attr)
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
    return None


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


def _sop_snippets(text):
    """私有规则文档里**可照抄**的片段（issue #87）。

    两类片段的口径不同，所以分开返回：

    - 围栏代码块里的整行：命令本身就是照抄对象，块里任何 `--timeout N` /
      `--wait N` 都算推荐值。
    - 行内代码（`` `...` ``）：多半是「引用某个参数名」（如 `` `--wait` ``），
      只有片段里点名了 daemon 脚本时才算推荐值。否则「`--wait 180` 会被砍掉」
      这类**故意讲反面**的解释会被误判——那是私有 SOP 里的真实陷阱。
    """
    blocks, inlines = [], []
    fenced = False
    for idx, line in enumerate(text.splitlines(), 1):
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            blocks.append((idx, line))
            continue
        for match in INLINE_CODE_RE.finditer(line):
            inlines.append((idx, match.group(1)))
    return blocks, inlines


def _private_sop_over_budget(rules_dir):
    """私有规则文档里超预算的照抄取值（issue #87）。

    返回 [(文件名, 行号, 原文片段)]，供契约打印。
    """
    over = []
    for path in sorted(Path(rules_dir).glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        blocks, inlines = _sop_snippets(text)
        for idx, line in blocks:
            for match in TIMEOUT_FLAG_RE.finditer(line):
                if int(match.group(1)) > wg_budget.WAIT_BUDGET:
                    over.append((path.name, idx, match.group(0)))
        for idx, snippet in inlines:
            if not DAEMON_HINT_RE.search(snippet):
                continue
            for match in TIMEOUT_FLAG_RE.finditer(snippet):
                if int(match.group(1)) > wg_budget.WAIT_BUDGET:
                    over.append((path.name, idx, match.group(0)))
    return over


# ---------- listen.py：退出时给出可粘贴的下一步 ----------


@case
def 静默超时后带重挂提示():
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        proc = run_script(home, "listen.py", "--cycles", "1", "--timeout", "1",
                          "--no-digest")
    out = proc.stdout
    work = index_of(out, "NO_WORK")
    restart = index_of(out, "RESTART:")
    return proc.returncode == 0 and work >= 0 and restart > work


@case
def 唤醒之后带重挂提示():
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        write_events(home, [event(1, 5, "@机器人 在吗")])
        proc = run_script(home, "listen.py", "--cycles", "1", "--timeout", "10",
                          "--no-digest")
    out = proc.stdout
    wake = index_of(out, "WAKE")
    restart = index_of(out, "RESTART:")
    return proc.returncode == 0 and wake >= 0 and restart > wake


@case
def 重挂命令可原样粘贴且回到常驻周期():
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        proc = run_script(home, "listen.py", "--cycles", "1", "--timeout", "1",
                          "--no-digest")
    hint = next((ln for ln in lines(proc.stdout) if ln.startswith("RESTART:")), "")
    # 短等待 1s 是 reply --wait 的临时边界，重挂必须回到常驻周期；
    # --cycles / --no-digest 同样不该透传。周期值读常量而不是写死 300（issue #83）：
    # 与 #79 的 `"默认 240s"` 同一种耦合，断言存在性时不该顺手锁死数值。
    return (hint.endswith(f"--timeout {listen.DEFAULT_TIMEOUT}") and "listen.py" in hint
            and "--no-digest" not in hint and "--cycles" not in hint
            and listen.DEFAULT_TIMEOUT > 0)


# ---------- watch.py：过期未回唤醒不再静默 ----------


@case
def 过期未回唤醒有计数():
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        write_events(home, [event(9, 3 * 3600, "@机器人 旧话题"),
                            event(10, 1, "路过回一句", mention=False)])
        (home / "state" / "wg_watch.txt").write_text("8", encoding="utf-8")
        proc = run_script(home, "watch.py", "--timeout", "2")
    out = proc.stdout
    idx = index_of(out, "EXPIRED:")
    return (proc.returncode == 2 and idx >= 0
            and not any(ln == "WAKE" for ln in lines(out))
            and "9" in lines(out)[idx]
            and "TIMEOUT" in lines(out))


@case
def 冷启动不翻历史积压():
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        write_events(home, [event(9, 3 * 3600, "@机器人 旧话题"),
                            event(10, 1, "路过回一句", mention=False)])
        proc = run_script(home, "watch.py", "--timeout", "2")
    out = proc.stdout
    return (proc.returncode == 2 and "TIMEOUT" in lines(out)
            and index_of(out, "EXPIRED:") < 0
            and not any(ln == "WAKE" for ln in lines(out)))


# ---------- reply.py：默认续挂 ----------


@case
def 发送后默认续挂():
    """默认续挂必须存在，且 `--help` 自述的是**实际**默认值（issue #79 追评）。

    这里曾经写死 `"默认 240s"`，于是「默认续挂存在」被绑成了「默认值等于 240」：
    改默认值就得改门禁，门禁从契约变成绊脚石。现在读常量拼断言串，数值边界交给
    `默认等待短于宿主超时` 那条——两件事各管一头。
    """
    proc = subprocess.run([sys.executable, str(DAEMON / "reply.py"), "--help"],
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60,
                          env=dict(os.environ, PYTHONUTF8="1"))
    out = proc.stdout or ""
    return (proc.returncode == 0 and "--no-wait" in out and reply.DEFAULT_WAIT > 0
            and f"默认 {reply.DEFAULT_WAIT}s" in out)


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
    显式把 `--wait` 开到宿主超时以上时，还要多一行 `WARN` 点名后果。
    """
    originals = (up.listener_pids, reply.winconsole.run_passthrough)
    up.listener_pids = lambda *args, **kwargs: []
    reply.winconsole.run_passthrough = lambda *args, **kwargs: 0

    def capture(seconds):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            reply.wait_for_next(seconds)
        return buf.getvalue()

    # 用显式的 60 而不是 DEFAULT_WAIT 当正常样本：默认值是否合规由
    # `默认等待短于宿主超时` 管，这条只管「提示在不在、内容够不够改参」，
    # 否则两个契约会一起红、分不清坏在哪。
    too_long = max(reply.AGENT_TOOL_TIMEOUT, 60) + 60
    try:
        normal = capture(60)
        over = capture(too_long)
    finally:
        up.listener_pids, reply.winconsole.run_passthrough = originals

    return ("最多 60s" in normal
            and f"约 {60 + reply.SEND_BUDGET}s" in normal
            and "被杀" in normal and "--no-wait" in normal
            and "WARN" not in normal
            and "WARN" in over and f"{too_long}s" in over)


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
def 等待开始前声明边界():
    """进入 `watch.py` 之前必须说一句「在等、等多久」（issue #83）。

    这条是 issue 的决定性一条：`listen.py` 按设计不返回，群里一直安静时宿主迟早
    会截断这次调用，而截断路径上一个字都不打印。所以断言不是「有输出」，而是
    「等待开始那一句在 `TIMEOUT` 之前就出来了」——被砍掉时它是唯一的痕迹。

    锚点必须是 `TIMEOUT`（`watch.py` 等完之后才打的那行），不能用 `NO_WORK`：
    两者都在等待之后，把这句话挪到 `run()` 下面仍然会排在 `NO_WORK` 前面，
    只有排在 `TIMEOUT` 之前才证明「等待开始前」。变异实测：把打印挪到
    `run()` 之后，这条红。
    """
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        proc = run_script(home, "listen.py", "--cycles", "1", "--timeout", "1",
                          "--no-digest")
    out = proc.stdout
    waiting = index_of(out, "等最多 1s（第 1 轮）")
    timed_out = index_of(out, "TIMEOUT")
    return (proc.returncode == 0 and waiting >= 0 and timed_out >= 0
            and timed_out > waiting and index_of(out, "NO_WORK") > timed_out)


@case
def 超预算的取值不写进重挂命令():
    """RESTART 行不推荐撑不住的取值（issue #83）。

    `restart_hint` 原来把任何 ≥60s 的取值原样透传，于是 `--timeout 300` 被推荐
    回来、被截断、再被推荐回来。现在三个区间：短等待回到常驻周期、超预算压回
    预算、其余原样透传。
    """
    def hint(seconds):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            listen.restart_hint(seconds)
        return buf.getvalue()

    inside = wg_budget.WAIT_BUDGET - 10
    return (f"--timeout {wg_budget.WAIT_BUDGET}" in hint(300)
            and f"--timeout {wg_budget.WAIT_BUDGET}" in hint(wg_budget.AGENT_TOOL_TIMEOUT * 2)
            and f"--timeout {wg_budget.WAIT_BUDGET}" in hint(wg_budget.WAIT_BUDGET)
            and f"--timeout {wg_budget.WAIT_BUDGET}" in hint(1)
            and f"--timeout {inside}" in hint(inside))


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

    仓外那份私有 SOP 由下一条契约单扫（口径不同，见「私有SOP示例不超预算」）。
    """
    over = []
    for rel in DOC_SAMPLES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        over += [f"{rel}: --timeout {value}"
                 for value in re.findall(r"--timeout[ =](\d+)", text)
                 if int(value) > wg_budget.WAIT_BUDGET]
    for rel in DAEMON_SOURCES:
        over += [f"{rel}: --timeout {value}"
                 for value in _quoted_timeouts(rel)
                 if int(value) > wg_budget.WAIT_BUDGET]
    if over:
        print("  " + "; ".join(over))
    return not over


@case
def 私有SOP示例不超预算():
    """仓外的私有规则文档同样不得教超预算的取值（issue #87）。

    私有 SOP（`~/.water-group/rules/*.md`）是 Agent「动手前先读」的最高权威文档
    （`AGENTS.md` §0 与 `SKILL.md` 都这么写），而第 11 条只扫仓内那四份——于是权威
    路径反向：仓库说 90、私有 SOP 说 240 / 300，Agent 照抄后者就复现 #83 原报。

    口径与第 11 条**不同**，别当同一件事：这里扫的是「可照抄的片段」（围栏代码块
    的整行 + 行内代码），不是全文。私有文档里有「`--wait 180` 会被砍掉」这类**故意
    讲反面**的解释，全文正则扫会误报；行内代码还要求点名 daemon 脚本才算推荐值。
    围栏块里不点名脚本也照样算——块里的命令本身就是照抄对象。

    私有库不存在（CI、全新机器）时跳过：这条在 CI 上恒绿，所以另配一条自检契约
    证明扫描器不是瞎的。
    """
    if PRIVATE_HOME is None:
        return True
    rules_dir = PRIVATE_HOME / "rules"
    if not rules_dir.is_dir():
        return True
    over = _private_sop_over_budget(rules_dir)
    if over:
        print("  " + "; ".join(f"rules/{name}:{idx}: {flag}"
                                for name, idx, flag in over))
    return not over


@case
def 私有SOP扫描器能识别漂移():
    """扫描器必须真能报出漂移，否则上面那条在 CI 上是永远绿的空壳（issue #87）。

    CI 上没有私有库 →「私有SOP示例不超预算」恒绿；没有这条自检，就分不清「没问题」
    和「根本没扫」。三份样本各自只该命中该命中的：脏的被抓、干净的放过、
    反面示例（`` `--wait 180` 会被砍掉 ``）也放过。
    """
    with temp_dir(prefix="wg-sop-selftest-") as home:
        rules = home / "rules"
        rules.mkdir(parents=True, exist_ok=True)
        (rules / "dirty.md").write_text(
            "```bash\n"
            "python water-group/daemon/watch.py --timeout 240\n"
            "```\n", encoding="utf-8")
        (rules / "clean.md").write_text(
            "```bash\n"
            "python water-group/daemon/watch.py --timeout 90\n"
            "```\n", encoding="utf-8")
        (rules / "counter.md").write_text(
            "工具调用有超时上限，`--wait 180` 会被砍掉。\n"
            "但 `reply.py <id> \"<正文>\" --wait 240` 是推荐写法。\n",
            encoding="utf-8")
        over = _private_sop_over_budget(rules)
    hits = {(name, idx) for name, idx, _flag in over}
    return (("dirty.md", 2) in hits
            and ("counter.md", 2) in hits
            and ("counter.md", 1) not in hits
            and ("clean.md", 2) not in hits)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    bad = 0
    for fn in CASES:
        try:
            got = fn()
        except Exception as exc:  # noqa: BLE001
            got = False
            print(f"✗ {fn.__name__}: {type(exc).__name__}: {exc}")
            bad += 1
            continue
        if got:
            print(f"✓ {fn.__name__}")
        else:
            bad += 1
            print(f"✗ {fn.__name__}")
    print("全部通过" if not bad else f"{bad} 项失败")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
