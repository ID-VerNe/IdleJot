"""一次 Agent 工具调用能等多久：预算的唯一出口（issue #79 / #83）。

宿主（Agent 的工具调用）有一个常见的默认超时，120s。而值守脚本里所有「等一会儿」
的动作都在那个上限之内才能跑完：超了，宿主会把进程树杀掉，子进程跟着死，**而且
这条路径上不会打印任何东西**——Agent 看到的最后一行停在「等…」，分不清是被杀还是
还在等。2026-10-24 的 #79（`reply.py --wait` 默认 240s）和 #83（`listen.py` 常驻
周期 300s、文档示例 300/240）是同一个根因的两例。

所以三个数只在这里写一次，`reply.py` / `listen.py` 都从这里取：

    AGENT_TOOL_TIMEOUT  120   宿主常见默认超时
    SEND_BUDGET          30   发送 / 启动 / 记账 / 进程退出的实测余量
    WAIT_BUDGET          90   = 120 - 30，单次调用里最多能等多久

`over_budget()` 只回答「这个取值会不会超」，判断留给调用方：静默夹断用户的显式
取值会让「我就是要等 300s」变成谎言，所以这里只提供事实，不替人做决定。
"""

AGENT_TOOL_TIMEOUT = 120
SEND_BUDGET = 30
WAIT_BUDGET = AGENT_TOOL_TIMEOUT - SEND_BUDGET


def over_budget(seconds):
    """这个等待取值是否已经吃掉发送余量（是则调用方该提示或压回预算）。"""
    return int(seconds) + SEND_BUDGET > AGENT_TOOL_TIMEOUT
