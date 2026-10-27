"""群消息复读 / 接龙判定与安全闸门（issue #155）。

群里自发形成的复读与接龙是纯机械模式，不需要模型参与——过 Agent 等于每条群消息
都要唤醒一次，token 与延迟都不可接受；而「最多跟一次」「不跟自己形成死循环」
这类约束放在模型侧只能靠提示词自觉。所以判定全在这里，`plugins/repeater_plugin.py`
只做插座（「文件不搬迁」，见 `plugins/base.py` 契约第 2 条）。

**数据从哪来**：`collector` 用 `mention_only=True` 只投递 @ / 关键词
（`collector.py:143`），普通群消息进不了 `wg_events.jsonl`。全量群事件只有
`commands` 的 WS 循环持有（`commands.py:645` 的 `_serve`），宿主在那里派发
`group_message` 事件——本模块不碰连接，只收一条事件。

**两种模式，接龙优先**

* 复读：窗口内同一句（空白折叠后比较）由 ≥ `min_repeats` 个**不同 QQ** 发出，
  且窗口尾部就是这句 → 跟一句同样的。
* 接龙：窗口尾部是递增序号（`1.` / `1、` / `+1` 三种标记，见 `chain_parse`），
  连续几项**格式与正文都相同**、≥ `min_repeats` 个不同 QQ → 发下一号，格式照抄。
  先判接龙再判复读：`1` `2` `3` 是接龙（发 `4`），`6` `6` `6` 是复读（发 `6`）。

**闸门（每一条对应 issue 里的一行要求，`scripts/check_repeater.py` 逐条钉住）**

============  ============================================================
自身消息      不进窗口（`user_id` 命中 `self_ids`）：自己的话不延长复读队列
非纯文本      带图/表情/语音/转发/引用/`@` 的整条只作**断点**，不参与判定
指令          以 `#` 前缀开头（与 `commands.command_key` 同源）不跟，防指令死循环
敏感词        `deny_words` 子串命中不跟；默认空表——仓库是公开的，真实词表放
              部署者本机的 `config.json`（该文件不入库）
本机名字      正文含 `self_names` 任一不跟：跟一句等于替人召唤自己
外链          含 `http(s)://` 不跟：跟了就是把别人的链接再发一遍
长度          `max_chars`（默认 60）以上不跟
冷却          同群 `cooldown_seconds`（默认 600s）内最多跟一次
一波一次      同一波（`repeat:<正文>` / `chain:<格式>|<正文>`）只**尝试**一次，
              骰子没中也算尝试过——否则消息越多必然跟
概率          `probability`（默认 0.5）不中就不跟
判不出自己    `self_ids` 为空时整条不跟：认不出自己的话就无法保证不死循环
============  ============================================================

「一波一次」与「冷却」都要跨进程重启生效，落 `state/wg_repeater.json`；窗口本身
（`WINDOWS`）是进程内的两分钟上下文，重启丢掉是诚实的——没有人会为了复读等一次
重启。

已知边界（不声称任何错都红）：

* **裸数字不算接龙**：`1` `2` `3` 三条独立消息不被认成接龙，只有带 `.`/`、`/`+`
  标记的才算。裸数字在正常对话里太常见，认了会把「3」「4」「5」当成接龙。
* **接龙允许中间插话**：只按序号连续性与格式一致性判定，两句之间有人聊别的
  不影响（窗口 `window_seconds` 兜住跨度）。这是刻意放松的——真实接龙里没人
  会排成一列不说话。
* **图文复读（issue 场景 3）不做**：重发图要么把远端 URL 交回 NapCat 去拉，要么
  自己下载再走 `#128` 的本地图通路，两条都超出「跟一句」的复杂度。见 plan。
"""
from __future__ import annotations

import json
import random
import re
import time

from wg_paths import state_dir

DEFAULTS = {
    # 窗口：多少秒 / 最多几条之内算「一波」
    "window_seconds": 120,
    "window_messages": 12,
    # 至少几个不同的人
    "min_repeats": 3,
    # 同一群多久内最多跟一次
    "cooldown_seconds": 600,
    # 参与概率：0 = 从不，1 = 每次
    "probability": 0.5,
    # 超过这个字数不跟（接龙正文通常很短，长段落复读像刷屏）
    "max_chars": 60,
    # 敏感/违规词，子串匹配（大小写不敏感）。默认空表，真实词表在 config.json
    "deny_words": [],
}

