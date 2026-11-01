"""一次 Agent 工具调用能等多久：预算的唯一出口（issue #79 / #83）。

宿主（Agent 的工具调用）有一个常见的默认超时，120s。而值守脚本里所有「等一会儿」
的动作都在那个上限之内才能跑完：超了，宿主会把进程树杀掉，子进程跟着死，**而且
这条路径上不会打印任何东西**——Agent 看到的最后一行停在「等…」，分不清是被杀还是
还在等。2026-10-24 的 #79（`reply.py --wait` 默认 240s）和 #83（`listen.py` 常驻
周期 300s、文档示例 300/240）是同一个根因的两例。

所以三个数只在这里写一次，`reply.py` / `listen.py` / `watch.py` 都从这里取：

    AGENT_TOOL_TIMEOUT  120   宿主常见默认超时（可用 WG_AGENT_TOOL_TIMEOUT 覆盖）
    SEND_BUDGET          30   发送 / 启动 / 记账 / 进程退出的实测余量
    WAIT_BUDGET          90   = 120 - 30，单次调用里最多能等多久

## 覆盖：`WG_AGENT_TOOL_TIMEOUT`（issue #313）

120s 只是**常见**默认，不是所有宿主的上限：Antigravity 这类宿主一次工具调用能等
300s 以上。所以在长超时宿主里可以把第一项放宽，后两项自动跟着推：

    WG_AGENT_TOOL_TIMEOUT=300  →  AGENT_TOOL_TIMEOUT=300 / WAIT_BUDGET=270

只读**环境变量**，不读 `config.json`：等待预算是「宿主侧、逐次调用」的属性
（同一个部署换宿主就该换值），而 `config.json` 是部署常量；何况 import 期读
配置还会把开发机的设置带进门禁（`dossier.py:103` 的教训）。env 则天然被
`check_fixture_support.isolated_env()` 清掉，门禁不受宿主影响。

**放宽是成对动作**：脚本侧设 `WG_AGENT_TOOL_TIMEOUT=300` 时，调 shell 工具必须
同时把宿主侧的 `timeout` 开到 300000ms 以上——只放宽脚本侧，等于把被静默截断的
窗口从 90s 挪到 270s，更晚出现、更难查（这正是本模块开头描述的症状）。

取值在模块顶层求值一次，`reply.py:94/101`、`listen.py:63`、`watch.py:104` 这几个
import 期快照因此自动拿到新值，`--wait` / `--timeout` 的默认值一起推——只让运行时
参数生效、默认值不动是假绿（不传参就永远得不到大窗口）。

`over_budget()` 只回答「这个取值会不会超」，判断留给调用方：静默夹断用户的显式
取值会让「我就是要等 300s」变成谎言，所以这里只提供事实，不替人做决定。覆盖值
读不出来时同理：**不静默回落**，打一行 `WARN` 再说回落成多少。

`clamp_wait()` 是 #88 补的第二个事实函数：额度是**一次调用共享的总额**，不是每轮
一份。`listen.py` 之前默认不限轮，于是「每轮 90s」乘以无限轮 = 整次调用永不返回，
宿主在 120s 处把进程树连同 `watch.py` 一起砍掉——监听静默消失，而 `--status` 在那
之前一直报「已挂」。`AGENTS.md` 里那句「单次调用最多能等 WAIT_BUDGET=90」从此由代码
兜住。
"""

import math
import os
import sys

# 未配置时的取值：宿主常见默认超时。文档与门禁的「默认 120/90」指的就是它。
DEFAULT_AGENT_TOOL_TIMEOUT = 120
SEND_BUDGET = 30
# 覆盖值的天花板，只防手滑（`300000` 是毫秒，写进秒位会静默等 83 小时）。
# 它不替宿主做判断：WorkBuddy / Claude Code 的 600s 工具上限是另一码事。
MAX_AGENT_TOOL_TIMEOUT = 3600
# 允许得比预算还短的**最低**预算（issue #313）：预算 ≥ 这个值时，「比预算还短」
# 的出口（`--wait 1` 之类）才有意义；低于它就只剩「不等」，不该给假出口。
MIN_SHORTER_WAIT = 4
ENV_AGENT_TOOL_TIMEOUT = "WG_AGENT_TOOL_TIMEOUT"


