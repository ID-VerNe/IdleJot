"""``group_ban`` 事实的只读渲染（issue #390）——``reply.py --list`` 的事实行。

事实由 app 侧写入：只有连着 OneBot 事件 WS 的 server_wg 收得到 notice（落盘在
``chatlocal/mcp_chat_ban.py``，schema 契约写在那边的 docstring），daemon 进程不连
事件 WS、写不了。这边只回答三件事：

* 本群最近一次禁言 / 解禁是谁操作的（``operator_id``）、何时、禁了多久；
* 禁言到期时刻到没到——到期只说「已到点」，解禁与否的实测归账本那条线
  （``wg_send_failure.probe()``，issue #145 / #192）；
* 本会话钉死群时只看本群，未钉死取全库最新一条并带 ``group=`` 标注——与
  ``wg_send_failure.print_last()`` 的全局口径一致，事实行不比失败行更神秘。

时刻一律 ``wg_clock.stamp()`` 全日期格式：#392 的教训是只报时分秒会让人把
「明天 18:25」读成「今天 18:25」。

明确不做：与 ``wg_send_failure.json`` 互写（事件到达即清账本 + 立刻 WAKE 是
issue #391）；operator 花名解析（``reply.py --who`` 已有画像卡，Agent 想认人自己查）。
"""
import json
import time

import wg_clock
from wg_paths import state_dir

FACTS = state_dir() / "wg_group_ban.json"


def read():
    """整份事实（groups 映射）；文件不存在 / 读坏返回空 dict，不冒充有事实。"""
    try:
        data = json.loads(FACTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    groups = data.get("groups") if isinstance(data, dict) else None
    return groups if isinstance(groups, dict) else {}


def _latest(groups, group_id=None):
    """``(gid, entry, fact)``：最近一条事实所在群、该群整份事实与那条事实。

    同一秒里 ban 与 lift 并存时 lift 胜（lift 是更靠后的状态）；``group_id``
    钉死时只在本群挑，别的群的禁言不是本群的事实。
    """
    if group_id:
        pairs = [(str(group_id), groups.get(str(group_id)) or {})]
    else:
        pairs = [(str(gid), entry or {}) for gid, entry in groups.items()]
    best = None
    for gid, entry in pairs:
        for key in ("lift_ban", "ban"):
            fact = entry.get(key)
            if not isinstance(fact, dict):
                continue
            try:
                at = float(fact.get("at"))
            except (TypeError, ValueError):
                continue
            if best is None or at > best[0]:
                best = (at, gid, entry, fact)
    return None if best is None else (best[1], best[2], best[3])


def line(group_id=None, now=None):
    """事实行的单行描述；没有事实返回空串（调用方据此一行不出声）。"""
    moment = time.time() if now is None else float(now)
    found = _latest(read(), group_id)
    if not found:
        return ""
    gid, entry, fact = found
    at = wg_clock.stamp(fact.get("at"))
    operator = str(fact.get("operator_id") or "?")
    if fact.get("action") == "lift_ban":
        text = f"# 解禁事件：{at} group={gid} 被 QQ {operator} 解禁"
        ban = entry.get("ban")
        if isinstance(ban, dict) and isinstance(ban.get("at"), (int, float)) \
                and isinstance(ban.get("operator_id"), str) and ban["at"] <= (fact.get("at") or 0):
            text += (f"（此前 {wg_clock.stamp(ban['at'])} 被 QQ {ban['operator_id']}"
                     f" 禁言 {int(ban.get('duration') or 0)} 秒）")
        return text
    duration = int(fact.get("duration") or 0)
    until = int(fact.get("at") or 0) + duration
    # user_id == 0 是全员禁言（NapCat 常常不给成员写 shut_up_timestamp）；
    # 其余（user_id == 本机）就是本机被单独禁言。
    scope = "设全员禁言" if str(fact.get("user_id")) == "0" else "禁言"
    if until > moment:
        tail = f"（至 {wg_clock.stamp(until)}，还剩 {int(until - moment)} 秒）"
    else:
        tail = f"（至 {wg_clock.stamp(until)}，已到点；解禁与否以上方账本行为准）"
    return f"# 禁言事件：{at} group={gid} 被 QQ {operator} {scope} {duration} 秒{tail}"


def print_line(group_id=None):
    """``reply.py --list`` 抬头的一行：有事实才出声（与 ``print_last`` 同纪律）。"""
    text = line(group_id=group_id)
    if text:
        print(text)
