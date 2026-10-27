"""「现在」的唯一出口：每个值守报告都要带当前时刻（issue #95）。

2026-10-23 的真实翻车：Agent 把官方公告的「今晚八点开播」照搬进群里，而当时已经
20:51。资料没错，错的是它没有任何地方能读到「现在」——整条工具链只输出消息时间戳、
相对时间（`19s 前`）和未来截止时刻（`静默至 20:55:15`），这三种都不能当基准：
消息可能是十分钟前的，相对量在模型思考几十秒后就失真了。

所以时间格式化统一收在这里，不让每个脚本各写一份 strftime：

    now_line()   "现在 2026-10-23 20:52:07 (UTC+8)"   报告顶部的锚点
    stamp(ts)    "2026-10-23 20:52:07"                 需要绝对时刻的地方
    clock(ts)    "20:52:07"                            消息时间戳（沿用旧格式）
    clock_ms(ms) 同上，入参是毫秒（wg_events.jsonl 的 timestamp 单位）
    span(secs)   "3 小时 12 分"                        时长（多久 / 空窗多长）
    ago(ts)      "3 小时 12 分前"                      距今多久（issue #148）
    window(start, end) "07:21:40–09:30:26"             一段窗口；跨天时两端补日期

`span` / `ago` / `window` 是 issue #148 收上来的：缺口账本要报「账本是什么时候算的、
距今多久」，而时长渲染此前私有在 `wg_listener_state._span()` 里。同一件事只许有一份
实现（与 `text_limits.clip()` 同口径），否则「14 分 37 秒」与「14 分 37 秒前」会在
两处慢慢漂。

时区不写死 UTC+8：偏移从本地时区算，跨时区部署时标签跟着走，报告不会说谎。
"""

import time
from datetime import datetime, timedelta, timezone

FORMAT_STAMP = "%Y-%m-%d %H:%M:%S"
FORMAT_CLOCK = "%H:%M:%S"
FORMAT_MONTH_DAY = "%m-%d"


def _local(ts=None):
    """本地时区的 datetime；ts 省略就是此刻（秒）。

    先按 UTC 造 aware datetime 再转本地，而不是 `fromtimestamp(ts).astimezone()`：
    后者在 Windows 上对 epoch 附近的时刻会走 mktime，`ts=0` 直接抛
    OSError(22)——`wg_events.jsonl` 里时间戳缺失时恰好会落到这条路径。
    """
    if ts is None:
        return datetime.now().astimezone()
    return datetime.fromtimestamp(float(ts), timezone.utc).astimezone()


def _label(moment):
    offset = moment.utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    if minutes == 0:
        return "UTC"
    sign = "+" if minutes > 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" if not rest else f"UTC{sign}{hours}:{rest:02d}"


def stamp(ts=None):
    """`2026-10-23 20:52:07`：需要绝对日期时刻的地方。"""
    return _local(ts).strftime(FORMAT_STAMP)


def clock(ts=None):
    """`20:52:07`：消息时间戳沿用的短格式。"""
    return _local(ts).strftime(FORMAT_CLOCK)


def clock_ms(ms):
    """事件时间戳是毫秒（wg_events.jsonl / chat_inbox payload），转成本地时钟。"""
    return clock((ms or 0) / 1000)


def tz_label(ts=None):
    """`UTC+8` / `UTC+5:30` / `UTC`：从本地时区偏移算，不写死。"""
    return _label(_local(ts))


def now_line():
    """报告顶部的锚点行。Agent 拿到它才能判断「今晚八点」是过去还是未来。"""
    moment = _local()
    return f"现在 {moment.strftime(FORMAT_STAMP)} ({_label(moment)})"


def seconds_left(deadline, now=None):
    """离截止时刻还有多少秒（不小于 0）；截止时刻非法时返回 None。"""
    try:
        left = float(deadline) - (time.time() if now is None else float(now))
    except (TypeError, ValueError):
        return None
    return max(0, int(left))


def span(seconds):
    """时长的人话：`37 秒` / `14 分 37 秒` / `3 小时 12 分`。

    从 `wg_listener_state._span()` 搬上来的唯一实现（issue #148）：缺口账本要报
    「账本距今多久」，监听账本报「空窗多长」，两处必须是同一套刻度，否则同一个
    时长在两个入口读起来不一样。负数按 0 处理（时钟回拨不该报出「-3 秒」）。
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {rest} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def ago(ts, now=None):
    """`3 小时 12 分前`：某个时刻距今多久（issue #148）。

    与 `span()` 同一刻度。取不到时刻（None / 非数值）时返回空串，调用方据此
    不加这句话——说不出「多久前」比说一句假的好（与 #112 同口径）。
    """
    try:
        moment = time.time() if now is None else float(now)
        return f"{span(moment - float(ts))}前"
    except (TypeError, ValueError):
        return ""


def window(start, end):
    """一段窗口：`07:21:40–09:30:26`；**跨天**时两端补 `MM-DD`。

    跨天必须补日期（issue #148）：早上拉起的停机窗口是 `10-26 23:50–10-27 07:21`，
    只写时分秒的话下午读起来像「今天早上七点漏的」，而它其实是前一夜。同一天则
    不补——`07:21:40–09:30:26` 已经够读，多加日期只是噪声。
    `start` / `end` 任一取不到时返回空串（调用方不加窗口）。
    """
    try:
        begin = _local(float(start))
        finish = _local(float(end))
    except (TypeError, ValueError, OSError):
        return ""
    if begin.date() == finish.date():
        return f"{begin.strftime(FORMAT_CLOCK)}–{finish.strftime(FORMAT_CLOCK)}"
    return (f"{begin.strftime(FORMAT_MONTH_DAY)} {begin.strftime(FORMAT_CLOCK)}"
            f"–{finish.strftime(FORMAT_MONTH_DAY)} {finish.strftime(FORMAT_CLOCK)}")
