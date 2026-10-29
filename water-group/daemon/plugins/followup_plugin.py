"""追问窗口插件：回复之后继续听群里说什么，没人接话就收回（issue #4 PD2a）。

它不是 ``#`` 指令——是指令之外的第一个事件型能力，所以挂 ``Hooks.on_event``：
reply / watch / up 三个宿主在通用流程点派发命名事件，插件自己决定接不接、
要不要过三态门控。窗口机件一行不改，仍在 ``followup.py``（「插件化改的是
注册方式，不是目录结构」，见 ``base`` 契约第一条）。

事件契约（改名只改这一处，宿主照派）：

* ``reply_sent``  ``{settings, group_id, event_id, after_ref, not_before}`` → 窗口 state。
  回复发出后开窗。**受三态门控（按群）**：停用 = 不开窗——与旧的全局
  ``followup.enabled`` 等价，外加 groups 白名单与群级关闭。
  ``after_ref`` 是**宿主说的**窗口游标（"从这条之后看"），它归 host 定命名空间
  （``q<n>`` 指会话库那条，裸数字仍是事件流那条，见 ``wg_event_ref``，issue #203）；
  插件原样交给 ``followup.open(after_ref=...)``，自己不解释——不认识这条消息是
  哪套编号，就不该替它猜。
* ``event_lookup`` ``{event_id}`` → ``(group_id, onebot_message_id)`` 或 None。
  wg_events 里没有的事件（追问唤醒的闲聊）回库拿编号，引用才挂得上。
  ``event_id`` 必须是**库记号**（``q<n>`` / ``qid:<n>``）；带别的写法返回 None，
  不再 ``int()`` 硬转——撞号时把 ``id:7`` 当库 rowid 查就是 #203 本体。
  **不受门控**：它是回复链路的 DB 兜底，不是窗口行为——关了窗口也要能回
  ``--context`` 里看到的闲聊（与改前行为一致）。
* ``active_group`` ``{}`` → 群号或 ``""``。一条唤醒都没有时回库问当前会话
  在哪个群。同样不受门控，理由同上。
* ``wake_scan``   ``{settings}`` → ``{"closed": 原因}`` /
  ``{"woke": 行, "report": 文案}`` / None。watch.py 每轮顺带查窗口。
  **受三态门控**：停用 = 窗口不唤醒。改前 ``enabled:false`` 只挡开窗、
  已开的窗继续唤醒到到期；「停用即降级」补上这个洞，是本节点唯一的行为
  变化（PR 影响面里显式列出）。门控用全局态（``group_id=""``）：窗口能存在
  就因为开窗时按群放过；配置中途改白名单、已开窗继续醒到期这个边角不额外
  处理——比旧的全局开关已经更严。
* ``window_status`` ``{settings}`` → 追问行文案或 None。``up.py --status``
  用。同样过门控——插件行说「停用」而追问行说「进行中」是自相矛盾。

配置：``max_messages`` / ``idle_seconds`` 从归一后的 options 显式传进
``followup.open()``；旧版顶层 ``followup`` 段经 ``Plugin.legacy_section``
平铺进 options（``commands._plugins_from``），老配置逐字段等效。
"""
from __future__ import annotations

from typing import Any

import followup
import wg_event_ref

from plugins.base import Hooks, Plugin


def _on_event(event: str, ctx: dict[str, Any]):
    if event == "reply_sent":
        return _reply_sent(ctx)
    if event == "event_lookup":
        # DB 兜底，不过门控：见模块 docstring。
        return _inbox_lookup(ctx.get("event_id"))
    if event == "active_group":
        return followup.latest_group_id()
    if event == "wake_scan":
        return _wake_scan(ctx)
    if event == "window_status":
        return _window_status(ctx)
    return None


def _reply_sent(ctx: dict[str, Any]):
    settings = ctx.get("settings") or {}
    if not PLUGIN.enabled(settings, str(ctx.get("group_id") or "")):
        return None
    options = PLUGIN.options(settings)
    # 游标由宿主按它自己的命名空间给（issue #203）；没给就用 event_id——
    # 那是改前的唯一来源，直接调本事件的旧调用方（夹具）仍然能开窗。
    after_ref = ctx.get("after_ref")
    if after_ref is None:
        after_ref = ctx.get("event_id")
    return followup.open(
        None,
        after_ref=after_ref,
        max_messages=options.get("max_messages"),
        idle_seconds=options.get("idle_seconds"),
        not_before=ctx.get("not_before"),
    )


def _inbox_lookup(ref):
    """库兜底只认库记号（``q<n>`` / ``qid:<n>``）；别的一律 None（issue #203）。"""
    try:
        namespace, number = wg_event_ref.parse(ref)
    except wg_event_ref.BadRef:
        return None
    if namespace != wg_event_ref.INBOX:
        return None
    return followup.lookup(number)


def _wake_scan(ctx: dict[str, Any]):
    if not PLUGIN.enabled(ctx.get("settings") or {}):
        return None
    window = followup.load()
    if not window:
        return None
    reason = followup.stale(window)
    if reason:
        followup.close()
        return {"closed": reason}
    woke = followup.consume(
        window,
        followup.since(window.get("after_id"), window.get("session"),
                       window.get("not_before")),
    )
    if not woke:
        return None
    return {"woke": woke, "report": followup.wake_report(window, len(woke))}


def _window_status(ctx: dict[str, Any]):
    if not PLUGIN.enabled(ctx.get("settings") or {}):
        return None
    return followup.status_text()


PLUGIN = Plugin(
    name="followup",
    version="1.0",
    hooks=Hooks(on_event=_on_event),
    default_enabled=True,
    legacy_section="followup",
)
