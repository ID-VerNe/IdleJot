"""用例 15-22：积压不是爆发、真爆发仍触发、同一批只叫一次、reap 兜底、due 状态（#183）。

从旧 `scripts/check_dossier_cadence.py` 整体搬运，断言与正文逐字保持，零重排。
"""
import time

from ._common import (
    write_config, seed_store, add_message, run, mark, write_mark, check, tick,
    DOSSIER, LISTEN,
)


# ---------- 15..17：积压不是爆发、真爆发仍触发（issue #183） ----------


def test_backlog_is_not_a_burst(home):
    """窗口外的 200 条积压 + 3 轮静默 → `YES quiet`，不是 `YES burst`（issue #183）。

    issue 现象的直接回归：`pending` 是「从游标起的总量」，旧代码拿它跟
    `burst_messages` 比，游标一停就恒大于 80，于是永远 `YES burst`、quit 分支
    永远走不到。这里 200 条全部落在窗口外，quiet 条件已满足，必须报 quiet。
    """
    write_config(home, {"quiet_minutes": 0, "burst_window_minutes": 10})
    seed_store(home, messages=200, base_ts=(time.time() - 3600) * 1000)
    for _ in range(3):
        tick(home)
    done = check(home)
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    line = next((ln for ln in done.stdout.splitlines() if ln.startswith("YES")), "")
    assert line.startswith("YES quiet"), \
        f"窗口外的历史积压被误报成爆发（issue #183 的原始症状）：{line!r}"
    assert "pending=200" in line, line
    print(f"✓ 15. 窗口外 200 条积压 + 3 轮 → {line}（不再被 burst 短路）")


def test_recent_spike_is_still_a_burst(home):
    """窗口内真爆发 + 轮次不足 → `YES burst`（quiet 没有把原语义抢走）。"""
    write_config(home, {"burst_window_minutes": 10})
    seed_store(home, messages=80, base_ts=time.time() * 1000)
    done = check(home)
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    line = next((ln for ln in done.stdout.splitlines() if ln.startswith("YES")), "")
    assert line.startswith("YES burst"), f"窗口内的真爆发没被认出来：{line!r}"
    assert "recent=80" in line and "pending=80" in line, line
    print(f"✓ 16. 10 分钟窗口内 80 条 → {line}（爆发语义保留）")


def test_same_batch_is_not_announced_twice(home):
    """`--tick due` 之后同一批素材 `NO due-hold`；新消息进来重新 DUE（issue #183）。

    游标不动时 `--check` 每轮都判 DUE，`listen.py` 于是每轮打一次 `DOSSIER_DUE`
    然后退出——调用方分不清「有新活」和「还是我没处理的那批」。这条把「叫醒」
    变成有状态的一次性事件。
    """
    write_config(home, {"quiet_minutes": 0})
    seed_store(home, messages=4, base_ts=time.time() * 1000)
    for _ in range(3):
        tick(home)
    first = check(home)
    assert first.returncode == 0, (first.returncode, first.stdout)
    assert first.stdout.startswith("YES quiet"), first.stdout

    recorded = run(home, DOSSIER, "--tick", "due")
    assert recorded.returncode == 0, (recorded.returncode, recorded.stdout, recorded.stderr)
    assert "due recorded event=4" in recorded.stdout, recorded.stdout

    held = check(home)
    assert held.returncode == 3, f"同一批素材又 DUE 了一次（每轮空转）：{held.stdout}"
    assert held.stdout.startswith("NO due-hold"), held.stdout
    assert "latest=4" in held.stdout, held.stdout

    add_message(home, 5, ts=time.time() * 1000)
    fresh = check(home)
    assert fresh.returncode == 0, f"有新素材必须重新 DUE：{fresh.stdout}"
    assert fresh.stdout.startswith("YES quiet"), fresh.stdout
    assert "pending=5" in fresh.stdout, fresh.stdout
    print("✓ 17. --tick due → NO due-hold；新增一条后重新 YES quiet")


