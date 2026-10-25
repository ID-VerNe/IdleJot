"""一次 Agent 工具调用能等多久：预算的唯一出口（issue #79 / #83）。

宿主（Agent 的工具调用）有一个常见的默认超时，120s。而值守脚本里所有「等一会儿」
的动作都在那个上限之内才能跑完：超了，宿主会把进程树杀掉，子进程跟着死，**而且
这条路径上不会打印任何东西**——Agent 看到的最后一行停在「等…」，分不清是被杀还是
还在等。2026-10-24 的 #79（`reply.py --wait` 默认 240s）和 #83（`listen.py` 常驻
周期 300s、文档示例 300/240）是同一个根因的两例。

所以三个数只在这里写一次，`reply.py` / `listen.py` / `watch.py` 都从这里取：

    AGENT_TOOL_TIMEOUT  120   宿主常见默认超时
    SEND_BUDGET          30   发送 / 启动 / 记账 / 进程退出的实测余量
    WAIT_BUDGET          90   = 120 - 30，单次调用里最多能等多久

`over_budget()` 只回答「这个取值会不会超」，判断留给调用方：静默夹断用户的显式
取值会让「我就是要等 300s」变成谎言，所以这里只提供事实，不替人做决定。

`clamp_wait()` 是 #88 补的第二个事实函数：额度是**一次调用共享的总额**，不是每轮
一份。`listen.py` 之前默认不限轮，于是「每轮 90s」乘以无限轮 = 整次调用永不返回，
宿主在 120s 处把进程树连同 `watch.py` 一起砍掉——监听静默消失，而 `--status` 在那
之前一直报「已挂」。`AGENTS.md` 里那句「单次调用最多能等 WAIT_BUDGET=90」从此由代码
兜住。
"""

import math

AGENT_TOOL_TIMEOUT = 120
SEND_BUDGET = 30
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
    """
    remaining = WAIT_BUDGET - max(0.0, float(elapsed))
    return max(0, min(int(timeout), math.ceil(remaining)))
