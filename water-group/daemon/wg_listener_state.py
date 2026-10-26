"""监听存亡账本：退出本身留下痕迹，「无人值守」成为可读事实（issue #112）。

issue #69 修了「退出时说不说下一步」：每种退出都以 `RESTART:` 收尾。2026-10-25
18:02 那次证明「做不做」仍靠记性——`RESTART:` 送到了、也读到了，然后 14 分 37 秒
没有任何监听，而磁盘上零痕迹：`wg_watch.txt` 记的是游标不是存活，`up.py --status`
的监听行是现算的，空窗期里没人跑它，于是整段失聪没有任何东西能报出来。

本模块把那件事写成文件里的事实：

* `listen.py` 起来时 `start()` 写一份（pid、起点、超时、是否探针）；
* 等待期间由 `watch.py` 的轮询循环 `beat()` 续心跳；
* 每次退出 `stop(reason)` 落 `stopped_at` 与原因；
* 任何一方（`--status`、门禁、以后的组件）随时 `read()` / `on_watch()` /
  `describe()`：「现在有没有人值守」「上一轮是几点到几点」不再依赖谁恰好想起来
  跑一次命令。

**每个监听一份记录（issue #119）**：`state/wg_listeners/<pid>.json`。同一数据根
下并行挂多个监听时，后起的不再覆盖先起的；`on_watch()` / `describe()` 汇总所有
记录——「有没有人在值守」看的是任意一条新鲜的 `watching`，不是最后写入的那一行。
#112 的单行账本 `state/wg_listener.json` 仍只读兼容（升级时可能还有旧监听在写
它），新记录不再写那里。

`watch.py` 是 `listen.py` 的子进程，心跳要续**父进程**那一槽：`start()` 把监听
pid 写进环境变量 `WG_LISTENER_PID`，子进程据此定位；裸跑 `watch.py` 没有记录，
`beat()` 保持 no-op。

分工（issue #94 / #112）：`wg_up_probe.listener_pids()` 回答「此刻有没有**真监听
进程**」，那是续挂判断的权威；本账本回答「上一轮值守是几点到几点、现在为什么
没人」，**不参与**续挂判断——心跳新鲜的记录也可能属于刚被杀的进程，拿它拦
`reply.py` 的重挂会把 #69 的空窗改头换面放回来。

硬杀（宿主截断，issue #88）写不了 `stopped_at`：心跳过期同样给出判定，说明最后
心跳在几点、此后没有退出记录——「被杀了」与「正常退出」因此不再同形。

窗口只在 `_stale_window()` 一处取值（issue #118）：`on_watch()` 与 `describe()`
都调它，改 `STALE_AFTER` 不会只改到一半。

槽里的字段按「读不出来就当坏槽跳过」处理（issue #124）：时间戳走 `_stamp()`，
pid 走 `_pid()`——一个坏槽不许让 `read()` / `on_watch()` / `describe()` 抛，
也不许被当成一条真记录（`true` 不是 pid）。
"""
import json
import os
import time

import wg_clock
from wg_paths import state_dir

# 每个监听一份：#112 的单行账本在并行场景下会被后起者覆盖（issue #119）。
STATE_DIR = state_dir() / "wg_listeners"
# #112 的单行账本，只读兼容：升级瞬间可能还有旧监听在写它。不迁移、不删除。
LEGACY_STATE = state_dir() / "wg_listener.json"
# 监听 pid 的进程树标记：`watch.py` 是 `listen.py` 的子进程，心跳续的是父进程的槽。
OWNER_ENV = "WG_LISTENER_PID"

# 心跳过期窗口。必须装得下一整轮等待（等待预算 90s）加一轮沉淀（积压厚时要几十
# 秒），又要短到能及时暴露「被硬杀后没人续挂」。正常退出由 `stopped_at` 兜底，
# 这个窗口只负责那条写不了退出记录的路径。
STALE_AFTER = 180

# 旧槽保留条数：每次 `listen.py` 起一轮都是新 pid，不收会堆文件；最近几条足够
# 回答「上一轮几点到几点」，仍在值守的不受这个上限约束。
KEEP_RECORDS = 12


def _stale_window():
    """心跳过期窗口的唯一取值点（issue #118）。

    窗口必须在**调用期**现读 `STALE_AFTER`：`on_watch()` 与 `describe()` 是同一个
    事实的两个入口，窗口若在 def 期被绑定，改常量只会改到一半——`--status`（走
    `describe()`）说「疑似被硬杀」，按 `on_watch()` 判的调用方说「值守中」。
    """
    return STALE_AFTER


def _now():
    return int(time.time())


def _slot(pid):
    return STATE_DIR / f"{pid}.json"


