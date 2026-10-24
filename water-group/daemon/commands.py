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

回复内容四个来源：config 的静态映射（slash_commands.commands）、插件注册表的
动态 handler（米游社取数 issue 方案 D、60s 资讯 issue #53 方案 A，都经
``plugins/`` 适配器注册）、qr_login.py 的登录指令（#登录/#登录状态/#解绑，
issue #6：每用户扫码绑定）。本文件只留传输与仲裁（前缀、冷却、去重、引用、
凭证链路、发送），域知识一律在插件里（issue #4 PA）。

issue #6 的两条硬规则：
  - §4.2 隐私缺口：取数 ctx 带用户维度，未绑定的群友**不再**回落全局 CK
    （否则任何群友发 #体力 看到的都是管理员的账号）；全局 CK 只留给
    slash_commands.miyoushe.admin_qq 白名单与 --test 合成事件；
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
from wg_paths import logs_dir, state_dir
STATE_DIR = state_dir()
LOG = logs_dir() / "wg_commands.log"
HEARTBEAT = STATE_DIR / "wg_commands_heartbeat"

RELAY_SECONDS = 30        # 配置热重载周期
RECV_TIMEOUT = 2          # WS 单次 recv 超时（热重载检查点；心跳节拍）
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
    "miyoushe": {"ck": [], "uid": "", "admin_qq": [],
                 "qr_login": {"enabled": True, "daily_limit": 5, "session_ttl": 300}},
    "sixty": {"enabled": False, "base_url": "", "timeout": 0,
              "max_items": 5, "default_city": "", "allow": None},
    "ws_url": "ws://127.0.0.1:43001",
    "http_url": "http://127.0.0.1:43000",
    "token": "",
    "self_account": "",
    "groups": set(),
}

def log(line):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


# 插件在 import 期装配（issue #4 PA）：契约在 plugins/base.py，注册表在
# plugins/registry.py，米游社/60s 的适配器在 plugins/*_plugin.py。
# 显式静态 import 是硬要求——check_hot_reload 靠 AST 闭包把改动映射到进程。
import plugins  # noqa: E402

PLUGINS = plugins.load_all(log=log)

# 动态 handler 合成一张表（米游社取数 + 60s 资讯，issue #53）。合并成模块级
# 名字而不是在 resolve_reply 里现拼：测试直接改 commands.HANDLERS 打桩。
HANDLERS = PLUGINS.commands()

# 取数层异常的统一出口：插件声明自己的域异常，宿主统一「str(exc) 当回复」。
FETCH_ERRORS = PLUGINS.error_types()

# §4.2：未绑定群友的取数指令统一回这句，不再拿管理员的全局 CK 顶。
BIND_HINT = plugins.BIND_HINT

try:
    import qr_login
except Exception as _exc:  # noqa: BLE001  登录模块坏了不该拖垮取数与静态指令
    qr_login = None
    _LOGIN_LOAD_ERROR = f"{type(_exc).__name__}: {_exc}"
else:
    _LOGIN_LOAD_ERROR = ""

# 扫码登录指令（issue #6 §4.5）。#解绑 是两步确认（60 秒内再发一次即生效，见
# qr_login.unbind）——只认字面量「确认」会把群友卡在提示里，实机踩过；
# #绑定 只改 active_uid——UID 是取数参数不是凭证，没 CK 时单绑 UID 取不到数据。
# PC 会把这一整块迁成插件；本轮保留原样，先把契约与两个取数插件落地。
LOGIN_DESCS = {
    "登录": "扫码绑定米游社账号（凭证只存本机）",
    "登录状态": "查看自己的绑定情况",
    "绑定": "绑定/切换游戏 UID（需先 #登录）",
    "解绑": "解绑米游社账号（再发一次即确认）",
}


def beat():
    try:
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass


def pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:  # noqa: BLE001  探测失败按死处理，让锁可被接管
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


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


