import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ._common import (
    case, ROOT, DAEMON, fs, temp_dir,
    run_script, fixture_home, child_env,
    FENCE_RE, INLINE_CODE_RE, DAEMON_HINT_RE, TIMEOUT_FLAG_RE,
    PRIVATE_HOME, _PRE_WGH, _PRE_IWGH, _live_private_home,
    CASES,
    listen, reply, up, wg_budget, wg_up_probe,
)


# 这两个 SOP 函数**必须同处一个模块**：`私有SOP扫描器能识别漂移` 在自检里用
# `globals()["PRIVATE_HOME"] = dirty_root` 重绑 `PRIVATE_HOME`，再直接调
# `私有SOP示例不超预算()`（读 `PRIVATE_HOME`）。`globals()` 返回的是「定义该函数
# 的模块」的命名空间，所以两个函数必须在同一模块里，`PRIVATE_HOME` 的重绑才会被
# 读取侧看到。`PRIVATE_HOME` 本身由 `_common` 在 import `check_fixture_support`
# 之前初始化（保锚点序），这里 `from ._common import PRIVATE_HOME` 在本模块建一份
# `ownership.PRIVATE_HOME` 绑定供 `globals()` 重绑——import-then-rebind 是桥
# （issue #169 PF-2）。
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


@case
def 别的树里的监听不算已有监听():
    """探针只认本 checkout 的监听（issue #94；对照组 issue #106）。

    按 worktree 布局在 temp 下起一个异树 listen.py，同时起一个**本树同形监听**
    （本仓绝对路径、`cwd=ROOT`、不带 `--probe`，与「本树门禁临时监听不算已有
    监听」的对照组同口径）当对照。先轮询 `_match_rows` 证明 WMI 已看见两个
    进程——否则「进程还没注册」和「被过滤掉」分不开，断言会假绿；再断言异树
    的那个 `listener_pids()` 与 `listener_specs()` 都不报、本树对照的两个
    finder 都必须报。

    对照组防的是「过滤层整体失效」（issue #106）：只断言「异树不被报出」时，
    把两个 finder 改成永远返回空（或把 `_own_checkout` 改成永远 False），
    异树照样「不被报出」，用例全绿——而异树监听「不被报出」与「什么都不报出」
    在断言下长得一模一样。契约 22 声明的后半句「本树的监听（含绝对路径起的
    孤儿）仍必须被认出」，自此才有断言兜底。

    异树监听被当成「已有监听」的后果：`reply.py` 跳过一次必要的续挂，那一次
    回复之后群里就没人值守。断言都做在进程**活着**的时候（收树之后再查，死
    进程谁也查不到，「不过滤」的坏实现也照样绿）。
    """
    if os.name != "nt":
        return True  # 本仓只跑 Windows（探针在 posix 上是 ps 兑底）
    with temp_dir(prefix="wg-foreign-") as home:
        fixture_home(home)
        script = home / "water-group" / "daemon" / "listen.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        procs = [subprocess.Popen([sys.executable, str(script), "--timeout", "1"],
                                  cwd=str(home), stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)]
        # 对照组（issue #106）：本树同形监听，两个 finder 都必须认出它——
        # 只钉「异树不报」时，过滤层整体失效（恒空 / _own_checkout 恒 False）
        # 会让这条用例假绿。
        procs.append(subprocess.Popen(
            [sys.executable, str(DAEMON / "listen.py"),
             "--cycles", "1", "--timeout", "20", "--no-digest"],
            cwd=str(ROOT), env=child_env(home),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        foreign, plain = str(procs[0].pid), str(procs[1].pid)
        raw = []
        ok = False
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                raw = [pid for pid, _ in wg_up_probe._match_rows(
                    "daemon[\\\\/]listen\\.py", exclude=up.self_pids())]
                if foreign in raw and plain in raw:
                    break
                time.sleep(1)
            if foreign not in raw or plain not in raw:
                ok = False  # WMI 始终没看见：用例前提不成立，不能当通过
            else:
                # 断言必须在进程**活着**的时候做（独立复核抓出的假绿）：收树
                # 之后再查，死进程谁也查不到，「不过滤」的坏实现也照样绿。
                pids = up.listener_pids()
                specs = [str(s.get("pid")) for s in up.listener_specs()]
                ok = (foreign not in pids and foreign not in specs
                      and plain in pids and plain in specs)
        finally:
            for proc in procs:
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

    1. 扫描器：脏的被抓、干净的放过、反面示例（超预算但不点名的行内代码）放过；
    2. 接线：**直接调用上面那条契约的函数体**（把数据根换掉），脏的必须红，
       干净的与不存在的根必须绿；
    3. 根解析：`WATER_GROUP_HOME` / `IDLEJOT_WG_HOME` 指到哪，数据根就该解析到哪；
       **家目录兜底腿（`~/.water-group`）也要认**——它的期望值不经被测函数自己算，
       否则兜底腿被删掉或改指别处时这一层照样绿，而 `PRIVATE_HOME` 变 `None`
       会让判定层静默跳过，issue #87 的盲区原样重现；
    4. 模块级绑定与注册：`PRIVATE_HOME` 必须仍由 import **之前**记下的 env 值解析
       而来（取根时机被颠倒时它会落进夹具目录，issue #87 的盲区原样重现）、
       本机有私有库时不能是 `None`、契约本身不能从 `CASES` 里被摘掉。
       锚点按口径**独立拼装**，不经被测函数自己算（自指就等于没测）。

    第 2 层刻意直接调函数体而不是复制它的逻辑：把契约改成永远 `return True`，
    或者把跳过条件放宽，复制逻辑的自检照样绿，直接调就会红。第 4 层同理：
    前三层验的都是函数，模块级的绑定与注册出了错，它们照样全绿。

    **夹具样本的秒数取自 `wg_budget.WAIT_BUDGET` 的相对值，不写死具体数字**
    （脏 = 预算 +1、干净 = 预算本身、反面示例 = 预算 +2）。写死会让这条自检
    在**预算常量一改**时假红：脏样本不再是「超预算」的那一侧，判定层就不再
    报它，自检随之变红——而预算变化本身并不是本次改动坏了（同一个坑：
    #79 的「默认 240s」、#83 的「--timeout 300」）。
    """
    # 脏样本必须**恰好超预算一格**；干净样本**恰好等于预算**（判定层是严格
    # `> WAIT_BUDGET`，等于不算超）；反面示例要比脏样本还大，确保它在任何
    # 预算下都是「超预算」的值 —— 它该被放过的理由是**没有点名 daemon 脚本**，
    # 不是「值不够大」。
    over_val = wg_budget.WAIT_BUDGET + 1
    clean_val = wg_budget.WAIT_BUDGET
    counter_val = wg_budget.WAIT_BUDGET + 2
    with temp_dir(prefix="wg-sop-selftest-") as home:
        dirty_root = home / "dirty-root"
        clean_root = home / "clean-root"
        for root in (dirty_root, clean_root):
            (root / "rules").mkdir(parents=True, exist_ok=True)
        (dirty_root / "rules" / "water_group.md").write_text(
            "```bash\n"
            f"python water-group/daemon/watch.py --timeout {over_val}\n"
            "```\n"
            f"工具调用有超时上限，`--wait {counter_val}` 会被砍掉。\n"
            f"但 `reply.py <id> \"<正文>\" --wait {over_val}` 是推荐写法。\n",
            encoding="utf-8")
        (clean_root / "rules" / "water_group.md").write_text(
            "```bash\n"
            f"python water-group/daemon/watch.py --timeout {clean_val}\n"
            "```\n"
            f"工具调用有超时上限，`--wait {counter_val}` 会被砍掉。\n"
            f"但 `reply.py <id> \"<正文>\" --wait {clean_val}` 是推荐写法。\n",
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

        # 3) 根解析：env 指到哪就解析到哪。**三条腿都要认**——两条 env 腿
        #    （`WATER_GROUP_HOME` / `IDLEJOT_WG_HOME`）之外还有家目录兜底腿
        #    （`~/.water-group`）。兜底腿的**期望值不经被测函数自己算**：
        #    直接按口径拼出 `Path.home()/".water-group"`，再拿被测函数的解析
        #    结果来比。否则这一层自指（用 `_live_private_home` 算期望值），
        #    兜底腿被删掉或改指别处时它照样绿——而 `PRIVATE_HOME` 会变 None，
        #    判定层随即静默跳过，issue #87 的盲区原样重现。
        resolved = _live_private_home({"WATER_GROUP_HOME": str(dirty_root)})
        root_ok = resolved is not None and Path(resolved) == dirty_root
        resolved_alt = _live_private_home({"IDLEJOT_WG_HOME": str(dirty_root)})
        root_ok = root_ok and (resolved_alt is not None
                               and Path(resolved_alt) == dirty_root)
        # 兜底腿：喂空 env → 只剩家目录腿可用。家目录本身不存在时这条腿
        # 压根不成立，跳过（CI 与全新机器都属正常情况）。
        home_leg = Path.home() / ".water-group"
        if home_leg.is_dir():
            resolved_home = _live_private_home({})
            root_ok = root_ok and (resolved_home is not None
                                   and Path(resolved_home) == home_leg)

        # 4) 模块级绑定与注册：取根时机被颠倒 / 绑定被置空 / case 注册被摘掉。
        #    锚点是 import 之前记下的 env 快照（公开信息）：模块级的 PRIVATE_HOME
        #    必须仍由它解析而来。取根时机一旦被颠倒，它会落回夹具目录，issue #87
        #    的盲区原样重现——而上面三层验的都是函数，照样全绿。
        #    两条 env 腿都要喂（WATER_GROUP_HOME 与 IDLEJOT_WG_HOME）：少喂一条，
        #    只设了那条的机器上锚点会算出另一个根，契约 27 平白变红。
        #    锚点**不经被测函数自己算**（`_live_private_home` 自指 = 它自己坏了
        #    也测不出来）：按口径独立拼出三腿里第一个真是目录的那个。
        expected_home = None
        for cand in (_PRE_WGH, _PRE_IWGH, str(Path.home() / ".water-group")):
            if cand and Path(cand).is_dir():
                expected_home = Path(cand)
                break
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


@case
def 归属判定按worktree边界():
    """`_own_checkout()` 按 worktree 边界判归属，不按「落在根下」（issue #101）。

    §2.7 把每个 issue 的 worktree 放在 `<ROOT>/.tmp/wt-<issue>`——仓库根**之内**，
    于是 `root in script.parents` 把另一棵树的监听成本树：`reply.py` 打印
    「已有监听在跑，不再重复挂」跳过一次必要的续挂，而本树监听早已退出
    （issue 正文的现场）。纯函数四例：

    - `.tmp/wt-87/...`（异树槽位）→ False；
    - `.tmp/plain/...`（根下的普通子目录，非槽位）→ True——防「把 `.tmp` 整个
      当边界」的过度过滤：门禁与工具在根下别处起的监听仍算本树；
    - 本树布局 → True；
    - 仓库外绝对路径（#94 PA 的既有契约）→ False，不因新判据回退。

    只在 Windows 上跑：`_ABS_LISTEN` 锚盘符，posix 上永远匹配不到绝对路径，
    这条会退化成恒真空壳（本仓只跑 Windows，与其余探针用例同口径）。
    """
    if os.name != "nt":
        return True
    root = str(wg_up_probe.ROOT)
    foreign = Path(tempfile.gettempdir()) / "wg-foreign-abc" / "water-group" \
        / "daemon" / "listen.py"
    return (wg_up_probe._own_checkout(
                root + r"\.tmp\wt-87\water-group\daemon\listen.py --timeout 90") is False
            and wg_up_probe._own_checkout(
                root + r"\.tmp\plain\water-group\daemon\listen.py") is True
            and wg_up_probe._own_checkout(
                root + r"\water-group\daemon\listen.py") is True
            and wg_up_probe._own_checkout(str(foreign)) is False)


@case
def 嵌套worktree里的监听不算已有监听():
    """`.tmp/wt-*` 里的监听不算「已有监听」，本树对照必须仍被认出（issue #101）。

    §2.7 的 worktree 布局是仓库根**子目录**：`<ROOT>/.tmp/wt-<issue>`。里面跑着
    的 listen.py 绝对路径落在根下，`root in parents` 判它成本树——`reply.py`
    于是打印「已有监听在跑，不再重复挂」，而本树监听已经退出，群里再有人 @
    就没人叫醒（issue 正文现场）。

    这里起两个进程：一个假监听按 §2.7 布局放在 `<ROOT>/.tmp/wt-<rand>/water-
    group/daemon/listen.py`（`time.sleep` 桩，**不注册**——约定判据不依赖
    git），一个真监听用本仓绝对路径起（对照组）。先轮询 `_match_rows` 证明
    WMI 两个都看见了（否则「进程还没注册」和「被过滤掉」分不开，断言会假绿）；
    再断言两个 finder 都不报槽位里的、都报本树那个——后半条防「过滤层整体
    失效」的坏实现（永远返回空时前半条照样绿，而真监听会被一起漏掉）。
    断言都做在进程**活着**的时候：收树之后再查，死进程谁也查不到，「不过滤」
    的坏实现也照样绿。
    """
    if os.name != "nt":
        return True  # 本仓只跑 Windows（探针在 posix 上是 ps 兜底）
    (ROOT / ".tmp").mkdir(parents=True, exist_ok=True)
    slot = Path(tempfile.mkdtemp(prefix="wt-", dir=str(ROOT / ".tmp")))
    with temp_dir(prefix="wg-nested-") as home:
        fixture_home(home)
        procs = []
        try:
            script = slot / "water-group" / "daemon" / "listen.py"
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
            procs.append(subprocess.Popen(
                [sys.executable, str(script), "--timeout", "1"],
                cwd=str(slot), stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL))
            procs.append(subprocess.Popen(
                [sys.executable, str(DAEMON / "listen.py"),
                 "--cycles", "1", "--timeout", "20", "--no-digest"],
                cwd=str(ROOT), env=child_env(home),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            nested, mine = str(procs[0].pid), str(procs[1].pid)
            raw = []
            deadline = time.time() + 30
            while time.time() < deadline:
                raw = [pid for pid, _ in wg_up_probe._match_rows(
                    "daemon[\\\\/]listen\\.py", exclude=up.self_pids())]
                if nested in raw and mine in raw:
                    break
                time.sleep(1)
            if nested not in raw or mine not in raw:
                return False  # WMI 没看见：用例前提不成立，不能当通过
            pids = up.listener_pids()
            specs = [str(s.get("pid")) for s in up.listener_specs()]
            return (nested not in pids and nested not in specs
                    and mine in pids and mine in specs)
        finally:
            for proc in procs:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               capture_output=True)
            shutil.rmtree(slot, ignore_errors=True)


@case
def 非约定路径的注册worktree不算已有监听():
    """`git worktree list` 注册表也参与边界判定（issue #101）。

    约定判据（`.tmp/wt-*`）覆盖 §2.7 布局，但 worktree 不按约定放时它看不见
    ——`git worktree add .tmp/nested-x` 这样的路径同样落在 ROOT 之下，只实现
    约定判据的实现会把里面的监听成本树。这里真建一棵**非约定路径**的嵌套
    worktree（注册进本仓）并起假监听，断言两个 finder 都不报它；git 不可用
    或建不起树时跳过不红——约定判据已由『嵌套worktree里的监听不算已有监听』
    端到端覆盖，这条只证明第二个判据真的接了线。
    """
    if os.name != "nt":
        return True
    if shutil.which("git") is None:
        print("  git 不可用：跳过注册表用例（约定判据由上一条端到端覆盖）")
        return True
    (ROOT / ".tmp").mkdir(parents=True, exist_ok=True)
    nested = Path(tempfile.mkdtemp(prefix="nested-", dir=str(ROOT / ".tmp")))
    procs = []
    try:
        add = subprocess.run(["git", "-c", "core.quotepath=false", "worktree",
                              "add", "--detach", str(nested)],
                             cwd=str(ROOT), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=120)
        if add.returncode != 0:
            print(f"  git worktree add 失败：{(add.stderr or add.stdout).strip()}")
            return False
        script = nested / "water-group" / "daemon" / "listen.py"
        script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        procs.append(subprocess.Popen(
            [sys.executable, str(script), "--timeout", "1"],
            cwd=str(nested), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL))
        foreign = str(procs[0].pid)
        raw = []
        deadline = time.time() + 30
        while time.time() < deadline:
            raw = [pid for pid, _ in wg_up_probe._match_rows(
                "daemon[\\\\/]listen\\.py", exclude=up.self_pids())]
            if foreign in raw:
                break
            time.sleep(1)
        if foreign not in raw:
            return False  # WMI 没看见：用例前提不成立，不能当通过
        pids = up.listener_pids()
        specs = [str(s.get("pid")) for s in up.listener_specs()]
        return foreign not in pids and foreign not in specs
    finally:
        for proc in procs:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True)
        subprocess.run(["git", "worktree", "remove", "--force", str(nested)],
                       cwd=str(ROOT), capture_output=True, timeout=60)
        subprocess.run(["git", "worktree", "prune"],
                       cwd=str(ROOT), capture_output=True, timeout=60)
        shutil.rmtree(nested, ignore_errors=True)
