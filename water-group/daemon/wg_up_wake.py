"""配置、账号与唤醒判定——栈的「这条消息该不该叫醒 Agent」层（issue #37 分节 PC）。

从 `up.py` 搬出来的四分之一：配置读口（委托 `wg_config`）、OneBot 账号探测、
唤醒理由（@ / 关键词 / 合并转发体内）、待回时效、账本时序、以及 `--status` 用的两段
数据（`app_plugins_line`）。

这一层不知道进程、不知道端口、不知道栈怎么起停——`wg_up_stack` 反过来依赖它（注入
NapCat 前要问「OneBot 答不答」），所以方向只能是 stack → wake，反向会绕回 `up.py`
成环。

`STATE`/`CONFIG` 在 import 时求值一次，与拆分前的 `up.py` 完全一致（门禁靠改
`WATER_GROUP_HOME` 在 import 前隔离，见 `scripts/check_stop_stack.py`）。
"""
import json
import os
import time
import urllib.request
from pathlib import Path

import wg_config  # noqa: E402  (config.json 的唯一读取口，issue #4 PA0)
from wg_paths import data_dir, state_dir  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
STATE = state_dir()

# 待回唤醒的时效上限（秒）：超过它的 @ /关键词不再诱导回复。冷启动后
# wg_events.jsonl 里从未回复过的老唤醒会永远挂 [待回]，Agent 据此会回头
# 回复早已过时的话题——这个上限就是那条时间维度上的刀。
DEFAULT_PENDING_REPLY_TTL = 7200

# 合并转发的唤醒判定预算（issue #9）。转发体只用来回答「叫不叫得醒」，
# 不承载正文：节点数与字符数都封顶，避免一条 100 条转发把 --status / watch
# 的每次扫描拖成毫秒级长尾。与 chatlocal 的 MAX_NODES 对齐（QQ 单条上限）。
FORWARD_WAKE_NODES = 100
FORWARD_WAKE_CHARS = 4000

# `get_group_msg_history` 单次上限（OneBot 11）。`wg_gap` 据返回的最老一条判
# `truncated`，`--status` 必须说「只能核对最近 200 条」（issue #114 候选 4）。
HISTORY_COUNT = 200


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


def config():
    """委托 ``wg_config``：配置只有一份实现（issue #4 PA0）。"""
    return wg_config.config()


def config_problems():
    """配置健康度（issue #51）：缺失/占位被说出来，不静默退化。

    委托 ``wg_config.problems()``——「缺什么」只有一份实现，``--status`` 的配置行
    与 bootstrap 注入前的拒绝判定共用它。空列表 = 健康。
    """
    return wg_config.problems()


def onebot_token():
    # env 覆盖优先，与 watchdog.onebot_token()（#17）同一组变量、同一语义：
    # 轮换/冷启动/只用 env 的部署三种情况下探针行为一致。缺省再读 config.json。
    token = (os.environ.get("IDLEJOT_ONEBOT_TOKEN")
             or os.environ.get("TULPA_ONEBOT_TOKEN", "")).strip()
    if token:
        return token
    return str(config().get("onebot_token") or "")


def login_info():
    """OneBot `get_login_info` 的 `data` 字典，NapCat 不答时 None。

    返回的是 OneBot 11 的原始形状 ``{'user_id': ..., 'nickname': ...}``——**不是**
    元组。这句话以前写成 ``(account, nickname) from OneBot``，于是 ``--status`` 那边
    按元组解包，dict 迭代出两个 key，NapCat 行永远印成「在线 (user_id nickname)」
    （issue #156）。要账号文本请走 `login_account()`，别在这里再解一次。

    Port open is not enough: the port stays bound while the renderer behind it is
    already wedged, so only an HTTP answer proves the bridge is alive.
    """
    return _onebot_call("get_login_info", {})