LEDGER_NAME = "wg_repeater.json"

# `1.` / `1、` / `1）` / `+1` 三种接龙标记。`num` 限 3 位：4 位以上多半是年份或
# 数量（`2026年`），认成接龙只会在无关对话上乱跟。
CHAIN = re.compile(
    r"^(?P<plus>\+)?(?P<num>\d{1,3})"
    r"(?P<sep>[.、．)）:：]?)"
    r"(?P<gap>\s*)"
    r"(?P<rest>.*)$", re.S)

# 进程内的两分钟上下文，按群分开。重启即清空（见模块 docstring 末段）。
WINDOWS: dict[str, list[dict]] = {}
_LEDGER: dict | None = None


def _num(value, fallback, minimum=0):
    """数值归一：坏值回落 fallback，低于 minimum 也回落（`min_repeats=0` 无意义）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number < minimum:
        return fallback
    return int(number) if float(number).is_integer() else number


def _probability(value, fallback):
    """概率归一：坏值回落 fallback，**越界钳回 [0, 1]**。

    与 `_num` 分开写：那边「低于下限」当坏值回落（`min_repeats=0` 没有意义），
    这边越界是有意义的写错法——`probability: -0.2` 的意思是「别跟」，钳到 0 才
    符合直觉；回落 0.5 会让一个想关掉的人反而开了五成。`1.5` 同理钳到 1。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return min(1.0, max(0.0, number))


def normalize(options):
    """配置归一。宿主只认三态，字段语义在这里（与 sixty/busboss 同款分工）。"""
    words = options.get("deny_words")
    if isinstance(words, str):
        words = [words]
    # `str(None)` 是 `"None"`：直接 str 化会把空项变成一条永远匹配不到的假词，
    # 白白占一个槽位、也让「配了几条」变成假数字。空项按没有处理。
    words = [str(w).strip() for w in (words or ()) if w is not None]
    words = [w for w in words if w]
    return {
        "window_seconds": _num(options.get("window_seconds"),
                               DEFAULTS["window_seconds"], 1),
        "window_messages": _num(options.get("window_messages"),
                                DEFAULTS["window_messages"], 1),
        "min_repeats": _num(options.get("min_repeats"),
                            DEFAULTS["min_repeats"], 2),
        "cooldown_seconds": _num(options.get("cooldown_seconds"),
                                 DEFAULTS["cooldown_seconds"], 0),
        "probability": _probability(options.get("probability"),
                                    DEFAULTS["probability"]),
        "max_chars": _num(options.get("max_chars"), DEFAULTS["max_chars"], 1),
        "deny_words": words,
    }


def _ledger_path():
    """账本路径。用函数而不是模块常量：`state_dir()` 读环境变量，测试要在
    调用期隔离（与 `wg_gap` 的 `DB` 同一族约束）。"""
    return state_dir() / LEDGER_NAME


def _read_ledger():
    try:
        data = json.loads(_ledger_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "groups": {}}
    # 写一半的脏文件可能是 list / str：当成空账本，别让一条坏记录让复读永久停摆。
    if not isinstance(data, dict) or not isinstance(data.get("groups"), dict):
        return {"version": 1, "groups": {}}
    data.setdefault("version", 1)
    return data


def _ledger():
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = _read_ledger()
    return _LEDGER


def _save(ledger):
    path = _ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
    except OSError:
        # 写不进去只影响「重启后还记得跟过没有」，不该让消息处理链崩。
        pass


def reset():
    """清空进程内窗口与账本缓存（测试用；不删磁盘文件）。"""
    global _LEDGER
    WINDOWS.clear()
    _LEDGER = None


def plain_text(event):
    """事件的纯文本，或 `(None, 原因)`——原因非空表示**不是纯文本**。

    与 `commands.plain_text` 分开写：那个把非文本段静默丢掉（指令判定只看正文），
    而这里「有没有图 / 有没有 @」本身就是判据——一条 `@某人` 的复读跟上去等于
    替别人 @ 人。
    """
    segs = event.get("message")
    if isinstance(segs, list):
        parts = []
        for seg in segs:
            if not isinstance(seg, dict):
                return None, "bad-segment"
            kind = seg.get("type")
            if kind == "text":
                data = seg.get("data")
                parts.append(str((data or {}).get("text") or "")
                             if isinstance(data, dict) else "")
                continue
            return None, f"non-text:{kind}"
        return "".join(parts), ""
    raw = event.get("raw_message")
    if isinstance(raw, str):
        # CQ 码形态说明里面至少有一个非文本段（`[CQ:image,...]`），一律不认。
        if "[CQ:" in raw:
            return None, "cq"
        return raw, ""
    return None, "no-message"


