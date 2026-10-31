"""用例 13、14、23：默认配置第 3 轮 DUE、listen 回推、下限读法（#162/#196/#215）。

从旧 `scripts/check_dossier_cadence.py` 整体搬运，断言与正文逐字保持，零重排。
**#215 的耦合在这条线**：用例 13 与 23 共用 `_common` 里的 `FIXTURE_*` 常量与
`fixture_floor_is_tight()`，拆分不许松掉它。
"""
import time
from unittest.mock import patch

from check_fixture_support import temp_dir

from ._common import (
    seed_store, run, mark, write_mark, check, write_config, tick, LISTEN,
    FIXTURE_ROUND_SECONDS, FIXTURE_FLOOR_MINUTES, BLOCKED_FLOOR_MINUTES,
    fixture_floor_is_tight, MARK_REL, dossier_module,
)


def test_default_config_fires_on_round_three(home):
    """默认配置 + 默认 90s 轮长 → **第 3 轮就该 DUE**（issue #162）。

    这条用例来自一次真实的自查：前面 12 条全绿，默认配置却要到第 5 轮才触发。

    根因是 `--tick` 跑在 `watch.py` **返回之后**，所以首轮记下的 `quiet_since`
    是本轮**结束**的时刻；N 轮于是只跨 N-1 个间隔，`quiet_minutes=5` 的下限
    要求「5 分钟」就等于 3.33 个间隔，把默认配置推到第 5 轮。轮次判据在 `NO`
    行里照样显示 `3/3`，看起来是活的，实际永远差一步。

    为什么前面没抓住：用例 2 把 `quiet_minutes` 覆盖成 0（下限完全不参与），
    用例 3 只断言「3 轮 + 默认下限 → NOT_YET」，**没有任何一条断言默认组合下
    第 3 轮必须 DUE**。缺的不是数量，是「默认值组合」这个维度。

    修法两头都要：`--tick` 用 `--round-seconds` 把 `quiet_since` 回推到本轮开始，
    `quiet_minutes` 的默认值取 4（< 4.5 分钟的三个间隔，留 30s 余量）。这条用例
    同时钉住两头——只改一边，第 3 轮就过不去。
    """
    seed_store(home, messages=4)
    # 默认配置：不覆盖任何阈值，`quiet_rounds=3` / `quiet_minutes=4`。
    dossier = dossier_module()
    cfg = dict(dossier.DEFAULTS)
    assert cfg["quiet_rounds"] == 3, f"默认轮次判据被改了：{cfg['quiet_rounds']}"
    # 三个默认 90s 轮次 = 270s = 4.5 分钟。下限必须小于它，否则默认配置下
    # 轮次判据永远被下限挡住，变成死码。
    span = cfg["quiet_rounds"] * 90 / 60.0
    assert cfg["quiet_minutes"] < span, (
        f"默认 quiet_minutes={cfg['quiet_minutes']} 不小于 "
        f"{cfg['quiet_rounds']} 个默认轮次（{span:.1f}min）——"
        f"默认配置下轮次判据永远触发不了，issue #162 的需求等于没做")

    # 回推那一维用进程内单测：`--check` 是真实子进程、读真实时钟，patch 过的
    # 模块时钟只影响本进程，混用会得到「写进去的是模拟时刻、读出来按真实时钟算」
    # 的假象。所以这里只验 `quiet_since` 落盘的位置对不对。
    start = time.time()
    with patch.object(dossier, "MARK", home / MARK_REL):
        for index in range(3):
            moment = start + index * 90
            with patch.object(dossier.time, "time", lambda moment=moment: moment):
                assert dossier.cmd_tick(False, 90) == 0
        state = mark(home)
        assert state.get("quiet_rounds") == 3, state
        elapsed = (moment - float(state["quiet_since"])) / 60
        assert 4.4 <= elapsed <= 4.6, (
            f"首轮开始时刻没被回推：elapsed={elapsed:.2f}min，"
            f"应约等于 3 × 90s = 4.5min；"
            f"若约 3.0min 说明 quiet_since 记的是首轮结束（N 轮只跨 N-1 个间隔）")

    # 端到端那一维用真实子进程 + 合成轮长：`quiet_since` 只在首轮写入
    # （`dossier.py:500-508`），所以 `--check` 看到的下限跨度 = 首轮回推的
    # `--round-seconds` + 首轮 tick 到 `--check` 的真实耗时。这里验的是默认
    # `quiet_minutes` 与默认 `quiet_rounds` 的**关系**能真的触发，不是数字本身。
    #
    # 真实耗时那一截必须**不是必要条件**（issue #196）。早先写成
    # `--round-seconds 1` + 下限 0.02min（1.2s），跨度 = 1s 回推 + 耗时：
    # 首轮落盘之后只要再快 0.2s 就翻红（PR #194 的 storage 分片），本机重跑又绿。
    # 现在回推 30s、下限 0.5min（30s）：跨度 >= 30s，耗时压到 0 也不低于下限。
    # `--round-seconds` 只写进 mark、不 sleep，进程开销与原来一样。
    #
    # 这两个数提成模块级常量、与用例 23 共用（issue #215），且**恰好相等**：偏大重新
    # 变成机器速度的函数，偏小只是让断言更好过、门禁不再钉住任何东西。
    assert fixture_floor_is_tight(), (
        f"夹具自相矛盾：单轮回推 {FIXTURE_ROUND_SECONDS}s 与下限 {FIXTURE_FLOOR_MINUTES}min "
        f"不相等——偏大时结论变成机器速度的函数（#196），偏小时只是让断言更好过（#215）")
    write_config(home, {"quiet_minutes": FIXTURE_FLOOR_MINUTES})
    reset = tick(home, "reset")
    assert reset.returncode == 0, (reset.returncode, reset.stdout, reset.stderr)
    for _ in range(3):
        done = tick(home, "--round-seconds", str(FIXTURE_ROUND_SECONDS))
        assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    state = mark(home)
    assert state.get("quiet_rounds") == 3, state
    three = check(home)
    assert three.returncode == 0, (
        f"3 轮且下限已过必须 DUE，实际 {three.returncode}：{three.stdout}")
    line = next((ln for ln in three.stdout.splitlines() if ln.startswith("YES")), "")
    assert line.startswith("YES quiet"), line
    print(f"✓ 13. 默认轮次=3 + 下限 {cfg['quiet_minutes']}min < 3×90s={span:.1f}min，"
          f"第 3 轮 DUE（{line}）")