def _field_text(value):
    """OneBot 字段归一成可打印文本；`None`/空白/无法表达 → None。

    QQ 号在 OneBot 11 里是数字，昵称是字符串；两者都要进同一行，所以统一成
    str。布尔是 int 的子类，但它在账号字段里只可能是坏数据，不当数字放行。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip() or None
    return None


def login_account(info):
    """把 `login_info()` 的 `data` 解析成 `(account, nickname)`，缺失的一侧为 None。

    解析只放这一处：`--status` 与以后任何要印账号的入口共用，避免各自再解一次
    （issue #156 就是两处口径不一致）。

    两种「拿不到账号」要分开读：`info is None` 是 OneBot 没答（端口在、桥不应答）；
    空字典 `{}` 是答了、只是没报账号。调用方按 `info is None` 区分这两件事，所以
    `None` 与 `{}` 都返回 `(None, None)` 并不表示它们同义。

    非 dict（元组是 issue #156 之前的历史形状）同样返回 `(None, None)`：形状漂移
    在这里被归一成「未报账号」，而不是抛异常把 `--status` 整条打挂。代价是它
    不会自己喊出来，所以生产者的形状由 `scripts/check_status_napcat_line.py`
    契约 1 单独钉住（issue #159）。
    """
    if not isinstance(info, dict):
        return (None, None)
    return (_field_text(info.get("user_id")), _field_text(info.get("nickname")))


def group_msg_history(group_id, count=HISTORY_COUNT):
    """最近 N 条群历史（OneBot 11 `get_group_msg_history`），NapCat 不答时返回 []。

    `wait_chat_messages` 是游标式长轮询——后端不在的那段时间，游标停推进，窗口内的
    消息永久不进 `chat_inbox`（`chatlocal/mcp_chat.py` 只在收到 OneBot 事件时写）。
    取历史是事后补缺的唯一缺口数据源（issue #114）：`collector` 启动时用它与本地
    `chat_inbox` diff，算「这段时间漏了什么」。返回 OneBot 原始消息（`message` 段是
    `{"type":"at","data":{"qq":...}}`，未经 chatlocal 拍平）——`wg_gap._is_at_me` 按
    这套格式判定 @ 本机。

    单次上限 `HISTORY_COUNT`：超出时 `wg_gap` 据返回的最老一条判 `truncated`，`--status`
    必须说「只能核对最近 200 条」，不静默截断（候选 4）。
    """
    data = _onebot_call("get_group_msg_history",
                        {"group_id": str(group_id), "count": int(count)})
    if not data:
        return []
    return data.get("messages") or []


def _onebot_call(action, params):
    """OneBot 11 HTTP loopback 调用，返回 `data` 字段或 None（NapCat 不答时）。

    `login_info` 与 `group_msg_history` 共用同一条路：bearer token + `ProxyHandler({})`
    绕开宿主代理（loopback 上 NapCat 只答 127.0.0.1）。失败一律返回 None——
    `--status` 与 `collector` 的缺口计算都不能因为 NapCat 没响应而崩。
    """
    body = json.dumps(params).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:43000/" + action, data=body,
        headers={"Authorization": f"Bearer {onebot_token()}",
                 "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if data.get("status") != "ok":
        return None
    return data.get("data")


def events():
    path = STATE / "wg_events.jsonl"
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
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


def self_account():
    return str(config().get("self_account") or "")


def wake_words(event=None):
    """唤醒词合并后的有序 list——委派 ``wg_wake_words.wake_words``（issue #279）。

    合并规则（顶层 ``self_names`` + per-group ``self_names``/``wake_words`` + 人格
    名 + account）单一出处在 ``wg_wake_words``；改前这里只取顶层 ``self_names``，
    per-group ``wake_words`` 配了但顶层没配的群 daemon 不认（issue #279 复现命令
    指向的就是这条差异）。

    ``event`` 给定时从中反解 ``conversation_id`` 的群号与账号，把 per-group 名单
    纳入；缺省（如 ``check_trigger_mode.py`` 的退化桩）只读顶层——与改前同形状。
    """
    import wg_wake_words  # noqa: PLC0415  (同目录 flat import，与 wg_aliases 同范式)

    group_id = ""
    persona_name = ""
    account = self_account()
    cfg = None
    if isinstance(event, dict):
        cfg = config()
        conv = str(event.get("conversation_id") or "")
        parts = conv.split(":")
        if len(parts) == 3 and parts[1] == "group":
            group_id = parts[2]
            if parts[0]:
                account = parts[0]
        # daemon 事件流不带 persona_name（chat_sessions 才有），preset id 走 config：
        # per-group persona 回退 default_persona。preset id 是文件名、几乎不出现在
        # 群友正文里，这一支形式对齐 chatlocal、运行上对 daemon 几乎无新增唤醒。
        grp_cfg = (cfg.get("groups") or {}).get(group_id) if group_id else None
        if isinstance(grp_cfg, dict) and grp_cfg.get("persona"):
            persona_name = str(grp_cfg["persona"])
        elif cfg.get("default_persona"):
            persona_name = str(cfg["default_persona"])
    return wg_wake_words.wake_words(cfg=cfg, group_id=group_id,
                                    persona_name=persona_name, account=account)


def trigger_mode(group_id=None):
    """本会话群的触发模式（issue #188）：`natural` / `mention_only`。

    决定两件事：collector 向 ``wait_chat_messages`` 请求的 ``mention_only`` 值
    （natural 给 False 让普通消息投递），与 ``wake_reason`` 对普通消息的放行
    （natural 群普通消息也算唤醒）。两处合起来才打通——只改采集层会被读侧
    ``wake_reason`` 再滤掉，只改读侧会被采集层先丢掉。

    ``group_id`` 缺省取本会话的群（``wg_isolation.group_id()``）——一个会话只
    管一个群（#164），collector / watch / reply 三处都不带群号调用。显式传则
    读 ``config.groups.<gid>.trigger_mode``，回退 ``default_trigger_mode``，
    再缺省 ``mention_only``：与 ``sync_groups`` / ``bootstrap`` 的派生口径一致，
    拼错的配置值不会把门禁关掉。

    每次现读、不缓存：config 是热文件，测试与热重载都靠替换
    ``wg_up_wake.config`` 打桩，缓存会让两者失效。
    """
    if group_id is None:
        # 延迟 import：wg_isolation import 本模块会成环（它只被读，但 import 图上
        # 它在 wg_up_wake 之后），与 wake_words 现读 config 同一理由——不在模块级拉。
        import wg_isolation  # noqa: PLC0415
        group_id = wg_isolation.group_id()
    cfg = config()
    if group_id:
        item = (cfg.get("groups") or {}).get(str(group_id))
        if isinstance(item, dict) and item.get("trigger_mode"):
            return str(item["trigger_mode"])
    return str(cfg.get("default_trigger_mode") or "mention_only")


def command_prefix():
    """`#` 指令前缀，读 config.json 的 slash_commands.prefix。

    归一化与 commands.settings_from 一致：缺失/空值回落 "#"。每次现读、不缓存：
    config 是热文件，测试也靠替换 `wg_up_wake.config` 打桩。
    """
    section = config().get("slash_commands") or {}
    return str(section.get("prefix") or "#")


def is_command(text, prefix=None):
    """这条纯文本是不是 `#` 指令守护的消息（issue #70）。

    接受规则必须与 commands.command_key 完全一致：前缀在行首、且前缀之后还有
    非空白内容。宽一点会吞掉守护也不认的消息，窄一点会让指令混进追问窗口——
    两边都不行。行内出现不算（`C#`、`第#3名`、URL fragment）。
    """
    prefix = command_prefix() if prefix is None else prefix
    if not prefix or not text or not text.startswith(prefix):
        return False
    return bool(text[len(prefix):].strip())


def _forward_at(nodes, account, budget, depth=0):
    """转发体里有没有 @ 我（issue #9）。只读落盘节点，不联网。

    `budget` 是共享的 ``{"nodes": N}`` 计数器：一条消息挂多个转发时，总扫描量
    仍受 ``FORWARD_WAKE_NODES`` 约束。嵌套最多跟两层，与 chatlocal 的
    ``sanitize_nodes`` 展开深度一致。
    """
    if depth >= 2:
        return False
    for node in nodes or []:
        if budget["nodes"] <= 0:
            return False
        budget["nodes"] -= 1
        if not isinstance(node, dict):
            continue
        for seg in node.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "at" and str(seg.get("qq")) == account:
                return True
            if seg.get("type") == "forward" and _forward_at(seg.get("nodes"), account, budget, depth + 1):
                return True
    return False


def _forward_word(nodes, budget, words, depth=0):
    """转发体里有没有唤醒词，返回命中的那个（issue #9）。"""
    if depth >= 2:
        return ""
    for node in nodes or []:
        if budget["nodes"] <= 0 or budget["chars"] <= 0:
            return ""
        budget["nodes"] -= 1
        if not isinstance(node, dict):
            continue
        for seg in node.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            kind = seg.get("type")
            if kind == "text":
                text = str(seg.get("text") or "")[: max(0, budget["chars"])]
                budget["chars"] -= len(text)
                for word in words:
                    if word in text:
                        return word
            elif kind == "forward":
                word = _forward_word(seg.get("nodes"), budget, words, depth + 1)
                if word:
                    return word
    return ""


