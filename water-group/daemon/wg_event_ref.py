"""一条消息的记号：两件事各归一处——**它指哪套编号**与**它指向哪条消息**。

## 一、``id=<n>`` 与 ``qid=<n>`` 是两个命名空间（issue #203）

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
``7``         事件流事件（**与改前一致**）          ``wg_events``；未命中**且库中
                                                   唯一命中**时回库，并标明来源
``id=7``     行首原样：唤醒行 / ``--list`` 打的      同 ``7``
``id:7``      同上（冒号是 #203 起的内部写法）      同 ``7``
``q7``        会话库消息的简写                    只查 ``chat_inbox``
``id=q7``     行首原样：追问行 / ``--show`` 首行打的 同 ``q7``
``qid=7``     状态行 / 拒绝信息里打的写法          同 ``q7``
``qid:7``     同上（冒号写法）                     同 ``q7``
===========  ==================================  ================================

裸数字仍然是事件流 id，这是**故意**的：唤醒行在改前就是 ``id=<n>``，而 Agent 抄的
绝大多数编号来自唤醒行；把它悄悄改成「两套里任意一套」就等于把 #164 的护栏从
「挡住跨群」变成「挡错对象」（#203 的 P0 判级）。撞号时裸数字只认事件流，真要看
库那条就写 ``q7``——记号上写得出区别，才谈得上不猜。

**往返契约（issue #204）**：输出侧打进行首的记号，原样抄回 ``parse()`` 必须认，
而且落进打它的那一套编号——``id=7`` 是事件流那条、``id=q7`` 是会话库那条、
``qid=7`` 是会话库那条。#204 的缺口就在这一层：形态早就能分辨（#203），但
``parse()`` 只认冒号写法，于是唤醒行的 ``id=7``、追问行的 ``id=q7``、状态行的
``qid=7`` 抄回来全是 ``BadRef``，而报错文案自己宣传的正是 ``id=<n>`` / ``qid=<n>``
这套等号写法——文案与解析器互相打架，读者只能靠手工剥前缀，剥错一次就拒。
门禁 ``scripts/check_id_contract.py`` 钉住这条往返。

## 二、按事件 id 取一条消息也只有一个入口（issue #216）

改前「按 id 找一条消息」这件事在四个地方各写了一份，来源顺序与认的字段名都不同：

| 入口 | 只查 | 认的字段 |
|---|---|---|
| `reply.event_target` | 先 `wg_events`，再 `followup.lookup` | payload 的 `conversation_id` + `onebot_message_id` |
| `followup._group_of` | 只有 payload | 同上，缺一个就 `None` |
| `wg_text.show_event` | 先 `wg_events`，再 `chat_inbox` | 只按行取，不解析群号 |
| `look.event_by_id` | 只有 `wg_events` | — |

于是同一个 id 会得到两种结论：`--context` / `--show` 读得出全文（它们只认「行」），
`reply.py <id>` 却回 `None` 并抛 `CrossGroup`「查不到所属群」——读者会以为这条
消息真的不属于本群（issue #216 的现场）。

**为什么「字段名兼容」必须在这一层**：`payload` 是入库时的自由 JSON，群号与消息
编号在库的**列**上本来就有（`chat_sessions.conversation_id`、`chat_inbox.message_id`）。
解析方只认 payload 里的两个键名，等于把「写方永不变」当成契约：payload 少一个键
（旧行、别的写入方、被裁过的行）就静默 `None`，而这一行完全读得出来。

## 三、两者怎么配合

``parse()`` 只负责**语法**（记号 → ``(namespace, 数字)``），``resolve()`` /
``resolve_id()`` 是「按裸 id 找一条消息」的单点入口（返回 :class:`Ref`，
`wg_events` 优先、`chat_inbox` 兜底——兜底时 ``Ref.namespace`` 会如实说是
会话库；``resolve`` 是上游兼容名，见函数 docstring），``format_ref()`` 负责
**渲染**（怎么打进行里给 Agent 抄）。

``resolve_id()`` 与 ``resolve()`` 的口径必须一致，这是本模块唯一容易走岔的地方：
两条路径都得「裸数字先认事件流，事件流没有才回库」，否则撞号时同一个 id 会在
``reply.py 7`` 与 ``followup.lookup(7)`` 之间给出两个答案——那正是 #203。

``parse()`` 与渲染侧同样只许有一份口径（issue #204）：``format_ref()`` 打出去的、
各渲染点拼上 ``id=`` 打出去的，都必须能原样回到 ``parse()``。写法表、拒绝文案
（``HINT``）与门禁 ``check_id_contract.py`` 三者钉的是同一件事——**输出即输入**。

三者分开，是因为渲染风格会变（哪天 ``qid=`` 要改名），而查找口径不该跟着变。

本模块只做解析，不做发送、不做隔离判断、不渲染——`reply` / `followup` / `wg_text` /
`look` 都调它，来源顺序与字段兼容只在这里维护一份。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from wg_paths import data_dir, state_dir

# --- 一、命名空间（issue #203） -------------------------------------------

# 命名空间：事件流（wg_events.jsonl）与会话库（chat_inbox）是两套编号。
EVENT = "event"
INBOX = "inbox"
NAMESPACES = (EVENT, INBOX)

# 显式前缀的分隔符 ``:`` 与 ``=`` 都认：``id:7`` / ``id=7`` / ``qid:7`` / ``qid=7``。
# ``=`` 是输出行首的形态（``watch.py`` / ``up.py --list`` / ``followup.py`` 状态行
# 就是这么打的），``parse()`` 不认它，行首记号就抄不回来（issue #204）。
_EXPLICIT = re.compile(r"^(id|qid)[=:](\d+)$")
# 行首整段照抄的形态：追问行 / ``--show`` 首行打的是 ``id=`` + ``format_ref(INBOX, n)``
# 即 ``id=q7``。``qid:`` / ``qid=`` 不许再挂 ``q``（``qid=q7`` 不是任何一行的输出）。
_LINE_INBOX = re.compile(r"^id=q(\d+)$")
_SHORT = re.compile(r"^q(\d+)$")
_BARE = re.compile(r"^\d+$")

# 拒绝信息里列的写法（issue #204）：文案与解析器必须逐字一致——列出来的每一种
# 都得真的能 ``parse()``，否则报错本身就在教人写一个跑不通的记号。这条文案由
# ``check_id_contract.py`` 拆开逐个解析验证，改文案等于同步改契约。
HINT = "需要 <n>、q<n>、id=<n>、id=q<n> 或 qid=<n> 之一"


class BadRef(ValueError):
    """记号长得不像任何一种合法写法。调用方据此拒绝，不猜。"""


def parse(ref):
    """记号 -> ``(namespace, int)``。非法记号抛 :class:`BadRef`。

    接受的写法见模块 docstring 的表（含 ``=`` 与 ``:`` 两种分隔符，以及行首整段
    ``id=q<n>``，issue #204 的往返契约）。空白先剥：Agent 从行里抄编号时经常带个
    空格或换行，那不是另一个记号。
    """
    text = str(ref if ref is not None else "").strip()
    if not text:
        raise BadRef(f"空记号：{HINT}")
    matched = _LINE_INBOX.match(text)
    if matched:
        return INBOX, int(matched.group(1))
    matched = _EXPLICIT.match(text)
    if matched:
        return (EVENT if matched.group(1) == "id" else INBOX), int(matched.group(2))
    matched = _SHORT.match(text)
    if matched:
        return INBOX, int(matched.group(1))
    if _BARE.match(text):
        return EVENT, int(text)
    raise BadRef(f"看不懂的记号 {text!r}：{HINT}")


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


def resolve(ref):
    """按裸 id 解析一条事件，返回 :class:`Ref`；两个来源都没有返回 ``None``。

    这是 `resolve_id` 的**上游兼容名**（issue #216 曾叫 ``resolve``，单参数返回
    ``Ref``）。`resolve_id` 加了命名空间参数（issue #203），解析口径完全一致：
    ``wg_events`` 优先、``chat_inbox`` 兜底。撞号时裸数字只认事件流，事件流没有
    才回库——两个名字给出同一个答案，调用方随便用哪个。

    行在但群号取不到时仍返回 ``Ref``（``resolvable`` 为假），让调用方能把
    「不存在」与「存在但解析不出群」分开说——这正是 issue #216 修复候选 2。
    """
    return resolve_id(ref)


# --- 二、按事件 id 取一条消息（issue #216） -------------------------------

EVENTS = state_dir() / "wg_events.jsonl"
DB = Path(os.environ.get("IDLEJOT_WG_DB")
          or os.environ.get("TULPA_WG_DB", data_dir() / "mcp-access.sqlite3"))

# 消息编号在 payload 里的两种键名：标准入库写 `onebot_message_id`，图片索引那一路
# 与旧行用 `message_id`。两者都是「QQ 消息编号」，不是本地行号。
MESSAGE_ID_KEYS = ("onebot_message_id", "message_id")

SRC_EVENTS = "wg_events"
SRC_INBOX = "chat_inbox"
# 插件兜底（`reply.event_ref` 在解析器两个来源都没有时才问 `event_lookup`）：
# 来源写成 plugin，免得 `src=wg_events` 这种标记把插件答案冒充成事件流答案。
SRC_PLUGIN = "plugin"


@dataclass(frozen=True)
class Ref:
    """一条事件的身份：哪个来源、哪个群、哪条 QQ 消息。

    `group_id` 为空 = 这条行在，但取不到群号（payload 没有 `conversation_id`，
    `chat_sessions` 也查不到）；`message_id` 为空 = 群号有、编号没有（能回，
    只是挂不上引用）。两者分开表达，调用方不必再从 `None` 里猜是哪一种。

    issue #203：`source` 同时决定**这是哪套编号**——`wg_events` 就是事件流
    （`Ref.namespace == EVENT`），`chat_inbox` 就是会话库（`INBOX`）。
    """
    event_id: int
    source: str
    conversation_id: str = ""
    group_id: str = ""
    message_id: str = ""
    payload: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def namespace(self) -> str:
        """这套编号属于哪个命名空间（issue #203）。"""
        return INBOX if self.source in (SRC_INBOX, SRC_PLUGIN) else EVENT

    @property
    def resolvable(self) -> bool:
        """群号拿得到吗——`reply.py` 能不能发到正确的群，全看这一位。"""
        return bool(self.group_id)

    @property
    def target(self):
        """`(群号, 消息编号)`；群号都没有时返回 `None`。编号可能为空串。"""
        return (self.group_id, self.message_id) if self.group_id else None

    def describe(self) -> str:
        """来源标记：`src=wg_events` / `src=chat_inbox`（issue #204 的同形契约）。"""
        return f"src={self.source}"


