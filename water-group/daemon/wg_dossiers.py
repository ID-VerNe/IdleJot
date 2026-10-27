"""唤醒行里的群友画像：把沉淀下来的小本本接到 daemon 链路（issue #163）。

画像一直在沉淀（`member_profiles` / `member_events` / `member_group_memories`），
但只有 MCP 那条路在用：`chatlocal/mcp_chat.py` 的 `guidance()` 把 L1 卡片塞进
`packet['member_dossiers']`，产物落进 `wg_state.json` 的 `chat_guidance`——而水群
Agent 实际走的三个入口（`listen.py` / `reply.py` / `up.py --status`）一个都不读它。
结果是「小本本」这个卖点在真实对话里不生效：群友问「你还记得我吗」，Agent 手上
只有 `--list` 的外号锚。

本模块补 daemon 侧的**读出口**：把一个人的档案压成一行，挂在他自己那一行消息后
面。三条口径不许各写一份，都从这里出：

- `anchors()`：`(档案锚, 正文指向, 画像卡)` 三段，唤醒行 / `--list` / `--context`
  共用一个调用（`wg_aliases` 那三行原先在三个文件里各写一遍）；
- `render_for_event()`：这一条消息该附谁——发送者优先，再是正文点到的人，
  最多两个，**没命中就一张都不附**；
- `group_note_line()`：本群术语（黑话 / 店名 / 约定）每块只印一次。

数据源只用引擎的公开原语 `query_member_dossier()`：它返回的就是「主档 ∪ 本群
覆盖行」的并集视野（issue #5 同一视图）＋ 最近一条本群事件。不直连库、不为渲染
新开引擎接口——读侧多一条私路，写侧的边界（#19/#34）就白做了。

**预算**：画像另计，不占 `wg_text.WAKE_BODY_CHARS` 的正文额度。每人一行，
`CARD_CHARS` 硬顶，超了走 `wg_text.render_fitted`（截断的唯一归属仍是
`chatlocal.text_limits.clip`），尾部 `…` 让「被切过」可检出；要看全文用
`reply.py --who <花名>`。

**降级**：引擎不可用（库锁、缺失、异常）时整段吞掉，返回空串，唤醒行照常输出。
与 `wg_aliases` / `reply.dossier_matches` 同一姿势——认错人比不认严重得多，没数据
就不指。进程内按 `(名字, 群号)` 缓存一次：CLI 一命一进程，一次唤醒里重复渲染
（发送者又被正文点名）不会多查一遍库。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import wg_aliases  # noqa: E402  (正文点名的命中规则，issue #30)
import wg_text  # noqa: E402  (截断与单行折叠的唯一入口)

CARD_CHARS = 60       # 每人画像一行的字符上限（唤醒行现在是 80 字正文额度）
MAX_PEOPLE = 2        # 一条消息最多附几个人：发送者 + 正文点名的
GROUP_NOTE_CHARS = 80  # 群级术语一行的上限
WHO_CARD_CHARS = 160  # `--who` 是专门问一个人，给得下完整卡

_CACHE = {}


def _engine():
    from water_group.memory_engine import get_memory_engine

    return get_memory_engine()


def _dossier(identifier, group_id=None):
    """`query_member_dossier` 的结果；查不到 / 出错都返回 None（并记住这个结论）。

    名字也是缓存键的一部分：`--who` 传的是花名，正文点名的也是别名，同一个 uid
    会以不同名字进来，第二次在这里就命中，不再回库。
    """
    name = str(identifier or "").strip()
    if not name:
        return None
    key = (name, str(group_id or ""))
    if key in _CACHE:
        return _CACHE[key]
    try:
        res = _engine().query_member_dossier(name, group_id=key[1] or None, limit=1)
    except Exception:  # noqa: BLE001  渲染层不因档案库故障而失败
        res = None
    if not (isinstance(res, dict) and res.get("found")):
        res = None
    _CACHE[key] = res
    return res


def _profile(res):
    prof = res.get("profile") if isinstance(res, dict) else None
    return prof if isinstance(prof, dict) else {}


def _gossip(res):
    """本群最近一条事件（`query_member_dossier` 带回来的那条）的八卦点。"""
    events = res.get("recent_events") if isinstance(res, dict) else None
    if not events:
        return ""
    event = events[0] if isinstance(events[0], dict) else {}
    return str(event.get("context") or event.get("quote") or "").strip()


def card_for(identifier, group_id=None, limit=CARD_CHARS):
    """一个人的画像卡：``画像 主名(QQ)｜外号 a/b｜XP x、y｜记 …｜本群 …``。

    字段顺序按「接话时最用得上」排：先认出人（名字 + 外号），再是他爱聊什么，
    然后是最近一条记忆，最后是本群八卦。越靠后越可能被 `…` 切掉——要看全就
    `--who`。查不到档案返回空串，调用方按「没有这个人」处理，不留占位。
    """
    res = _dossier(identifier, group_id)
    if res is None:
        return ""
    prof = _profile(res)
    uid = str(prof.get("user_id") or "")
    name = str(prof.get("primary_name") or uid or "").strip()
    if not name:
        return ""
    aliases = [str(a) for a in (prof.get("aliases") or [])
               if str(a).strip() and str(a) not in (name, uid)][:2]
    interests = [str(i).strip() for i in (prof.get("interests") or []) if str(i).strip()][:2]
    memories = [str(m).strip() for m in (prof.get("key_memories") or []) if str(m).strip()]
    parts = [f"画像 {name}({uid})" if uid else f"画像 {name}"]
    if aliases:
        parts.append("外号 " + "/".join(aliases))
    if interests:
        parts.append("XP " + "、".join(interests))
    if memories:
        # 记忆列表是「最久没被重申的在前」（memory_format.merge_text_list），
        # 尾部才是最新的一条。
        parts.append("记 " + memories[-1])
    gossip = _gossip(res)
    if gossip:
        parts.append("本群 " + gossip)
    return wg_text.render_fitted("｜".join(parts), limit)


def render_for_event(sender, body, group_id=None, limit=CARD_CHARS):
    """这一条消息该附谁的画像：发送者优先，再是正文点到的人，最多 `MAX_PEOPLE` 个。

    没有命中就返回空表——不是「找不到就印个空档案」，那种行会让 Agent 以为人
    认出来了。同一个 uid 只出一张卡：发送者往往也在正文里被点名（引用体里就有）。
    """
    cards, seen = [], set()
    targets = [str(sender or "").strip()] + [uid for uid, _, _ in
                                             wg_aliases.body_targets(body, group_id,
                                                                     sender=sender)]
    for ident in targets:
        if not ident or len(cards) >= MAX_PEOPLE:
            continue
        res = _dossier(ident, group_id)
        if res is None:
            continue
        uid = str(_profile(res).get("user_id") or ident)
        if uid in seen:
            continue
        seen.add(uid)
        card = card_for(ident, group_id, limit)
        if card:
            cards.append(card)
    return cards


def group_note_line(group_id=None, limit=GROUP_NOTE_CHARS):
    """本群术语一行：``【群级术语】a=b；c=d``；一条都没有就空串。

    术语是群级的（issue #31），跨群不共享。每块唤醒只印一次，不随每条消息重复——
    它回答的是「这个群里这些词是什么意思」，不是这条消息的属性。
    """
    gid = str(group_id or "").strip()
    if not gid:
        return ""
    key = ("__notes__", gid)
    if key in _CACHE:
        return _CACHE[key]
    line = ""
    try:
        from water_group.memory_engine import GROUP_NOTE_L1_CAP

        notes = _engine().query_group_notes(gid, limit=GROUP_NOTE_L1_CAP)
        terms = [f"{n['term']}={n['value']}" for n in notes if n.get("term")]
        if terms:
            line = wg_text.render_fitted("【群级术语】" + "；".join(terms), limit)
    except Exception:  # noqa: BLE001  术语表读不到等于没有术语
        line = ""
    _CACHE[key] = line
    return line


def anchors(sender, body, group_id=None, with_card=True):
    """一行消息尾巴的三段：``(档案锚, 正文指向, 画像卡串)``。

    `tag` 拼在人名后面、`hits` 拼在行尾（`wg_aliases` 的老位置），`cards` 整体附在
    最后：每人一段、段前两个空格，和 `hits` 同款缩进。`with_card=False` 只关画像
    ——`--context` 里普通闲聊不附卡，那些行要的是「在聊什么」，不是「他是谁」。
    """
    tag = wg_aliases.sender_tag(sender, group_id)
    hits = wg_aliases.body_hits(body, group_id, sender=sender)
    cards = render_for_event(sender, body, group_id) if with_card else []
    return tag, hits, "".join("  " + card for card in cards)


def who_card(name, group_id=None):
    """`reply.py --who` 的完整卡：一行放得下就说全，放不下才切。"""
    return card_for(name, group_id, WHO_CARD_CHARS)