def wake_reason(event):
    """Why this message should wake an agent: an explicit @, or our name in the text.

    The server already applies the same three rules (at / reply-to-self / keyword)
    before it returns a batch; repeating them here lets the report say which
    messages still deserve a reply without a second round trip.

    合并转发（issue #9）：正文在 ``referenced_messages[].nodes`` 里，顶层
    ``segments`` 只有 ``{"type":"forward","forward_index":0}``、``content`` 只有
    ``[合并转发]``。判定因此要下沉到节点内——转发里 @ 我、或转发正文里有我的
    名字，都该叫醒。数据全部来自落盘事件，这里不发起网络请求。
    """
    if event.get("is_self"):
        return ""
    account = self_account()
    for seg in event.get("segments") or []:
        if seg.get("type") == "at" and str(seg.get("qq")) == account:
            return "at"
    text = event.get("content") or ""
    # `#` 指令归 commands.py。@ 已经在上面先返回了：ignore_at_self=true 时守护会
    # 把带 @ 的混合消息让给 Agent，所以这里只挡纯指令。闸门放在关键词之前——
    # `#帮助 机器人` 里的名字不该把 Agent 叫醒、和守护抢同一条消息（issue #70）。
    if is_command(text):
        return ""
    # 只认服务端当场展开过的转发：state 不是 available 就没有正文可判，宁可不醒
    # 也不猜（#8 的「状态必须说出来」同一口径）。
    forwards = [d for d in event.get("referenced_messages") or []
                if isinstance(d, dict) and d.get("type") == "forward"
                and d.get("state") == "available"]
    # 转发体内的 @ 与顶层同一优先级：@ 先于关键词。
    budget = {"nodes": FORWARD_WAKE_NODES}
    for detail in forwards:
        if _forward_at(detail.get("nodes"), account, budget):
            return "at"
    # issue #279：唤醒词走合并规则（顶层 + per-group + persona + account），
    # 委派 wg_wake_words；传 event 让 per-group 名单按本条消息的群号生效。
    # 退化兼容（check_pending_reply 等旧桩把 ``wake_words`` 打成 0 参 lambda）：
    # 桩按 0 参调用回退，不传 event——测的是账本/时效，不是 per-group 合并。
    try:
        words = wake_words(event)
    except TypeError:
        words = wake_words()
    for word in words:
        if word in text:
            return f"keyword:{word}"
    budget = {"nodes": FORWARD_WAKE_NODES, "chars": FORWARD_WAKE_CHARS}
    for detail in forwards:
        word = _forward_word(detail.get("nodes"), budget, words)
        if word:
            return f"keyword:{word}"
    # natural 群的普通消息也算唤醒——叫醒决策交给 Agent，不强制每条必回（#188）。
    # 放在所有显式判定之后：@ / 关键词 / 合并转发仍返回它们的精确理由，watch 与
    # --list 的 `[<reason>]` 标记据此区分「点名」与「普通闲聊」。mention_only 群
    # 走到这里返回空串，维持「闲聊不唤醒」的既有门禁。
    if trigger_mode() == "natural":
        return "natural"
    return ""


