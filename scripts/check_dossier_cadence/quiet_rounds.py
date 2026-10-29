"""用例 1-8：计数、轮次判据、时长下限、save_mark、listen 接线（issue #162）。

从旧 `scripts/check_dossier_cadence.py` 整体搬运，断言与正文逐字保持，零重排。
"""
import time

from ._common import (
    write_config, seed_store, add_message, run, mark, write_mark, write_events,
    mention_event, tick, check, DOSSIER, LISTEN,
)


# ---------- 1..3：计数、轮次判据、时长下限 ----------


def test_tick_accumulates_across_processes(home):
    """`--tick` 必须跨进程累加，且 `quiet_since` 只在首轮写入（issue #162）。

    `listen.py --cycles` 默认 1，一次调用只跑一轮，所以「连续 3 轮」是三次进程
    调用。进程内变量做不到这件事，这是判据必须落盘的唯一理由。
    """
    first = tick(home)
    assert first.returncode == 0, (first.returncode, first.stdout, first.stderr)
    seeded = mark(home)
    assert seeded.get("quiet_rounds") == 1, seeded
    since = seeded.get("quiet_since")
    assert isinstance(since, (int, float)), seeded

    second = tick(home)
    assert second.returncode == 0, (second.returncode, second.stdout, second.stderr)
    grown = mark(home)
    assert grown.get("quiet_rounds") == 2, grown
    assert grown.get("quiet_since") == since, \
        f"quiet_since 被移动了：首轮 {since} → 现在 {grown.get('quiet_since')}（下限会永远追不上）"
    print("✓ 1. --tick 两次独立进程累加到 2，quiet_since 未被移动")


def test_three_rounds_are_due(home):
    """3 轮 + 素材 ≥ 1 + 下限已过 → `YES quiet`；2 轮 → NOT_YET（issue #162）。"""
    import re

    write_config(home, {"quiet_minutes": 0})
    seed_store(home, messages=4)
    tick(home)
    tick(home)
    two = check(home)
    assert two.returncode == 3, (two.returncode, two.stdout)
    assert two.stdout.startswith("NO "), two.stdout
    assert not any(line.startswith("YES") for line in two.stdout.splitlines()), two.stdout

    tick(home)
    three = check(home)
    assert three.returncode == 0, (three.returncode, three.stdout, three.stderr)
    line = next((ln for ln in three.stdout.splitlines() if ln.startswith("YES")), "")
    match = re.match(r"^YES quiet rounds=(\d+) since=(\d\d:\d\d:\d\d) pending=(\d+)$",
                     line)
    assert match, f"YES 行格式不对（要能区分是哪条判据命中的）：{line!r}"
    assert match.group(1) == "3", line
    assert match.group(3) == "4", line
    print(f"✓ 2. 2 轮 NOT_YET、3 轮 YES quiet（{line}）")


def test_floor_blocks_short_rounds(home):
    """下限真的在拦：3 轮在几秒内跑完，默认 `quiet_minutes=5` 时不许 DUE（issue #162）。

    `--timeout 30` 时三轮只有 90 秒。只钉轮次会让文档里「约 5 分钟」变成一句假话，
    所以轮次是主判据、时长是下限——这条用例把下限那一维单独钉住。
    """
    seed_store(home, messages=4)
    tick(home)
    tick(home)
    tick(home)
    done = check(home)
    assert done.returncode == 3, (done.returncode, done.stdout)
    assert "quiet=3/3" in done.stdout, done.stdout
    print("✓ 3. 3 轮但首轮距今不足 5 分钟 → 仍 NOT_YET（下限生效）")


# ---------- 4：素材下限 ----------


def test_no_material_is_not_due(home):
    """`pending=0` 时即便 3 轮也不 DUE：空跑一次模型调用（issue 待讨论 1）。"""
    write_config(home, {"quiet_minutes": 0})
    seed_store(home, messages=0)
    for _ in range(3):
        tick(home)
    done = check(home)
    assert done.returncode == 3, (done.returncode, done.stdout)
    assert "pending=0" in done.stdout, done.stdout
    print("✓ 4. pending=0 + 3 轮 → NOT_YET（素材下限 quiet_min_messages=1）")


# ---------- 5..7：listen.py 接线 ----------


def test_listen_counts_quiet_rounds(home):
    """真实 `listen.py` 静默两轮 → 两次 tick，`quiet_rounds==2`（issue #162）。

    只测 `--tick` 本身不够：把 `listen.py` 里那行删掉，前面几条照样全绿，
    而线上「连续 3 轮」永远数不起来——那正是 issue 的症状。
    """
    seed_store(home, messages=0)
    done = run(home, LISTEN, "--timeout", "1", "--cycles", "2")
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    state = mark(home)
    assert state.get("quiet_rounds") == 2, (state, done.stdout[-800:])
    assert isinstance(state.get("quiet_since"), (int, float)), state
    print("✓ 5. listen.py 静默两轮 → quiet_rounds=2（接线真的接上了）")


