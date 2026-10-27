"""本会话只管一个群：群身份的解析、读侧过滤与发送前归属校验（issue #164）。

多群的设计是「用户开 N 个会话，一个会话只看一个群」（#160）。这个模块是那条设计的
运行时支点，全仓**唯一**回答「我这个会话是哪个群」的地方：

    env IDLEJOT_WG_CONVERSATION / TULPA_WG_CONVERSATION   ← 测试与手工隔离
      > <state>/wg_conversation.txt                        ← 一群一状态目录时的权威
      > config.json 里**唯一**一个 enabled 的群            ← 单群机器零配置
      > 空（未钉死）

**为什么第四档是「空」而不是「取第一个 enabled」**：单群机器上取第一个与唯一一个
是同一件事，两者都只有一个群时没有歧义；多群时取第一个则是**按书写顺序**替用户选
群——B 群的会话会静默锁到 A 群，读侧再把两个群都给它看。这正是 issue #164 的现场。
所以多群且未钉死时宁可返回空：读侧不做隔离（与改前一致，不制造新故障），但
``note()`` 会说清「未钉死、因此没有隔离」，``up.py --status`` 也照实印出来。

**读侧**（``rows_of``）：把事件流裁到本群。``watch.py`` 与 ``reply.py --list`` 共用
这一个裁剪口，不各写一份。
**写侧**（``target_group``）：回复的目标事件必须属于本群，否则抛 ``CrossGroup``。
改前没有这一句，钉死群 111 的会话能把话发进群 222，还把群 222 那条记成「已回」，
另一个会话于是永远不再回它（``doc/MULTI_GROUP_PLAN.md:63`` 的 P0）。

判定全部基于 ``conversation_id`` 的 ``<账号>:group:<群号>`` 形状，与
``wg_aliases.group_of`` 同一口径；这里不复用那个函数，是因为它在
``wg_aliases`` 里（会拖进记忆引擎），而本模块要能被任何 daemon 入口轻量导入。
"""
from __future__ import annotations

import os

import wg_config
from wg_paths import state_dir

# 与 `wg_paths` / `wg_config` 同一族：新名在前，旧名兜底。
CONVERSATION_ENV = ("IDLEJOT_WG_CONVERSATION", "TULPA_WG_CONVERSATION")
CONVERSATION_FILE = "wg_conversation.txt"


class CrossGroup(Exception):
    """目标事件属于别的群：本会话不许回，也不许把它记成已回（issue #164 PB）。"""


def group_of(conversation_id) -> str:
    """``123456:group:789`` -> ``789``；不是群会话返回空串。"""
    parts = str(conversation_id or "").split(":")
    return parts[2] if len(parts) == 3 and parts[1] == "group" else ""


def conversation_of(group_id, account=None) -> str:
    """群号 -> ``<账号>:group:<群号>``；没有账号时返回空串（不猜）。"""
    gid = str(group_id or "").strip()
    if not gid:
        return ""
    account = str(account if account is not None else wg_config.config().get("self_account") or "").strip()
    return f"{account}:group:{gid}" if account else ""


def enabled_groups():
    """config 里显式 enabled 的群号，保持书写顺序。

    公开名（不带下划线）：`session.identity_problem()` 要把它写进拒绝信息里，
    跨模块调私有函数等于把「这是内部细节」的约定当没写。
    """
    groups = wg_config.config().get("groups") or {}
    return [str(gid) for gid, item in groups.items()
            if isinstance(item, dict) and item.get("enabled")]


def resolve():
    """``(conversation_id, source)``。source 说明这一档是哪来的，供状态行照实印。

    source 取值：``env:<变量名>`` / ``file:<路径>`` / ``config:唯一启用群`` /
    ``config:唯一群``（没写 enabled 但只有一个）/ ``""``（未钉死）。
    """
    for key in CONVERSATION_ENV:
        raw = str(os.environ.get(key) or "").strip()
        if raw:
            return raw, f"env:{key}"
    path = state_dir() / CONVERSATION_FILE
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        raw = ""
    if raw:
        return raw, f"file:{path}"
    groups = wg_config.config().get("groups") or {}
    enabled = enabled_groups()
    if len(enabled) == 1:
        return conversation_of(enabled[0]), "config:唯一启用群"
    # 没写 enabled 但整份配置只有一个群：老配置的常见形态，不算歧义。
    if not enabled and len(groups) == 1:
        only = str(next(iter(groups)))
        return conversation_of(only), "config:唯一群"
    return "", ""


def group_id() -> str:
    """本会话的群号；未钉死时返回空串（调用方据此决定要不要过滤）。"""
    return group_of(resolve()[0])