def pending_reply_ttl():
    """待回唤醒的时效上限（秒），读 config.json 顶层 pending_reply.max_age_seconds。

    缺失或非法（含 0 / 负数）时回落 7200：一个拼错的配置不该把时效关掉，那正
    是这个修复要治的病。
    """
    raw = (config().get("pending_reply") or {}).get("max_age_seconds")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PENDING_REPLY_TTL
    return value if value > 0 else DEFAULT_PENDING_REPLY_TTL


def wake_expired(event, now=None):
    """这条唤醒是否已过待回时效——按事件时间实时计算，不落任何账本。

    过期不等于已回：不写 wg_replied.jsonl（那是「已回复」账本，语义不同），
    也不另存 expired 集合。每次报告现场算，改 TTL 对历史事件立即生效。

    没有时间戳的事件无法判定新旧，一律当没过期：宁可多显示一条，也不把
    数据异常悄悄藏成「没问题」。
    """
    ts = event.get("timestamp") or 0
    if not ts:
        return False
    now = time.time() if now is None else now
    return (now - ts / 1000) > pending_reply_ttl()


def heartbeat_time():
    """采集器最后一次完成循环的墙上时间（秒），没有心跳时返回 None。

    报告要同时给「19s 前」和绝对时刻：只有相对量的话，Agent 读到它时可能已经
    又过了几十秒，换算回去仍然不准（issue #95）。
    """
    raw = read_text(STATE / "wg_heartbeat")
    try:
        return int(raw)
    except ValueError:
        return None