def test_wake_resets_counter(home):
    """收到 @ → `WAKE` 之后计数归零（issue #162）。

    有人指名正是「连续无人指名」的反例。不清零的话，一次 @ 之后只要再来两轮
    静默就会触发，而「连续 3 轮」本来该是三次都没人叫。
    """
    seed_store(home, messages=0)
    tick(home)
    tick(home)
    assert mark(home).get("quiet_rounds") == 2, mark(home)
    write_events(home, [mention_event(1)])
    done = run(home, LISTEN, "--timeout", "10", "--cycles", "1")
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    assert "WAKE" in done.stdout, done.stdout[-800:]
    state = mark(home)
    assert state.get("quiet_rounds") == 0, (state, done.stdout[-800:])
    assert state.get("quiet_since") is None, state
    print("✓ 6. WAKE 之后 quiet_rounds 归零、quiet_since 清空")


def test_no_digest_and_probe_do_not_count(home):
    """`--no-digest` 与 `--probe` 的超时都不计数（issue #162）。

    两条都会让计数变成噪音：续挂每次回完消息就跑一次（`--cycles 1 --no-digest`），
    探针每次跑门禁就跑一次。后者更糟——门禁跑在开发机上，计数却写进生产状态目录。

    `--no-digest` 只在**超时**时不计数：那是「有界等待」而不是「值守一轮」。
    它收到 @ 时仍然清零，因为有人指名就是有人指名（第 6 条钉清零，这里钉超时）。
    """
    seed_store(home, messages=0)
    tick(home)
    before = mark(home)
    assert before.get("quiet_rounds") == 1, before

    bounded = run(home, LISTEN, "--timeout", "1", "--cycles", "1", "--no-digest")
    assert bounded.returncode == 0, (bounded.returncode, bounded.stdout, bounded.stderr)
    assert mark(home).get("quiet_rounds") == 1, \
        f"--no-digest 超时是续挂的有界等待，不该计入轮次：{mark(home)}"

    probe = run(home, LISTEN, "--timeout", "1", "--cycles", "1", "--probe")
    assert probe.returncode == 0, (probe.returncode, probe.stdout, probe.stderr)
    assert mark(home).get("quiet_rounds") == 1, \
        f"--probe 是门禁/测试的临时监听，不该推高生产计数：{mark(home)}"

    # 对照组：同样的有界等待，但这一轮有人指名 —— 必须清零。
    # 只钉「不计数」时，把 WAKE 侧的 reset 也一并砍掉仍然全绿（变异实测），
    # 而线上表现是「@ 之后计数不归零、下一轮静默就凑够 3」。
    write_events(home, [mention_event(1)])
    woke = run(home, LISTEN, "--timeout", "10", "--cycles", "1", "--no-digest")
    assert woke.returncode == 0, (woke.returncode, woke.stdout, woke.stderr)
    assert "WAKE" in woke.stdout, woke.stdout[-800:]
    assert mark(home).get("quiet_rounds") == 0, \
        f"--no-digest 期间收到 @ 仍然算「有人指名」，必须清零：{mark(home)}"
    print("✓ 7. --no-digest / --probe 超时不计轮次；--no-digest 收到 @ 仍清零")


# ---------- 8：save_mark 读-改-写 ----------


def test_mark_and_tick_do_not_clobber(home):
    """`--mark` 清零计数并推进游标，之后再 tick，游标仍在（issue #162）。

    `save_mark` 原来是整体覆盖写：`cmd_mark` 只传 `{last_event_id, last_run}`，
    于是新字段被静默冲掉。症状是「沉淀完立刻又 DUE」或「计数永远不涨」，两种都
    像偶发——所以这条用例两侧都钉：`--mark` 不许留计数，`--tick` 不许丢游标。
    """
    seed_store(home, messages=6)
    tick(home)
    tick(home)
    tick(home)
    assert mark(home).get("quiet_rounds") == 3, mark(home)

    marked = run(home, DOSSIER, "--mark", "--through", "5")
    assert marked.returncode == 0, (marked.returncode, marked.stdout, marked.stderr)
    after_mark = mark(home)
    assert after_mark.get("last_event_id") == 5, after_mark
    assert after_mark.get("quiet_rounds") == 0, \
        f"--mark 之后计数必须归零，否则沉淀完立刻又 DUE：{after_mark}"
    assert after_mark.get("quiet_since") is None, after_mark

    tick(home)
    after_tick = mark(home)
    assert after_tick.get("quiet_rounds") == 1, after_tick
    assert after_tick.get("last_event_id") == 5, \
        f"--tick 冲掉了游标（save_mark 退回覆盖写的签名）：{after_tick}"
    print("✓ 8. --mark 清计数留游标；再 tick 一次游标仍在（读-改-写）")
