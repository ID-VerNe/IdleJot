"""一条消息的记号：``id=<n>`` 与 ``qid=<n>`` 是两个命名空间（issue #203）。

issue #203 的翻车形态：追问行与唤醒行打同一种 ``id=<数字>``，而这两套数字来自
**两个各自独立分配的编号**——唤醒行是 ``wg_events.jsonl`` 的 ``id``，追问行是
``chat_inbox`` 的 AUTOINCREMENT 全局 rowid（``followup.py`` 的模块 docstring 明说
它是全局 rowid，跨会话唯一，但与会话内的 ``wg_events`` 编号毫无关系）。撞号是常态：
``event_target(7)`` 拿到群 111 的 ``message_id=7007``，``lookup(7)`` 拿到群 222 的
``9999``，于是回复正文发进正确的群，引用气泡却指向另一个群那条消息；钉死群的会话
还会在 ``target_group()`` 上被拒，理由与眼前那一行对不上。

改前每一处入口各自 ``int()`` 之后再各写一套「先查 wg_events、查不到回库兜」的
回退链，撞号时每一处都**静默二选一**。本模块把「这个记号指的是哪套编号」收成
唯一归属，回退链一起收掉：

===========  ==================================  ================================
写法          含义                                查找范围
===========  ==================================  ================================
``id:7``      事件流事件（唤醒行 / ``--list``）    只查 ``wg_events.jsonl``
``qid:7``     会话库消息（追问行 / ``--context``） 只查 ``chat_inbox``
``7``         事件流事件（**与改前一致**）          ``wg_events``；未命中**且库中
                                                   唯一命中**时回库，并标明来源
``q7``        会话库消息的简写                    只查 ``chat_inbox``
===========  ==================================  ================================

裸数字仍然是事件流 id，这是**故意**的：唤醒行在改前就是 ``id=<n>``，而 Agent 抄的
绝大多数编号来自唤醒行；把它悄悄改成「两套里任意一套」就等于把 #164 的护栏从
「挡住跨群」变成「挡错对象」（#203 的 P0 判级）。撞号时裸数字只认事件流，真要看
库那条就写 ``q7``——记号上写得出区别，才谈得上不猜。

``parse()`` 只负责**语法**（记号 → ``(namespace, 数字)``），``resolve()`` 负责
**语义**（去哪个 store 取），``format_ref()`` 负责**渲染**（怎么打进行里给 Agent
抄）。三者分开，是因为渲染风格会变（哪天 ``qid=`` 要改名），而查找口径不该跟着变。
"""
from __future__ import annotations

import re

# 命名空间：事件流（wg_events.jsonl）与会话库（chat_inbox）是两套编号。
EVENT = "event"
INBOX = "inbox"
NAMESPACES = (EVENT, INBOX)

# 显式前缀写在前面，简写 ``q7`` 写在后面：``id:7`` / ``qid:7`` / ``7`` / ``q7``。
# 只认小写——``Q7`` 是打错，不是另一种写法；报错比猜好。
_EXPLICIT = re.compile(r"^(id|qid):(\d+)$")
_SHORT = re.compile(r"^q(\d+)$")
_BARE = re.compile(r"^\d+$")


class BadRef(ValueError):
    """记号长得不像任何一种合法写法。调用方据此拒绝，不猜。"""


def parse(ref):
    """记号 -> ``(namespace, int)``。非法记号抛 :class:`BadRef`。

    接受的写法见模块 docstring 的表。空白先剥：Agent 从行里抄编号时经常带个空格
    或换行，那不是另一个记号。
    """
    text = str(ref if ref is not None else "").strip()
    if not text:
        raise BadRef("空记号：需要 id=<n>、qid=<n>、<n> 或 q<n> 之一")
    matched = _EXPLICIT.match(text)
    if matched:
        return (EVENT if matched.group(1) == "id" else INBOX), int(matched.group(2))
    matched = _SHORT.match(text)
    if matched:
        return INBOX, int(matched.group(1))
    if _BARE.match(text):
        return EVENT, int(text)
    raise BadRef(f"看不懂的记号 {text!r}：需要 id=<n>、qid=<n>、<n> 或 q<n> 之一")


def format_ref(namespace, number):
    """``(namespace, int)`` -> 打进行里的写法。``event`` 仍是裸数字。

    唤醒行沿用 ``id=<n>`` 的历史形态（那是一整排 RSS 一样的行，加前缀只会挤掉
    正文额度）；追问行与上下文行打 ``q<n>``，短，且与裸数字在字面上就能分开。
    两条都不带 ``<...>`` 括号——Agent 是直接抄这一段去跑的。
    """
    number = int(number)
    return str(number) if namespace == EVENT else f"q{number}"


def describe(namespace):
    """一行中文，用在拒绝信息里说清「你说的是哪套编号」。"""
    return "事件流（wg_events）" if namespace == EVENT else "会话库（chat_inbox）"


def resolve(ref, events_of, inbox_of):
    """记号 -> ``(namespace, number, found)``；``found`` 原样来自两个取数回调。

    ``events_of(number)`` / ``inbox_of(number)`` 由调用方给：本模块不认识
    ``wg_events.jsonl`` 也不认识 SQLite，只决定「该问谁、要不要回退」。返回的
    ``namespace`` 是**实际命中**的那一套，可能与字面写法不同（裸数字回库兜底），
    调用方据此在输出里说明来源，不许把兜底来的当成事件流那条。

    回退只在**裸数字**上发生，且只在该数字在事件流里**确实没有**时：这就是改前的
    兜底行为（``--context`` 里看到的闲聊能直接回），只是现在它会标明自己是回库
    命中的。显式 ``qid`` 不回退到事件流——写清楚要的就是库那条，替它改主意等于
    再造一次静默二选一。
    """
    namespace, number = parse(ref)
    if namespace == INBOX:
        return INBOX, number, inbox_of(number)
    found = events_of(number)
    if found is not None:
        return EVENT, number, found
    fallback = inbox_of(number)
    if fallback is not None:
        return INBOX, number, fallback
    return EVENT, number, None
