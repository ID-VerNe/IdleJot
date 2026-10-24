"""钉住待回时效：超时的唤醒不再算 [待回]，回复默认拒绝。

2026-10-22 的真实故障：隔了很久第一次启动水群栈，`reply.py --list` 把昨天及更早
从未回复过的 @ 全部翻出来标 [待回]，Agent 据此回复早已过时的话题。根因是待回
判定链路（wake_reason + replied_after）没有任何时间维度，而 wg_events.jsonl
只增不减——旧唤醒永远挂 [待回]。

现在按事件时间实时判定（up.wake_expired，TTL 取 config.json 的
pending_reply.max_age_seconds，默认 7200）：

  - `--list` / `up.py --status`：过期显示 [过期]，不再诱导回复
  - `unanswered()`：过期不进发送前 NOTE，不被陈年旧账刷屏
  - `reply.py <id>`：目标是过期唤醒时默认拒绝，--force 显式放行
  - `watch.py`：冷启动游标归零后，过期唤醒不当新唤醒打印

过期不等于已回：不写 wg_replied.jsonl，每次现场算，改 TTL 立即对历史生效。

    .venv/Scripts/python.exe scripts/check_pending_reply.py
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

import up  # noqa: E402

import reply  # noqa: E402

NOW = time.time()
FRESH = (NOW - 5 * 60) * 1000        # 5 分钟前：任何合理 TTL 内都算新鲜
OLD = (NOW - 3 * 3600) * 1000        # 3 小时前：默认 TTL 7200 下已过期
TTL = 7200

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def rows():
    return [
        {"id": 9001, "timestamp": FRESH, "is_self": False,
         "sender": "甲", "content": "@小鲸鱼 在吗", "conversation_id": "10001:group:123"},
        {"id": 9002, "timestamp": OLD, "is_self": False,
         "sender": "乙", "content": "@小鲸鱼 旧话题", "conversation_id": "10001:group:123"},
        {"id": 9003, "timestamp": 0, "is_self": False,
         "sender": "丙", "content": "@小鲸鱼 没有时间戳", "conversation_id": "10001:group:123"},
    ]


def patch_up(event_rows, ledger=()):
    """固定 up 的输入：事件流、账本、唤醒词、已回判定。"""
    up.events = lambda: list(event_rows)
    up.replied_ids = lambda: set(ledger)
    up.replied_after = lambda *a, **k: False
    up.self_account = lambda: "10001"
    up.wake_words = lambda: ["小鲸鱼"]


_real_config = up.config


def restore_config():
    up.config = _real_config


# ---------- wake_expired：时效判定的边界 ----------


@case
def 新鲜唤醒不过期():
    return up.wake_expired({"timestamp": FRESH}) is False


@case
def 超龄唤醒过期():
    return up.wake_expired({"timestamp": OLD}) is True


@case
def 卡在边界上不算过期():
    # 严格大于才算过期：恰好等于 TTL 的那一刻还新鲜，再挪一微秒就过期。
    # 用小整数时间戳配显式 TTL，不用真实 time.time() 反推边界——纪元秒量级下
    # (x*1000)/1000 的浮点来回有约 1e-7 秒误差，实测约 1.2% 概率把「恰好
    # 等于」舍成「大于」，CI 上随机红过一次；这里全部取值精确可表示。
    ttl = 1.0
    up.config = lambda: {"pending_reply": {"max_age_seconds": ttl}}
    try:
        edge = 1_000_000                       # ms，精确
        now = edge / 1000 + ttl                # 1001.0，精确
        return (up.wake_expired({"timestamp": edge}, now=now) is False
                and up.wake_expired({"timestamp": edge}, now=now + 0.001) is True)
    finally:
        restore_config()


@case
def 没有时间戳不判过期():
    # 数据异常宁可多显示一条，也不悄悄藏成「没问题」
    return (up.wake_expired({"timestamp": 0}) is False
            and up.wake_expired({}) is False)


@case
def 过期判定读配置():
    up.config = lambda: {"pending_reply": {"max_age_seconds": 60}}
    two_min_old = {"timestamp": (time.time() - 120) * 1000}
    try:
        got = up.wake_expired(two_min_old)
    finally:
        restore_config()
    return got is True


@case
def TTL_配置非法回落默认():
    results = []
    for raw in (None, "abc", 0, -5, ""):
        up.config = lambda raw=raw: {"pending_reply": {"max_age_seconds": raw}}
        results.append(up.pending_reply_ttl())
    up.config = lambda: {}  # 整段缺失
    results.append(up.pending_reply_ttl())
    restore_config()
    return results == [up.DEFAULT_PENDING_REPLY_TTL] * 6


_real_config = up.config


# ---------- wake_state / --list：三态标记 ----------


@case
def 三态标记齐全():
    patch_up(rows())
    by_id = {e["id"]: e for e in rows()}
    fresh = up.wake_state(by_id[9001], rows(), set())
    old = up.wake_state(by_id[9002], rows(), set())
    return fresh == "待回" and old == "过期"


@case
def 已回仍然优先():
    patch_up(rows())
    up.replied_after = lambda *a, **k: True
    by_id = {e["id"]: e for e in rows()}
    got = up.wake_state(by_id[9002], rows(), set())  # 过期但回过
    up.replied_after = lambda *a, **k: False
    return got == "已回"


@case
def list_输出区分待回和过期():
    patch_up(rows())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        reply.list_wakes()
    lines = {ln.split()[0]: ln for ln in buf.getvalue().splitlines() if ln.startswith("id=")}
    return ("[待回]" in lines.get("id=9001", "")
            and "[过期]" in lines.get("id=9002", "")
            and "[过期]" not in lines.get("id=9003", ""))


@case
def unanswered_滤掉过期唤醒():
    patch_up(rows())
    got = reply.unanswered([9001])
    ids = [e["id"] for e in got]
    # 9001 被这条回复覆盖；9002 过期不该再进 NOTE；9003 无时间戳不过期，仍在
    return ids == [9003]


# ---------- reply.py 主路径：拒绝 / 放行 / 不误伤 ----------


def run_reply(args, state_dir):
    env = dict(os.environ)
    env["IDLEJOT_WG_STATE"] = str(state_dir)
    return subprocess.run([sys.executable, str(ROOT / "water-group" / "daemon" / "reply.py"),
                           *args],
                          capture_output=True, text=True, timeout=90, cwd=str(ROOT), env=env)


def make_state():
    tmp = tempfile.mkdtemp(prefix="wg_pending_")
    state = Path(tmp)
    (state / "wg_session.txt").write_text("session-fixture", encoding="utf-8")
    lines = [
        {"id": 9001, "timestamp": FRESH, "is_self": False, "sender": "甲",
         "content": "新的 @", "conversation_id": "10001:group:123",
         "onebot_message_id": "555001"},
        {"id": 9002, "timestamp": OLD, "is_self": False, "sender": "乙",
         "content": "旧的 @", "conversation_id": "10001:group:123",
         "onebot_message_id": "555002"},
    ]
    (state / "wg_events.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in lines) + "\n", encoding="utf-8")
    return state


@case
def 回复过期唤醒被拒绝():
    # --no-quote 让流程停在计划通道（临时 STATE 没有批次），任何情况下都不会真发
    proc = run_reply(["9002", "测试", "--no-quote"], make_state())
    return proc.returncode == 1 and "拒绝" in proc.stdout and "--force" in proc.stdout


@case
def force_显式放行过期唤醒():
    proc = run_reply(["9002", "测试", "--no-quote", "--force"], make_state())
    return "拒绝" not in proc.stdout and "没有可用批次" in proc.stdout


@case
def 新鲜唤醒不受影响():
    proc = run_reply(["9001", "测试", "--no-quote"], make_state())
    return "拒绝" not in proc.stdout and "没有可用批次" in proc.stdout


@case
def 不在事件流里的id不误伤():
    # 追问闲聊被 mention_only 滤掉，压根不在 wg_events.jsonl，不能判过期
    proc = run_reply(["9999", "测试", "--no-quote"], make_state())
    return "拒绝" not in proc.stdout


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    bad = 0
    for fn in CASES:
        try:
            got = fn()
        except Exception as exc:  # noqa: BLE001
            got = False
            print(f"✗ {fn.__name__}: {type(exc).__name__}: {exc}")
            bad += 1
            continue
        if got:
            print(f"✓ {fn.__name__}")
        else:
            bad += 1
            print(f"✗ {fn.__name__}")
    print("全部通过" if not bad else f"{bad} 项失败")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
