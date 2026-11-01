"""唤醒账本：谁把 Agent 叫醒过、从哪条路叫的、一次叫醒带了几条（issue #144 P1）。

issue #144 的核心症状是「turn 的数量随群活跃度线性膨胀」，但在动手之前先撞上了
一个问题：**按来源的唤醒次数与密度无处可查**。`wg_listener_state`（#112）记的是
值守轮次的起止（「有没有人挂着」），`wg_replied.jsonl`（#209）记的是「回过没有」，
而「被叫醒了多少次、其中多少次来自追问窗口」没有任何文件回答得了。追问窗口的
来源是 `chat_inbox`，从不落 `wg_events.jsonl`，所以它连事件流里都不存在。

没有这份数据，去抖该设几秒、追问窗口该不该攒批，全都只能拍脑袋。本模块补的就是
这份数据：一行一次**被叫醒**。

    {"at": 1793600000.0, "source": "at", "batch": 3, "ids": [11, 12, 13],
     "group_hash": "9d3f1a2b7c4e", "pid": 4321}

* `source` 取 `at` / `keyword` / `followup` / `unmute` 四者之一。前两者直接沿用
  `wg_up_wake.wake_reason()` 的返回值（它已经区分 @ 与关键词），不另算一份口径——
  两份口径迟早会分叉，而分叉那天没有任何东西会报警。
* `batch` 是这一批叫醒里带了几条消息。它就是去抖和攒批到底省了多少的原始度量：
  `batch` 全是 1 说明每条消息各醒一次，合并空间大；`batch` 多是 5 说明早就在合并。
* **群号不落盘**：`group_hash` 是 `sha256(群号)` 的前 12 位十六进制。账本要能在
  issue 里公开讨论（#144 就要求「按来源分别报出」），而公开讨论里不该出现真实群号。
  哈希只用于区分「同一时刻是几个群在叫」，不做任何反查。

## 谁能写

只有三个地方：`watch.py` 的两处 `print("WAKE")` 之前（@/关键词、追问），以及
`watch.poll_unmute()` 的解禁唤醒。**不要**在渲染分支里散写——一处漏写就是一次
「明明叫醒了但账本里没有」，而这种缺失比没有账本更坏：它会让人相信已经合并够了。

## 读侧

`up.py --status` 的「唤醒」行：过去 24h 按来源报出。窗口内一条都没有时明说
「过去 24h 无唤醒」，**不打印 0**——「0 次」和「账本还没开始记」在屏幕上长得一样，
而这两件事的后续动作完全不同（前者说明群里安静，后者说明要等它攒数据）。

## 不做什么

* 不扩 `wg_listener_state`：它回答「有没有人在值守」，与「叫醒了几次」是两个问题，
  混进去只会让 #112 那行越读越长。
* 不轮转：845 次唤醒 / 13 天 ≈ 每行 100 字节 → 一年约 2.5 MB。真到 50 MB 再加，
  别为一年后的事提前写代码。
"""
import hashlib
import json
import os
import time

import wg_paths

# 与其它 daemon 模块同一写法规约：状态目录是模块常量，门禁靠改它来隔离。
STATE = wg_paths.state_dir()

# 一天的窗口是 `--status` 的判据，也写在文案里；两处必须是同一个数。
WINDOW_SECONDS = 24 * 3600
# `batch` 只长这样；来源写错时读侧会归到 `other` 而不是崩。
SOURCES = ("at", "keyword", "followup", "unmute")
_OTHER = "other"


def path():
    return STATE / "wg_wakes.jsonl"


def group_hash(group_id):
    """群号 → 12 位十六进制；空群号给空串（「不知道是哪个群」，不编一个）。"""
    text = str(group_id or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _normalize(source):
    """`wake_reason()` 会返回 `keyword:<命中词>` 这类带明细的串，归一到分类。

    明细（命中了哪个关键词）不进账本：它随群友黑话漂移，统计用不上，却会把
    账本变成一份可以反推群内用语的记录。
    """
    text = str(source or "").strip()
    head = text.split(":", 1)[0].lower()
    return head if head in SOURCES else _OTHER


def record(source, ids=(), group_id="", batch=None):
    """追加一行唤醒记录；返回写入的字典（失败时返回 None）。

    写入**永不抛**：账本是观测件，不该因为磁盘满、目录被删、权限不对就让一次
    真实的唤醒发不出去。失败时静默返回 None——「账本少一行」可以由 `--status`
    的「账本距今多久」看出来，而「因为写账本失败所以没叫醒人」是无法接受的。
    """
    ids = [i for i in dict.fromkeys(ids) if i is not None]
    row = {
        "at": round(time.time(), 3),
        "source": _normalize(source),
        "batch": int(batch if batch is not None else len(ids) or 1),
        "ids": ids,
        "group_hash": group_hash(group_id),
        "pid": os.getpid(),
    }
    try:
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        # 一行一次 `write` + 追加模式：多进程并发写时最多交错整行，不会互相截断
        # 半行（O_APPEND 下小于 PIPE_BUF 的写入是原子的，这里每行远小于它）。
        with open(target, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return row


def rows(since=None):
    """读账本，按时间升序；坏行跳过（issue #144 验收条件 6）。

    `since` 是 Unix 秒下限（含）。文件不存在返回空列表——**没开始记**与
    「记了但是空」在调用方由 `exists()` 区分，这里不替它判断。
    """
    target = path()
    if not target.exists():
        return []
    floor = float(since) if since is not None else None
    out = []
    try:
        with open(target, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                try:
                    moment = float(row.get("at"))
                except (TypeError, ValueError):
                    continue  # `at` 是字符串或是空的：这行说不清「什么时候」
                if floor is not None and moment < floor:
                    continue
                row["at"] = moment
                row["source"] = _normalize(row.get("source"))
                try:
                    row["batch"] = max(1, int(row.get("batch") or 1))
                except (TypeError, ValueError):
                    row["batch"] = 1
                out.append(row)
    except OSError:
        return []
    out.sort(key=lambda r: r["at"])
    return out


def exists():
    return path().exists()


def counts(since=None):
    """按来源计数：`{"at": 2, "keyword": 1, ...}`，只含有出现的来源。"""
    tally = {}
    for row in rows(since=since):
        tally[row["source"]] = tally.get(row["source"], 0) + 1
    return tally


def describe(now=None):
    """`--status` 的「唤醒」行正文。

    账本不存在时说「账本还没开始记」，不说「0 次」——两者在屏幕上长得一样，
    后续该做的事却相反（等它攒 vs 群里真的安静）。窗口内无记录时才说
    「过去 24h 无唤醒」，此时账本是**存在的**，所以这句话是核实的结论。
    """
    moment = time.time() if now is None else float(now)
    if not exists():
        return "账本还没开始记（state/wg_wakes.jsonl 会在下一次唤醒时出现）"
    tally = counts(since=moment - WINDOW_SECONDS)
    if not tally:
        return "过去 24h 无唤醒"
    total = sum(tally.values())
    parts = [f"{name} {tally[name]}"
             for name in (*SOURCES, _OTHER) if name in tally]
    return f"过去 24h {total} 次（{' / '.join(parts)}）"