def heartbeat_age():
    beat = heartbeat_time()
    return None if beat is None else time.time() - beat


def replied_ids():
    """Ids reply.py has already answered.

    The collector only delivers a batch when someone mentions the bot, so a reply
    we just sent is usually absent from wg_events.jsonl. Without this ledger the
    next agent would read the report, see [待回] on an answered message, and reply
    to it a second time.
    """
    ids = set()
    path = STATE / "wg_replied.jsonl"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                ids.add(json.loads(line)["id"])
            except (ValueError, KeyError):
                continue
    except OSError:
        pass
    return ids


_LEDGER_START = {"size": None, "ts": 0.0}


def ledger_started():
    """Timestamp of the first entry in wg_replied.jsonl, or 0.0 when there is none.

    Everything older than this was answered before the ledger existed, so a later
    self message is the only evidence left for it. Everything newer has to be in
    the ledger itself.

    The starting point is the first entry's own ``ts`` (issue #209), not the file's mtime:
    mtime is however precise the filesystem happens to be and lands *after* the
    line was written, so comparing event timestamps against it made the two
    clocks disagree by up to a second. Cached on ``st_size`` instead: the file is
    append-only, so the first line only moves when the ledger is rewritten from
    scratch — and then the size drops to 0 first, which busts the cache. Keying
    on mtime instead meant every reply (each one appends) invalidated it, so the
    whole file was re-read on every judgement.
    """
    path = STATE / "wg_replied.jsonl"
    try:
        size = path.stat().st_size
    except OSError:
        return 0.0
    if _LEDGER_START["size"] == size:
        return _LEDGER_START["ts"]
    ts = 0.0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ts = float(json.loads(line)["ts"])
            break
        except (ValueError, KeyError, TypeError):
            continue
    _LEDGER_START.update(size=size, ts=ts)
    return ts


