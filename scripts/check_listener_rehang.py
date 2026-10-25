"""钉住监听空窗：退出要说下一步，过期唤醒不许静默消失（issue #69）。

2026-10-24 一晚上 `listen.py` 掉了 3 次。它按设计「有事就退出叫人」，但退出
之后没有任何东西保证下一次挂上：Agent 回消息、读图、写 issue 的几十秒里，
@ 落进文件而没人看。三次都是用户主动问「是不是没挂上」才发现的。

这条门禁钉住下面这些契约（编号是分组，一组里可能有多条断言），任何一处退回旧行为都红：

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
   `--no-wait`）；显式取值超限时另给 `WARN`，并点名被压回的实际秒数。
7. 续挂不许留下孤儿监听：`watch.py` 打完唤醒块立刻推进游标，没人读的监听会把
   唤醒吃掉，`up.listener_pids()` 还会把孤儿当成「已有监听在跑」拒绝重挂。
8. `listen.py` 的常驻周期不得长于等待预算（`wg_budget.WAIT_BUDGET`，issue #83）。
9. 进入 `watch.py` 之前必须打印「等最多 Ns（第 k 轮）…」，且位置在 `NO_WORK` 之前
   ——被宿主截断时它是唯一的痕迹。
10. `RESTART:` 行不推荐撑不住的取值：短等待回常驻周期、超预算压回预算、其余透传。
11. 文档（`AGENTS.md` / `README.md` / `.agents/rules/water_group.md` / `SKILL.md`）
    里的 `--timeout` 示例不得超预算——Agent 照抄的是它们，不是源码。守护脚本里的
    字符串字面量（`up.py --status` 的提示串）同扫，注释与 docstring 不算推荐值。
    仓外那份私有 SOP 口径不同，由第 26 条单扫。
12. `watch.py` 裸跑时的 `--timeout` 默认值同样不超预算，且 argparse 的实际 default
    必须等于模块常量（写死 `default=240` 而常量仍是 90 会绕过第 8 条）。
13. `--status` 的「监听」行必须带上每个监听**实际的** `--timeout`（issue #86）：
    修复前的监听行只有 pid，修复前起的超预算监听和修复后起的健康监听长得一模一样。
14. 超预算的监听必须在状态行里被点名（带 ⚠ 与秒数），预算内的不得被误报。
15. `listen.py` 显式传超预算 `--timeout` 时自己打 WARN，与 `reply.py --wait` 同口径
    （都写「已压回等待预算 N s、不会因此被截断」，issue #97），预算内的取值不得
    出现 WARN。
16. 探针必须从**真实进程**的命令行里读出 `--timeout`（WMI 那条查询曾经只取 pid）。
17. 默认形态（不传 `--cycles`）自己就能回来：rc=0、只跑一轮、末行 `NO_WORK` +
    `RESTART:`。整次调用的等待额度由所有轮次共享，不能每轮领一份新的（issue #88）。
18. 不限轮数（`--cycles 0`）也得在额度内收手，不是不等就是被宿主砍掉。
19. `clamp_wait()` 只给事实：额度用尽时返回 0，已经过去的时间要从额度里扣掉，
    超预算的取值压回额度内；`listen.py --cycles` 的 argparse 实际 default 必须是 1
    （写死 `default=0` 就退回无限轮）。
20. `clamp_wait()` 算出来的秒数必须真的交给 `watch.py`，而且打印的秒数要跟它
    一致：只断言「函数算得对」时，把实等改回传入值仍全绿（issue #88 复核）。
21. 透传样本值必须真的落在透传区间里且不等于常量本身；预算收缩到装不下
    「短等待」与「预算内」两个区间时，红在**预算不自洽**上，不红在重挂提示的
    措辞上（issue #95：合法的预算收缩不许变成门禁假红）。
22. 监听探针按仓库根圈定（issue #94）：别的 worktree / 门禁临时起的 listen.py
    不算「已有监听」，`reply.py` 的续挂判断与 `--status` 的监听行都只看本树；
    本树的监听（含绝对路径起的孤儿）仍必须被认出。
23. `reply.py --wait` 的打印与传参都是**实际**等待值（`clamp_wait` 之后）：请求
    240s 时打 90s、合计 120s ≤ `AGENT_TOOL_TIMEOUT`、子监听命令行 `--timeout 90`；
    预算内的取值原样透传（issue #97）。
24. `reply.py` 与 `listen.py` 的超预算措辞不得互相矛盾：两处都写「不会因此被
    截断」，都不许再出现「很可能…被截断」（issue #97）。
25. 边界行推荐的「更短」取值必须真的更短：严格小于实际等待值（因此也小于
    `WAIT_BUDGET`）；折半后不足一秒时只留 `--no-wait`（issue #97）。
26. 仓外的私有 SOP（`~/.water-group/rules/*.md`）里的示例命令同样不得超预算——
    它是 Agent「动手前先读」的最高权威文档，却不在第 11 条的扫描范围里，于是
    权威路径反向：仓库说 90、私有 SOP 说 240 / 300（issue #87）。扫的是**可照抄
    的片段**（围栏代码块的整行 + 行内代码），不是全文：私有文档里有「`--wait 180`
    会被砍掉」这类故意讲反面的解释，全文正则扫会误报。私有库不存在时跳过。
27. 私有 SOP 的扫描器**、接线与模块级绑定**都必须真能报出漂移：脏样本必被抓，
    干净样本与反面示例必被放过，判定层的跳过条件、模块级绑定的取根时机与
    `@case` 注册也必被覆盖。否则第 26 条在 CI 上（没有私有库）就是永远绿的空壳。

28. 本树门禁/测试起的临时监听（命令行带 `--probe`）不算「已有监听」：
    `listener_pids()` 与 `listener_specs()` 都不报它，`reply.py` 的续挂判断
    与 `--status` 的监听行因此不受跑门禁影响；不带 `--probe` 的本树监听
    （含绝对路径起的孤儿）仍必须被认出（issue #94 分节 PB）。

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

# `import check_fixture_support` 之前先把 env 里的数据根**记下来**：那模块在 import 期
# 会把它钉成临时目录，事后只剩夹具目录。这里记的是「import 之前真实的那些值」，
# 契约 27 用它们当锚点——验「模块级的 PRIVATE_HOME 仍由它们解析而来」。
# 锚点只用公开信息（env + 家目录），不碰别的模块的私有变量：绑在
# `check_fixture_support._PINNED_HOME` 上时，那变量一旦改名，锚点就静默消失。
# 两条腿都要记：`WATER_GROUP_HOME` 与 `IDLEJOT_WG_HOME`（后者是
# `scripts/check_publication.py` 认的正式候选）。少记一条，锚点在只设了那条的
# 机器上就会算出另一个根，契约 27 平白变红。
_PRE_WGH = os.environ.get("WATER_GROUP_HOME")
_PRE_IWGH = os.environ.get("IDLEJOT_WG_HOME")


def _live_private_home(env=None):
    """真实的私有数据根（**必须在** import `check_fixture_support` 之前调用）。

    `check_fixture_support` 在 import 期把 `WATER_GROUP_HOME` 钉成临时目录（防夹具
    写进实机，issue #79/#89）。之后再取就只剩夹具目录，仓外的私有规则永远扫不到
    （issue #87）。口径与 `scripts/check_publication.py` / `scripts/check_tmp_hygiene.py`
    一致：env 优先，`~/.water-group` 兜底，只有真是目录才算数。

    `env` 只给自检用（传一份 import 之前的 env 快照），生产路径走默认的 `os.environ`。
    """
    source = os.environ if env is None else env
    for cand in (source.get("WATER_GROUP_HOME"),
                 source.get("IDLEJOT_WG_HOME"),
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
import wg_up_probe  # noqa: E402  (issue #94 新用例轮询原始匹配行要用 _match_rows)

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


def status_listener_line(specs):
    """`up.py --status` 的「监听」行（进程探针被换成给定的 specs）。

    `report()` 是总览屏：每一层的答案都汇到那几行里。这里只换掉会碰到真机器
    的那几处（进程探针、端口探测、账号探测、配置体检），其余照常跑，断言只看
    「监听」那一行——它必须说得出每个监听实际用的 `--timeout`（issue #86）。

    `listener_pids` 也一起打桩：issue #86 之前只有它，于是「新门禁 × 旧代码」
    红在**行为**上（行里只有 pid、没有秒数），而不是 AttributeError。
    """
    names = ("listener_specs", "listener_pids", "supervisor_pids", "commands_pids",
             "login_info", "port_open", "config_problems")
    saved = {name: getattr(up, name, None) for name in names}
    pids = [str(s.get("pid")) for s in specs]
    up.listener_specs = lambda *args, **kwargs: [dict(s) for s in specs]
    up.listener_pids = lambda *args, **kwargs: list(pids)
    up.supervisor_pids = lambda *args, **kwargs: []
    up.commands_pids = lambda *args, **kwargs: []
    up.login_info = lambda *args, **kwargs: None
    up.port_open = lambda *args, **kwargs: False
    # 配置体检清空：不带 self_account 的告警会往监听行里塞一个 ⚠，那不是本组
    # 断言要认的东西（配置那组由 check_config_fail_loud.py 管）。
    up.config_problems = lambda *args, **kwargs: []
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            up.report()
    finally:
        for name, value in saved.items():
            if value is None:
                if hasattr(up, name):
                    delattr(up, name)
            else:
                setattr(up, name, value)
    return next((ln for ln in buf.getvalue().splitlines() if ln.startswith("监听")), "")


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
                          "--no-digest", "--probe")
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
                          "--no-digest", "--probe")
    out = proc.stdout
    wake = index_of(out, "WAKE")
    restart = index_of(out, "RESTART:")
    return proc.returncode == 0 and wake >= 0 and restart > wake


@case
def 重挂命令可原样粘贴且回到常驻周期():
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        proc = run_script(home, "listen.py", "--cycles", "1", "--timeout", "1",
                          "--no-digest", "--probe")
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
    """
    over = wg_budget.AGENT_TOOL_TIMEOUT * 2
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        write_events(home, [event(1, 5, "@机器人 在吗")])
        loud = run_script(home, "listen.py", "--cycles", "1", "--timeout",
                          str(over), "--no-digest", "--probe")
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        write_events(home, [event(2, 5, "@机器人 在吗")])
        quiet = run_script(home, "listen.py", "--cycles", "1", "--timeout",
                           str(wg_budget.WAIT_BUDGET), "--no-digest", "--probe")
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
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True)
    mine = [cmdline for pid, cmdline in rows if str(pid) == str(proc.pid)]
    return bool(mine) and wg_up_probe.parse_timeout(mine[0]) == over


