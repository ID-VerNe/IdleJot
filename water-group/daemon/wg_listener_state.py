"""监听存亡账本：退出本身留下痕迹，「无人值守」成为可读事实（issue #112）。

issue #69 修了「退出时说不说下一步」：每种退出都以 `RESTART:` 收尾。2026-10-25
18:02 那次证明「做不做」仍靠记性——`RESTART:` 送到了、也读到了，然后 14 分 37 秒
没有任何监听，而磁盘上零痕迹：`wg_watch.txt` 记的是游标不是存活，`up.py --status`
的监听行是现算的，空窗期里没人跑它，于是整段失聪没有任何东西能报出来。

本模块把那件事写成文件里的事实：

* `listen.py` 起来时 `start()` 写一行（pid、起点、超时、是否探针）；
* 等待期间由 `watch.py` 的轮询循环 `beat()` 续心跳；
* 每次退出 `stop(reason)` 落 `stopped_at` 与原因；
* 任何一方（`--status`、门禁、以后的组件）随时 `read()` / `on_watch()` /
  `describe()`：「现在有没有人值守」「上一轮是几点到几点」不再依赖谁恰好想起来
  跑一次命令。

分工（issue #94 / #112）：`wg_up_probe.listener_pids()` 回答「此刻有没有**真监听
进程**」，那是续挂判断的权威；本账本回答「上一轮值守是几点到几点、现在为什么
没人」，**不参与**续挂判断——心跳新鲜的记录也可能属于刚被杀的进程，拿它拦
`reply.py` 的重挂会把 #69 的空窗改头换面放回来。

硬杀（宿主截断，issue #88）写不了 `stopped_at`：心跳过期同样给出判定，说明最后
心跳在几点、此后没有退出记录——「被杀了」与「正常退出」因此不再同形。

窗口只在 `_stale_window()` 一处取值（issue #118）：`on_watch()` 与 `describe()`
都调它，改 `STALE_AFTER` 不会只改到一半。

已知边界：同一数据根下并行挂多个监听会共用这一行记录（后写的覆盖先写的）；
`stop()` 只认自己的 pid，不会把别人的记录写成自己的退出。「有几个在跑」继续问
进程探针。
"""
import json
import os
import time

import wg_clock
from wg_paths import state_dir

STATE = state_dir() / "wg_listener.json"

# 心跳过期窗口。必须装得下一整轮等待（等待预算 90s）加一轮沉淀（积压厚时要几十
# 秒），又要短到能及时暴露「被硬杀后没人续挂」。正常退出由 `stopped_at` 兜底，
# 这个窗口只负责那条写不了退出记录的路径。
STALE_AFTER = 180


def _stale_window():
    """心跳过期窗口的唯一取值点（issue #118）。

    窗口必须在**调用期**现读 `STALE_AFTER`：`on_watch()` 与 `describe()` 是同一个
    事实的两个入口，窗口若在 def 期被绑定，改常量只会改到一半——`--status`（走
    `describe()`）说「疑似被硬杀」，按 `on_watch()` 判的调用方说「值守中」。
    """
    return STALE_AFTER


def _now():
    return int(time.time())


def _write(record):
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    except OSError:
        # 账本写不进去不该让值守本身失败：监听的价值是叫醒人，不是记账。
        pass


def read():
    """当前记录（dict）；文件不存在或读坏时 None。"""
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def start(pid=None, timeout=None, probe=False):
    """一轮值守开始：写起点。

    `probe` 是门禁/测试起的临时监听标识（issue #94 分节 PB）：留痕是为了事后
    归因，但 `on_watch()` 不把它算作值守——与进程探针跳过它的口径一致。
    """
    now = _now()
    _write({"pid": pid or os.getpid(), "started_at": now, "beat_at": now,
            "stopped_at": None, "reason": None, "timeout": timeout,
            "probe": bool(probe), "state": "watching"})


def beat():
    """续心跳。没有在值守的记录时什么都不做（裸跑 `watch.py` 走这条路径）。"""
    record = read()
    if not record or record.get("state") != "watching":
        return
    record["beat_at"] = _now()
    _write(record)


def stop(reason=None):
    """一轮值守结束：落 `stopped_at` 与退出原因。

    只写自己的记录（pid 不符就不动）：同一数据根里并行挂了两个监听时，先退出的
    那个不能把后写的那一行改成自己的退出。
    """
    record = read()
    if not record or record.get("state") != "watching":
        return
    if record.get("pid") != os.getpid():
        return
    record.update({"stopped_at": _now(), "reason": reason, "state": "exited"})
    _write(record)


def _last_beat(record):
    return record.get("beat_at") or record.get("started_at") or 0


def on_watch(stale_after=None, now=None):
    """此刻有没有人值守——纯文件判定，不问进程表，也不需要谁先跑一次命令。

    探针记录（`--probe`）不算（issue #94 PB）。心跳过期的 `watching` 记录也不算：
    写不了退出记录的进程（宿主截断、被杀）必须与「还在等」区分开。

    `stale_after` / `now` 只给测试钉窗口语义用，生产路径走默认值。默认 `None`
    表示现读 `_stale_window()`——不在 def 期绑定常量，否则改窗口只改到一半
    （issue #118）。
    """
    record = read()
    if not record or record.get("probe"):
        return False
    if record.get("state") != "watching":
        return False
    moment = _now() if now is None else now
    window = _stale_window() if stale_after is None else stale_after
    return moment - _last_beat(record) <= window


def _span(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {rest} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def describe(now=None):
    """一行人话：值守中 / 上一轮几点到几点 / 被硬杀没有退出记录。

    没有记录（从未挂过监听，或账本写不进去）时返回空串——不制造一条假事实。
    """
    record = read()
    if not record:
        return ""
    started_at = record.get("started_at")
    if not started_at:
        # 起点读不到（文件被外部改写）时不出声：`wg_clock.clock(None)` 会拿此刻
        # 当起点——一句假话比没有输出更坏。
        return ""
    moment = _now() if now is None else now
    started = wg_clock.clock(started_at)
    if record.get("state") == "watching":
        if record.get("probe"):
            return ""
        beat = _last_beat(record)
        age = max(0, moment - beat)
        if age <= _stale_window():
            return (f"值守中 pid={record.get('pid')}（自 {started} 起，"
                    f"心跳 {age}s 前）")
        return (f"上一轮监听自 {started} 起失去心跳（最后心跳 "
                f"{wg_clock.clock(beat)}，{age}s 前），没有退出记录——疑似被硬杀")
    stopped = record.get("stopped_at")
    if stopped:
        return (f"上一轮监听 {started}–{wg_clock.clock(stopped)} 已退出"
                f"（{record.get('reason') or '原因未知'}），"
                f"此后 {_span(moment - stopped)}无人值守")
    return f"上一轮监听自 {started} 起，没有退出记录"