def group_of(conversation_id) -> str:
    """``123456:group:789`` -> ``789``；不是群会话返回空串。

    与 `wg_isolation.group_of` 同一口径；这里不 import 它，是为了让本模块能被
    任何入口轻量导入（wg_isolation 反过来要用本模块的 `explain()`）。
    """
    parts = str(conversation_id or "").split(":")
    return parts[2] if len(parts) == 3 and parts[1] == "group" else ""


def message_id_of(payload) -> str:
    """payload 里的 QQ 消息编号：两种键名都认，取第一个非空。"""
    if not isinstance(payload, dict):
        return ""
    for key in MESSAGE_ID_KEYS:
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def group_of_payload(payload, message_id=None):
    """只有 payload 时能解析出的 ``(群号, 编号)``；缺一不可，否则 `None`。

    与 `resolve_id()` 的区别只在于「没有行可回查」：这里没有 `session_id`，回不到
    `chat_sessions`。有行的时候一律走 `resolve_id()`。
    """
    if not isinstance(payload, dict):
        return None
    gid = group_of(payload.get("conversation_id"))
    mid = message_id_of(payload) or str(message_id or "")
    return (gid, mid) if gid and mid else None


def events():
    """`wg_events.jsonl` 的全部行（坏行跳过）；文件不在就返回空表。"""
    rows = []
    try:
        for line in EVENTS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return rows