def replied_after(event_id, rows, ledger=None):
    """Whether the bot spoke after this message — a cheap proxy for "answered".

    The ack cursor only records what reached disk, so it cannot answer this. The
    local ledger is the primary signal; a self message appearing later in the
    stream is the fallback for replies sent before the ledger existed.

    That fallback now only has historical rows to work with: collector stopped
    appending self echoes (#28), so everything after the ledger's first entry is
    judged by the ledger alone. The data it still sees is the pre-ledger tail
    already on disk, which has to keep its old meaning.

    That fallback is fenced to pre-ledger events on purpose. Left open it turns
    "answered" into "any self message came later", so a reply to one message
    marks every @ that arrived while it was being typed as answered — which is
    exactly how a burst gets dropped: a later message swallowed by the
    reply to 1563「？」, because that reply landed two seconds later.

    The fence is half-open on the ledger side: strictly after ``ledger_started()``
    and the ledger is the only judge; at or before it, the fallback decides. The
    comparison is strict (issue #209): an event whose millisecond timestamp lands
    exactly on the starting point could not be proven to come after the ledger
    started. With ``>=`` that single millisecond was handed to the ledger —
    ``seen`` was already set, so it returned False and a message that was in fact
    answered stayed ``[待回]``, replaying it. The starting point is
    seconds-granular, so an event and the first ledger entry collide on it
    routinely. Landing on the fallback side does not by itself answer "yes" —
    the fallback still needs a later self message; it only stops the ledger from
    answering "no" on evidence it does not have.
    """
    if ledger and event_id in ledger:
        return True
    started = ledger_started()
    seen = False
    for row in rows:
        if row.get("id") == event_id:
            seen = True
            if started and (row.get("timestamp") or 0) / 1000 > started:
                # The ledger was already keeping score by then. Not in it, not answered.
                return False
            continue
        if seen and row.get("is_self"):
            return True
    return False


def wake_state(event, rows, ledger):
    """一条唤醒的三态：已回 / 过期 / 待回。报告与 --list 共用这一个判定。

    过期是时间维度上的第二把刀：replied_after 只回答「回过没有」，不回答
    「还值不值得回」。两问都答了，[待回] 才是真正该回复的那批。
    """
    if replied_after(event.get("id"), rows, ledger):
        return "已回"
    if wake_expired(event):
        return "过期"
    return "待回"


def app_plugins_line():
    """app 侧插件摘要，一行。

    **不能 `import chatlocal`**：`chatlocal/config.py` 在模块级建数据目录并把
    进程的 TEMP/TMP 改到数据根，而 `--status` 的契约是「只看不动」。所以按
    文件载入 `chatlocal/app_plugins/spec.py`——它是纯数据的叶子模块（issue #4
    PD 就是这么切的），exec 一次不会拉起 chatlocal 包。开关直接读数据根下的
    policy 文件，路径与 app 侧 `Access.path` 同目录。
    """
    import importlib.util

    spec_path = ROOT / 'chatlocal' / 'app_plugins' / 'spec.py'
    module_spec = importlib.util.spec_from_file_location('wg_app_plugin_spec', spec_path)
    spec = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(spec)
    parts = []
    for plugin in spec.PLUGINS:
        policy = data_dir() / plugin['policy'] if plugin.get('policy') else None
        state = '已启用'
        if policy is not None and policy.exists():
            try:
                # 只说开/关，不在这里复述 app 侧的完整校验（那份在 policy() 里）。
                state = '已启用' if json.loads(policy.read_text('utf-8-sig')).get('enabled', True) else '停用'
            except (OSError, ValueError, TypeError):
                state = '配置无效（app 会停用自己）'
        # tools 是元组（一个插件可挂多个工具）；scope/expression 为 None 表示
        # 没有独立授权位/不是表达型插件，开关在 DB 数据态里（如 language）。
        tools = '、'.join(plugin['tools'])
        scope = plugin['scope'] or '沿用 chat 授权'
        expression = plugin['expression'] or '无'
        parts.append(f"{plugin['name']} {state}（{tools} / 授权位 {scope} / 表达 {expression}）")
    return ' / '.join(parts) if parts else '（未注册）'