@case
def 别的树里的监听不算已有监听():
    """探针只认本 checkout 的监听（issue #94）。

    按 worktree 布局在 temp 下起一个异树 listen.py，先轮询 `_match_rows` 证明
    WMI 已看见它——否则「进程还没注册」和「被过滤掉」分不开，断言会假绿；再断言
    `listener_pids()` 与 `listener_specs()` 都不报它。异树监听被当成「已有监听」
    的后果：`reply.py` 跳过一次必要的续挂，那一次回复之后群里就没人值守。
    """
    if os.name != "nt":
        return True  # 本仓只跑 Windows（探针在 posix 上是 ps 兑底）
    with temp_dir(prefix="wg-foreign-") as home:
        script = home / "water-group" / "daemon" / "listen.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        proc = subprocess.Popen([sys.executable, str(script), "--timeout", "1"],
                                cwd=str(home), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        raw = []
        ok = False
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                raw = [pid for pid, _ in wg_up_probe._match_rows(
                    "daemon[\\\\/]listen\\.py", exclude=up.self_pids())]
                if str(proc.pid) in raw:
                    break
                time.sleep(1)
            if str(proc.pid) not in raw:
                ok = False  # WMI 始终没看见它：用例前提不成立，不能当通过
            else:
                # 断言必须在进程**活着**的时候做（独立复核抓出的假绿）：收树
                # 之后再查，死进程谁也查不到，「不过滤」的坏实现也照样绿。
                ok = (str(proc.pid) not in up.listener_pids()
                      and all(str(s.get("pid")) != str(proc.pid)
                              for s in up.listener_specs()))
        finally:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True)
    return ok


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
    rules_dir = Path(PRIVATE_HOME) / "rules"
    if not rules_dir.is_dir():
        return True
    over = _private_sop_over_budget(rules_dir)
    if over:
        print("  " + "; ".join(f"rules/{name}:{idx}: {flag}"
                                for name, idx, flag in over))
    return not over


