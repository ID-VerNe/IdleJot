"""# 前缀指令守护：群里的 #关键词 直接由脚本回复，不经过 Agent。

为什么要有它：唤醒链路（collector → wait_chat_messages → listen.py → Agent）只对
@ / 引用 / 关键词放行，`#xxx` 在服务端就被 mention_only 丢弃（mcp_chat.py:807-812），
Agent 从来不是 `#` 指令的消费者。缺的只是一条独立的消费+回复路径——本进程就是这条
路径：自己连 NapCat WS 收全量事件，过滤 `#` 前缀群消息，查 config.json 的
slash_commands 映射，经 OneBot HTTP 直接回群（带原生引用）。

与 Agent 的边界（两者永不见面）：
  - `#关键词`      → 本进程回复；Agent 侧根本收不到（mention_only 丢弃）
  - `@机器人 / 关键词` → Agent 唤醒；本进程不碰（不含 # 前缀）
  - `@机器人 #关键词`  → 混合消息，默认让给 Agent（ignore_at_self），避免双响应

依赖与安全面：只连 127.0.0.1 的 NapCat（WS 43001 收、HTTP 43000 发），不 import
chatlocal、不碰 SQLite、不调 MCP；与 collector 各持各的 WS 客户端（NapCat 向所有
WS 客户端广播事件，已实测：第二个客户端并发在线时事件照收，原连接无感）。

回复内容三个来源：config 的静态映射（slash_commands.commands）、插件注册表的
动态 handler（米游社取数 issue 方案 D、60s 资讯 issue #53 方案 A、扫码登录
issue #6，都经 ``plugins/`` 适配器注册）。本文件只留传输与仲裁（前缀、冷却、
去重、引用、凭证链路、发送），域知识一律在插件里（issue #4 PA–PC）。

issue #6 的两条硬规则：
  - §4.2 隐私缺口：取数 ctx 带用户维度，未绑定的群友**不再**回落全局 CK
    （否则任何群友发 #体力 看到的都是管理员的账号）；全局 CK 只留给
    plugins.miyoushe.options.admin_qq 白名单与 --test 合成事件；
  - §4.3 轮询在后台线程：扫码是多轮有状态流程，handler 里同步等会卡住 WS 事件循环。
"""
import argparse
import collections
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
import winconsole  # noqa: E402  (stdout 编码 + 控制台代码页，一次收口)
import reload as wg_reload  # noqa: E402  (热重载合作退出检查点，不 spawn)
from wg_daemon import beater, logger, pid_alive
from wg_emit_pool import EmitPool  # noqa: E402  (事件型派发耗时预算池，issue #225)
from wg_paths import logs_dir, state_dir
STATE_DIR = state_dir()
LOG = logs_dir() / "wg_commands.log"
HEARTBEAT = STATE_DIR / "wg_commands_heartbeat"

RELAY_SECONDS = 30        # 配置热重载周期
RECV_TIMEOUT = 2          # WS 单次 recv 超时（热重载检查点；心跳节拍）
EMIT_TIMEOUT = 1          # 事件型派发耗时预算（issue #225）：超时跳过本轮，不挡 # 指令
SEEN_MAX = 4096           # 已处理 message_id 的去重窗口
LOCK = STATE_DIR / "wg_commands.lock"   # 单实例锁，卡死可手删

CQ_CODE = re.compile(r"\[CQ:[^\]]*\]")

# 未在 slash_commands.commands 配置、也不是帮助关键词时，默认静默忽略：
# 群里打错一个 `#体力查` 不该收到一条"未知指令"，那比不回复更吵。
DEFAULTS = {
    "enabled": False,
    "prefix": "#",
    "help_keywords": ["帮助", "命令"],
    "cooldown_seconds": 5,
    "ignore_at_self": True,
    "unknown_reply": "",
    "commands": {},
    # 插件配置由 settings_from 归一：{名字: {enabled, groups, group_off, options}}。
    # 宿主不认识任何 provider 字段——默认值与类型归一在 plugins/*_plugin.py
    # （issue #4 PB）。plugins_legacy = 仍在用 slash_commands.<name> 旧键的名字。
    "plugins": {},
    "plugins_legacy": [],
    "ws_url": "ws://127.0.0.1:43001",
    "http_url": "http://127.0.0.1:43000",
    "token": "",
    "self_account": "",
    # 自己的名字（config.json 的 self_names）：复读插件要判「正文是不是在叫我」
    # （issue #155），跟一句带自己名字的话等于替人召唤自己。宿主不认识具体插件，
    # 只把这份名单归一后放进设置——与 self_account 同族，都是「谁是本机」。
    "self_names": [],
    "groups": set(),
}

# issue #11 PB: 日志/心跳助手收口到 wg_daemon
log = logger(LOG)
beat = beater(HEARTBEAT)


