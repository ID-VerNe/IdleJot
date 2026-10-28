import os
import subprocess
import sys
import time

from ._common import (
    case, ROOT, DAEMON, fs, temp_dir,
    fixture_home, child_env,
    status_listener_line,
    listen, up, wg_budget, wg_listener_state,
)
from .ledger import ledger_at, write_ledger, write_raw_slot


@case
def 并行监听各记各的账():
    """并行监听时先起者仍在跑，账本不许说「无人值守」（issue #119）。

    issue 的复现顺序：A 起、B 起、B 先退出、A 仍在跑。修复前两个监听共用一行，
    B 的 `start()` 覆盖 A，B 的 `stop()` 又把这一行写成退出——`on_watch()` 为
    False，`describe()` 说「此后 0 秒无人值守」，与进程事实相反。修复后每个监听
    一槽：B 的退出只落 B 的槽，A 仍是新鲜 `watching`。

    用 `os.getpid` 打桩模拟两个 pid，与 issue 现象段的复现命令同形；不真起进程，
    避免把这条契约变成进程表测试。A 也退出后必须转「无人值守」——恒 True 的
    坏实现红在后半条。

    多监听分支（issue #125）：A、B 同时新鲜时取样 `describe()`，文案必须报
    「值守中 2 个监听」且两个 pid 都在里头。`_watching_note()` 的 `len(rows) > 1`
    分支在原门禁里从未被构造——把 `{len(rows)}` 改成 `1` 的变异原存活，本条钉死。
    反向顺序（A 先退、B 仍在跑）由 `并行监听反向退出不覆盖后起者` 单独钉。
    """
    with temp_dir(prefix="wg-parallel-") as home:
        restore = ledger_at(home)
        original_getpid = os.getpid
        try:
            wg_listener_state.start(pid=111, timeout=wg_budget.WAIT_BUDGET)
            wg_listener_state.start(pid=222, timeout=wg_budget.WAIT_BUDGET)
            # A、B 同时新鲜：钉多监听分支——「值守中 2 个监听」且两个 pid 都在文案里。
            both_note = wg_listener_state.describe()
            os.getpid = lambda: 222
            wg_listener_state.stop("NO_WORK")
            os.getpid = original_getpid
            after_first_exit = wg_listener_state.on_watch()
            note_first_exit = wg_listener_state.describe()
            os.getpid = lambda: 111
            wg_listener_state.stop("WAKE")
            after_both = wg_listener_state.on_watch()
            note_both = wg_listener_state.describe()
        finally:
            os.getpid = original_getpid
            restore()
    return (after_first_exit
            and "值守中" in note_first_exit
            and "无人值守" not in note_first_exit
            and not after_both
            and "无人值守" in note_both
            and "值守中" not in note_both
            and "值守中 2 个监听" in both_note
            and "pid=111" in both_note and "pid=222" in both_note)


@case
def 并行监听反向退出不覆盖后起者():
    """反向顺序：A 先退、B 仍在跑——`stop()` 必须只动自己那一槽（issue #119 / #125）。

    `stop()` 按 `os.getpid()` 定位自己的槽。B-先退那条用例（`并行监听各记各的账`）
    红在「第二次 stop 读到已退出记录」上，属于副作用——把 `read(os.getpid())` 改成
    `read()`（不按 pid 定位）时，那条用例同样会红，于是「stop 作用域」这条性质
    看似被覆盖、实则是误打误撞。这里用反向顺序把作用域单独钉住：A 先退出，B 仍在
    跑——A 槽必须落 `exited`、B 槽仍是 `watching`、`on_watch()` 仍 True。

    `read()` 而非 `read(pid)` 的坏实现会读到最后起的那条（B），把 B 改成 exited，
    A 仍是 watching——`on_watch()` 碰巧还是 True（A 在跑），但 B 槽 state 错、A 槽
    state 错，红在槽状态断言上。
    """
    with temp_dir(prefix="wg-parallel-reverse-") as home:
        restore = ledger_at(home)
        original_getpid = os.getpid
        try:
            wg_listener_state.start(pid=111, timeout=wg_budget.WAIT_BUDGET)
            wg_listener_state.start(pid=222, timeout=wg_budget.WAIT_BUDGET)
            os.getpid = lambda: 111
            wg_listener_state.stop("WAKE")
            os.getpid = original_getpid
            after_a_exit = wg_listener_state.on_watch()
            note_after_a = wg_listener_state.describe()
            slot_a = wg_listener_state.read(111)
            slot_b = wg_listener_state.read(222)
        finally:
            os.getpid = original_getpid
            restore()
    return (after_a_exit
            and "值守中" in note_after_a
            and "无人值守" not in note_after_a
            and slot_a is not None and slot_a.get("state") == "exited"
            and slot_a.get("reason") == "WAKE"
            and slot_b is not None and slot_b.get("state") == "watching"
            and "pid=222" in note_after_a)


