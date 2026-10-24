"""钉住「已回」的判定：一条回复不能把后面来的 @ 全洗成已回。

2026-10-20 的真实事故：群里两条消息相隔 30 秒，后一条 @ 了一条更早的消息（`1564`），回 1563
的那条落在 1564 之后，`replied_after` 的兜底（这条之后出现过自己的消息就算回过）
就把 1564 记成已回——`--list` 显示 `[已回]`，`watch.py` 也把它从 fresh 里滤掉，
用户看到的就是「@ 了没反应」。现在兜底只对账本建立之前的老消息生效。

    .venv/Scripts/python.exe scripts/check_wake_ledger.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

import up  # noqa: E402

START = 1000.0


def rows():
    return [
        {"id": 1, "timestamp": (START - 60) * 1000, "is_self": False},  # 账本之前
        {"id": 2, "timestamp": (START + 10) * 1000, "is_self": False},  # 后一条 @ 了更早的一条
        {"id": 3, "timestamp": (START + 20) * 1000, "is_self": True},   # 回的是 id=1
        {"id": 4, "timestamp": (START + 30) * 1000, "is_self": False},  # 又一条 @
    ]


CASES = [
    ("老消息靠兜底判已回", 1, {1}, True),
    ("账本期内不许被自己的消息洗白", 2, {1}, False),
    ("更后面那条同样不许", 4, {1}, False),
    ("账本里明确记过就是已回", 3, {3}, True),
]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    up.ledger_started = lambda: START  # 固定账本起点，不吃真实文件
    bad = 0
    for name, event_id, ledger, want in CASES:
        got = up.replied_after(event_id, rows(), ledger)
        if got == want:
            print(f"✓ {name}")
        else:
            bad += 1
            print(f"✗ {name}: got={got} want={want}")
    print("全部通过" if not bad else f"{bad} 项失败")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