def _read_path(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(record):
    pid = _pid(record)
    if pid is None:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        # 槽名与槽里的 pid 同源：归一后的 int 两边都写，坏 pid 不落盘（issue #124）。
        _slot(pid).write_text(json.dumps(dict(record, pid=pid), ensure_ascii=False),
                              encoding="utf-8")
    except OSError:
        # 账本写不进去不该让值守本身失败：监听的价值是叫醒人，不是记账。
        pass


def records():
    """所有监听记录（含 #112 的单行旧账本），按起点升序。

    同 pid 以新槽为准：旧账本只在没有对应槽时参与汇总。pid 或起点读不出来的
    槽按坏槽跳过——与 `_stamp()` 同口径，不让一个坏槽拖垮只读入口（issue #124）。
    """
    rows = []
    seen = set()
    try:
        paths = sorted(STATE_DIR.glob("*.json"))
    except OSError:
        paths = []
    for path in paths:
        record = _read_path(path)
        if not record:
            continue
        pid = _pid(record)
        if pid is None or not _stamp(record.get("started_at")):
            continue
        rows.append(dict(record, pid=pid))
        seen.add(pid)
    legacy = _read_path(LEGACY_STATE)
    legacy_pid = _pid(legacy) if legacy else None
    if (legacy_pid is not None and _stamp(legacy.get("started_at"))
            and legacy_pid not in seen):
        rows.append(dict(legacy, pid=legacy_pid))
    rows.sort(key=lambda r: (_stamp(r.get("started_at")), str(r.get("pid"))))
    return rows


def read(pid=None):
    """一条记录：给了 pid 就读那一槽，否则读最近起的那一条。

    不传 pid 的语义与 #112 的单行账本一致（最近一次 `start()` 写的那条），
    既有调用方与门禁不需要知道分槽布局。
    """
    if pid is not None:
        record = _read_path(_slot(pid))
        if record:
            return record
        legacy = _read_path(LEGACY_STATE)
        return legacy if legacy and legacy.get("pid") == pid else None
    rows = records()
    return rows[-1] if rows else None


def _owner():
    """这一进程树在账本里的 pid：子进程认父进程留下的标记，裸跑认自己。"""
    raw = os.environ.get(OWNER_ENV)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return os.getpid()


def _pid(record):
    """pid 按正整数读；坏值返回 `None`，调用方当坏槽跳过（issue #124）。

    `_read_path()` 只校验顶层是 dict，槽里的 `pid` 可能是列表 / 对象 / 布尔：
    `seen.add()` 对前两者抛 `TypeError`，三个只读入口一起失效；布尔能哈希，
    却会被当成 pid 1 造出一条「疑似被硬杀」的假事实。布尔是 `int` 的子类，
    所以必须先于整数分支排除。数字字符串归一为 int（`_stamp()` 同样容忍字符串）。
    """
    value = record.get("pid")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _stamp(value):
    """时间戳按整数读；坏值当 0，不让一个坏槽拖垮整条状态行。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _last_beat(record):
    return _stamp(record.get("beat_at")) or _stamp(record.get("started_at"))


def _last_moment(record):
    """这条记录最后「发生」在什么时候：退出时刻优先于心跳。"""
    return max(_last_beat(record), _stamp(record.get("stopped_at")))


def _fresh(record, moment, stale_after=None):
    """心跳窗口内仍在值守。

    `stale_after=None` 现读 `_stale_window()`，`on_watch()` 与 `describe()` 因此
    永远同一窗口——#118 的两处取值来源在这里收敛。
    """
    if record.get("state") != "watching":
        return False
    window = _stale_window() if stale_after is None else stale_after
    return moment - _last_beat(record) <= window


def _prune(moment):
    """收旧槽：保留仍在值守的与最近 `KEEP_RECORDS` 条（issue #119）。

    只删槽里的文件，不动 #112 的旧账本——升级前的监听可能还在写它。
    """
    try:
        paths = list(STATE_DIR.glob("*.json"))
    except OSError:
        return
    rows = [(path, _read_path(path)) for path in paths]
    rows = [(path, record) for path, record in rows if record]
    rows.sort(key=lambda item: _last_moment(item[1]), reverse=True)
    for index, (path, record) in enumerate(rows):
        if index < KEEP_RECORDS or _fresh(record, moment):
            continue
        try:
            path.unlink()
        except OSError:
            pass


def start(pid=None, timeout=None, probe=False):
    """一轮值守开始：写自己那一槽。

    `probe` 是门禁/测试起的临时监听标识（issue #94 分节 PB）：留痕是为了事后
    归因，但 `on_watch()` 不把它算作值守——与进程探针跳过它的口径一致。

    监听自己启动时（`pid` 即本进程）把 pid 写进 `OWNER_ENV`，子进程 `watch.py`
    据此续这一槽的心跳；测试传别的 pid 模拟并行监听时不碰环境变量。
    """
    now = _now()
    pid = pid or os.getpid()
    _prune(now)
    _write({"pid": pid, "started_at": now, "beat_at": now,
            "stopped_at": None, "reason": None, "timeout": timeout,
            "probe": bool(probe), "state": "watching"})
    if pid == os.getpid():
        os.environ[OWNER_ENV] = str(pid)


def beat():
    """续心跳。没有在值守的记录时什么都不做（裸跑 `watch.py` 走这条路径）。

    `watch.py` 跑在子进程里：`OWNER_ENV` 让它续父进程那一槽，而不是给
    `watch.py` 自己新建一条记录。
    """
    record = read(_owner())
    if not record or record.get("state") != "watching":
        return
    record["beat_at"] = _now()
    _write(record)


def stop(reason=None):
    """一轮值守结束：落 `stopped_at` 与退出原因。

    只写自己那一槽（pid 不符就不动）：同一数据根里并行挂了两个监听时，先退出的
    那个不能把后起的那一条改成自己的退出。`watch.py` 子进程不调用这里，所以
    不需要 `OWNER_ENV`——结束这一轮的永远是监听进程自己。
    """
    record = read(os.getpid())
    if not record or record.get("state") != "watching":
        return
    record.update({"stopped_at": _now(), "reason": reason, "state": "exited"})
    _write(record)


def on_watch(stale_after=None, now=None):
    """此刻有没有人值守——纯文件判定，不问进程表，也不需要谁先跑一次命令。

    任意一条非探针、心跳新鲜的 `watching` 记录都算（issue #119：先起的监听不能
    被后起者的退出记录盖掉）。探针记录（`--probe`）不算（issue #94 PB）；心跳
    过期的 `watching` 也不算——写不了退出记录的进程（宿主截断、被杀）必须与
    「还在等」区分开。

    `stale_after` / `now` 只给测试钉窗口语义用，生产路径走默认值。默认 `None`
    表示现读 `_stale_window()`——不在 def 期绑定常量，否则改窗口只改到一半
    （issue #118）。
    """
    moment = _now() if now is None else now
    window = _stale_window() if stale_after is None else stale_after
    return any(not r.get("probe") and _fresh(r, moment, window) for r in records())


def _span(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {rest} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def _watching_note(rows, moment):
    if len(rows) == 1:
        record = rows[0]
        age = max(0, moment - _last_beat(record))
        return (f"值守中 pid={record.get('pid')}（自 "
                f"{wg_clock.clock(_stamp(record.get('started_at')))} 起，心跳 {age}s 前）")
    pieces = [f"pid={r.get('pid')}（自 {wg_clock.clock(_stamp(r.get('started_at')))} 起）"
              for r in rows]
    return f"值守中 {len(rows)} 个监听：" + "、".join(pieces)


def describe(now=None):
    """一行人话：值守中 / 上一轮几点到几点 / 被硬杀没有退出记录。

    汇总所有非探针记录（issue #119）：只要还有一条新鲜的 `watching`，就说
    「值守中」——并行场景下先起的那条不能因为后起者退出而消失。没有记录（从未
    挂过监听，或账本写不进去）时返回空串，不制造一条假事实。
    """
    moment = _now() if now is None else now
    rows = [r for r in records() if not r.get("probe")]
    if not rows:
        return ""
    fresh = [r for r in rows if _fresh(r, moment)]
    if fresh:
        return _watching_note(fresh, moment)
    latest = max(rows, key=_last_moment)
    started = wg_clock.clock(_stamp(latest.get("started_at")))
    # issue #142：没有新鲜 `watching` 时，这一行说的都是**历史**。以前它与现刻结论
    # （`up.py` 的「未挂」段）同形，陈旧槽被无限复述成「疑似被硬杀」也看不出是旧账。
    # 两种历史分支统一带「仅供参考」，与「值守中」分支明确分开。
    if latest.get("state") == "watching":
        beat = _last_beat(latest)
        age = max(0, moment - beat)
        return (f"上一轮监听自 {started} 起失去心跳（最后心跳 "
                f"{wg_clock.clock(beat)}，{age}s 前），没有退出记录——疑似被硬杀"
                f"（此槽已过期，仅供参考）")
    stopped = _stamp(latest.get("stopped_at"))
    if stopped:
        return (f"上一轮监听 {started}–{wg_clock.clock(stopped)} 已退出"
                f"（{latest.get('reason') or '原因未知'}），"
                f"此后 {_span(moment - stopped)}无人值守（账本历史，仅供参考）")
    return f"上一轮监听自 {started} 起，没有退出记录（此槽已过期，仅供参考）"
