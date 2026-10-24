"""别名指向渲染：正文里的外号要能对到人（issue #30）。

写侧一直落得进去（档案 `aliases` 有数据），读侧不接：唤醒行只印发送者的 QQ
群名片，正文里出现的外号没有任何指向信息，Agent 判不出「这是谁」。这里补读侧。

数据源是记忆引擎的只读原语（主档 ∪ 本群覆盖行并集，issue #5 同一视图），
daemon 不直连库。两个用途各读一张视图：

- `alias_index_exact`：精确等值查找（`sender_tag`）。单字真名和泛称都保留——
  显示名就是那个字符串，不是猜测（issue #54）；
- `alias_index`：子串扫描（`body_hits`）。单字名和泛称挡掉——它们几乎在任何
  句子里都能命中，会制造假指向（issue #30 的原始约束只针对这里）；纯数字
  别名（建档时写进 aliases 的 QQ 号）同样挡掉——正文里一串数字不代表有人在
  叫这个外号，@ 段渲染出的号码更是指向信息不是正文（issue #52）。

别名表按进程缓存一次：CLI 一命一进程，一批唤醒行共用一份，不会读到半新半旧
的中间态。

引擎不可用（库锁、缺失、异常）时静默降级：只印原样，不长出任何指向，也不拖垮
唤醒链路——与 `reply.dossier_matches` 同一兜底姿势。认错人比不认严重得多，
没有数据就不指。
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SENDER_ALIAS_CHARS = 2   # 发送者后最多附几个档案外号，多了把唤醒行撑散
BODY_HITS = 3            # 一条正文最多指出几个外号指向
# @ 段渲染出的 `@<qq>` 是指向信息不是正文（issue #52）：位数口径与
# wg_text.PLACEHOLDERS 的 `@\d{5,}` 保持一致。
AT_MENTION_RE = re.compile(r"@\d{5,}")
_CACHE = {}


def group_of(conversation_id):
    """``123456:group:789`` -> ``789``；不是群会话返回空串。"""
    parts = str(conversation_id or "").split(":")
    return parts[2] if len(parts) == 3 and parts[1] == "group" else ""


def alias_map(group_id=None, exact=False):
    """``{名字: (QQ, 主名)}``；拿不到就空表（调用方按没有指向处理）。

    ``exact=True`` 取精确等值视图（单字名也保留），``exact=False`` 取子串扫描
    视图（单字名与泛称挡掉）。缓存键必须带视图维度，否则两张表互相污染
    （issue #54）。
    """
    key = (str(group_id or ""), bool(exact))
    if key in _CACHE:
        return _CACHE[key]
    try:
        from water_group.memory_engine import get_memory_engine

        engine = get_memory_engine()
        gid = key[0] or None
        table = (engine.alias_index_exact(group_id=gid) if exact
                 else engine.alias_index(group_id=gid))
    except Exception:  # noqa: BLE001  渲染层不因档案库故障而失败
        table = {}
    _CACHE[key] = table
    return table


def sender_tag(sender, group_id=None, limit=SENDER_ALIAS_CHARS):
    """发送者名后的档案锚：``（档案：主名，外号：甲/乙）``；对不上返回空串。

    只在发送者本名或外号能在档案里找到时附——群名片和档案主名对得上，Agent
    才知道「这个显示名是谁」，正文里他的外号也才有处可查。
    """
    name = str(sender or "").strip()
    if not name:
        return ""
    # 精确等值查找：单字真名必须是确定的命中，不能被子串扫描的 min_len 挡掉
    # （issue #54）。
    table = alias_map(group_id, exact=True)
    hit = table.get(name)
    if not hit:
        return ""
    uid, primary = hit
    aliases = [a for a, (u, _) in table.items()
               if u == uid and a != primary and a != name][:limit]
    if primary == name and not aliases:
        return ""
    parts = [f"档案：{primary}"] if primary != name else []
    if aliases:
        parts.append("外号：" + "/".join(aliases))
    return f"（{'，'.join(parts)}）" if parts else ""


def body_hits(body, group_id=None, limit=BODY_HITS, sender=""):
    """正文里命中的别名指向：``["↪ 外号「X」= 群友甲(12345)", ...]``。

    长的名字先匹配（「时之锁」优先于「时之」），同一个人只指一次；发送者自己
    的名字不指——那一行本来就是他说的话。没有命中就没有输出，不猜。

    issue #52 两条防自指：扫描前剥掉 `@<数字>`（@ 段是指向信息不是正文，与
    `wg_text.PLACEHOLDERS` 的 `@\\d{5,}` 同一口径）；纯数字别名（QQ 号）不
    参与正文命中。`@<显示名>` 命中别名视为真实提及，保留。
    """
    text = AT_MENTION_RE.sub("", str(body or ""))
    if not text:
        return []
    table = alias_map(group_id)
    mine = str(sender or "").strip()
    seen_uids, hits = set(), []
    for alias in sorted(table, key=len, reverse=True):
        uid, primary = table[alias]
        if uid in seen_uids or alias == mine:
            continue
        if alias.isdigit() or alias not in text:
            continue
        seen_uids.add(uid)
        hits.append(f"↪ 外号「{alias}」= {primary}({uid})")
        if len(hits) >= limit:
            break
    return hits
