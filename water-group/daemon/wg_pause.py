"""采集器暂停标记：谁写的、凭什么还算数、什么时候自己消失（issue #189）。

`state/wg_pause` 曾经是个只有读方的控制文件：`collector` 在它存在时每 2 秒空转、
且**不写心跳**，而全仓没有任何写入方（`grep -rn wg_pause water-group chatlocal
scripts` 只有三处读，零处写）。2026-10-28 现场那份的内容是 `placeholder`——交接时
手写进去的占位符。它一存在就等于永久停采：`supervisor` 按 `STALE_AFTER=300` 判死
重启，重启后第一轮又看见同一个文件，回到同一条空转分支。进程在、端口在、监听也挂
着，事件流水却不推进，`up.py --status` 一个字都不提——这就是 issue #189 的现场。

本模块把它变成有主、有时限、可判定的记录，并成为**唯一读取口径**：`collector`、
`up.py --status`、`reload_exec._busy()` 都调这里，不再各自 `exists()`。

规范格式（一行 JSON）：

    {"reason": "发送中", "pid": 12345, "at": 1761642000.0, "until": 1761642030.0}

判定表（`probe()`，`now` 可注入，门禁据此扫时钟）：

| 文件 | 判定 | 依据 |
|---|---|---|
| 不存在 / 读不出 | `none` | 不暂停、不报错 |
| 规范记录，`pid` 存活且未到期限 | `active` | 有效暂停，受保护 |
| 规范记录，`pid` 已退出 | `stale` | 写它的进程没了，它保护的发送不可能还在进行 |
| 规范记录，超过 `until`（无 `until` 则 `at+TTL`） | `stale` | 时限到 |
| 非规范内容（纯文本、坏 JSON、缺 `reason`） | 宽限内 `active`，超宽限 `stale` | 遗留标记：给不出 owner，就只能靠时间分辨 |

两条方向性约定：

* **fail-open**：判定不出来就当没有暂停。暂停消失的默认后果是继续采集，而不是
  继续停——「读不懂就停」正是本 issue 的现场。反过来「读不懂就删」也不对：那会
  把一份正在保护发送的记录当成垃圾清掉，所以非规范内容仍给它 `LEGACY_GRACE`。
* **时限优先于存在**：`TTL=300` 是上限，`until` 只能比它更早；`at` 写成未来时刻
  按现在算。任何标记都不可能无限期成立——否则一次崩溃就换来永久停采。

`LEGACY_GRACE` 取 `2 × wg_budget.SEND_BUDGET`（60 秒）：一次正常发送最多 30 秒，
两份余量足够把「正在发」与「有人塞了个哑文件」分开。
"""
import json
import math
import os
import time
from pathlib import Path
from typing import NamedTuple

import wg_budget
import wg_clock
from wg_daemon import pid_alive, write_json
from wg_paths import state_dir

NAME = "wg_pause"

# 任何标记最长成立多久（秒）。与 supervisor.STALE_AFTER 同量级：暂停期间采集器照写
# 心跳，所以两者不必相等；选同一个数是为了运维只需记一个「五分钟」。
TTL = 300.0

# 非规范内容的宽限：一次正常发送 ≤ SEND_BUDGET=30 秒，两份余量足够区分。
LEGACY_GRACE = float(wg_budget.SEND_BUDGET * 2)

# 只读前 4 KiB：坏文件可能是任意大小，状态行不该为了报一行字把整个文件读进内存。
READ_LIMIT = 4096

# 原因字段在日志/状态行里的显示上限（按 `text_limits.clip` 的约定，被切过带 `…`）。
REASON_CHARS = 40


class Pause(NamedTuple):
    """一次判定的结果。`state` 三态：`none` / `active` / `stale`。"""

    state: str
    reason: str
    pid: int | None
    at: float | None
    age: float
    why: str


def path(root=None) -> Path:
    """标记文件路径。`root` 省略时取当前 state 目录（多群各一份，天然互不影响）。"""
    return (Path(root) if root is not None else state_dir()) / NAME


def _clip(text) -> str:
    """单行化 + 按 `text_limits.clip` 截断（「怎么切」的唯一归属）。"""
    import wg_text  # noqa: PLC0415  延迟：它要往 sys.path 塞仓库根

    flat = " ".join(str(text or "").split())
    clipped = wg_text.clip()(flat, REASON_CHARS)
    return clipped.text + ("…" if clipped.truncated else "")


def _num(value):
    """数字字段：布尔与非有限值一律当没写（坏数据不该换来一个期限）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _pid(value):
    """pid 字段：正整数或纯数字字符串，其余 None（布尔是 int 子类，先排除）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, int) and value > 0:
        return value
    return None


def _record(raw):
    """规范记录：一行 JSON 对象且 `reason` 非空；其余一律 None（走 legacy 判定）。"""
    try:
        record = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    reason = record.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    return record


