"""用例 9-12：扫时钟、轮次维度、NO 行字段、坏 mark 降级（issue #162）。

从旧 `scripts/check_dossier_cadence.py` 整体搬运，断言与正文逐字保持，零重排。
"""
import re
import time
from io import StringIO
from contextlib import redirect_stdout
from unittest.mock import patch

from ._common import (
    seed_store, run, mark, write_mark, check, write_config, DOSSIER, GROUP,
    MARK_REL, dossier_module,
)


# ---------- 9..11：扫时钟与 NO 行字段 ----------


def test_clock_sweep_matches_floor():
    """判据对时钟的依赖：扫全天 + 边界，`>= quiet_minutes` 才 DUE（issue #162）。

    `quiet_since` 是**相对量**，固定时刻夹具在跨午夜/跨日时会假绿（#153 的教训：
    当时「相对 now」那组被换成「当天零点后」的固定时刻，288 个时刻里 275 个假绿）。
    所以这里把 `since` 钉成一个固定 epoch，`now` 扫 0…24 小时，再加临界点前后各
    0.1 秒与整点边界。

    `pending_rows` / `active_session` 被打桩：这条测的是时间那一维，素材条数由
    第 2、4 条子进程用例钉住（那里走真实 SQLite）。`idle` 走 `inf`（没有
    `wg_replied.jsonl`），但 `min_messages=25` 挡住它，不会误命中既有两条判据。
    """
    dossier = dossier_module()
    cfg = dict(dossier.DEFAULTS)
    cfg["quiet_rounds"] = 3
    cfg["quiet_minutes"] = 5
    cfg["quiet_min_messages"] = 1
    since = 1_700_000_000.0
    marks = {"quiet_rounds": 3, "quiet_since": since}

    points = [since + step for step in range(0, 86400 + 1, 300)]
    points += [since + 299.9, since + 300.0, since + 300.1, since + 3599.0,
               since + 3600.0]
    wrong = []
    with patch.object(dossier, "active_session", lambda: ("s1", "group:" + GROUP)), \
            patch.object(dossier, "pending_rows", lambda *a, **k: [{"id": 1}]), \
            patch.object(dossier, "load_mark", lambda: dict(marks)), \
            patch.object(dossier.time, "time", lambda: 0.0):
        for now in points:
            with patch.object(dossier.time, "time", lambda now=now: now):
                buf = StringIO()
                with redirect_stdout(buf):
                    code = dossier.cmd_check(cfg)
            expected = dossier.DUE if (now - since) >= 300 else dossier.NOT_YET
            if code != expected:
                wrong.append((round(now - since, 1), code, buf.getvalue().strip()))
    assert not wrong, (f"{len(wrong)}/{len(points)} 个时刻判错（第一个：{wrong[0]}）"
                       "——下限比较写错了？")
    print(f"✓ 9. 扫 {len(points)} 个时刻（含跨午夜/跨日与 ±0.1s 边界），"
          f"判据与 quiet_minutes 完全一致")


def test_rounds_dimension_is_live():
    """对照组：同样的时刻、同样的 `quiet_since`，只把轮次降到 2 → 全 NOT_YET。

    「扫时钟全绿」本身可能只说明时长那一维被读到了，轮次那一维可以是死的
    （比如判据写成 `elapsed >= quiet_minutes` 就够）。这组把轮次压到 2，
    下限早就过了，结果必须全部 NOT_YET——否则第 9 条是假绿。
    """
    dossier = dossier_module()
    cfg = dict(dossier.DEFAULTS)
    cfg["quiet_rounds"] = 3
    cfg["quiet_minutes"] = 5
    cfg["quiet_min_messages"] = 1
    since = 1_700_000_000.0
    marks = {"quiet_rounds": 2, "quiet_since": since}
    points = [since + step for step in range(0, 86400 + 1, 900)]
    wrong = []
    with patch.object(dossier, "active_session", lambda: ("s1", "group:" + GROUP)), \
            patch.object(dossier, "pending_rows", lambda *a, **k: [{"id": 1}]), \
            patch.object(dossier, "load_mark", lambda: dict(marks)), \
            patch.object(dossier.time, "time", lambda: 0.0):
        for now in points:
            with patch.object(dossier.time, "time", lambda now=now: now):
                buf = StringIO()
                with redirect_stdout(buf):
                    code = dossier.cmd_check(cfg)
            if code != dossier.NOT_YET:
                wrong.append((round(now - since, 1), buf.getvalue().strip()))
    assert not wrong, (f"轮次不足 3 却判 DUE（{len(wrong)}/{len(points)} 个时刻，"
                       f"第一个：{wrong[0]}）——轮次那一维是死的")
    print(f"✓ 10. 对照：{len(points)} 个时刻下 rounds=2 全部 NOT_YET（轮次维度是活的）")


