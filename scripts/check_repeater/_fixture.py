"""check_repeater 包的夹具层（issue #285）。

从旧 `scripts/check_repeater.py` 整体搬运，按 #169 PF-2（`scripts/check_listener_rehang/`）
与 #247（`scripts/check_dossier_cadence/`）、#282（`scripts/check_qr_login/`）的先例拆出。
本模块只放 `Clock` fake 与 `text_event` / `settings` / `feed` / `feed_all` /
`PLUGIN_OPTIONS` / `fresh` helper，以及数据根钉住 + R4 的 `_runner` / `check`。13 条
`test_*` 用例与 `main()` 留主模块 `__init__.py`。

R4（issue #280）合流：本模块的 `check()` runner 委派 `assert_runner`（`markers="ok"`），
与 #169 PE-1 的 `run_cases()`（`(name, fn)` case 列表族）形态不同、互不替。`__init__.py`
的 `main()` 用 `_runner.summary()` 出尾行。

与 `check_qr_login/_fixture.py` 同口径：包式门禁经
`python -m scripts.check_repeater` 调起，此时 `sys.path[0]` 是仓库根（cwd）而非
`scripts/`——后者才是 `check_fixture_support` 所在地。无法先调
`bootstrap_paths`（它自己要 import fs），故手动把 `scripts/` 插进 `sys.path`，
再进口 `check_fixture_support`，随后 `bootstrap_paths()` 收口 root + daemon。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 包式门禁经 `python -m scripts.check_repeater` 调起，此时 `sys.path[0]` 是
# 仓库根（cwd）而非 `scripts/`——后者才是 `check_fixture_support` 所在地。无法先调
# `bootstrap_paths`（它自己要 import fs），故手动把 `scripts/` 插进 `sys.path`，
# 再进口 `check_fixture_support`，随后 `bootstrap_paths()` 收口 root + daemon。
_scripts = Path(__file__).resolve().parents[1]
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

from check_fixture_support import assert_runner, pin_state_env, scratch_dir  # noqa: E402

# 数据根在 import 任何守护模块之前钉住：`repeater` 的账本路径在调用期读
# `state_dir()`，而 `commands` 在 import 期就把 `STATE_DIR` 求值完了。
# state 两腿钉合走 pin_state_env（#281）。
_SCRATCH = scratch_dir(prefix="wg_repeater_")
os.environ["WATER_GROUP_HOME"] = str(_SCRATCH)
pin_state_env(_SCRATCH / "state")
(_SCRATCH / "state").mkdir(parents=True, exist_ok=True)

import commands  # noqa: E402
import repeater  # noqa: E402
from plugins.base import Hooks, Plugin, PluginError  # noqa: E402
from plugins.registry import Registry  # noqa: E402

# 即时断言 runner（issue #280 合流）：check() 打印 + 累积，summary() 出尾行。
_runner = assert_runner(markers="ok")
check = _runner.check
SELF = "100000000"
SELF_IDS = {SELF}
GROUP = "123456789"
OTHER = "987654321"


# ---- 夹具 ---------------------------------------------------------------------

def text_event(text, uid="200000001", mid=1, group=GROUP, **extra):
    """一条纯文本群消息。`text=None` 表示不带文本段（走 `extra["message"]`）。"""
    event = {
        "post_type": "message",
        "message_type": "group",
        "group_id": int(group),
        "user_id": int(uid),
        "message_id": mid,
        "message": [{"type": "text", "data": {"text": text}}],
    }
    event.update(extra)
    return event


class Clock:
    """可推进的假时钟：窗口 / 冷却都要能一秒一秒推，不睡真时间。"""

    def __init__(self, start=1_800_000_000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def tick(self, seconds=1.0):
        self.now += float(seconds)
        return self.now


# `self_names` 是「本机名字」闸门的参数，闸门要真有一个具体名字才测得到；但夹具里
# **不许出现像真实昵称的字面值**（issue #254）：`check_publication.py` 从私有
# `self_names` / 人设卡 / 群友档案派生 token 后全量扫库，一个测试夹具常量撞上它就会把
# 「误报」报成「个人标识泄露」，挡下任何人在这台机器上的正常推送。第一个元素是公开
# 人设模板名（`#112` 时已登记进私有 allow），第二个元素只占位、不承担任何判据，
# 用只可能出现在夹具里的合成串。
SELF_NAMES = ["小鲸鱼", "self-b"]


def settings(**over):
    """一份开着 repeater、概率恒 1 的设置（概率另有用例单独钉）。"""
    cfg = {
        "self_account": SELF,
        "self_names": list(SELF_NAMES),
        "groups": {GROUP: {"enabled": True}},
        "slash_commands": {"enabled": True},
        "plugins": {"repeater": {
            "enabled": True,
            "options": {"probability": 1.0, "min_repeats": 3},
        }},
    }
    cfg.update(over)
    return commands.settings_from(cfg)


def feed(rows, options, clock, rng=None, self_ids=SELF_IDS, conf=None):
    """按序喂一串 `(文本, uid)`，返回**全程**发出的第一条（None = 一条都没发）。

    不能「见到回复就停」：`该跟` 与 `不该跟` 往往只差最后一条消息（第四人改口），
    提前返回就把后半段判据跳过了——那正是假绿的形状。整串跑完再看。
    """
    replies = feed_all(rows, options, clock, rng, self_ids, conf)
    return replies[0] if replies else None


def feed_all(rows, options, clock, rng=None, self_ids=SELF_IDS, conf=None):
    """整串跑完，返回**每一次**发出的文本（按发生顺序）。

    一个场景里可能「该跟一次、之后不该再跟」：只看第一条或只看最后一条都会漏掉
    另一半判据，所以两个视角都留。
    """
    conf = conf or settings()
    options = options or PLUGIN_OPTIONS(conf)
    clock = clock or Clock()
    replies = []
    for index, row in enumerate(rows):
        text, uid = row if isinstance(row, tuple) else (row, "20000000%d" % index)
        event = text_event(text, uid=uid, mid=index + 1)
        reply = repeater.consider(event, conf, options, self_ids, clock=clock,
                                  rng=rng or (lambda: 0.0))
        clock.tick(1.0)
        if reply is not None:
            replies.append(reply)
    return replies


def PLUGIN_OPTIONS(conf):
    return commands.PLUGINS.get("repeater").options(conf)


def fresh():
    """每个用例一个干净窗口 + 空账本。"""
    repeater.reset()
    path = repeater._ledger_path()
    try:
        path.unlink()
    except OSError:
        pass