@case
def 旧账本与新槽合并():
    """同 pid 的 #112 单行旧账本与新槽都在时，以新槽为准（issue #119 / #125）。

    升级期里旧监听可能仍在写 `state/wg_listener.json`，新监听已开始写
    `state/wg_listeners/<pid>.json`。`records()` 合并两者时同 pid 以新槽为准：
    旧账本里那条 `watching`、心跳再新鲜也不能顶掉新槽的 `exited` 当值守——否则
    一个已退出的监听会被旧账本的新鲜心跳谎报成「值守中」。

    旧账本的 pid 没有对应槽时仍参与汇总（升级前只有旧账本在用）——这条钉
    「不迁移、只读兼容」的设计本身：旧账本不是死文件，没对应槽时仍是有效事实。
    """
    import json
    with temp_dir(prefix="wg-legacy-merge-") as home:
        (home / "state").mkdir(parents=True, exist_ok=True)
        restore = ledger_at(home)
        try:
            now = int(time.time())
            # 新槽 pid=111 已退出；旧账本 pid=111 仍是 watching 且心跳新鲜。
            write_ledger(home, {"pid": 111, "started_at": now - 500,
                                "beat_at": now - 500, "stopped_at": now - 1,
                                "reason": "NO_WORK", "timeout": wg_budget.WAIT_BUDGET,
                                "probe": False, "state": "exited"})
            wg_listener_state.LEGACY_STATE.write_text(
                json.dumps({"pid": 111, "started_at": now - 1000,
                            "beat_at": now - 5, "stopped_at": None, "reason": None,
                            "timeout": wg_budget.WAIT_BUDGET, "probe": False,
                            "state": "watching"}, ensure_ascii=False),
                encoding="utf-8")
            merged_rows = wg_listener_state.records()
            same_pid_watch = wg_listener_state.on_watch(now=now)
            same_pid_note = wg_listener_state.describe(now=now)

            # 旧账本 pid=333 无对应槽、watching 新鲜 → 参与汇总，算值守。
            wg_listener_state.LEGACY_STATE.write_text(
                json.dumps({"pid": 333, "started_at": now - 200,
                            "beat_at": now - 3, "stopped_at": None, "reason": None,
                            "timeout": wg_budget.WAIT_BUDGET, "probe": False,
                            "state": "watching"}, ensure_ascii=False),
                encoding="utf-8")
            orphans_rows = wg_listener_state.records()
            orphan_watch = wg_listener_state.on_watch(now=now)
            orphan_note = wg_listener_state.describe(now=now)
        finally:
            restore()
    return (len([r for r in merged_rows if r.get("pid") == 111]) == 1
            and not same_pid_watch
            and "无人值守" in same_pid_note
            and "值守中" not in same_pid_note
            and any(r.get("pid") == 333 for r in orphans_rows)
            and orphan_watch
            and "值守中" in orphan_note and "pid=333" in orphan_note)