def _connect():
    """只读连接：`chatlocal.sqlite_util.connect_ro` 的延迟导入门面。

    与 `wg_text.connect_ro` 同一范式（daemon 脚本直接跑时 `sys.path[0]` 是
    daemon 目录，仓库根不在路径上），本模块不反向 import `wg_text`——那会成环。
    """
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from chatlocal.sqlite_util import connect_ro
    return connect_ro(DB, timeout=5)


def _row_as_dict(db, sql, params):
    """`SELECT *` 的一行转 dict：列名从 cursor 取，缺列（老库/夹具）自动缺席。"""
    cursor = db.execute(sql, params)
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(zip([d[0] for d in cursor.description], row))


def _session_conversation(db, session_id) -> str:
    """会话行上的 `conversation_id`；表不存在或没这行都返回空串。"""
    if not session_id:
        return ""
    try:
        row = db.execute("SELECT conversation_id FROM chat_sessions WHERE id=?",
                         (session_id,)).fetchone()
    except sqlite3.Error:
        return ""
    return str(row[0] or "") if row else ""


def payload_conversation(payload) -> str:
    """payload 自己的 `conversation_id`；不是 dict、缺键、空串都返回空串。

    这是**唯一**读 payload 这个键的地方。批量读（`followup.since` /
    `wg_context.recent_chatter` / `wg_gap._local_state`）以前各写一份
    `data.get("conversation_id")`，缺键就静默读成空（issue #219）。
    """
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("conversation_id") or "")