def _env_seconds(name, default, *, minimum, maximum):
    """读一个「秒数」环境变量；读不出来时**说出来**再回落默认值（issue #313）。

    「我配了 300」被静默读成 120，下一个故障就是「为什么还是 90s」——与本模块
    「只给事实、不替人夹断」同源。所以非数字、越界一律打 WARN 到 stderr（不碰
    stdout：`reply.py` / `listen.py` 的输出会被 Agent 当内容读）。
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    text = raw.strip()
    if not text:
        # 空串按「没设」算，不告警：启动脚本里 `VAR=` 是常见的条件导出写法。
        return default
    try:
        value = int(text)
    except ValueError:
        value = None
    if value is None or not minimum <= value <= maximum:
        print(f"WARN: {name}={raw!r} 不是 {minimum}–{maximum} 之间的整数秒；"
              f"按默认 {default}s 处理（issue #313）", file=sys.stderr, flush=True)
        return default
    return value


AGENT_TOOL_TIMEOUT = _env_seconds(
    ENV_AGENT_TOOL_TIMEOUT, DEFAULT_AGENT_TOOL_TIMEOUT,
    minimum=SEND_BUDGET + 1, maximum=MAX_AGENT_TOOL_TIMEOUT)
WAIT_BUDGET = AGENT_TOOL_TIMEOUT - SEND_BUDGET


def over_budget(seconds):
    """这个等待取值是否已经吃掉发送余量（是则调用方该提示或压回预算）。"""
    return int(seconds) + SEND_BUDGET > AGENT_TOOL_TIMEOUT


def clamp_wait(timeout, elapsed=0.0):
    """这一轮实际还能等多少秒（额度是所有轮次共享的总额，issue #88）。

    `timeout` 是调用方要的每轮等待秒数，`elapsed` 是这次调用已经过去的秒数（负值
    按 0 算：否则额度会凭空多出一截）。返回 0 表示额度已经用尽：调用方该自己收手，
    而不是等宿主来砍——被砍时进程树连同子监听一起死，监听就此静默消失。

    额度往大取整（ceil）：默认调用是「等最多 90s」，截断成 89s 会让边界行
    与文档对不上，而差的那一秒本来就还在额度里。

    与 `over_budget()` 一样，这里只给事实：压回多少、什么时候收手，交给调用方。
    但**夹断要真的夹断**（issue #313）：分钟前的写法是
    `min(int(timeout), ceil(remaining))`，后半段恒不小于请求值，是个空夹——默认
    （90 == WAIT_BUDGET）时看不出来，覆盖预算后才现形。

    第二个下限：预算被覆盖到比 `timeout` 还小时，夹断后可能只剩 1–2s，而此时
    「比等得久还真更短」的出口已经不存在（`MIN_SHORTER_WAIT`）；再夹就只能返回 0。
    这时**如实返回 0**（额度用尽），由调用方去说「这次没等」。否则 `reply.py` 会把
    请求值打印成「已压回 Ns」——那是替一个它没做过的夹断作保（#88 的假话）。
    """
    remaining = WAIT_BUDGET - max(0.0, float(elapsed))
    cap = math.ceil(remaining) if remaining > 0 else 0
    return max(0, min(int(timeout), cap))


def shorter_wait(wait, limit=None):
    """「比 `wait` 还真更短」的出口秒数；没有这样的出口时返回 0（issue #97 / #313）。

    逃生口必须真的更短：按 `wait` 折半，且永远小于等待预算（否则等于同一个等待）。
    预算被覆盖到比 `MIN_SHORTER_WAIT` 还窄时**不存在**这样的数——`--wait 1` 与
    「最多 2s」同义，是一句做不到什么的建议。这时返回 0，调用方只留 `--no-wait`。
    """
    cap = WAIT_BUDGET if limit is None else limit
    if cap < MIN_SHORTER_WAIT:
        return 0
    shorter = min(int(wait) // 2, cap - 1)
    return shorter if 0 < shorter < int(wait) else 0


def remaining_budget(elapsed=0.0):
    """整次调用还剩多少秒等待额度（原始浮点，issue #224）。

    给判定用——`wait_plan()` 回答「need 秒塞不塞得进剩余额度」，是 `need > remaining`
    的不等式两边：左边是 `mute + UNMUTE_MARGIN`（秒），右边必须同口径、不取整。
    `clamp_wait()` 的 ceil 是给「等最多 90s」类调用方（`reply.py`/`listen.py`）取
    实际睡眠秒数的——拿它当不等式操作数会让 `90 > 89.6` 被读成「90 不大于 90」，
    于是 0.4s 已用 + 90s 等待 + 30s 发送 = 120.4s 超出宿主 120s，进程树被砍，
    `record(..., recovered=True)` 跑不到，账本卡在旧失败结论（#224）。

    与 `over_budget()` / `clamp_wait()` 同口径：只给事实，不替调用方做决定。
    """
    return WAIT_BUDGET - max(0.0, float(elapsed))
