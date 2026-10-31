"""事件型派发的耗时预算池（issue #225）。

为什么单独一个文件、且不直接用 ``concurrent.futures.ThreadPoolExecutor``：

``commands._serve`` 的 WS 循环是 ``recv → beat → json.loads → _emit_group_message
→ should_dispatch_command → dispatch``。``_emit_group_message`` 是同步调用，紧跟
其后的 ``dispatch`` 才处理 ``#`` 指令。``RECV_TIMEOUT`` 只约束 ``ws.recv`` 读帧，
对插件 ``on_event`` 的执行没有任何约束——一个慢/卡在 I/O 上的事件型插件会把这一帧
之后到达的所有 ``#`` 指令挡在帧队列里，而 ``#`` 指令没有任何「我收到了」的回执，
群里看到的就是「发了指令完全没反应」。``beat()`` 排在派发之前还照写心跳，所以连
``--status`` 与 ``supervisor``（``STALE_AFTER=300s``）都看不出这种 10–30 秒级的慢。

本池给事件型派发一个**有界的同步等待**：插件正常快 → 同步拿结果、立即返回；插件慢
→ ``future.result(timeout=…)`` 超时返回 ``None``、记一行慢日志，WS 循环立刻继续读
下一帧，``#`` 指令照常派发。

**为什么自写而不是 ``ThreadPoolExecutor``**（都实测过）：

* ``ThreadPoolExecutor`` 的 worker 非 daemon，且注册进
  ``concurrent.futures.thread._threads_queues``；``threading._register_atexit``
  注册的 ``_python_exit`` 在解释器退出时 ``t.join()`` 每个 worker。
  ``shutdown(wait=False)`` 不 cancel 正在跑的 task——一个卡死的 worker 会让进程退出
  **永久挂起**（实测 ``timeout 124``）。
* ``daemon_threads=True`` 这个参数在 stdlib ``ThreadPoolExecutor`` **不存在**（实测
  ``TypeError``）。
* 子类化 + 改写 ``_adjust_thread_count`` 让 worker 变 daemon、且不注册进
  ``_threads_queues`` 可以绕开退出挂起（实测退出干净），但依赖 stdlib 私有 ``_worker``
  与 ``_threads_queues``，跨版本脆弱。

所以这里只复用 stdlib 的 ``concurrent.futures.Future``（超时等待与结果/异常传递——
不自己造 future），线程与队列自己写：一条 daemon worker（与 ``qr_login.py:686`` 的
``threading.Thread(..., daemon=True)`` 同款），``max_workers=1``。daemon 线程兜底：
超时跳过的那次卡死调用仍在后台跑完，进程退出不挂。

**``max_workers=1`` 的理由**：事件型派发要求**保序**——复读/接龙的窗口按群消息到达
顺序累积，并发派发会让顺序乱掉。而且慢插件的处理方式是「超时跳过本轮」而非「并发
排队」，一个 worker 足够：正常情况下插件亚毫秒级返回，worker 不成为瓶颈；异常情况
下 worker 被一次卡死占住，后续帧的派发超时跳过——这正是要的行为（不堆积成「整段
静默停摆」），并发 worker 反而会把慢插件期间积压的事件并发灌进插件，放大副作用。
"""
from __future__ import annotations

import queue
import threading
from concurrent.futures import Future
from typing import Any, Callable


class EmitPool:
    """单 worker 的 daemon 派发池：``run(emit, ctx, timeout)`` 同步等结果，超时跳过。

    线程在 ``__init__`` 时起、``shutdown`` 时停。常驻不每帧起线程（issue #225 的
    硬要求）。``Future`` 用 stdlib 的——超时与结果/异常传递不自己造。
    """

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="wg-emit",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            task = self._q.get()
            if task is None:  # shutdown 哨兵
                return
            emit, ctx, future = task
            try:
                future.set_result(emit(ctx))
            except BaseException as exc:  # noqa: BLE001  插件异常进 future，不杀 worker
                future.set_exception(exc)

    def submit(self, emit: Callable[[dict], Any], ctx: dict) -> Future:
        """入队一次派发，返回未完成的 ``Future``。不阻塞。"""
        future: Future = Future()
        self._q.put((emit, ctx, future))
        return future

    def run(self, emit: Callable[[dict], Any], ctx: dict, timeout: float) -> Any:
        """同步等结果，``timeout`` 秒内拿不到就返回 ``None``。

        超时后 worker **仍在跑**那次卡死的调用——daemon 线程兜底，进程退出不挂。
        调用方负责记慢日志（宿主才知道是哪个事件、记哪一行）。
        """
        future = self.submit(emit, ctx)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            return None

    def shutdown(self) -> None:
        """往队列塞哨兵；worker 在当前任务跑完后退出。不 join（daemon 兜底）。"""
        self._q.put(None)