# ---------- 18..19：停滞兜底与 --mark 清 due ----------


def test_digest_reap_releases_a_stalled_cursor(home):
    """`--digest --reap`：宽限内不动游标，超宽限推进到本批末尾（issue #183）。

    `--digest` 不推游标是刻意的（留给模型深加工），但没人跑模型时游标就永久
    停滞——issue 的 `pending=385` 就是这么来的。兜底必须同时满足两条：DUE 已经
    过了 `reap_after_minutes`、且本批规则消化真的跑完了。宽限内推进会让模型
    深加工拿不到素材，所以两侧都钉。
    """
    write_config(home, {"quiet_minutes": 0, "reap_after_minutes": 15})
    seed_store(home, messages=6, base_ts=time.time() * 1000)
    for _ in range(3):
        tick(home)
    assert check(home).returncode == 0, check(home).stdout
    assert run(home, DOSSIER, "--tick", "due").returncode == 0

    fresh = run(home, DOSSIER, "--digest", "--reap")
    assert fresh.returncode == 0, (fresh.returncode, fresh.stdout, fresh.stderr)
    assert "reaped" not in fresh.stdout, f"宽限内不许推进游标：{fresh.stdout}"
    assert mark(home).get("last_event_id") is None, mark(home)

    # 把 DUE 回拨到宽限之外：真实等 15 分钟没有意义，时钟是夹具的一部分。
    state = mark(home)
    state["due_at"] = time.time() - 20 * 60
    write_mark(home, state)

    stale = run(home, DOSSIER, "--digest", "--reap")
    assert stale.returncode == 0, (stale.returncode, stale.stdout, stale.stderr)
    assert "reaped through event 6" in stale.stdout, stale.stdout
    after = mark(home)
    assert after.get("last_event_id") == 6, f"游标没被兜底推进：{after}"
    assert after.get("due_event_id") is None and after.get("due_at") is None, after
    assert after.get("quiet_rounds") == 0, f"推进游标后 quiet 计数必须清零：{after}"

    done = check(home)
    assert done.returncode == 3, f"兜底推进后不该再 DUE：{done.stdout}"
    assert "pending=0" in done.stdout, done.stdout
    print("✓ 18. --digest --reap：宽限内不动；超宽限推进到 6，pending 归零")


def test_mark_clears_due_state(home):
    """`--mark` 清 due 状态：沉淀完成后下一批不该被上一批的 DUE 挡住（issue #183）。"""
    write_config(home, {"quiet_minutes": 0})
    seed_store(home, messages=6, base_ts=time.time() * 1000)
    for _ in range(3):
        tick(home)
    assert check(home).returncode == 0
    assert run(home, DOSSIER, "--tick", "due").returncode == 0
    assert mark(home).get("due_event_id") == 6, mark(home)

    marked = run(home, DOSSIER, "--mark", "--through", "5")
    assert marked.returncode == 0, (marked.returncode, marked.stdout, marked.stderr)
    state = mark(home)
    assert state.get("due_event_id") is None, f"--mark 没清 due_event_id：{state}"
    assert state.get("due_at") is None, f"--mark 没清 due_at：{state}"
    assert state.get("last_event_id") == 5, state
    print("✓ 19. --mark 清 due_event_id / due_at，游标推进到 5")