def probe(root=None, now=None) -> Pause:
    """读一次标记并判定。**只读**：不删文件、不写日志，`--status` 可以放心调。"""
    now = time.time() if now is None else float(now)
    target = path(root)
    try:
        with open(target, "rb") as fh:
            raw = fh.read(READ_LIMIT)
        mtime = target.stat().st_mtime
    except OSError:
        return Pause("none", "", None, None, 0.0, "没有暂停标记")

    record = _record(raw)
    if record is None:
        text = _clip(raw.decode("utf-8", "replace")) or "（空文件）"
        age = max(0.0, now - mtime)
        if age < LEGACY_GRACE:
            left = wg_clock.span(max(1, int(LEGACY_GRACE - age)))
            return Pause("active", text, None, mtime, age,
                         f"遗留标记（非规范内容 {text!r}），{left}后自动清理")
        return Pause("stale", text, None, mtime, age,
                     f"遗留标记（非规范内容 {text!r}）已过 {wg_clock.span(int(LEGACY_GRACE))}宽限")

    reason = _clip(record["reason"])
    pid = _pid(record.get("pid"))
    # `at` 写成未来时刻就按 mtime 算：坏时间戳不该换来无限期暂停。这里不能夹到
    # 调用方的 `now` 上——那样每次 probe 都会得到一个「刚写的」时刻，等于永不过期。
    written_at = _num(record.get("at"))
    at = mtime if written_at is None or written_at > now else written_at
    # mtime 也可能是未来时刻（时钟回拨 / 从别处拷来的文件）：再夹一次，期限才有上界。
    at = min(at, now)
    deadline = at + TTL
    until = _num(record.get("until"))
    if until is not None:
        # `until` 只能提前收工，不能突破 TTL——「任何标记最长 TTL 秒」是硬不变量。
        deadline = min(deadline, max(until, at))
    age = max(0.0, now - at)
    alive = None if pid is None else pid_alive(pid)

    if alive is False:
        return Pause("stale", reason, pid, at, age,
                     f"owner pid {pid} 已退出——写它的进程没了，"
                     f"它保护的发送不可能还在进行")
    if now >= deadline:
        return Pause("stale", reason, pid, at, age,
                     f"时限已到（{wg_clock.stamp(deadline)} 起失效）")
    who = f"owner pid {pid} 存活" if alive else "没有 owner pid"
    return Pause("active", reason, pid, at, age,
                 f"{who}，{wg_clock.span(max(1, int(deadline - now)))}后自动恢复")


def active(root=None, now=None) -> bool:
    """这一轮该不该停采（只有 `active` 算数；`stale` 不算）。"""
    return probe(root, now).state == "active"


def describe(info: Pause) -> str:
    """状态行里的一句话（`none` 时调用方不该 print，这里也返回空串）。"""
    if info.state == "none":
        return ""
    age = wg_clock.span(max(0, int(info.age)))
    if info.state == "active":
        return f"暂停中（原因：{info.reason}；{info.why}；已停 {age}）"
    return f"标记已失效（原因：{info.reason}；{info.why}；存在 {age}）"


def clear_stale(root=None, now=None) -> str:
    """删掉一个已失效的标记，返回一句可写进日志的说明；没东西可删时返回 ""。

    删前复查一次（TOCTOU）：判定与删除之间标记可能已被换成一份有效暂停，
    那时删掉它等于替别人结束发送。清理归读取方——没有别的人会来清。
    """
    info = probe(root, now)
    if info.state != "stale":
        return ""
    try:
        path(root).unlink()
    except FileNotFoundError:
        return ""
    except OSError as exc:  # noqa: BLE001  删不掉也要说清，别静默继续停
        return f"失效的暂停标记删不掉（{type(exc).__name__}: {exc}）：{info.why}"
    return f"清掉失效的暂停标记（{info.why}），继续采集"


_LAST_LOGGED = {"key": None}


def gate(root=None, now=None):
    """采集器每轮调一次：返回 `(是否停采, 该记的一行日志)`。

    有效暂停返回 `True`，并且只在「刚进入 / 原因或 owner 变了」时给出一行说明，
    不按 2 秒一轮刷屏；失效标记照旧返回 `False`（该采集），顺手删掉并说明。
    """
    info = probe(root, now)
    key = (str(root), info.state, info.reason, info.pid, info.at)
    if info.state == "active":
        if key == _LAST_LOGGED["key"]:
            return True, ""
        _LAST_LOGGED["key"] = key
        return True, f"采集暂停（{info.why}）：{info.reason}——这一轮不采，心跳照写"
    _LAST_LOGGED["key"] = None
    if info.state == "stale":
        return False, clear_stale(root, now)
    return False, ""


def acquire(reason, root=None, ttl=None, pid=None) -> Path:
    """写一份规范标记，返回文件路径。

    `pid` 默认当前进程，于是标记跟着写它的进程活：进程一退（正常退出或崩溃），
    下一轮就判 `stale`，不会留下没人认领的哑文件——这正是 issue #189 的成因。
    """
    text = " ".join(str(reason or "").split())
    if not text:
        raise ValueError("acquire() 需要一个说明原因的字符串")
    now = time.time()
    window = TTL if ttl is None else max(1.0, min(float(ttl), TTL))
    record = {
        "reason": text[:REASON_CHARS],
        "pid": int(pid if pid is not None else os.getpid()),
        "at": now,
        "until": now + window,
    }
    target = path(root)
    if not write_json(target, record):
        # 写不出去就别假装暂停成立：发送方会继续发，而不是以为有人替它挡着。
        raise OSError(f"暂停标记写不出去：{target}")
    return target


def release(root=None) -> bool:
    """删掉标记；本来就不存在时返回 False（幂等，不算失败）。"""
    try:
        path(root).unlink()
        return True
    except OSError:
        return False
