"""issue #9 gate: 合并转发里的 @ 与关键词必须参与唤醒判定。

转发消息在唤醒链路上只有 `[合并转发]` 占位：顶层 `segments` 是
`{"type":"forward","forward_index":0}`，正文在 `referenced_messages[].nodes`。
`up.wake_reason` 原先只看顶层 at 段与 `content` 关键词，于是「转发里点名我」
不会唤醒。本脚本钉住下沉后的判定：首层/嵌套的 @ 与关键词、不可得时不猜、
预算封顶、以及 `#` 指令闸门优先。

    .venv/Scripts/python.exe scripts/check_forward_wake.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_fixture_support  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)

sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

import up  # noqa: E402

SELF = "999"
NAME = "小助手"
CONFIG = {"self_account": SELF, "self_names": [NAME], "slash_commands": {"prefix": "#"}}


def event(**extra):
    base = {"id": 1, "is_self": False, "content": "[合并转发]",
            "segments": [{"type": "forward", "forward_index": 0}]}
    base.update(extra)
    return base


def forward(nodes, state="available"):
    return {"type": "forward", "state": state, "nodes": nodes}


def node(text=None, at=None, nodes=None):
    segments = []
    if text is not None:
        segments.append({"type": "text", "text": text})
    if at is not None:
        segments.append({"type": "at", "qq": at})
    if nodes is not None:
        segments.append({"type": "forward", "nodes": nodes})
    return {"sender": "某人", "segments": segments}


def cases():
    # 常量取默认值再回退：基线（未打补丁的 up.py）上没有这个常量，此时门禁应该
    # 红在「转发体不参与判定」的行为上，而不是红在 AttributeError 上。
    nodes_budget = getattr(up, "FORWARD_WAKE_NODES", 100)
    return [
        ("顶层 @ 仍然直接命中", event(segments=[{"type": "at", "qq": SELF}]), "at"),
        ("转发体首层 @", event(referenced_messages=[forward([node(at=SELF)])]), "at"),
        ("转发体首层关键词", event(referenced_messages=[forward([node(text=f"{NAME}在吗")])]),
         f"keyword:{NAME}"),
        ("嵌套第二层 @", event(referenced_messages=[forward([node(nodes=[node(at=SELF)])])]), "at"),
        ("嵌套第二层关键词", event(referenced_messages=[forward([node(nodes=[node(text=NAME)])])]),
         f"keyword:{NAME}"),
        ("正文不可得时不猜", event(referenced_messages=[forward([node(at=SELF)], state="unavailable")]), ""),
        ("节点为空", event(referenced_messages=[forward([])]), ""),
        ("顶层指令闸门优先", event(content=f"#帮助 {NAME}",
                                  referenced_messages=[forward([node(at=SELF)])]), ""),
        # 预算：第 101 个节点里的 @ 不该被扫到。
        (f"超过 {nodes_budget} 节点不再扫描",
         event(referenced_messages=[forward([node() for _ in range(nodes_budget)] + [node(at=SELF)])]),
         ""),
        # 字符预算：前两个节点各 2000 字，名字在第 3 个节点时预算已耗尽。
        ("超过字符预算不再扫描",
         event(referenced_messages=[forward([node(text="x" * 2000), node(text="x" * 2000),
                                             node(text="x" * 2000 + NAME)])]), ""),
    ]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    up.config = lambda: CONFIG
    bad = 0
    items = cases()
    for name, ev, want in items:
        got = up.wake_reason(ev)
        ok = got == want
        print(("PASS" if ok else "FAIL"), name, "->", repr(got), "" if ok else f"(want {want!r})")
        bad += 0 if ok else 1
    print(f"{len(items) - bad}/{len(items)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