def settings_from(cfg):
    """把 config.json 拍平成运行期设置。纯函数，检查脚本直接喂字典。"""
    section = cfg.get("slash_commands") or {}
    groups = {str(g) for g, gcfg in (cfg.get("groups") or {}).items()
              if (gcfg or {}).get("enabled")}
    mys = section.get("miyoushe") or {}
    ck = mys.get("ck") or []
    if isinstance(ck, str):
        ck = [ck] if ck.strip() else []
    admin = mys.get("admin_qq") or []
    if isinstance(admin, (str, int)):
        admin = [admin]
    qr = mys.get("qr_login") or {}
    six = section.get("sixty") or {}
    allow = six.get("allow")
    if allow is not None:  # None = 用 sixty.DEFAULT_ALLOW；[] = 全禁
        allow = [str(k).strip() for k in allow if str(k).strip()]
    merged = dict(DEFAULTS)
    merged.update({
        "enabled": bool(section.get("enabled", DEFAULTS["enabled"])),
        "prefix": str(section.get("prefix") or DEFAULTS["prefix"]),
        "help_keywords": [str(k) for k in (section.get("help_keywords") or DEFAULTS["help_keywords"])],
        "cooldown_seconds": _positive(section.get("cooldown_seconds"), DEFAULTS["cooldown_seconds"]),
        "ignore_at_self": bool(section.get("ignore_at_self", DEFAULTS["ignore_at_self"])),
        "unknown_reply": str(section.get("unknown_reply") or ""),
        "commands": section.get("commands") or {},
        "sixty": {
            "enabled": bool(six.get("enabled", False)),
            "base_url": str(six.get("base_url") or "").strip(),
            "timeout": _positive(six.get("timeout"), 0),
            "max_items": int(_positive(six.get("max_items"), 5)) or 5,
            "default_city": str(six.get("default_city") or "").strip(),
            "allow": allow,
        },
        "miyoushe": {
            "ck": [str(c) for c in ck if str(c).strip()],
            "uid": str(mys.get("uid") or ""),
            "admin_qq": [str(q).strip() for q in admin if str(q).strip()],
            "qr_login": {
                "enabled": bool(qr.get("enabled", True)),
                "daily_limit": int(_positive(qr.get("daily_limit"), 5)),
                "session_ttl": _positive(qr.get("session_ttl"), 300),
            },
        },
        "ws_url": str(cfg.get("onebot_ws_url") or DEFAULTS["ws_url"]),
        "http_url": str(cfg.get("onebot_http_url") or DEFAULTS["http_url"]),
        "token": str(cfg.get("onebot_token") or ""),
        "self_account": str(cfg.get("self_account") or ""),
        "groups": groups,
    })
    return merged


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
    for key, desc in LOGIN_DESCS.items():
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
    image 是 OneBot image 段）。优先登录指令，再动态 handler（米游社取数），
    再静态配置，最后 unknown_reply。
    """
    group_id = str((event or {}).get("group_id") or "")
    if key in settings["help_keywords"]:
        return help_text(settings, group_id)
    if key in LOGIN_DESCS:
        return login_reply(key, args, settings, event)
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


def login_reply(key, args, settings, event):
    """#登录 / #登录状态 / #绑定 / #解绑（issue #6 §4.5）。"""
    if _LOGIN_LOAD_ERROR:
        log(f"login unavailable: {_LOGIN_LOAD_ERROR}")
        return "登录功能暂不可用（模块加载失败），稍后再试"
    event = event or {}
    user_id = str(event.get("user_id") or "")
    if not user_id or user_id == "0":
        return "这条指令要群友本人在群里发（合成测试事件没有真实 QQ）"
    if key == "绑定":
        return qr_login.bind_uid(user_id, args)
    if key == "登录":
        mys = settings.get("miyoushe") or {}
        if not (mys.get("qr_login") or {}).get("enabled", True):
            return "扫码登录未开启（config → slash_commands.miyoushe.qr_login.enabled）"
        try:
            session = qr_login.start_login(settings, user_id, event.get("group_id"))
        except qr_login.QrLoginError as exc:
            return str(exc)
        return {"text": "5 分钟内用米游社 App 扫这张码\n"
                        "凭证只加密存本机，机器人不会发出去；随时 #解绑 可删",
                "image": qr_login.qr_image_segment(session["url"])}
    if key == "登录状态":
        return qr_login.status_text(user_id)
    # 两步确认在 qr_login.unbind 里收口：窗口内再发一次裸 #解绑 也算确认。
    # 原来这里硬比 args=="确认"，而提示文案让人「再发一次 #解绑」，于是死循环。
    return qr_login.unbind(user_id, args)


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
           "group_id": str(event.get("group_id") or ""), "user_id": user_id}
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
    # 待确认流程的第二步（如 #解绑 的 60s 窗口内再发）豁免冷却闸门，
    # 否则 5s 冷却会把第二步静默丢弃（issue #66）。
    pending_keys = (qr_login.pending_confirmation_keys(user_id)
                    if qr_login is not None else frozenset())
    if key not in pending_keys and not cooldown_ok(state["last_at"], slot, now, settings["cooldown_seconds"]):
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
        if key == "登录" and qr_login is not None:
            # 二维码没发出去：撤销会话并退还本次额度，别让用户白等 5 分钟
            # 才收到「已过期」，也别白扣当天 5 次里的一次（#41-E2）。
            qr_login.discard_session(event.get("user_id"))
        return False
    sent = (data.get("data") or {}).get("message_id")
    log(f"replied #{key} group={event.get('group_id')} user={event.get('user_id')} "
        f"msg={event.get('message_id')} -> {sent}")
    return True