def test_floor_reading_survives_a_slow_host():
    """同一份 mark、同一组时刻，只改下限，`--check` 的结论必须翻转（issue #196）。

    用例 13 走真实子进程，它的跨度必然掺进「进程启动 + 落盘」的真实耗时；一旦夹具把
    余量留到零点几秒，结论就变成机器速度的函数（PR #194 的 storage 分片就是这么红的）。这条用进程内 patch 时钟
    把「跨度」与「下限」都变成合成量：同一组 `quiet_since` 与 `now`，下限取
    「已过」与「未过」两档，`quiet_minutes_elapsed()` 必须一正一反。它不读真实
    时钟，所以快慢都红不了，同时挡住「把下限调松以换绿」的修法。

    两档下限取自用例 13 的夹具常量（#215）：早先各写一份字面量，把夹具调松到 0.01 时这条
    照样全绿；现在共用 `FIXTURE_*`，偏松偏紧都由下面的判据翻红。
    """
    dossier = dossier_module()
    start = 1_700_000_000.0
    round_seconds = FIXTURE_ROUND_SECONDS
    with temp_dir(prefix="wg-cadence-") as home, \
            patch.object(dossier, "MARK", home / MARK_REL):
        for index in range(3):
            moment = start + index
            with patch.object(dossier.time, "time", lambda moment=moment: moment):
                assert dossier.cmd_tick(False, round_seconds) == 0
        rounds, since = dossier.quiet_state()
        assert rounds == 3, (rounds, since)
        assert since == start - round_seconds, (
            f"首轮没按 --round-seconds 回推：since={since}，应为 {start - round_seconds}")
        # `--check` 判 DUE 的那一刻：首轮 tick 之后 >= 30s。
        now = start + round_seconds
        elapsed = dossier.quiet_minutes_elapsed(now)
    assert elapsed >= round_seconds / 60.0, f"跨度算错了：{elapsed:.3f}min"
    passed = dict(dossier.DEFAULTS, quiet_minutes=FIXTURE_FLOOR_MINUTES)    # 刚好够
    blocked = dict(dossier.DEFAULTS, quiet_minutes=BLOCKED_FLOOR_MINUTES)   # 不够
    # 夹具下限必须恰好等于回推跨度（#215）；两个变异对照证明这条判据不是「常量等于
    # 常量」的空话——调松（0.01）与相对偏小（跨度翻倍）都必须判「不紧」。
    assert fixture_floor_is_tight(), (
        f"夹具下限 {FIXTURE_FLOOR_MINUTES}min ≠ 回推跨度 {round_seconds / 60.0}min（#215）")
    assert not fixture_floor_is_tight(floor_minutes=0.01), "调松到 0.01 也判「够紧」（#215）"
    assert not fixture_floor_is_tight(round_seconds=round_seconds * 2), "跨度翻倍也判「够紧」（#215）"
    # 判据是 `elapsed >= cfg["quiet_minutes"]`（`dossier.py:438-440`），这里直接比同一对量。
    assert elapsed >= passed["quiet_minutes"], (
        f"下限 {FIXTURE_FLOOR_MINUTES}min 应已过，实际跨度 {elapsed:.3f}min")
    assert elapsed < blocked["quiet_minutes"], (
        f"对照组失效：跨度 {elapsed:.3f}min 也过了 {BLOCKED_FLOOR_MINUTES}min 下限，"
        f"这条就只剩一半")
    print(f"✓ 23. 下限 {FIXTURE_FLOOR_MINUTES}min 已过、{BLOCKED_FLOOR_MINUTES}min 未过"
          f"（跨度 {elapsed:.3f}min，不读真实时钟）")


