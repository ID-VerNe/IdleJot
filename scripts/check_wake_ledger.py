"""钉住「已回」的判定：一条回复不能把后面来的 @ 全洗成已回。

2026-10-20 的真实事故：群里两条消息相隔 30 秒，后一条 @ 了一条更早的消息（`1564`），回 1563
的那条落在 1564 之后，`replied_after` 的兜底（这条之后出现过自己的消息就算回过）
就把 1564 记成已回——`--list` 显示 `[已回]`，`watch.py` 也把它从 fresh 里滤掉，
用户看到的就是「@ 了没反应」。现在兜底只对账本建立之前的老消息生效。

issue #209：账本起点的**边界那一毫秒**以前被划进「账本期内」——`seen` 已经置上，
于是 `>=` 直接 `return False`，那条其实回过的消息永久 `[待回]`、被重放一次。起点现取
账本首行的 `ts`（整秒），事件时间戳撞上它是常规情况，所以边界必须有一档钉死；同时
钉住「起点之后仍归账本」，防止修复方式变成把整条边推到兜底一侧。

    .venv/Scripts/python.exe scripts/check_wake_ledger.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

import wg_up_wake  # noqa: E402  (issue #37 PC: 账本判定的真实调用点)

# 相对真实时钟取基准：`ledger_started()` 现在返回账本首行的 `ts`，是真实时间；
# 固定绝对值（曾是 1000.0）会让下面「之前 / 之内」的位置关系失真（issue #209）。
START = float(int(time.time()))


def rows():
    # 时间戳升序 = 群内实际发言顺序。id=5（恰好等于账本起点）在 id=3（自己的回复）
    # 之前，兜底「之后有自发言」才成立——这正是 issue #209 的复现命令形状。
    return [
        {"id": 1, "timestamp": (START - 60) * 1000, "is_self": False},  # 账本之前
        {"id": 5, "timestamp": START * 1000, "is_self": False},         # 恰好等于账本起点（#209）
        {"id": 6, "timestamp": (START + 0.001) * 1000, "is_self": False},  # 起点之后 1ms
        {"id": 2, "timestamp": (START + 10) * 1000, "is_self": False},  # 后一条 @ 了更早的一条
        {"id": 3, "timestamp": (START + 20) * 1000, "is_self": True},   # 回的是 id=1
        {"id": 4, "timestamp": (START + 30) * 1000, "is_self": False},  # 又一条 @
    ]


CASES = [
    ("老消息靠兜底判已回", 1, {1}, True),
    ("账本期内不许被自己的消息洗白", 2, {1}, False),
    ("更后面那条同样不许", 4, {1}, False),
    ("账本里明确记过就是已回", 3, {3}, True),
    # issue #209：边界那一毫秒归兜底，账本不许拿「没记过」去否定它。
    ("恰好等于账本起点走兜底", 5, {1}, True),
    ("起点之后 1ms 仍归账本", 6, {1}, False),
]


def ledger_started_from_content(real):
    """起点取账本首行的 `ts`，不是文件 mtime（issue #209）。

    在临时数据根里真写一份账本，把 mtime 显式改到别的秒上做对照：起点必须跟首行走，
    否则「首行 ts 与 mtime 差最多 1 秒」会让边界判定整体漂移。

    ``real`` 是本函数唯一的起点来源：``main()`` 已把 ``ledger_started`` 换成
    固定常量的桩（下面的 CASES 靠它），这里若直接调模块属性，桩会把结论钉成
    ``START``，mtime 换成多少都能「通过」——测的就不是本用例了。
    """
    first = START - 3600  # 首行远比 mtime 老，两者分得开
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state"
        state.mkdir()
        path = state / "wg_replied.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": 7, "ts": int(first), "text": "x"}) + "\n")
            fh.write(json.dumps({"id": 8, "ts": int(START), "text": "y"}) + "\n")
        stamp = START - 600  # mtime 落在两者之间：只读 mtime 会得到这个值
        os.utime(path, (stamp, stamp))
        saved_state, saved_cache = wg_up_wake.STATE, dict(wg_up_wake._LEDGER_START)
        try:
            wg_up_wake.STATE = state
            wg_up_wake._LEDGER_START.update(size=None, ts=0.0)  # 别吃上一个用例的缓存
            got = real()
        finally:
            wg_up_wake.STATE = saved_state
            wg_up_wake._LEDGER_START.clear()
            wg_up_wake._LEDGER_START.update(saved_cache)
    ok = got == first
    print(f"{'✓' if ok else '✗'} 账本起点取首行 ts 而非 mtime")
    if not ok:
        print(f"  got={got} want={first}（mtime={stamp}）")
    return ok


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    real = wg_up_wake.ledger_started  # 真实现，content 用例要用
    wg_up_wake.ledger_started = lambda: START  # 固定账本起点，不吃真实文件
    bad = 0
    try:
        for name, event_id, ledger, want in CASES:
            got = wg_up_wake.replied_after(event_id, rows(), ledger)
            if got == want:
                print(f"✓ {name}")
            else:
                bad += 1
                print(f"✗ {name}: got={got} want={want}")
        if not ledger_started_from_content(real):
            bad += 1
    finally:
        wg_up_wake.ledger_started = real
    print("全部通过" if not bad else f"{bad} 项失败")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