def test_listen_records_due_batch(home):
    """真实 `listen.py` 判 DUE 后写 `due_event_id`；探针不写（issue #183 接线）。

    前一条钉 `--tick due` 本身，这一条钉「`listen.py` 真的调了它」。不接线的
    话，`--check` 永远看不到 due 记录，同一批素材照样每轮重复 DUE——issue 的
    空转现象原样保留，而前面几条照样全绿（用例 14 的同类教训）。
    """
    write_config(home, {"quiet_minutes": 0})
    seed_store(home, messages=4, base_ts=time.time() * 1000)
    done = run(home, LISTEN, "--timeout", "1", "--cycles", "4")
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    assert "DOSSIER_DUE" in done.stdout, done.stdout[-800:]
    state = mark(home)
    assert state.get("due_event_id") == 4, f"listen.py 没记录本批 DUE：{state}"
    assert isinstance(state.get("due_at"), (int, float)), state

    held = check(home)
    assert held.returncode == 3, f"listen 刚叫醒过，下一轮不该再 DUE：{held.stdout}"
    assert "due-hold" in held.stdout, held.stdout

    # 探针是门禁的临时监听，不许写 due 记录（否则跑一次门禁就抑制一轮生产 DUE）。
    write_mark(home, {"quiet_rounds": 3, "quiet_since": time.time() - 600})
    probe = run(home, LISTEN, "--timeout", "1", "--cycles", "1", "--probe")
    assert probe.returncode == 0, (probe.returncode, probe.stdout, probe.stderr)
    assert "DOSSIER_DUE" in probe.stdout, probe.stdout[-800:]
    assert mark(home).get("due_event_id") is None, \
        f"探针写了生产状态（issue #94 PB 的同类污染）：{mark(home)}"
    print("✓ 20. listen.py 判 DUE 后写 due_event_id；--probe 不写")


def test_reap_only_covers_the_digested_prefix(home):
    """`--reap` 只推进到本批规则消化过的末尾，不许跳过中间（issue #183）。

    用例 18 只有 6 条素材，`batch_last == due_event_id`，把实现换成「直接推到
    due_event_id」照样全绿——而 200 条积压时那条实现会跳过 50 条从未消化的消息，
    正是 `--dump` oldest-first 想避免的漏读。这条用 200 条把两批分开。
    """
    write_config(home, {"quiet_minutes": 0, "reap_after_minutes": 15})
    seed_store(home, messages=200, base_ts=(time.time() - 3600) * 1000)
    for _ in range(3):
        tick(home)
    assert check(home).returncode == 0, check(home).stdout
    assert run(home, DOSSIER, "--tick", "due").returncode == 0

    state = mark(home)
    state["due_at"] = time.time() - 20 * 60
    write_mark(home, state)

    done = run(home, DOSSIER, "--digest", "--reap")
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    assert "reaped through event 150" in done.stdout, \
        f"兜底水位越过了本批（150 条）该到的位置：{done.stdout}"
    after = mark(home)
    assert after.get("last_event_id") == 150, after
    following = check(home)
    assert "pending=50" in following.stdout, f"中间 50 条被跳过了：{following.stdout}"
    print("✓ 21. 200 条积压 + 单批 150 → reaped through event 150，pending 剩 50")


def test_future_due_does_not_hold_forever(home):
    """`due_at` 在未来（时钟回拨 / 手改 mark）不许把 DUE 永久压住（issue #183）。

    复核发现：`(now - at) / 60` 为负时恒小于 `reap_after_minutes`，`due_hold`
    永远 True；`reap_stalled` 又把 stalled 夹成 0，连兜底都放不出来。于是
    quiet 主路径被一条坏记录压死，且 `due_state` 的 docstring 明明承诺「坏值
    降级成没有 DUE」。
    """
    write_config(home, {"quiet_minutes": 0, "reap_after_minutes": 15})
    seed_store(home, messages=4, base_ts=time.time() * 1000)
    for _ in range(3):
        tick(home)
    write_mark(home, {"quiet_rounds": 3, "quiet_since": time.time() - 600,
                      "due_event_id": 4, "due_at": time.time() + 6 * 3600})

    done = check(home)
    assert done.returncode == 0, f"未来的 due_at 把 DUE 永久压住了：{done.stdout}"
    assert done.stdout.startswith("YES quiet"), done.stdout
    assert "due-hold" not in done.stdout, done.stdout
    print("✓ 22. due_at 在未来 → 按「没有 DUE」处理，quiet 照常触发")