def test_listen_backdates_first_round(home):
    """真实 `listen.py` 静默一轮 → `quiet_since` 被回推到**这一轮开始**（issue #162）。

    `--tick` 跑在 `watch.py --timeout N` 返回**之后**，所以它看到的「现在」是本轮
    结束。不回推的话首轮记成结束时刻，N 轮只跨 N-1 个间隔，默认配置被
    `quiet_minutes` 推到第 5 轮（用例 13 钉的是判据那一侧，这条钉接线）。

    这条来自变异测试：把 `listen.py` 的 `tick(round_seconds=wait)` 改回 `tick()`，
    用例 13 照样全绿——它走的是直接调 `--tick`，根本没经过 `listen.py` 的传参。
    新加的接线必须有对应的用例，否则等于没接。

    用 `--timeout 2` 把量级压到秒：`quiet_since` 距 now 应约 2s，而不是约 0s。
    """
    seed_store(home, messages=0)
    before = time.time()
    done = run(home, LISTEN, "--timeout", "2", "--cycles", "1")
    after = time.time()
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    state = mark(home)
    assert state.get("quiet_rounds") == 1, (state, done.stdout[-800:])
    since = float(state["quiet_since"])
    # 回推量 = 这一轮实际等的秒数。允许 watch.py 自己的启动开销，只要求
    # 「since 早于本轮开始之前一点点」而不是「约等于 now」。
    back = after - since
    assert 1.5 <= back <= (after - before) + 3.5, (
        f"quiet_since 没有回推到本轮开始：距 now {back:.2f}s，"
        f"--timeout 2 时应约 2s 以上（记成结束时刻的话约 0s）")
    print(f"✓ 14. listen.py --timeout 2 → quiet_since 回推 {back:.2f}s（首轮开始）")