def notify_login(settings, group_id, qq, text):
    """扫码登录的主动通知（§4.3）：@ 本人 + 文本，发到发起 #登录 的那个群。"""
    segments = [{"type": "at", "data": {"qq": str(qq)}},
                {"type": "text", "data": {"text": " " + text}}]
    try:
        send_group_msg(settings, group_id, segments)
        log(f"qr login notify group={group_id} qq={qq}")
    except Exception as exc:  # noqa: BLE001  通知失败不能杀死轮询线程
        log(f"qr notify failed group={group_id}: {type(exc).__name__}: {exc}")


def serve():
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    settings = load_settings()
    if not settings["enabled"]:
        log("slash_commands.enabled 为假，指令守护不启动")
        return 0
    if not acquire_lock():
        return 0
    # 扫码轮询线程与 WS 循环共享最新设置：热重载只换 box 里的引用，线程不用重启。
    # 线程常驻，`qr_login.enabled` 由 cfg_getter 每 tick 重读（#41-D2）。
    box = {"settings": settings}
    if qr_login is not None:
        qr_login.start_poller(
            lambda group_id, qq, text: notify_login(box["settings"], group_id, qq, text),
            cfg_getter=lambda: ((box["settings"].get("miyoushe") or {})
                                .get("qr_login") or {}))
        log("qr-login poller started")
    try:
        return _serve(settings, connect, ConnectionClosed, box)
    finally:
        release_lock()


def plugin_counts():
    """启动日志里的插件指纹：``miyoushe:17,sixty:14``。缺项 = 那个插件没加载起来。"""
    return ",".join(f"{plugin.name}:{len(plugin.commands)}"
                    for plugin in PLUGINS.plugins())


def _serve(settings, connect, ConnectionClosed, box=None):
    # handler 计数写进启动日志：进程是不是旧代码，看这行数字对不对就知道（issue #68）。
    # 插件计数按 `名字:指令数` 列出（issue #4 PA），坏掉的插件在这里是 0 或缺项。
    log(f"commands start, prefix={settings['prefix']!r} "
        f"keywords={sorted(settings['commands'])} groups={sorted(settings['groups'])} "
        f"handlers={len(HANDLERS)} plugins={plugin_counts()}")
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
                        if not settings["enabled"]:
                            log("slash_commands.enabled 被关掉，退出")
                            return 0
                        reload_at = time.monotonic() + RELAY_SECONDS
                    if should_handle(data, settings, self_ids):
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
    if _LOGIN_LOAD_ERROR:
        log(f"qr login unavailable: {_LOGIN_LOAD_ERROR}")
    if args.test:
        return run_test(args.test)
    try:
        return serve()
    except KeyboardInterrupt:
        log("commands stop")
        return 0


if __name__ == "__main__":
    sys.exit(main())