@case
def 子进程续父进程的心跳():
    """`beat()` 推进父进程那一槽，不给 `watch.py` 自建记录（issue #119 / #125）。

    `watch.py` 跑在 `listen.py` 的子进程里：`start()` 把监听 pid 写进
    `OWNER_ENV`，`beat()` 据此 `_owner()` 定位。`read(os.getpid())` 而不认
    `OWNER_ENV` 的实现会让 `watch.py` 给自己新建一条 `222.json`（且 `111.json`
    心跳不前进）——`on_watch()` 在 `222.json` 新鲜时碰巧仍 True，但心跳续到了
    错的槽上，下一轮 `_prune` 会把 `111.json` 当旧槽收掉。这里钉两个事实：
    `111.json` 的 `beat_at` 被 `beat()` 推进，且 `222.json` 不出现。

    `_now` 打桩：`beat()` 用 `_now()` 取当前时刻，这里要断言「推进了」就得把
    打桩时刻拨到 `start()` 之后——否则 `beat_at == started_at` 无法分辨「续了」
    与「没续」。`OWNER_ENV` 显式置成 111（`start(pid=111)` 不碰环境变量，因为
    pid ≠ `os.getpid()`），模拟真实 `listen.py` 起监听那一槽时设的标记。
    """
    with temp_dir(prefix="wg-owner-beat-") as home:
        restore = ledger_at(home)
        original_getpid = os.getpid
        original_now = wg_listener_state._now
        owner_env = getattr(wg_listener_state, "OWNER_ENV", "WG_LISTENER_PID")
        try:
            base = int(time.time())
            wg_listener_state._now = lambda: base
            wg_listener_state.start(pid=111, timeout=wg_budget.WAIT_BUDGET)
            # 父进程那槽的 OWNER_ENV 标记：真实 listen.py 起监听时由 start(pid=self) 设。
            os.environ[owner_env] = "111"
            # 子进程：getpid 是 222，_now 拨到 base+10，beat 应续 111 那槽。
            wg_listener_state._now = lambda: base + 10
            os.getpid = lambda: 222
            wg_listener_state.beat()
            os.getpid = original_getpid
            slot_111 = wg_listener_state.read(111)
            slot_222 = wg_listener_state.read(222)
        finally:
            os.getpid = original_getpid
            wg_listener_state._now = original_now
            restore()
    return (slot_111 is not None
            and slot_111.get("state") == "watching"
            and slot_111.get("beat_at") == base + 10
            and slot_222 is None)


@case
def 收旧槽保留值守中的():
    """`_prune()` 收旧槽时，仍在值守的不受 `KEEP_RECORDS` 上限约束（issue #119 / #125）。

    `keep = KEEP_RECORDS`，`fresh_age = STALE_AFTER // 2`（窗口内，算值守）。值守中
    那条 `beat_at = now - fresh_age`、`started_at` 极老——`_last_moment` 取 `beat_at`
    （`stopped_at` 为空），所以它在「最近哪一条」里排最末；`keep + 2` 条已退出记录
    的 `stopped_at` 都比值守中那条的 `beat_at` 新，于是 top-`keep` 全是已退出、值守中
    那条落到 index ≥ keep。它必须被 `_fresh` 救回——去掉 `_fresh` 分支的实现会把它
    删掉，红在「值守中那条不见了」。恒不删的实现红在「超出的 exited 没被收」。
    """
    with temp_dir(prefix="wg-prune-") as home:
        restore = ledger_at(home)
        try:
            now = int(time.time())
            keep = wg_listener_state.KEEP_RECORDS
            fresh_age = wg_listener_state.STALE_AFTER // 2  # 窗口内，算值守
            # 值守中那条：started_at 极老、beat_at 新鲜——_last_moment 取 beat_at。
            write_ledger(home, {"pid": 1, "started_at": now - 100000,
                                "beat_at": now - fresh_age, "stopped_at": None,
                                "reason": None, "timeout": wg_budget.WAIT_BUDGET,
                                "probe": False, "state": "watching"})
            # keep + 2 条已退出，stopped_at 都比值守中那条的 beat_at 新（now-5..），
            # 于是 top-keep 全是已退出、值守中那条排到 index >= keep，靠 _fresh 留下。
            for i in range(2, keep + 4):
                write_ledger(home, {"pid": i, "started_at": now - 10000,
                                    "beat_at": now - 10000,
                                    "stopped_at": now - (i - 1) * 5,
                                    "reason": "NO_WORK",
                                    "timeout": wg_budget.WAIT_BUDGET,
                                    "probe": False, "state": "exited"})
            wg_listener_state._prune(now)
            watching_left = wg_listener_state.read(1)
            remaining = [r for r in wg_listener_state.records()
                         if r.get("state") == "exited"]
        finally:
            restore()
    return (watching_left is not None
            and watching_left.get("state") == "watching"
            and len(remaining) == keep)