# 插件在 import 期装配（issue #4 PA）：契约在 plugins/base.py，注册表在
# plugins/registry.py，米游社/60s 的适配器在 plugins/*_plugin.py。
# 显式静态 import 是硬要求——check_hot_reload 靠 AST 闭包把改动映射到进程。
import plugins  # noqa: E402

# 插件与轮询线程共享的最新设置（issue #4 PC）：热重载只换这里的引用，线程不用
# 重启。`load_all` 在 import 期跑，所以传的是取值函数而不是当时的 settings。
SETTINGS_BOX = {"settings": {}}


def _send_via_onebot(settings, group_id, payload):
    """宿主发送通道：插件给的 payload 与 handler 返回值同形状（str / {"text","image"}）。"""
    send_group_msg(settings, group_id, segments_for(payload))


PLUGINS = plugins.load_all(
    log=log,
    settings_provider=lambda: SETTINGS_BOX["settings"],
    sender=_send_via_onebot,
)

# 动态 handler 合成一张表（米游社取数 + 登录 + 60s 资讯）。合并成模块级
# 名字而不是在 resolve_reply 里现拼：测试直接改 commands.HANDLERS 打桩。
HANDLERS = PLUGINS.commands()

# 取数层异常的统一出口：插件声明自己的域异常，宿主统一「str(exc) 当回复」。
FETCH_ERRORS = PLUGINS.error_types()

# 事件型派发的耗时预算池（issue #225）：单 daemon worker，``_emit_group_message``
# 经它跑插件 ``on_event``，``EMIT_TIMEOUT`` 秒内拿不到结果就跳过本轮、记慢日志，
# 不让一个慢插件把紧随其后的 ``#`` 指令挡在帧队列里。import 期起线程，与
# ``PLUGINS`` 同一时机；``serve()`` 的 ``finally`` 里 ``shutdown``。
_EMIT_POOL = EmitPool()

# §4.2：未绑定群友的取数指令统一回这句，不再拿管理员的全局 CK 顶。
BIND_HINT = plugins.BIND_HINT


# issue #11 PB: pid_alive 收口到 wg_daemon（判活以更严格的 reload 版为基准：
# GetExitCodeProcess == STILL_ACTIVE，句柄未关的退出进程不再误判活）