@case
def 私有SOP扫描器能识别漂移():
    """扫描器**与接线**都必须真能报出漂移，否则上面那条在 CI 上是永远绿的空壳
    （issue #87）。

    CI 上没有私有库 →「私有SOP示例不超预算」恒绿；没有这条自检，就分不清「没问题」
    和「根本没扫」。四层都要验：

    1. 扫描器：脏的被抓、干净的放过、反面示例（`` `--wait 180` 会被砍掉 ``）放过；
    2. 接线：**直接调用上面那条契约的函数体**（把数据根换掉），脏的必须红，
       干净的与不存在的根必须绿；
    3. 根解析：`WATER_GROUP_HOME` / `IDLEJOT_WG_HOME` 指到哪，数据根就该解析到哪；
    4. 模块级绑定与注册：`PRIVATE_HOME` 必须仍由 import **之前**记下的 env 值解析
       而来（取根时机被颠倒时它会落进夹具目录，issue #87 的盲区原样重现）、
       本机有私有库时不能是 `None`、契约本身不能从 `CASES` 里被摘掉。

    第 2 层刻意直接调函数体而不是复制它的逻辑：把契约改成永远 `return True`，
    或者把跳过条件放宽，复制逻辑的自检照样绿，直接调就会红。第 4 层同理：
    前三层验的都是函数，模块级的绑定与注册出了错，它们照样全绿。
    """
    with temp_dir(prefix="wg-sop-selftest-") as home:
        dirty_root = home / "dirty-root"
        clean_root = home / "clean-root"
        for root in (dirty_root, clean_root):
            (root / "rules").mkdir(parents=True, exist_ok=True)
        (dirty_root / "rules" / "water_group.md").write_text(
            "```bash\n"
            "python water-group/daemon/watch.py --timeout 240\n"
            "```\n"
            "工具调用有超时上限，`--wait 180` 会被砍掉。\n"
            "但 `reply.py <id> \"<正文>\" --wait 240` 是推荐写法。\n",
            encoding="utf-8")
        (clean_root / "rules" / "water_group.md").write_text(
            "```bash\n"
            "python water-group/daemon/watch.py --timeout 90\n"
            "```\n"
            "工具调用有超时上限，`--wait 180` 会被砍掉。\n"
            "但 `reply.py <id> \"<正文>\" --wait 90` 是推荐写法。\n",
            encoding="utf-8")

        # 1) 扫描器：脏的被抓、反面示例放过
        over = _private_sop_over_budget(dirty_root / "rules")
        hits = {(name, idx) for name, idx, _flag in over}
        scanner_ok = (("water_group.md", 2) in hits
                      and ("water_group.md", 5) in hits
                      and ("water_group.md", 4) not in hits)

        # 2) 接线：调契约 26 的函数体，脏根必须红，干净根与不存在的根必须绿
        saved_home = PRIVATE_HOME
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                globals()["PRIVATE_HOME"] = dirty_root
                dirty_red = not 私有SOP示例不超预算()
                globals()["PRIVATE_HOME"] = clean_root
                clean_green = 私有SOP示例不超预算()
                globals()["PRIVATE_HOME"] = None
                missing_green = 私有SOP示例不超预算()
        finally:
            globals()["PRIVATE_HOME"] = saved_home
        wired_ok = dirty_red and clean_green and missing_green

        # 3) 根解析：env 指到哪就解析到哪（两条腿都要认）
        resolved = _live_private_home({"WATER_GROUP_HOME": str(dirty_root)})
        root_ok = resolved is not None and Path(resolved) == dirty_root
        resolved_alt = _live_private_home({"IDLEJOT_WG_HOME": str(dirty_root)})
        root_ok = root_ok and (resolved_alt is not None
                               and Path(resolved_alt) == dirty_root)

        # 4) 模块级绑定与注册：取根时机被颠倒 / 绑定被置空 / case 注册被摘掉。
        #    锚点是 import 之前记下的 env 快照（公开信息）：模块级的 PRIVATE_HOME
        #    必须仍由它解析而来。取根时机一旦被颠倒，它会落回夹具目录，issue #87
        #    的盲区原样重现——而上面三层验的都是函数，照样全绿。
        #    两条腿都要喂（WATER_GROUP_HOME 与 IDLEJOT_WG_HOME）：少喂一条，
        #    只设了那条的机器上锚点会算出另一个根，契约 27 平白变红。
        expected_home = _live_private_home({"WATER_GROUP_HOME": _PRE_WGH,
                                            "IDLEJOT_WG_HOME": _PRE_IWGH})
        bound_ok = PRIVATE_HOME == expected_home
        registered_ok = 私有SOP示例不超预算 in CASES

    return scanner_ok and wired_ok and root_ok and bound_ok and registered_ok