def row_conversation(row) -> str:
    """一条 `chat_inbox` 行的会话号：payload 优先，会话行兜底（issue #216 / #219）。

    名字里带 `row`：`wg_isolation.conversation_of(group_id)` 是反方向的「群号 →
    `<账号>:group:<群号>`」，两个方向同名会让调用点读不出自己在用哪一个。

    `row` 是 dict：`payload`（已解成 dict 或原样的 JSON 串）与 `conversation_id`
    ——后者是**会话行上的那一列**，由调用方的 SELECT（LEFT JOIN `chat_sessions`）
    或 `_row_as_dict` 一并取回；没带这一列时按「没有」处理，绝不回头再查一次库
    （批量读会变成 N+1）。

    这是**批量读**与**单条解析**的唯一出口：`followup.since()` /
    `wg_context.recent_chatter()` / `wg_gap._local_state()` 与 `_ref_from_inbox()`
    都走这里，别在调用点再写一次 `payload_conversation(payload) or row[...]`。

    payload 少这个键时两条路都退成空串：写方 `chatlocal/mcp_chat.py` 现在**是**
    写这个键的，但兜底不能建立在「写方永不变」上——旧行、被裁过的行、别的
    写入方都读得出来（issue #219：追问行与 `--context` 行按跨群主档挂别名指向）。
    """
    if not isinstance(row, dict):
        return ""
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = None
    return (payload_conversation(payload)
            or str(row.get("conversation_id") or ""))


def _ref_from_events(event_id):
    for row in events():
        if row.get("id") != event_id:
            continue
        conversation = str(row.get("conversation_id") or "")
        return Ref(event_id=event_id, source=SRC_EVENTS,
                   conversation_id=conversation, group_id=group_of(conversation),
                   message_id=message_id_of(row), payload=row)
    return None


def _ref_from_inbox(event_id):
    try:
        db = _connect()
    except sqlite3.Error:
        return None
    try:
        row = _row_as_dict(db, "SELECT * FROM chat_inbox WHERE id=?", (int(event_id),))
        if row is None:
            return None
        try:
            payload = json.loads(row.get("payload") or "")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        # 群号优先 payload（追问窗口与别名渲染都按它取），其次会话行——payload
        # 少这个键的行走的正是这一步（issue #216 的复现形态）。口径与批量读
        # （`followup.since` / `wg_context.recent_chatter`）同一份，见
        # `row_conversation()`（issue #219）。
        conversation = row_conversation(
            {"payload": payload,
             "conversation_id": _session_conversation(db, row.get("session_id"))})
        # 编号同理：payload 两种键名 → `chat_inbox.message_id` 列。
        message = message_id_of(payload) or str(row.get("message_id") or "")
        return Ref(event_id=int(event_id), source=SRC_INBOX,
                   conversation_id=conversation, group_id=group_of(conversation),
                   message_id=message, payload=payload)
    except (sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        db.close()


def inbox_session_of(event_id):
    """这条库行是**哪个会话**收下来的；没有或读不到返回空串（issue #203）。

    与群号校验并列的第二道：``chat_inbox`` 是跨会话的全局表，光有群号分不出
    「本会话在群里看到的那条」与「另一个会话在同一个群里的另一条」。
    """
    try:
        db = _connect()
    except sqlite3.Error:
        return ""
    try:
        row = db.execute("SELECT session_id FROM chat_inbox WHERE id=?",
                         (int(event_id),)).fetchone()
    except (sqlite3.Error, TypeError, ValueError):
        return ""
    finally:
        db.close()
    return str(row[0] or "") if row else ""


def resolve_id(event_id, namespace=None):
    """按 id 解析一条事件：`wg_events` 优先、`chat_inbox` 兜底；都没有返回 `None`。

    `namespace` 给定时只查那一处（issue #203 的显式写法），不做回退。

    顺序与 :func:`resolve` 的裸数字口径**完全相同**（issue #203）：事件流里有就
    是事件流那条，事件流里没有才回库——撞号时两个入口给出同一个答案。库兜底命中
    时 `Ref.source` 会说 `chat_inbox`，调用方据此在输出里标明来源。

    行在但群号取不到时仍返回 `Ref`（`resolvable` 为假），让调用方能把「不存在」
    与「存在但解析不出群」分开说——这正是 issue #216 修复候选 2。
    """
    try:
        eid = int(event_id)
    except (TypeError, ValueError):
        return None
    if namespace == INBOX:
        return _ref_from_inbox(eid)
    if namespace == EVENT:
        return _ref_from_events(eid)
    return _ref_from_events(eid) or _ref_from_inbox(eid)


def explain(event_id) -> str:
    """`target_group()` 拒绝信息里的那半句：两种「查不到」分开说。

    改前两者都是「查不到所属群」，读者分不清是 id 写错了、被 1000 条挤掉了，
    还是行在、只是群号解析不出来（issue #216 影响面第一条）。
    """
    ref = resolve_id(event_id)
    if ref is None:
        return (f"不在 {SRC_EVENTS} 也不在 {SRC_INBOX}"
                f"（可能已被 {SRC_INBOX} 的 1000 条上限挤掉，或这个 id 从未存在）")
    if not ref.group_id:
        return (f"在 {ref.source} 里，但取不到群号"
                f"（payload 没有 conversation_id，chat_sessions 也查不到这条的会话）")
    return f"在 {ref.source} 里属于群 {ref.group_id}"