def acquire_lock():
    """单实例锁：两个守护同时在线会把每条 # 指令回两遍。

    supervisor 重启与手动启动是最典型的双开路径。锁文件留 pid 痕迹；持锁进程
    死了（崩溃/被 kill）下一个实例接管，不会永远卡在陈锁上。
    """
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        return True
    except FileExistsError:
        try:
            pid = int(LOCK.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = 0
        if pid_alive(pid):
            log(f"另一个指令守护持有锁（pid={pid})，本实例退出")
            return False
        log(f"接管陈锁（pid={pid} 已死）")
        try:
            LOCK.unlink()
        except OSError:
            return False
        return acquire_lock()
    except OSError as exc:
        log(f"lock error: {exc}")
        return False


def release_lock():
    try:
        LOCK.unlink()
    except OSError:
        pass


def clear_stale_lock():
    """Drop the lock file a hard kill left behind.

    `taskkill /F` never lets `release_lock()` run, so every forced stop leaves a
    lock file naming a dead pid. `acquire_lock()` recovers from it on the next
    start, but until then the file is a lie: anyone reading state/ sees a lock
    that nothing holds. The stop path clears it so "stopped" means stopped.

    A live holder is never touched. Two instances racing between the kill and
    this call is exactly the case the lock exists to catch, and silently
    deleting a live lock would hand them both the same group.

    Returns the pid that was cleared, or None when there was nothing to clear.
    """
    try:
        pid = int(LOCK.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid_alive(pid):
        return None
    try:
        LOCK.unlink()
    except OSError:
        return None
    return pid


# slash_commands 段里属于平台（不是任何插件）的键。旧配置把插件配置和平台配置
# 放在同一段，PB 之后插件配置搬到顶层 plugins；这里只负责把两者分开，键名清单
# 与 DEFAULTS 一一对应。
_PLATFORM_KEYS = frozenset({
    "enabled", "prefix", "help_keywords", "cooldown_seconds",
    "ignore_at_self", "unknown_reply", "commands",
})


def _plugins_from(cfg):
    """插件三态 + options 归一（issue #4 PB）。宿主不认识任何插件名。

    两种写法等价，归一化后逐字段相等（``check_plugin_registry`` 直接比对两个 dict）：

    * ``slash_commands.<name>: {enabled, ck, ...}`` —— 旧键，除 ``enabled``/
      ``groups`` 外的键整体当 options。只认**注册表里有的**名字，避免把旧段里的
      杂键当成插件。
    * ``plugins.<name>: {enabled, groups, options}`` —— 新键。同名时新键整体覆盖
      旧键（不做字段级合并：合并会让「删掉旧键」变成一件看不出效果的事）。

    ``groups.<id>.plugins.<name>: false`` 归一成 ``group_off``：群级关闭表达的是
    「这个群不想看这个插件」，与 ``groups`` 白名单（只在这些群开）是两件事。

    第三种旧写法（issue #4 PD2a）：插件用 ``Plugin.legacy_section`` 声明自己
    历史上的**顶层**配置段（追问窗口的 ``"followup"``），宿主按旧键平铺读——
    ``enabled`` 进三态、其余键进 options。只认注册表里有的名字，优先级在新键
    与 ``slash_commands`` 旧键之后。它不进 ``plugins_legacy``：那个列表的
    --status 提示写死了 ``slash_commands.<name>``，顶层段不是同一种迁移。
    """
    section = cfg.get("slash_commands") or {}
    explicit = cfg.get("plugins") or {}
    known = {plugin.name for plugin in PLUGINS.plugins()}
    legacy = {name: entry for name, entry in section.items()
              if name in known and name not in explicit}
    sections = {}
    for plugin in PLUGINS.plugins():
        if (plugin.legacy_section and plugin.name not in explicit
                and plugin.name not in legacy):
            entry = cfg.get(plugin.legacy_section)
            if isinstance(entry, dict):
                sections[plugin.name] = entry

    group_off = {}
    for gid, gcfg in (cfg.get("groups") or {}).items():
        for name, on in ((gcfg or {}).get("plugins") or {}).items():
            if not on:
                group_off.setdefault(str(name), set()).add(str(gid))

    out = {}
    # 注册表里的插件先铺默认值（没配也要有 ``enabled``/``options``，否则
    # ``--status`` 与门控都得各自处理「键不存在」），再补配置里多出来的名字
    # （拼错的插件名也留一条，门禁才看得见）。
    order = [plugin.name for plugin in PLUGINS.plugins()]
    order += [name for name in explicit if name not in order]
    for name in order:
        if name in explicit:
            entry = explicit[name]
        elif name in legacy:
            entry = legacy[name]
        else:
            entry = sections.get(name)
        entry = entry if isinstance(entry, dict) else {}
        plugin = PLUGINS.get(str(name))
        options = dict(entry.get("options") or {})
        for key, value in entry.items():
            if key not in ("enabled", "groups", "options"):
                options[key] = value      # 旧键平铺写法
        scoped = entry.get("groups") or []
        if isinstance(scoped, (str, int)):
            scoped = [scoped]
        enabled = entry.get("enabled")
        out[str(name)] = {
            "enabled": bool(plugin.default_enabled if plugin else True)
                       if enabled is None else bool(enabled),
            "groups": [str(g).strip() for g in scoped if str(g).strip()],
            "group_off": sorted(group_off.get(str(name), ())),
            "options": {**dict(plugin.options_defaults if plugin else {}),
                        **(plugin.normalize(options) if plugin else options)},
        }
    return out, sorted(legacy)


def settings_from(cfg):
    """把 config.json 拍平成运行期设置。纯函数，检查脚本直接喂字典。

    插件配置走 ``_plugins_from``：默认值与类型归一都在插件自己那里
    （``Plugin.options_defaults`` / ``Plugin.normalize``），本函数只认三态。
    """
    section = cfg.get("slash_commands") or {}
    groups = {str(g) for g, gcfg in (cfg.get("groups") or {}).items()
              if (gcfg or {}).get("enabled")}
    plugins_map, legacy = _plugins_from(cfg)
    merged = dict(DEFAULTS)
    merged.update({
        "enabled": bool(section.get("enabled", DEFAULTS["enabled"])),
        "prefix": str(section.get("prefix") or DEFAULTS["prefix"]),
        "help_keywords": [str(k) for k in (section.get("help_keywords") or DEFAULTS["help_keywords"])],
        "cooldown_seconds": _positive(section.get("cooldown_seconds"), DEFAULTS["cooldown_seconds"]),
        "ignore_at_self": bool(section.get("ignore_at_self", DEFAULTS["ignore_at_self"])),
        "unknown_reply": str(section.get("unknown_reply") or ""),
        "commands": section.get("commands") or {},
        "plugins": plugins_map,
        "plugins_legacy": legacy,
        "ws_url": str(cfg.get("onebot_ws_url") or DEFAULTS["ws_url"]),
        "http_url": str(cfg.get("onebot_http_url") or DEFAULTS["http_url"]),
        "token": str(cfg.get("onebot_token") or ""),
        "self_account": str(cfg.get("self_account") or ""),
        "self_names": [str(n).strip() for n in (cfg.get("self_names") or ())
                       if str(n).strip()],
        "groups": groups,
    })
    return merged


def plugin_options(settings, name):
    """插件私有配置（``settings["plugins"][name]["options"]``）。"""
    return ((settings.get("plugins") or {}).get(name) or {}).get("options") or {}


def _positive(value, fallback):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number >= 0 else fallback


def load_settings():
    if str(ROOT / "water-group" / "daemon") not in sys.path:
        sys.path.insert(0, str(ROOT / "water-group" / "daemon"))
    # 只读配置，不 import up：那句 `import up` 会把整个 guard 树拖进本进程的
    # AST 闭包（issue #4 PA0，实测 26 个文件），改记忆引擎会连带重启指令守护。
    import wg_config  # noqa: PLC0415

    return settings_from(wg_config.config())


def plain_text(event):
    """事件的纯文本：数组段拼 text 段；退化到 raw_message 时剥掉 CQ 码。"""
    segs = event.get("message")
    if isinstance(segs, list):
        return "".join(str(s.get("data", {}).get("text", ""))
                       for s in segs if isinstance(s, dict) and s.get("type") == "text")
    return CQ_CODE.sub("", str(event.get("raw_message") or ""))


def command_key(text, prefix):
    """'#体力' → ('体力', '')；'#帮助 体力' → ('帮助', '体力')；无前缀 → None。"""
    if not text or not text.startswith(prefix):
        return None
    rest = text[len(prefix):].strip()
    if not rest:
        return None
    parts = rest.split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def at_self(event, self_ids):
    for seg in event.get("message") or []:
        if not isinstance(seg, dict) or seg.get("type") != "at":
            continue
        if str(seg.get("data", {}).get("qq", "")) in self_ids:
            return True
    return False


def should_handle(event, settings, self_ids):
    """四层过滤：群消息 → 群在白名单 → 非自身回灌 → 非 @自己 的混合消息。"""
    if event.get("post_type") != "message" or event.get("message_type") != "group":
        return False
    if str(event.get("group_id")) not in settings["groups"]:
        return False
    if str(event.get("user_id") or "") in self_ids:
        return False
    if settings["ignore_at_self"] and at_self(event, self_ids | {"all"}):
        return False
    return command_key(plain_text(event), settings["prefix"]) is not None


def cooldown_ok(last_at, slot, now, seconds):
    """同一群同一关键词的冷却。seconds<=0 表示不限。"""
    if seconds <= 0:
        return True
    return now - last_at.get(slot, 0.0) >= seconds


def new_state():
    """守护进程的去重/冷却账本。order 不带 maxlen——淘汰必须走显式 popleft，
    maxlen deque 的 append 会静默丢最旧项，seen 集合就永远清不干净。"""
    return {"seen": set(), "order": collections.deque(), "last_at": {}}


def mark_handled(state, message_id):
    """有界去重：WS 重连或重复投递时同一 msg_id 只回一次。"""
    if message_id in state["seen"]:
        return False
    state["seen"].add(message_id)
    state["order"].append(message_id)
    while len(state["order"]) > SEEN_MAX:
        state["seen"].discard(state["order"].popleft())
    return True


def help_text(settings, group_id=""):
    entries = dict(settings["commands"] or {})
    for key, (desc, _fn) in HANDLERS.items():
        # 插件门控（60s 白名单一类）没过就不该出现在帮助里：列了却不回，
        # 比不列更烦。与 resolve_reply 共用 registry.available，不再各写一遍。
        if not PLUGINS.available(key, settings, group_id):
            continue
        entries.setdefault(key, {"desc": desc})
    for key in settings["help_keywords"]:
        entries.setdefault(key, {"desc": "指令列表"})
    if not entries:
        return "暂无可用指令"
    prefix = settings["prefix"]
    lines = ["可用指令："]
    for key, entry in entries.items():
        desc = str((entry or {}).get("desc") or "")
        lines.append(f"{prefix}{key}" + (f" — {desc}" if desc else ""))
    return "\n".join(lines)


def build_message(text, quote_id=None):
    """与 message_sender.py:214-218 同一形状：reply 段在前，text 段在后。"""
    segments = []
    if quote_id:
        segments.append({"type": "reply", "data": {"id": str(quote_id)}})
    segments.append({"type": "text", "data": {"text": text}})
    return segments


def segments_for(reply, quote_id=None):
    """resolve_reply 的返回值 → OneBot message 段。

    纯文本走 build_message；{"text", "image"} 是扫码登录的二维码回复
    （image 是完整 image 段，或退化为 file:// 路径字符串）。
    """
    if not isinstance(reply, dict):
        return build_message(reply, quote_id)
    segments = []
    if quote_id:
        segments.append({"type": "reply", "data": {"id": str(quote_id)}})
    image = reply.get("image")
    if isinstance(image, dict):
        segments.append(image)
    elif image:
        segments.append({"type": "image", "data": {"file": str(image)}})
    segments.append({"type": "text", "data": {"text": str(reply.get("text") or "")}})
    return segments


def match_handler(key):
    """先整词命中；不中按最长前缀切——`#米游社胡桃` → `#米游社` + `胡桃`。

    Yunzai 那边指令本来就粘手（#原石10月、#米游社胡桃），要求带空格不现实；
    代价是 `#体力查` 这类 typo 会拿到参数错误提示而不是静默，有 CD 兜底。

    ASCII 键大小写不敏感：`#kfc` / `#b站` 也要命中 `#KFC` / `#B站`（手机英文键盘
    不一定是大写）。中文键的大小写映射是恒等，不受影响。
    """
    names = {k.lower(): k for k in HANDLERS}
    for size in range(len(key), 0, -1):
        head = key[:size]
        if head in HANDLERS:
            return head, key[size:]
        canonical = names.get(head.lower())
        if canonical:
            return canonical, key[size:]
    return None, ""


def resolve_reply(key, settings, event=None, args=""):
    """关键词 → 回复。None 表示不回复（未配置且未开 unknown_reply）。

    返回值两种形状：纯文本 str，或 {"text", "image"}（扫码登录的二维码回复，
    image 是 OneBot image 段）。优先动态 handler（取数 / 登录 / 资讯），再静态
    配置，最后 unknown_reply——PC 起登录指令也是插件指令，不再有前置分支。
    """
    group_id = str((event or {}).get("group_id") or "")
    if key in settings["help_keywords"]:
        return help_text(settings, group_id)
    handler_key, rest = match_handler(key)
    if handler_key:
        # 插件门控（issue #53 的 60s 白名单）：没开配置就静默落空，不走
        # unknown_reply 的吵人提示，也不冒充"未知指令"。
        if not PLUGINS.available(handler_key, settings, group_id):
            return settings["unknown_reply"] or None
        full_args = (rest + " " + args).strip() if (rest or args) else ""
        return run_handler(handler_key, HANDLERS[handler_key][1], settings, event, full_args)
    entry = (settings["commands"] or {}).get(key)
    if entry is None:
        return settings["unknown_reply"] or None
    text = str(entry.get("reply") or "")
    return text or None


def run_handler(key, fn, settings, event, args):
    """动态 handler 入口：组 ctx 时带用户维度（issue #6 §4.2 / #41-A）。

    优先级：本人绑定 → 全局 CK（仅 admin_qq 白名单 / --test 合成事件）→ 绑定提示。
    绑定检查**只针对需要凭证的 handler**（`fn.need_ck`，缺省 True）：#公告 / #活动 /
    #资讯 / #兑换码 / #米游社 / #攻略 / #抽卡记录 都是免登录接口（issue #2 附录 A
    §4/§6.2/§7），未绑定的群友照样能用——把它们一起拦掉是 P0 修复的过度收紧。

    凭证链路留在宿主（``plugins.host.resolve_credentials``）：测试探针 handler
    不在注册表里，也必须拿到 ctx.ck/uid 并触发 BIND_HINT。插件只在
    ``precheck`` / ``note_error`` 两个口子上补自己的域逻辑。
    """
    event = event or {}
    user_id = str(event.get("user_id") or "")
    need_ck = getattr(fn, "need_ck", True)
    owner = PLUGINS.owner(key)
    ctx = {"ck": [], "uid": "",
           "group_id": str(event.get("group_id") or ""), "user_id": user_id,
           # 插件 handler 只拿 (args, ctx)，配置从这里取（issue #4 PC：
           # #登录 要把 settings 交给 qr_login.start_login）。
           "settings": settings}
    hint = plugins.host.resolve_credentials(
        ctx, settings, user_id, need_ck, owner,
        provider=PLUGINS.credential_provider(settings))
    if hint:
        return hint
    precheck = owner.hooks.precheck if owner is not None else None
    if precheck is not None:
        blocked = precheck(ctx, settings)
        if blocked:
            return blocked
    try:
        return fn(args, ctx)
    except FETCH_ERRORS as exc:
        # 一次调用拿到全部副作用与通知：插件的失效提醒是**一次性**的
        # （mark_invalid 第二次返回 False），拆成两次调用会把提醒吃掉。
        notice = _note_error(ctx, settings, exc)
        if notice:
            notify_login(settings, ctx.get("group_id") or "", user_id, notice)
        return str(exc)
    except Exception as exc:  # noqa: BLE001  handler 炸了不能杀死监听
        log(f"handler #{key} failed: {type(exc).__name__}: {exc}")
        return "指令执行出错，稍后再试"


def _risk_blocked(ctx):
    """风控冷却期内不发请求，直接回文案（#55-C）。

    实现搬进米游社插件（``Hooks.cooldown``）；这个符号保留给
    ``check_miyoushe.py`` 直接调用，别删。
    """
    hook = PLUGINS.hook("cooldown")
    return hook(ctx) if hook is not None else ""


def _note_error(ctx, settings, exc):
    """域异常的统一出口：副作用（风控冷却 / 失效标记）在插件里，宿主只负责发通知。"""
    hook = PLUGINS.hook("note_error")
    return hook(ctx, settings, exc) if hook is not None else None


def _note_risk(ctx, exc):
    """命中 1034/10035/5003/10041 → 该账号进冷却，顺手换掉被标记的指纹。

    保留给 ``check_miyoushe.py`` 直接调用；``run_handler`` 走 ``_note_error``，
    不再分两次调用（失效提醒是一次性的，第二次会返回 False）。
    """
    _note_error(ctx, {}, exc)


def _note_ck_invalid(settings, ctx, qq, exc):
    """凭证失效（1001/10001/10103…）→ 标记 + 24h 内最多提醒一次。"""
    notice = _note_error(ctx, settings, exc)
    if notice:
        notify_login(settings, ctx.get("group_id") or "", qq, notice)


def send_group_msg(settings, group_id, segments):
    body = json.dumps({"group_id": int(group_id), "message": segments}).encode("utf-8")
    req = urllib.request.Request(
        settings["http_url"].rstrip("/") + "/send_group_msg", data=body,
        headers={"Authorization": "Bearer " + settings["token"],
                 "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def dispatch(event, settings, self_ids, state):
    """处理一条已通过过滤的事件。返回是否真的发出了回复。"""
    key, args = command_key(plain_text(event), settings["prefix"])
    if key is None:
        return False
    if not mark_handled(state, event.get("message_id")):
        return False
    user_id = str(event.get("user_id") or "")
    # 冷却 key 带上 user_id：避免 A 发完指令把 B 的同名指令静默吃掉（issue #66）。
    slot = f"{event.get('group_id')}:{user_id}:{key}"
    now = time.monotonic()
    owner = PLUGINS.owner(key)
    hooks = owner.hooks if owner is not None else None
    # 待确认流程的第二步（如 #解绑 的 60s 窗口内再发）豁免冷却闸门，
    # 否则 5s 冷却会把第二步静默丢弃（issue #66）。判定在插件里（issue #4 PC）。
    pending = hooks.pending(user_id) if (hooks is not None and hooks.pending) else frozenset()
    if key not in pending and not cooldown_ok(state["last_at"], slot, now, settings["cooldown_seconds"]):
        return False
    text = resolve_reply(key, settings, event, args)
    if text is None:
        return False
    state["last_at"][slot] = now
    segments = segments_for(text, event.get("message_id"))
    try:
        data = send_group_msg(settings, event["group_id"], segments)
    except Exception as exc:  # noqa: BLE001  发送失败不能杀死监听
        log(f"send failed group={event.get('group_id')} key={key}: {type(exc).__name__}: {exc}")
        if hooks is not None and hooks.on_send_failed is not None:
            # 回滚交给插件（二维码没发出去要撤会话、退额度，#41-E2）
            hooks.on_send_failed({"key": key, "user_id": user_id,
                                  "group_id": str(event.get("group_id") or "")}, settings)
        return False
    sent = (data.get("data") or {}).get("message_id")
    log(f"replied #{key} group={event.get('group_id')} user={event.get('user_id')} "
        f"msg={event.get('message_id')} -> {sent}")
    return True


def notify_login(settings, group_id, qq, text):
    """扫码登录的主动通知（§4.3）：@ 本人 + 文本，发到发起 #登录 的那个群。

    ``@ + 文本`` 的形状收在 ``Host.notify`` 里（插件回推同一条路），这里只剩
    日志与「通知失败不能杀死轮询线程」。``settings`` 只为保持调用签名——测试
    仍以 ``commands.notify_login`` 为打桩点。
    """
    try:
        PLUGINS.host.notify(group_id, qq, text)
        log(f"qr login notify group={group_id} qq={qq}")
    except Exception as exc:  # noqa: BLE001  通知失败不能杀死轮询线程
        log(f"qr notify failed group={group_id}: {type(exc).__name__}: {exc}")


def should_dispatch_command(settings, event, self_ids):
    """这条 `#` 指令要不要处理：总闸 + 四层过滤（issue #155）。

    总闸单独一层的理由：进程现在可能**只为事件型插件**而活着
    （``event_stream_needed``），那时 ``#`` 指令必须照旧静默——启动日志那句
    「只跑事件型插件（# 指令不回复）」要说到做到。``should_handle`` 只看消息
    形态与群白名单，它不知道总闸是开是关。
    """
    return bool(settings.get("enabled")) and should_handle(event, settings, self_ids)


def event_stream_needed(settings):
    """这个进程要不要起（issue #155）。

    历史总闸是 ``slash_commands.enabled``：关掉就整个不启动。事件型插件
    （``Plugin.needs_event_loop``）靠这条 WS 事件流吃饭，跟着一起死之后群里
    表现成「功能没了」，而日志里只有一行「不启动」。所以声明过那一位且当前
    启用的插件，能单独把事件流拉起来。

    ``group_id`` 传空串：只判总开关，不套群白名单——白名单是「这些群才跟」，
    不是「别的群就别起进程」。
    """
    if settings.get("enabled"):
        return True
    return any(plugin.needs_event_loop and plugin.enabled(settings)
               for plugin in PLUGINS.plugins())


def serve():
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    settings = load_settings()
    if not event_stream_needed(settings):
        log("slash_commands.enabled 为假且没有插件需要事件流，指令守护不启动")
        return 0
    if not settings["enabled"]:
        log("slash_commands.enabled 为假：只跑事件型插件（# 指令不回复）")
    if not acquire_lock():
        return 0
    # 扫码轮询线程与 WS 循环共享最新设置：热重载只换 box 里的引用，线程不用重启。
    # 线程常驻，扫码开关由插件的 cfg_getter 每 tick 重读（#41-D2）。
    SETTINGS_BOX["settings"] = settings
    # 插件的后台线程在这里起（不是 import 期的 load()）：此刻才有设置与发送通道。
    PLUGINS.start_all()
    try:
        return _serve(settings, connect, ConnectionClosed, SETTINGS_BOX)
    finally:
        PLUGINS.stop_all()
        _EMIT_POOL.shutdown()
        release_lock()


def plugin_counts(settings):
    """启动日志里的插件指纹：``名字:活跃指令数,名字:活跃指令数``。

    与 ``status_line`` 同一条口径：数**过门控**的指令（``plugin.enabled(settings)
    and cmd.available(settings)``），不再数注册数。改前写 ``sixty:14``（注册了 14
    条），``--status`` 写 ``sixty 停用(0 指令)``，启动行让人以为可用、探针说停用、
    #天气 又没反应——三方看起来打架（issue #259）。改后启动行也写 ``sixty:0``，
    与探针/运行期闸门一致。缺项仍是「插件没加载起来」（``load_error``）。
    """
    parts = []
    for plugin in PLUGINS.plugins():
        live = sum(1 for cmd in plugin.commands
                   if plugin.enabled(settings) and cmd.available(settings))
        parts.append(f"{plugin.name}:{live}")
    return ",".join(parts)


def _emit_group_message(event, settings, self_ids):
    """把一条群消息交给事件型插件；返回要发出去的文本就发（issue #155）。

    派发点在 ``should_handle`` 之前：``#`` 指令守护只认前缀，而事件型插件要看的
    是没有前缀的那些。发送走 ``PLUGINS.host.send``（与插件同一条通道，引用 / 编码 /
    失败处理都在宿主里），不是本进程私开一条 HTTP——否则「插件不碰传输」这条边界
    在这里就被绕开了。

    整段吞异常 + 耗时预算（issue #225）：插件 ``on_event`` 经 ``_EMIT_POOL.run`` 跑，
    ``EMIT_TIMEOUT`` 秒内拿不到结果就跳过本轮、记一行 ``event plugin slow: …ms``。
    这条预算挡的是「慢插件把紧随其后的 ``#`` 指令挡在帧队列里、群里看到发了指令
    完全没反应」——``#`` 指令没有任何「我收到了」的回执，被挡住就是静默停摆。
    ``beat()`` 排在派发之前仍照写心跳，所以慢插件期间 ``--status`` 仍报在线，诊断
    靠这一行慢日志。超时后 worker 仍在后台跑完那次卡死调用（daemon 兜底，进程退出
    不挂，见 ``wg_emit_pool``）。
    """
    if event.get("message_type") != "group":
        return None
    ctx = {"settings": settings, "event": event, "self_ids": set(self_ids)}
    started = time.monotonic()
    try:
        reply = _EMIT_POOL.run(lambda c: PLUGINS.emit("group_message", c),
                               ctx, EMIT_TIMEOUT)
    except Exception as exc:  # noqa: BLE001  插件判定炸了不能杀死监听
        log(f"group_message plugin failed: {type(exc).__name__}: {exc}")
        return None
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if reply is None:
        # ``run`` 超时返回 None；没人接 / 插件返回 None 也走这里，但那些亚毫秒
        # 返回，触发不了这条阈值。日志不写插件名——宿主不该知道是哪个插件接了
        # 这一手（同 app 侧「宿主里不许出现插件名字面量」）。
        if elapsed_ms >= int(EMIT_TIMEOUT * 1000) - 50:
            log(f"event plugin slow: {elapsed_ms}ms group={event.get('group_id')} "
                f"msg={event.get('message_id')} (budget={EMIT_TIMEOUT}s, skipped)")
        return None
    try:
        PLUGINS.host.send(str(event.get("group_id") or ""), str(reply))
        # 日志不写插件名：宿主不该知道是哪个插件接了这一手（同 app 侧
        # 「宿主里不许出现插件名字面量」）。要查是谁，看 `--status` 的插件行。
        log(f"event reply sent group={event.get('group_id')} "
            f"msg={event.get('message_id')} text={str(reply)[:40]!r}")
    except Exception as exc:  # noqa: BLE001  发送失败不能杀死监听
        log(f"event reply failed group={event.get('group_id')}: "
            f"{type(exc).__name__}: {exc}")
        return None
    return reply


def _serve(settings, connect, ConnectionClosed, box=None):
    # handler 计数写进启动日志：进程是不是旧代码，看这行数字对不对就知道（issue #68）。
    # 插件计数按 `名字:活跃指令数` 列出（issue #4 PA / #259）：与 status_line 同口径，
    # 关掉的插件写 :0、缺项才是没加载起来。
    log(f"commands start, prefix={settings['prefix']!r} "
        f"keywords={sorted(settings['commands'])} groups={sorted(settings['groups'])} "
        f"handlers={len(HANDLERS)} plugins={plugin_counts(settings)}")
    self_ids = {settings["self_account"]} - {""}
    state = new_state()
    backoff = 1
    while True:
        try:
            with connect(settings["ws_url"],
                         additional_headers={"Authorization": "Bearer " + settings["token"]}
                         if settings["token"] else {},
                         proxy=None, open_timeout=5, close_timeout=2,
                         max_size=1024 * 1024, max_queue=64,
                         ping_interval=20, ping_timeout=20) as ws:
                ws.send(json.dumps({"action": "get_login_info", "params": {}, "echo": "login"}))
                log(f"ws connected {settings['ws_url']}")
                backoff = 1
                reload_at = time.monotonic() + RELAY_SECONDS
                while True:
                    if wg_reload.soft_requested("commands"):
                        log("reload exit")
                        return 0
                    try:
                        raw = ws.recv(timeout=RECV_TIMEOUT)
                    except ConnectionClosed:
                        log("ws closed, reconnecting")
                        break
                    except Exception:  # noqa: BLE001  超时/帧异常不该杀死监听
                        beat()
                        if wg_reload.soft_requested("commands"):
                            log("reload exit")
                            return 0
                        continue
                    beat()
                    if wg_reload.soft_requested("commands"):
                        log("reload exit")
                        return 0
                    try:
                        data = json.loads(raw)
                    except ValueError:
                        continue
                    if data.get("echo") == "login" and data.get("status") == "ok":
                        account = str((data.get("data") or {}).get("user_id") or "")
                        if account:
                            self_ids = {account}
                            log(f"self_id={account}")
                        continue
                    if data.get("post_type") != "message":
                        continue
                    if time.monotonic() >= reload_at:
                        settings = load_settings()
                        if box is not None:
                            box["settings"] = settings
                        if not event_stream_needed(settings):
                            log("slash_commands.enabled 被关掉且没有插件需要事件流，退出")
                            return 0
                        reload_at = time.monotonic() + RELAY_SECONDS
                    # 事件型插件先看全量群消息（issue #155）：`should_handle` 只认
                    # `#` 前缀，而复读/接龙要判的恰恰是没有前缀的那些。派发点在
                    # 指令之前、与指令互不影响——插件自己过三态门控与群白名单，
                    # 宿主不替它做选择（见 base.Hooks.on_event）。
                    _emit_group_message(data, settings, self_ids)
                    if should_dispatch_command(settings, data, self_ids):
                        dispatch(data, settings, self_ids, state)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001  握手失败/WS 断开：退避重连
            log(f"ws error: {type(exc).__name__}: {exc}")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)
        beat()


def run_test(text):
    """--test：合成事件直跑一个关键词（可带参数），实机验证取数指令。

    机器人自己发的 # 指令会被自过滤挡掉（reportSelfMessage 回灌保护），群里没法
    自问自答；这个口子就是手动验证的通道。回复发到首个启用群，不带引用。
    """
    settings = load_settings()
    if not settings["enabled"]:
        print("slash_commands.enabled 为假")
        return 1
    prefix = settings["prefix"]
    if not text.startswith(prefix):
        text = prefix + text
    parsed = command_key(text, prefix)
    if not parsed:
        print(f"没解析到关键词（要 {prefix} 开头）")
        return 1
    key, args = parsed
    groups = sorted(settings["groups"])
    if not groups:
        print("没有启用的群")
        return 1
    event = {"post_type": "message", "message_type": "group",
             "group_id": int(groups[0]), "user_id": 0, "message_id": None}
    # 宿主发送通道读的是这个 box；--test 是独立进程，不设就拿不到 onebot 地址。
    SETTINGS_BOX["settings"] = settings
    reply = resolve_reply(key, settings, event, args)
    if reply is None:
        print(f"#{key} 不产生回复")
        return 1
    print(reply.get("text") if isinstance(reply, dict) else reply)
    try:
        data = send_group_msg(settings, groups[0], segments_for(reply, None))
    except Exception as exc:  # noqa: BLE001
        print(f"发送失败: {type(exc).__name__}: {exc}")
        return 1
    print(f"已发到群 {groups[0]}，msg_id={(data.get('data') or {}).get('message_id')}")
    return 0


def main():
    winconsole.ensure_utf8_stdio()
    parser = argparse.ArgumentParser(description="# 指令守护：群聊 slash command 脚本直回")
    parser.add_argument("--test", metavar="关键词",
                        help="合成事件直跑一个关键词（可带参数），回复发到首个启用群")
    args = parser.parse_args()
    for name, error in PLUGINS.load_errors.items():
        log(f"plugin {name} unavailable: {error}")
    if args.test:
        return run_test(args.test)
    try:
        return serve()
    except KeyboardInterrupt:
        log("commands stop")
        return 0


if __name__ == "__main__":
    sys.exit(main())
