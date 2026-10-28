import ast
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

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

# 包式门禁经 `python -m scripts.check_listener_rehang` 调起，此时 `sys.path[0]` 是
# 仓库根（cwd）而非 `scripts/`——后者才是 `check_fixture_support` 所在地。无法先调
# `bootstrap_paths`（它自己要 import fs），故手动把 `scripts/` 插进 `sys.path`，
# 再进口 `check_fixture_support`，随后 `bootstrap_paths()` 收口 root + daemon（issue #169 PF-2）。
_scripts = Path(__file__).resolve().parents[1]
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))

import check_fixture_support as fs  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import temp_dir  # noqa: E402
fs.bootstrap_paths()

DAEMON = ROOT / "water-group" / "daemon"

import listen  # noqa: E402
import reply  # noqa: E402
import up  # noqa: E402
import watch  # noqa: E402
import wg_budget  # noqa: E402
import wg_clock  # noqa: E402  (断言里的退出时刻按同一份渲染口径比)
import wg_listener_state  # noqa: E402  (监听存亡账本，issue #112)
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


def status_report_line(prefix, specs, **overrides):
    """`up.py --status` 中某一行（进程探针被换成给定的 specs 与 overrides）。

    `report()` 是总览屏：每一层的答案都汇到那几行里。这里只换掉会碰到真机器
    的那几处（进程探针、端口探测、账号探测、配置体检、collector 探针、心跳），
    其余照常跑，断言只看以 `prefix` 开头的那一行。

    `overrides` 按 `up.<name>` 覆盖下面的默认桩：issue #142 的三条用例分别要
    「栈全下线」「守护为空 + 心跳过期」「陈旧账本槽」，都不是同一组默认桩拼得出
    的组合。默认桩必须把机器层整个挡住——漏掉 `collector_pids`，实机正在跑的
    collector 就会顶掉用例给定的「不在」，断言测的就不再是栈已死那一格。
    """
    names = ("listener_specs", "listener_pids", "supervisor_pids", "commands_pids",
             "login_info", "port_open", "config_problems", "collector_pids",
             "heartbeat_age", "heartbeat_time")
    saved = {name: getattr(up, name, None) for name in names}
    pids = [str(s.get("pid")) for s in specs]
    stubs = {
        "listener_specs": lambda *args, **kwargs: [dict(s) for s in specs],
        "listener_pids": lambda *args, **kwargs: list(pids),
        "supervisor_pids": lambda *args, **kwargs: [],
        "commands_pids": lambda *args, **kwargs: [],
        "login_info": lambda *args, **kwargs: None,
        "port_open": lambda *args, **kwargs: False,
        # 配置体检清空：不带 self_account 的告警会往监听行里塞一个 ⚠，那不是本组
        # 断言要认的东西（配置那组由 check_config_fail_loud.py 管）。
        "config_problems": lambda *args, **kwargs: [],
        # issue #142：collector 探针与心跳都换成给定值。漏掉它们时用例的前置
        # （「栈已经全下线」）会被实机进程表/心跳文件顶掉，测出来的是另一台机器。
        "collector_pids": lambda *args, **kwargs: [],
        "heartbeat_age": lambda *args, **kwargs: None,
        "heartbeat_time": lambda *args, **kwargs: None,
    }
    stubs.update(overrides)
    for name in names:
        setattr(up, name, stubs[name])
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
    return next((ln for ln in buf.getvalue().splitlines() if ln.startswith(prefix)), "")


def status_listener_line(specs):
    """`up.py --status` 的「监听」行（进程探针被换成给定的 specs）。

    断言只看「监听」那一行——它必须说得出每个监听实际用的 `--timeout`
    （issue #86），以及栈下线时缺的是哪一环（issue #142）。

    `listener_pids` 也一起打桩：issue #86 之前只有它，于是「新门禁 × 旧代码」
    红在**行为**上（行里只有 pid、没有秒数），而不是 AttributeError。
    """
    return status_report_line("监听", specs)
