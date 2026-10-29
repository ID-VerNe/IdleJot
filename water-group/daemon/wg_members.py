"""花名 → QQ 的解析（issue #203 顺带搬出来的，issue #30 定的判定口径）。

原先与 `reply.py` 混在一起，让那个文件逼近 issue #11 的一千行红线。

判定顺序是 roster 先、档案后，每层都先精确再近似；匹配到不止一个人就如实报
出候选，绝不猜——@ 错人在群里读起来是机器人犯的错，漏掉一个 @ 不是。
"""
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import wg_config  # noqa: E402  (config.json 的唯一读取口)

# 一个进程内只取一次本群花名册：一条回复可能带好几个 @，而名单是一份不小的
# JSON，每次 @ 都重新拉一遍等于白花一次往返。
_ROSTER = {}


def onebot_config():
    """(http base, token)：与 `reply.onebot_config` 同一份取值口径。"""
    data = wg_config.config()
    return str(data.get("onebot_http_url") or "").rstrip("/"), str(data.get("onebot_token") or "")


def self_account():
    return str(wg_config.config().get("self_account") or "")


def group_members(group_id):
    """Live member list for one group: [{"qq", "card", "nickname"}].

    Memoised per process: one reply can carry several @-mentions, and the roster
    is a fat JSON list that costs a round trip each time.
    """
    if group_id in _ROSTER:
        return _ROSTER[group_id]
    base, token = onebot_config()
    if not base or not group_id:
        return []
    try:
        resp = requests.post(f"{base}/get_group_member_list",
                             json={"group_id": int(group_id)},
                             headers={"Authorization": f"Bearer {token}"}, timeout=15)
        rows = resp.json().get("data") or []
    except (requests.RequestException, ValueError) as exc:
        print(f"  member list unavailable: {type(exc).__name__}: {exc}")
        return []
    _ROSTER[group_id] = [{"qq": str(r.get("user_id") or ""),
                          "card": (r.get("card") or "").strip(),
                          "nickname": (r.get("nickname") or "").strip()} for r in rows]
    return _ROSTER[group_id]


def dossier_matches(raw):
    """(exact, loose) profiles from the memory DB; empty pair when it is absent.

    The dossier is a nice-to-have here: @-ing still works off the live roster, so
    a locked or missing database must not take the whole reply down with it.
    """
    try:
        from water_group.memory_engine import get_memory_engine

        return get_memory_engine().match_profiles(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"  dossier lookup failed: {type(exc).__name__}: {exc}")
        return [], []


def resolve_member(name, group_id):
    """花名 -> (qq, label), or (None, 原因).

    A bare number is taken as the QQ itself -- the one input that cannot be
    ambiguous. Everything else is tried against the live roster first and the
    dossier second: the group card is how people actually address each other in
    here ("某人的旧名片"、"某人的新名片"), the dossier is what survives a rename and
    holds the shorthand people type ("主名"、"外号"). Each tier is tried exact
    before loose, so a full name never loses to somebody else's substring.

    A name that fits more than one person is reported, not guessed. @-ing the
    wrong person reads as a mistake by the bot in a way that a missed @ does not.

    取 number / token 与「我是谁」都走本模块的 `onebot_config` / `self_account`，
    与 `reply.py` 读的是同一份 `config.json`（issue #203）。
    """
    raw = (name or "").strip().lstrip("@")
    if not raw:
        return None, "空名字"

    members = group_members(group_id)
    in_group = {m["qq"]: m for m in members}
    roster_known = bool(members)
    me = self_account()

    def label_of(qq):
        mate = in_group.get(qq)
        return ((mate["card"] or mate["nickname"]) if mate else "") or qq

    if raw.isdigit():
        if roster_known and raw not in in_group:
            # A number nobody in the group answers to is not an @, it is a typo.
            return None, f"{raw} 不在本群"
        if raw == me:
            return None, "是我自己"
        return raw, label_of(raw)

    # 档案里有、但不在本群的人：不能 @，可往往正是用户记错的那个，值得单独说一句。
    elsewhere = []

    def from_dossier(profs):
        out = []
        for p in profs:
            qq = str(p.get("user_id") or "")
            if not qq:
                continue
            if roster_known and qq not in in_group:
                elsewhere.append((qq, str(p.get("primary_name") or qq)))
                continue
            out.append((qq, label_of(qq) if qq in in_group else str(p.get("primary_name") or qq)))
        return out

    exact, loose = dossier_matches(raw)
    tiers = [("本群名片", [(m["qq"], label_of(m["qq"])) for m in members
                          if raw in (m["card"], m["nickname"])]),
             ("档案", from_dossier(exact))]
    if len(raw) >= 2:
        tiers.append(("本群名片近似", [(m["qq"], label_of(m["qq"])) for m in members
                                      if raw in m["card"] or raw in m["nickname"]]))
        tiers.append(("档案近似", from_dossier(loose)))

    for kind, cands in tiers:
        if len(cands) == 1:
            qq, label = cands[0]
            if qq == me:
                return None, "是我自己"
            return qq, label
        if len(cands) > 1:
            shown = "、".join(f"{label}({qq})" for qq, label in cands[:5])
            return None, f"{kind}里有 {len(cands)} 个：{shown}"

    if elsewhere:
        qq, label = elsewhere[0]
        return None, f"档案里是 {label}({qq})，但不在本群"
    return None, "没找到"