def pinned() -> bool:
    return bool(group_id())


def ambiguous() -> bool:
    """多个群都启用、却没有钉死：读侧不做隔离，回复也无法校验归属。"""
    return not pinned() and len(enabled_groups()) > 1


def describe() -> str:
    """一行状态：本会话钉在哪个群、依据是什么。`up.py --status` 用。"""
    conversation, source = resolve()
    if conversation:
        return f"{conversation}（{source}）"
    if ambiguous():
        groups = "、".join(enabled_groups())
        return (f"未钉死（config 有 {len(enabled_groups())} 个启用群：{groups}——"
                f"读侧不做隔离、回复不做归属校验；给每个会话设 "
                f"IDLEJOT_WG_CONVERSATION=<账号>:group:<群号> 或写 "
                f"{CONVERSATION_FILE}）")
    return "未钉死（config 里没有可用的群；读侧不做隔离）"


def note() -> str:
    """只在该说的话有用时说一句：歧义态告警，其余返回空串。"""
    if not ambiguous():
        return ""
    return (f"WARN: 群身份未钉死（config 有 {len(enabled_groups())} 个启用群），"
            f"本会话会看到所有群的唤醒、也能回任意群——issue #164 的隔离对多群无效。"
            f"用 IDLEJOT_WG_CONVERSATION 或 {CONVERSATION_FILE} 钉死本会话的群")


def belongs(event, group=None) -> bool:
    """这条事件属于本会话的群吗；本会话未钉死时一律放行（改前行为）。"""
    gid = group if group is not None else group_id()
    if not gid:
        return True
    return group_of((event or {}).get("conversation_id")) == gid


def rows_of(rows, group=None):
    """把事件流裁到本会话的群；未钉死时原样返回（不制造新故障）。"""
    gid = group if group is not None else group_id()
    if not gid:
        return list(rows)
    return [e for e in rows if group_of(e.get("conversation_id")) == gid]


def same_group_ids(rows, group=None):
    """本群事件 id 集合；未钉死时返回 ``None``（表示「不做这个判断」）。

    ``plan()`` 的批次轮换重定向只该在本群 id 里挑：改前它直接取 ``allowed[-1]``，
    批次里最新那条属于哪个群就发到哪个群（issue #164 PB）。
    """
    gid = group if group is not None else group_id()
    if not gid:
        return None
    return {e.get("id") for e in rows if group_of(e.get("conversation_id")) == gid}


def filter_report(rows, group=None):
    """过滤把**全部**事件都滤掉时说一句；其余情况返回空串。

    这条防的是过滤自己的静默失效：本会话钉在群 X，而事件流里全是群 Y（换过 QQ 号、
    搬过状态目录、config 的 self_account 与库里对不上），于是「过滤生效」与「群里
    真的没人说话」在输出里长得一模一样——Agent 会一直等一个永远不会来的唤醒。
    只在「有东西但一条都不属于我」时出声，正常空流不吵。
    """
    gid = group if group is not None else group_id()
    if not gid or not rows:
        return ""
    if any(group_of(e.get("conversation_id")) == gid for e in rows):
        return ""
    foreign = sorted({group_of(e.get("conversation_id")) or "（无群号）" for e in rows})
    return (f"WARN: 事件流里 {len(rows)} 条消息没有一条属于本群 {gid}"
            f"（实际出现的群：{'、'.join(foreign)}）——本会话一条唤醒都收不到。"
            f"群号、self_account 与状态目录是否配错？")


def target_group(event_id, found, group=None):
    """回复前的归属校验：返回 ``(group_id, message_id)``，跨群则抛 ``CrossGroup``。

    ``found`` 是 ``reply.event_target`` 的结果（``(群号, message_id)`` 或 None）。
    查不到 ``found`` 的事件（追问窗口里被 mention_only 滤掉的闲聊）只能靠库兜底，
    兜底也没给出群号时**不**放行到「按最近一条猜」那条路——猜出来的群正是跨群误发
    的来源，所以这里要求调用方自己拿 ``--group`` 明示。
    """
    gid = group if group is not None else group_id()
    if not gid:
        return found
    if found:
        if str(found[0]) != gid:
            raise CrossGroup(f"id={event_id} 属于群 {found[0]}，本会话只管群 {gid}"
                             f"——跨群回复会把那条记成「已回」，另一个会话就不会再回它")
        return found
    raise CrossGroup(f"id={event_id} 查不到所属群，本会话钉死群 {gid}，"
                     f"不能确认这条属于本群——请用 --group {gid} 明确指定，"
                     f"或确认这条事件来自本群")
