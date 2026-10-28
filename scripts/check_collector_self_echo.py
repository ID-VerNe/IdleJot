"""collector 不许把自己的回显写进唤醒流（issue #28）。

唤醒链路本身早就排除了 is_self（up.wake_reason 首行），但发送回执随 @ 批次
一起下发时，collector 照单全收写进 wg_events.jsonl：实测 1076 行里 222 行
（20.6%）是自己的话。它们不唤醒任何人，只撑大文件、拖慢每次 --status /
--list / watch 的全量解析，还让「事件 N 条」看不出真实群活跃度。

这里用临时数据根把落盘契约钉住：自己的回显一条都不许写；群友消息照旧写；
seen 与游标语义按整批推进（丢弃的回显也算处理过），重放同一批不重复写；
collector 重启后 load_seen 只认得文件里的行，重放旧批次也不会把回显补写回来。
自己的话在 chat_inbox 里的副本不受影响 —— 那是 reply.py --context 的来路。

    .venv/Scripts/python.exe scripts/check_collector_self_echo.py
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))
from check_fixture_support import isolated_env, temp_dir

# 一批真实形状：@ 唤醒、自己的回执、群友接话、自己再回一句。
BATCH = [
    {"id": 1, "is_self": False, "content": "@机器人 在吗"},
    {"id": 2, "is_self": True, "content": "在"},
    {"id": 3, "is_self": False, "content": "问你个事"},
    {"id": 4, "is_self": True, "content": "说"},
]


def read_rows(collector):
    if not collector.EVENTS.exists():
        return []
    return [json.loads(line) for line in
            collector.EVENTS.read_text(encoding="utf-8").splitlines() if line.strip()]


def check_filter(collector):
    seen = set()
    written = collector.capture_events(BATCH, seen)
    assert written == 2, f"只该落盘两条群友消息，实际 {written}"
    rows = read_rows(collector)
    assert [r["id"] for r in rows] == [1, 3], rows
    assert all(not r.get("is_self") for r in rows), "自己的回显漏进了唤醒流"
    # 游标/去重按整批推进：被丢弃的回显也算处理过，否则下一轮会反复判定
    assert seen == {1, 2, 3, 4}, seen
    print("  ok  自己的回显不落盘，seen 仍按整批推进")
    return seen


def check_replay(collector, seen):
    before = collector.EVENTS.read_text(encoding="utf-8")
    assert collector.capture_events(BATCH, seen) == 0, "同一批不该写第二遍"
    assert collector.EVENTS.read_text(encoding="utf-8") == before, "重放改变了文件"
    print("  ok  重放同一批不落盘")


def check_self_only(collector):
    seen = set()
    before = read_rows(collector)
    self_only = [{"id": 5, "is_self": True, "content": "嗯"},
                 {"id": 6, "is_self": True, "content": "先这样"}]
    assert collector.capture_events(self_only, seen) == 0, "纯回显批次不该写盘"
    assert seen == {5, 6}, seen
    assert read_rows(collector) == before, "纯回显批次写了文件"
    print("  ok  纯回显批次只推进 seen")


def check_restart(collector):
    # 重启后 load_seen 只从文件恢复；旧批次里被丢弃的回显不在其中，但重放时
    # 仍被 is_self 挡掉，不会补写回磁盘，已落盘的群友消息也不会写第二遍。
    seen = collector.load_seen()
    assert seen == {1, 3}, f"load_seen 只该读回落盘的行，实际 {seen}"
    assert collector.capture_events(BATCH, seen) == 0, "重启后重放旧批次又写了盘"
    assert [r["id"] for r in read_rows(collector)] == [1, 3], read_rows(collector)
    print("  ok  重启后重放旧批次不补写回显")


def check_missing_flag(collector):
    # is_self 缺失按群友处理：宁可多落一条，也不能把真发言悄悄吞掉。
    seen = set()
    assert collector.capture_events([{"id": 7, "content": "没带 is_self"}], seen) == 1
    assert [r["id"] for r in read_rows(collector)] == [1, 3, 7], read_rows(collector)
    print("  ok  is_self 缺失不被误判成自己")


def main():
    with temp_dir(prefix="wg-collector-") as home:
        with patch.dict("os.environ", isolated_env(home), clear=True):
            import collector
            assert str(collector.EVENTS).startswith(str(home)), \
                f"临时数据根没生效，会碰到真实状态目录：{collector.EVENTS}"
            # 真实入口 main() 启动时先建 state 目录，这里补同一前置条件
            collector.STATE_DIR.mkdir(parents=True, exist_ok=True)
            seen = check_filter(collector)
            check_replay(collector, seen)
            check_self_only(collector)
            check_restart(collector)
            check_missing_flag(collector)
    print("PASS: collector 唤醒流只留群友消息，自身回显不落盘（issue #28）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