@case
def 本树门禁临时监听不算已有监听():
    """本树门禁起的临时监听不算「已有监听」（issue #94 PB）。

    按仓库根圈定（PA）只挡得住**别的树**：门禁在**本树**起的临时监听用的是
    本仓绝对路径（`run_script` 的 `str(DAEMON / "listen.py")` + `cwd=ROOT`），
    命令行形状与真监听同根，`_own_checkout()` 认不出来——issue #94 复现里
    那样的进程被 `listener_pids()` 报出，挂机时跑门禁，`reply.py` 的续挂
    判断就跟着跳过一次必要的续挂，门禁跑完临时监听退出，群里没人值守。

    这里起两个同形本树监听（本仓绝对路径、`cwd=ROOT`、隔离
    `WATER_GROUP_HOME`）：一个带 `--probe`（门禁临时标识），一个不带。先轮询
    `_match_rows` 证明 WMI 都看见了它们（否则「进程还没注册」和「被过滤掉」
    分不开）；再断言带 `--probe` 的两个 finder 都不报，**不带 `--probe` 的
    必须报**——后半条防的是「过滤层整体失效」的坏实现：把两个 finder 改成
    永远返回空，前半条照样绿，而不带 `--probe` 的本树监听（`reply.py` 续挂、
    `RESTART:` 重挂、人工挂的）会被一起漏掉，那正是 issue #94 要保的东西。

    断言都做在进程**活着**的时候（独立复核抓出的假绿）：收树之后再查，死
    进程谁也查不到，「不过滤」的坏实现也照样绿。
    """
    if os.name != "nt":
        return True  # 本仓只跑 Windows（探针在 posix 上是 ps 兑底）
    with temp_dir(prefix="wg-probe-") as home:
        fixture_home(home)
        procs = []
        try:
            for extra in (("--probe",), ()):
                procs.append(subprocess.Popen(
                    [sys.executable, str(DAEMON / "listen.py"),
                     "--cycles", "1", "--timeout", "20", "--no-digest", *extra],
                    cwd=str(ROOT), env=child_env(home),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            marked, plain = str(procs[0].pid), str(procs[1].pid)
            raw = []
            deadline = time.time() + 30
            while time.time() < deadline:
                raw = [pid for pid, _ in wg_up_probe._match_rows(
                    "daemon[\\\\/]listen\\.py", exclude=up.self_pids())]
                if marked in raw and plain in raw:
                    break
                time.sleep(1)
            if marked not in raw or plain not in raw:
                return False  # WMI 没看见：用例前提不成立，不能当通过
            pids = up.listener_pids()
            specs = [str(s.get("pid")) for s in up.listener_specs()]
            return (marked not in pids and marked not in specs
                    and plain in pids and plain in specs)
        finally:
            for proc in procs:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               capture_output=True)


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
