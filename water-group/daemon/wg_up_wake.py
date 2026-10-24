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
    """(account, nickname) from OneBot, or None when NapCat is not answering.

    Port open is not enough: the port stays bound while the renderer behind it is
    already wedged, so only an HTTP answer proves the bridge is alive.
    """
    body = json.dumps({}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:43000/get_login_info", data=body,
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
    info = data.get("data") or {}
    return info.get("user_id"), info.get("nickname")


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


def wake_words():
    words = config().get("self_names") or []
    return [str(w) for w in words if w]


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
    for word in wake_words():
        if word in text:
            return f"keyword:{word}"
    words = wake_words()
    budget = {"nodes": FORWARD_WAKE_NODES, "chars": FORWARD_WAKE_CHARS}
    for detail in forwards:
        word = _forward_word(detail.get("nodes"), budget, words)
        if word:
            return f"keyword:{word}"
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


_LEDGER_START = {"mtime": None, "ts": 0.0}


def ledger_started():
    """Timestamp of the first entry in wg_replied.jsonl, or 0.0 when there is none.

    Everything older than this was answered before the ledger existed, so a later
    self message is the only evidence left for it. Everything newer has to be in
    the ledger itself. Cached on mtime: the file is append-only, so the first
    line never moves.
    """
    path = STATE / "wg_replied.jsonl"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return 0.0
    if _LEDGER_START["mtime"] == mtime:
        return _LEDGER_START["ts"]
    ts = 0.0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ts = float(json.loads(line)["ts"])
            break
        except (ValueError, KeyError, TypeError):
            continue
    _LEDGER_START.update(mtime=mtime, ts=ts)
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
    """
    if ledger and event_id in ledger:
        return True
    started = ledger_started()
    seen = False
    for row in rows:
        if row.get("id") == event_id:
            seen = True
            if started and (row.get("timestamp") or 0) / 1000 >= started:
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
        parts.append(f"{plugin['name']} {state}（{plugin['tool']} / 授权位 {plugin['scope']} / 表达 {plugin['expression']}）")
    return ' / '.join(parts) if parts else '（未注册）'