def test_no_line_reports_counter(home):
    """`NO` 行的 `quiet=2/3(4.1min)` 逐字段比，不拿子串（#151/#152 的教训）。

    `"2/3" in text` 挡不住「rounds 印对、since 印错」的坏实现——那种输出对读者
    与坏实现同害。所以用正则捕获组取三个字段：轮次、目标、首轮距今分钟数。
    另外钉住「没有首轮时不许印 `(0.0min)`」：那读起来像一个时刻，而不是「还没有」。
    """
    seed_store(home, messages=4)
    write_mark(home, {"quiet_rounds": 2, "quiet_since": time.time() - 246})
    done = check(home)
    assert done.returncode == 3, (done.returncode, done.stdout)
    line = next((ln for ln in done.stdout.splitlines() if ln.startswith("NO ")), "")
    match = re.search(r"quiet=(\d+)/(\d+)\(([\d.]+)min\)", line)
    assert match, f"NO 行没带计数：{line!r}"
    rounds, target, minutes = match.group(1), match.group(2), float(match.group(3))
    assert (rounds, target) == ("2", "3"), line
    assert 3.9 <= minutes <= 4.4, f"首轮距今应约 4.1 分钟，实得 {minutes}：{line!r}"

    write_mark(home, {"quiet_rounds": 0})
    fresh = check(home)
    line = next((ln for ln in fresh.stdout.splitlines() if ln.startswith("NO ")), "")
    assert "quiet=0/3" in line and "min)" not in line, \
        f"没有首轮时刻时不许印分钟数（0.0min 读起来像时刻）：{line!r}"
    print(f"✓ 11. NO 行 quiet 字段逐项正确（{line.strip()}）")


def test_bad_mark_values_do_not_crash(home):
    """mark 文件被写坏时降级成「没有计数」，不许崩、也不许变成「下限已过」（issue #162）。

    `wg_dossier.json` 与 `--mark` 共用，半截 JSON、字符串、列表都可能出现。
    最危险的一格是 `quiet_since: false`：`bool` 是 `int` 子类，`float(False)`
    等于 0，于是「首轮距今 55 年」恒过下限——一个写坏的文件会把判据变成恒真
    （与 #124 里 `_pid()` 踩的是同一个坑）。
    """
    dossier = dossier_module()
    cfg = dict(dossier.DEFAULTS)
    cfg["quiet_minutes"] = 5
    cfg["quiet_min_messages"] = 1
    # `dossier.MARK` 是 import 期常量，指向 `check_fixture_support` 钉的那个临时根，
    # 不是本用例的 `home`。不 patch 的话下面读的全是另一个空文件，断言恒真——
    # 这条用例自己就假绿过（变异「bool 未排除」没被抓住）。
    cases = [
        {"quiet_rounds": "abc", "quiet_since": "xyz"},
        {"quiet_rounds": True, "quiet_since": False},
        {"quiet_rounds": -5, "quiet_since": 1e18},
        {"quiet_rounds": [1], "quiet_since": {"a": 1}},
        {"quiet_since": 1_700_000_000.0},
    ]
    with patch.object(dossier, "MARK", home / MARK_REL):
        for payload in cases:
            write_mark(home, payload)
            rounds, since = dossier.quiet_state()
            assert isinstance(rounds, int) and rounds >= 0, (payload, rounds)
            if payload.get("quiet_since") is False:
                assert since is None, f"quiet_since:false 被当成 epoch 0（下限恒过）：{since}"
            if isinstance(payload.get("quiet_since"), (str, dict, list)):
                assert since is None, (payload, since)
            # 判据本身不许抛：把打坏的 mark 喂给 --check。
            done = run(home, DOSSIER, "--check")
            assert done.returncode in (0, 3), (payload, done.returncode, done.stderr)

        (home / MARK_REL).write_text("not-a-dict", encoding="utf-8")
        assert dossier.quiet_state() == (0, None), "顶层不是对象时按「没有计数」处理"
    print(f"✓ 12. {len(cases)} 种写坏的 mark 值都降级成「没有计数」，不崩、不变成恒真")
