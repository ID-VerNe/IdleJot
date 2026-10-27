"""钉住画像沉淀的第三条判据：连续 N 轮没人指名就触发（issue #162）。

issue #162 的现象：`dossier.py --check` 只有两条阈值（攒够 `burst_messages`，
或闲置 `idle_minutes` 且攒够 `min_messages`），**没有「连续几轮没被 @」这一维**。
于是安静的群（没人 @、但一直有人说话）永远判 `NO`：`idle` 不够 20 分钟、
`pending` 也涨不到 80，画像长期不更新。issue 里的实测是
`NO idle=13min pending=0`，本机复现是 `NO idle=7min pending=46` —— 同一形状。

这条门禁钉住新判据与它的接线，任何一处退回旧行为都红：

1. `--tick` 跨进程累加（`listen.py --cycles 1` 一次只跑一轮，「连续 3 轮」是三次
   进程调用），且 `quiet_since` 只在首轮写入、之后不再移动；
2. 轮次够了、素材够了、下限也过了 → `YES quiet` + exit 0；轮次不够 → NOT_YET；
3. `quiet_minutes` 是**下限**：3 轮在几秒内跑完时不触发（`--timeout 30` 时三轮
   只有 90 秒，只钉轮次会让「约 5 分钟」变成假话）；
4. `pending=0` 时即便 3 轮也不触发（空跑一次模型调用）；
5. 真实 `listen.py` 静默两轮 → 两次 tick，`quiet_rounds==2`（接线真的接上了）；
6. 真实 `listen.py` 收到 @ → `WAKE` 之后计数归零（有人指名是「连续无人指名」的反例）；
7. `--no-digest`（`reply.py` 续挂用的形态）与 `--probe`（门禁/测试的临时监听）
   都不计数——否则每次回完消息立刻满足条件，跑一次门禁就推高生产计数；
8. `--mark` 清零计数并推进游标，**再 tick 一次游标仍在**（`save_mark` 覆盖写的
   复发检测：覆盖写会把 `last_event_id` 冲掉，症状是「沉淀完立刻又 DUE」或
   「计数永远不涨」，两种都像偶发）；
9. 判据对时钟的依赖用**扫时钟**验证：`quiet_since` 固定，`now` 扫全天 + 边界，
   必须在 `>= quiet_minutes` 时 DUE、否则 NOT_YET。**不用固定时刻夹具**——
   `quiet_since` 是相对量，固定时刻在跨午夜/跨日时会假绿（#153 的教训）；
10. 对照组：同一组 `now`、同一份 `quiet_since`，只把 `quiet_rounds` 降到 2，
    全部必须 NOT_YET——否则「绿」只证明时长这一维被读到，轮次那一维是死的；
11. `NO` 行的 `quiet=2/3(4.1min)` 用**正则捕获组**逐字段比，不拿子串（#151/#152
    的教训：`"2/3" in text` 挡不住「rounds 印对、since 印错」的坏实现）。

已知边界（这条门禁**不**声称能抓住的）：

- **硬编码日期/时刻**：判据只比相对量，把 `quiet_since` 换成写死的字面量也过；
  要覆盖得多组不同日期的夹具，本门禁没做（#151 已把这条写成既有边界）。
- **多群分槽**：`wg_dossier.json` 本来就是单个全局文件，本 issue 不扩面，
  docstring 里已声明（`doc/MULTI_GROUP_PLAN.md` 负责那次拆分）。

全程临时 `WATER_GROUP_HOME` + `IDLEJOT_WG_CONFIG`（阈值走临时配置，不读开发机
的 `config.json`），不碰 `~/.water-group`：

    .venv/Scripts/python.exe scripts/check_dossier_cadence.py
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_fixture_support  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import isolated_env, temp_dir  # noqa: E402

DAEMON = ROOT / "water-group" / "daemon"
DOSSIER = DAEMON / "dossier.py"
LISTEN = DAEMON / "listen.py"
GROUP = "100200300"
ACCOUNT = "10001"
MARK_REL = Path("state") / "wg_dossier.json"

# 外部测试变量会把 state 指回实机，这里清掉，只留临时数据根。
ISOLATING_VARS = ("IDLEJOT_WG_STATE", "TULPA_WG_STATE", "IDLEJOT_WG_DB",
                  "IDLEJOT_WG_MEMORY_DB")


# ---------- 夹具 ----------


def write_config(home, dossier=None):
    """临时配置：watch.py 要 self_account（#51），阈值走 `dossier` 段。

    阈值必须能覆盖，否则门禁只能吃默认值——开发机的 `config.json` 里一旦出现
    `dossier` 段，门禁就变成依赖机器状态（与 `check_listener_rehang` 同一口径）。
    """
    payload = {"self_account": ACCOUNT, "onebot_token": "cadence-fixture"}
    if dossier:
        payload["dossier"] = dossier
    path = home / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def seed_store(home, messages=0):
    """最小 mcp-access 库：一个活跃会话 + N 条可提炼的群消息。"""
    data = home / "data"
    data.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(data / "mcp-access.sqlite3"))
    db.executescript(
        """
        CREATE TABLE chat_sessions (id INTEGER PRIMARY KEY, conversation_id TEXT, active INTEGER);
        CREATE TABLE chat_inbox (id INTEGER PRIMARY KEY, session_id INTEGER, payload TEXT);
        INSERT INTO chat_sessions (id, conversation_id, active) VALUES (1, 'group:100200300', 1);
        """
    )
    for index in range(messages):
        db.execute("INSERT INTO chat_inbox (id, session_id, payload) VALUES (?, 1, ?)",
                   (index + 1, json.dumps({"sender": "甲", "sender_id": "1",
                                           "content": f"第 {index + 1} 句闲聊"},
                                          ensure_ascii=False)))
    db.commit()
    db.close()


def run(home, script, *args, timeout=180):
    """跑真实子进程（`dossier.py` / `listen.py`），隔离环境 + 临时配置。"""
    env = isolated_env(home=home)
    env["IDLEJOT_WG_CONFIG"] = str(home / "config.json")
    env["SYSTEMDRIVE"] = os.environ.get("SYSTEMDRIVE", "C:")
    env["PYTHONUTF8"] = "1"
    for name in ISOLATING_VARS:
        env.pop(name, None)
    return subprocess.run([sys.executable, str(script), *args],
                          cwd=str(ROOT), env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def mark(home):
    try:
        return json.loads((home / MARK_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_mark(home, payload):
    path = home / MARK_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_events(home, rows):
    path = home / "state" / "wg_events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")


def mention_event(eid, content="@机器人 在吗"):
    """一条 @ 本机的落盘事件（`watch.py` 据此返回 0 = 唤醒）。"""
    return {"id": eid, "timestamp": time.time() * 1000, "is_self": False,
            "sender": "甲", "content": content,
            "segments": [{"type": "at", "qq": ACCOUNT},
                         {"type": "text", "text": content}],
            "conversation_id": "1:group:" + GROUP}


def tick(home, *extra):
    return run(home, DOSSIER, "--tick", *extra)


def check(home):
    return run(home, DOSSIER, "--check")


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


# ---------- 9..11：扫时钟与 NO 行字段 ----------


def _dossier_module():
    """进程内导入 `dossier`（数据根已在 `check_fixture_support` 里钉死）。"""
    sys.path.insert(0, str(DAEMON))
    import dossier  # noqa: PLC0415
    return dossier


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
    dossier = _dossier_module()
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
    dossier = _dossier_module()
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
    dossier = _dossier_module()
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


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with temp_dir(prefix="wg-cadence-") as home:
        seed_store(home)
        write_config(home)
        test_tick_accumulates_across_processes(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_three_rounds_are_due(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_floor_blocks_short_rounds(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_no_material_is_not_due(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_listen_counts_quiet_rounds(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_wake_resets_counter(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_no_digest_and_probe_do_not_count(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_mark_and_tick_do_not_clobber(home)
    test_clock_sweep_matches_floor()
    test_rounds_dimension_is_live()
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_no_line_reports_counter(home)
    with temp_dir(prefix="wg-cadence-") as home:
        seed_store(home)
        write_config(home)
        test_bad_mark_values_do_not_crash(home)
    print("=" * 72)
    print("✓ dossier 沉淀时机（连续 N 轮无人指名）12 项门禁全部通过")
    print("=" * 72)


if __name__ == "__main__":
    main()