def chain_parse(raw):
    """`1. 正文` → {plus, num, sep, gap, rest, fmt}；不是接龙项时返回 None。

    必须带显式标记（`+` 或 `.`/`、` 一类分隔符）：裸数字 `3` 在正常对话里太常见，
    认成接龙会在无关消息上乱跟（见模块 docstring 的已知边界）。
    """
    if not isinstance(raw, str) or not raw:
        return None
    match = CHAIN.match(raw)
    if not match:
        return None
    plus = match.group("plus") or ""
    sep = match.group("sep") or ""
    if not plus and not sep:
        return None
    gap = match.group("gap") or ""
    rest = (match.group("rest") or "").strip()
    return {
        "plus": plus,
        "num": int(match.group("num")),
        "sep": sep,
        "gap": gap,
        "rest": rest,
        "fmt": (plus, sep, gap),
    }


def _is_command(text, prefix):
    """与 `wg_up_wake.is_command` / `commands.command_key` 同一判据。

    不 import 那两处：`wg_up_wake` 是 guard-owned，拖进 `commands` 的 AST 闭包会
    让「改唤醒判定」连带重启指令守护（issue #4 PA0 的病）；`commands` 反过来
    import 本模块。等价性由 `scripts/check_repeater.py` 用同一张文本表钉住，
    不靠注释承诺。
    """
    if not prefix or not isinstance(text, str) or not text.startswith(prefix):
        return False
    return bool(text[len(prefix):].strip())


def _eligible(text, settings, options, reason):
    """这条消息能不能进判定（False 的仍然进窗口，但只作**断点**）。"""
    if reason:
        return False
    body = " ".join(text.split())
    if not body:
        return False
    if len(body) > options["max_chars"]:
        return False
    if _is_command(text, str(settings.get("prefix") or "#")):
        return False
    low = body.lower()
    for word in options["deny_words"]:
        if word.lower() in low:
            return False
    for name in settings.get("self_names") or ():
        name = str(name or "").strip()
        if name and name in body:
            return False
    if "http://" in low or "https://" in low:
        return False
    return True


def _window(group_id):
    return WINDOWS.setdefault(group_id, [])


def _trim(window, now, options):
    cutoff = now - float(options["window_seconds"])
    while window and float(window[0]["at"]) < cutoff:
        window.pop(0)
    while len(window) > int(options["window_messages"]):
        window.pop(0)


def _repeat_plan(window, options):
    """窗口内同一句由 ≥ min_repeats 个不同人发出、且**最后一条就是那句** → 跟。

    * **尾巴必须是那句**：`草 草 草 换个话题` 之后不跟——那波已经过去了，跟着
      把旧话再说一遍等于在无关话题上插话。这条由 `m["text"] == tail["text"]`
      实现：尾巴换成别的文本，`senders` 自然只数到新尾巴那几个人。
    * **人数按不同 QQ 数**（issue 的「默认 3 人且不同 QQ」）：同一个人刷三遍不算。

    中间有人插话不影响（真实复读队列不会排成一列不说话），但那条插话之后必须
    还有人复读才算数——否则尾巴不是那句，走上面第一条。

    `if not tail["ok"]` 这一句在当前判据下**冗余**（`ok=False` 的消息要么
    `text` 为空、要么是指令文本，`senders` 都数不到 `min_repeats`；打靶实测
    删掉行为等价）。留着是因为它表达「尾巴自己必须是可跟的消息」这条不变量，
    以后若 `ok` 的含义或 `text` 的归一方式变了，它会先挡住。
    """
    tail = window[-1]
    if not tail["ok"]:
        return None
    senders = {m["uid"] for m in window if m["ok"] and m["text"] == tail["text"]}
    if len(senders) < options["min_repeats"]:
        return None
    return f"r|{tail['text']}", tail["text"]


