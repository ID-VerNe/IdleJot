"""复读 / 接龙插件：群友自发复读时跟一句（issue #155）。

事件型插座（``Hooks.on_event``），与追问窗口同一类——没有 ``#`` 指令，宿主在
通用流程点派发命名事件。判定与安全闸门在 ``repeater.py``（「文件不搬迁」，见
``base`` 契约第 2 条）；本文件只做三件事：三态门控、配置归一、状态行。

事件契约（改名只改这一处，宿主照派）：

* ``group_message`` ``{settings, event, self_ids}`` → 要跟的文本或 None。
  ``commands._serve`` 在 ``should_handle`` **之前**派发：``#`` 指令守护只认前缀，
  而这里要看的是全部群消息。**受三态门控（按群）**：停用 = 不跟、不记账。

``needs_event_loop=True``：``slash_commands.enabled=false`` 时整个守护不启动
（``commands.py:622``），复读会跟着一起死且一个字都不说。声明这一位之后，
``commands.event_stream_needed()`` 会为了本插件把 WS 事件流拉起来——它是目前
唯一需要全量群事件的插件（追问窗口由 reply/watch/up 三个一次性 CLI 派发，
不依赖这个进程）。

``default_enabled=False``：会主动发言的功能必须显式开。与 sixty/busboss 同一
口径——「合入即静默生效」在群里表现为「机器人突然开始复读」，那是事故不是功能。
"""
from __future__ import annotations

from typing import Any, Mapping

import repeater

from plugins.base import Hooks, Plugin


def _on_event(event: str, ctx: dict[str, Any]):
    if event != "group_message":
        return None
    settings = ctx.get("settings") or {}
    message = ctx.get("event") or {}
    # 三态门控按群：停用的群不跟、也不记账（窗口不进 = 不攒阈值）。
    if not PLUGIN.enabled(settings, str(message.get("group_id") or "")):
        return None
    return repeater.consider(
        message,
        settings,
        PLUGIN.options(settings),
        ctx.get("self_ids") or set(),
    )


def _status(settings: Mapping) -> str:
    """``--status`` 插件行的附加说明。

    过三态门控：插件行说「停用」而这里报「累计跟读 N 次」是自相矛盾（与追问行
    同一口径）。没跟过时返回空串，行里不加字——排除型对照，不拿「0 次」占地方。
    """
    if not PLUGIN.enabled(settings):
        return ""
    return repeater.status_text()


def _normalize(options: Mapping) -> dict:
    return repeater.normalize(options)


class _RepeaterPlugin(Plugin):
    def normalize(self, options: Mapping) -> dict:
        return _normalize(options)


PLUGIN = _RepeaterPlugin(
    name="repeater",
    version="1.0",
    hooks=Hooks(on_event=_on_event, status=_status),
    default_enabled=False,
    needs_event_loop=True,
    options_defaults=dict(repeater.DEFAULTS),
)
