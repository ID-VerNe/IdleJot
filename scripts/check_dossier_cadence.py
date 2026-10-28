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
    的教训：`"2/3" in text` 挡不住「rounds 印对、since 印错」的坏实现）；
12. 写坏的 mark 值（`quiet_since: false` / 字符串 / 列表 / 负数）降级成「没有
    计数」，不崩、也不变成「下限恒过」（`bool` 是 `int` 子类的坑，同 #124）；
13. **默认配置下第 3 轮必须 DUE**。这条补的是一个真实漏测：前 12 条全绿时，
    默认 `quiet_minutes=5` 让 3 轮（4.5 分钟）永远过不了下限，实际要到第 5 轮
    才触发——轮次判据成了死码，而 `NO` 行照样显示 `3/3`。用例 2 把下限覆盖成 0、
    用例 3 只钉「下限在拦」，没有一条断言默认组合能真的触发。修法是两头：
    `--tick --round-seconds` 把 `quiet_since` 回推到首轮**开始**（否则 N 轮只跨
    N-1 个间隔），默认下限取 4（< 4.5 留余量）。用例同时钉住两头。
14. 真实 `listen.py` 静默一轮 → `quiet_since` 回推到本轮开始（`--round-seconds`）。
15. **积压不是爆发**（issue #183）：200 条全部落在 `burst_window_minutes` 之外
    + 3 轮静默 → `YES quiet`，不再被 `YES burst` 短路。这条是 issue 现象的直接
    回归：旧代码只要 pending ≥ 80 就恒报 `YES burst`，quiet 分支根本走不到。
16. 窗口内的真爆发仍报 `YES burst`（原语义没有被 quiet 抢走）。
17. **同一批素材只叫醒一次**：`--tick due` 之后 `--check` 输出 `NO due-hold` 且
    NOT_YET；新消息进来立刻重新 DUE。没有这条，游标不动时每轮都在叫同一批。
18. **停滞兜底**：`--digest --reap` 在宽限内不动游标；超过 `reap_after_minutes`
    才推进到本批末尾，之后 pending 归零、quiet 计数清零、`--check` 转 `NO`。
19. `--mark` 清 due 状态：沉淀完成后下一批不该被上一批的 DUE 挡住。
20. **接线真的接上了**：真实 `listen.py` 判 DUE 后写 `due_event_id`；`--probe`
    不写（临时监听不许污染生产状态）。
21. **reap 只吞已消化的前缀**：200 条积压、单批 150 条 → 推进到 150、剩 50 条
    pending。直接推到 `due_event_id` 会跳过中间 50 条，用例 18 只有 6 条时
    抓不住（复核发现）。
22. `due_at` 在未来（时钟回拨 / 手改 mark）不许把 DUE 永久压住，也不许让
    `--reap` 卡在 `stalled=0`（复核发现）。

已知边界（这条门禁**不**声称能抓住的）：

- **硬编码日期/时刻**：判据只比相对量，把 `quiet_since` 换成写死的字面量也过；
  要覆盖得多组不同日期的夹具，本门禁没做（#151 已把这条写成既有边界）。
- **多群分槽**：`wg_dossier.json` 本来就是单个全局文件，本 issue 不扩面，
  docstring 里已声明（`doc/MULTI_GROUP_PLAN.md` 负责那次拆分）。
- **时间戳缺失的消息算不算爆发**：`recent_count` 对没有 `timestamp` 的行按「不在
  窗口内」处理（不虚报），这里的用例都带时间戳；线上 `collector` 一定写，缺了是
  上游问题，不在本门禁的射程内。

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


def seed_store(home, messages=0, base_ts=None):
    """最小 mcp-access 库：一个活跃会话 + N 条可提炼的群消息。

    `base_ts` 是毫秒时刻（payload 里 `timestamp` 的单位）。issue #183 的 burst
    判据要看「最近窗口内有没有一波」，所以需要能造「一小时前的积压」与「刚刚的
    爆发」两种夹具；不给就是没有时间戳（旧夹具的形态）。
    """
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
        payload = {"sender": "甲", "sender_id": "1", "content": f"第 {index + 1} 句闲聊"}
        if base_ts is not None:
            payload["timestamp"] = int(base_ts + index * 1000)
        db.execute("INSERT INTO chat_inbox (id, session_id, payload) VALUES (?, 1, ?)",
                   (index + 1, json.dumps(payload, ensure_ascii=False)))
    db.commit()
    db.close()


def add_message(home, event_id, ts=None):
    """追加一条消息（issue #183 的「有新素材就重新 DUE」用例要用）。"""
    payload = {"sender": "甲", "sender_id": "1", "content": f"第 {event_id} 句闲聊"}
    if ts is not None:
        payload["timestamp"] = int(ts)
    db = sqlite3.connect(str(home / "data" / "mcp-access.sqlite3"))
    db.execute("INSERT INTO chat_inbox (id, session_id, payload) VALUES (?, 1, ?)",
               (event_id, json.dumps(payload, ensure_ascii=False)))
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
    dossier = _dossier_module()
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

    # 端到端那一维用真实子进程 + 缩短的轮长：三个 1s 轮次 = 3s，把下限临时
    # 压到 0.02min（1.2s）就等价于「3 个间隔已过下限」。这里验的是默认
    # `quiet_minutes` 与默认 `quiet_rounds` 的**关系**能真的触发，不是数字本身。
    write_config(home, {"quiet_minutes": 0.02})
    reset = tick(home, "reset")
    assert reset.returncode == 0, (reset.returncode, reset.stdout, reset.stderr)
    for _ in range(3):
        done = tick(home, "--round-seconds", "1")
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
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_default_config_fires_on_round_three(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_listen_backdates_first_round(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_backlog_is_not_a_burst(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_recent_spike_is_still_a_burst(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_same_batch_is_not_announced_twice(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_digest_reap_releases_a_stalled_cursor(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_mark_clears_due_state(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_listen_records_due_batch(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_reap_only_covers_the_digested_prefix(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_future_due_does_not_hold_forever(home)
    print("=" * 72)
    print("✓ dossier 沉淀时机（连续 N 轮无人指名 + 积压归因 + 游标兜底）22 项门禁全部通过")
    print("=" * 72)


if __name__ == "__main__":
    main()