def _chain_plan(window, options):
    items = [m for m in window if m["ok"] and m.get("chain")]
    if len(items) < options["min_repeats"]:
        return None
    # 尾巴必须是接龙项本身：`3. x` 之后有人说「哈哈」，那已经不是接龙队列了。
    if items[-1] is not window[-1]:
        return None
    run = [items[-1]]
    for candidate in reversed(items[:-1]):
        head = run[-1]["chain"]
        item = candidate["chain"]
        if item["num"] != head["num"] - 1:
            break
        if item["fmt"] != head["fmt"] or item["rest"] != head["rest"]:
            break
        run.append(candidate)
        if len(run) >= options["min_repeats"]:
            break
    if len(run) < options["min_repeats"]:
        return None
    if len({m["uid"] for m in run}) < options["min_repeats"]:
        return None
    # `run` 是从尾巴往前追出来的（`run[0]` = 最新一项），所以下一号取 `run[0]`。
    # 取 `run[-1]` 会拿到最旧的那项，算出「第二号」，接龙永远原地打转。
    # 格式三项在整条 run 上一致（上面按 `fmt` 相等筛过），从哪端取等价。
    last = run[0]["chain"]
    text = f"{last['plus']}{last['num'] + 1}{last['sep']}{last['gap']}{last['rest']}"
    key = f"c|{last['plus']}{last['sep']}{last['gap']}|{last['rest']}"
    return key, text


def _prune_waves(group, now, cooldown):
    waves = group.get("waves")
    if not isinstance(waves, dict):
        group["waves"] = {}
        return
    cutoff = now - max(float(cooldown), 1.0)
    for key in [k for k, ts in waves.items() if float(ts or 0) < cutoff]:
        waves.pop(key, None)


def consider(event, settings, options, self_ids, clock=None, rng=None):
    """吃一条群消息，返回要跟的文本（`None` = 不跟）。

    会原地更新窗口与账本：**先记账再掷骰**——一波只尝试一次，骰子没中也算试过，
    否则同一波每来一条消息都重掷，迟早必中。
    """
    clock = clock or time.time
    rng = rng or random.random
    group_id = str(event.get("group_id") or "")
    # 认不出自己是谁就无法保证不跟自己的话：整条不跟，也不进窗口。
    if not group_id or not self_ids:
        return None
    text, reason = plain_text(event)
    now = clock()
    ok = text is not None and _eligible(text, settings, options, reason)
    body = " ".join((text or "").split())
    uid = str(event.get("user_id") or "")
    own = uid in self_ids
    chain = chain_parse(text) if ok else None
    window = _window(group_id)
    window.append({
        "id": event.get("message_id"),
        "uid": uid,
        "text": body,
        "raw": text or "",
        "at": now,
        # 自己说的话、非纯文本、指令、敏感词……都只作断点，不参与计数。
        "ok": bool(ok) and not own,
        "chain": chain if (ok and not own) else None,
    })
    _trim(window, now, options)
    if own or not ok:
        return None

    ledger = _ledger()
    group = ledger["groups"].setdefault(
        group_id, {"waves": {}, "last_sent": 0.0, "sent": 0})
    if not isinstance(group, dict):
        group = {"waves": {}, "last_sent": 0.0, "sent": 0}
        ledger["groups"][group_id] = group
    _prune_waves(group, now, options["cooldown_seconds"])
    if now - float(group.get("last_sent") or 0) < float(options["cooldown_seconds"]):
        return None

    plan = _chain_plan(window, options) or _repeat_plan(window, options)
    if not plan:
        return None
    key, reply = plan
    waves = group["waves"]
    if key in waves:
        return None
    waves[key] = now
    if rng() >= float(options["probability"]):
        _save(ledger)
        return None
    group["last_sent"] = now
    group["sent"] = int(group.get("sent") or 0) + 1
    _save(ledger)
    return reply


def status_text():
    """`up.py --status` 插件行的附加说明；一次都没跟过时返回空串。

    不 import `wg_clock`：它是「无守护进程引用」的共享助手（`reload_plan.NO_RESTART`），
    本模块在 `commands` 的闭包里，拖进来会让那条注释变成假话。
    """
    try:
        ledger = _read_ledger()
    except Exception:  # noqa: BLE001  --status 不该因为一条坏账本整条崩（#124 的教训）
        return ""
    total = 0
    for group in (ledger.get("groups") or {}).values():
        if isinstance(group, dict):
            try:
                total += int(group.get("sent") or 0)
            except (TypeError, ValueError):
                continue
    return f"（累计跟读 {total} 次）" if total else ""