@case
def 坏槽不拖垮账本只读入口():
    """pid 不是标量的槽按坏槽跳过，只读入口一律不抛（issue #124）。

    `_read_path()` 只校验顶层是 dict：`pid` 是列表 / 对象时 `seen.add()` 抛
    `TypeError`，`records()` / `on_watch()` / `describe()` 一起失效；布尔能哈希，
    却会被当成 pid 1 造出一条「疑似被硬杀」的假事实。`up.py` 的「未挂」分支在
    `print()` 之前调 `describe()` 且没有 try/except，坏槽因此让整条 `--status`
    输出消失——这是 issue 的后果，也是本用例最后一段要钉的。

    三段断言，各自有对照组：

    1. 只有坏槽（列表 / 对象 / 布尔三份同时落盘）→ 空记录、False、空串、状态行
       仍是「未挂」且不加账本注脚。把坏槽当值守的实现红在这里；只 catch
       `TypeError` 的实现漏掉 `true` 那一份，同样红在这里。
    2. 坏槽 + 一条新鲜好槽 → 「值守中」，行里的 pid 归一成 int。把整个目录跳过
       的实现红在这里。
    3. 坏旧账本（`state/wg_listener.json` 的 pid 是对象）→ 结论同第 2 段。只修
       新槽、旧账本仍直接 `not in seen` 的实现红在这里。
    """
    bads = (
        '{"pid": [], "started_at": 1, "state": "watching"}',
        '{"pid": {"x": 1}, "started_at": 1, "state": "watching"}',
        '{"pid": true, "started_at": 1, "state": "watching"}',
    )
    with temp_dir(prefix="wg-badslot-") as home:
        fixture_home(home)
        restore = ledger_at(home)
        try:
            for index, text in enumerate(bads):
                write_raw_slot(home, f"bad{index}.json", text)
            only_bad_rows = wg_listener_state.records()
            only_bad_watch = wg_listener_state.on_watch()
            only_bad_note = wg_listener_state.describe()
            only_bad_line = status_listener_line([])
            wg_listener_state.start(timeout=wg_budget.WAIT_BUDGET)
            mixed_rows = wg_listener_state.records()
            mixed_watch = wg_listener_state.on_watch()
            mixed_note = wg_listener_state.describe()
            (home / "state" / "wg_listener.json").write_text(
                '{"pid": {"x": 1}, "started_at": 1, "state": "watching"}',
                encoding="utf-8")
            legacy_rows = wg_listener_state.records()
            legacy_note = wg_listener_state.describe()
        finally:
            restore()
    return (only_bad_rows == []
            and not only_bad_watch
            and only_bad_note == ""
            and "未挂" in only_bad_line and "账本" not in only_bad_line
            and len(mixed_rows) == 1 and isinstance(mixed_rows[0].get("pid"), int)
            and mixed_watch and "值守中" in mixed_note
            and len(legacy_rows) == 1 and "值守中" in legacy_note)
