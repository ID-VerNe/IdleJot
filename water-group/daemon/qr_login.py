"""每用户 CK 扫码登录（issue #6 / .plan/user-ck-qr-login.md）：会话表 + 单轮询线程。

链路：群友发 `#登录` → 本模块向米游社 passport 申请二维码（App 流优先，Web 流回退）
→ commands.py 把二维码图片段发回群 → 群友用米游社 App 扫一扫 → 后台轮询线程看到
Confirmed → 取凭据、补 LToken、发现 uid → user_store 加密落盘 → 主动回群通知。

边界（写死在文档里，别悄悄扩）：
  - 凭证只进 user_store（DPAPI 加密文件），本模块的日志/通知/异常文案绝不出现
    auth 任何字段——审计日志只记 QQ 号、状态与 retcode（§5.6）；
  - 会话表是进程内的，重启即失效（二维码 5 分钟过期，可接受，§7.7）；
  - 轮询必须放后台线程：handler 里同步等会卡住 WS 事件循环，同秒的其他群消息
    全部延迟（§4.3）；
  - 取数仍走 miyoushe.py，本模块不 import 它——取数层坏了不该拖垮登录。
"""
import base64
import collections
import io
import json
import os
import re
import threading
import time
import urllib.request
import uuid

import user_store
from wg_paths import logs_dir, state_dir

APP_ID = "bll8iq97cem8"
APP_VERSION = "2.90.1"
CLIENT_TYPE = "3"
UA_APP = "Mozilla/5.0 miHoYoBBS/2.90.1 Capture/2.2.0"
UA_WEB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "Chrome/124.0.0.0 Safari/537.36")

# stoken 换票（getLTokenBySToken / getCookieAccountInfoBySToken）和 roles 查询走的是
# 另一套约定：GET、无 query，stoken/mid/stuid 全在 Cookie 里，client_type=1，
# UA 是 H5 的 Hyperion（#41 实机 .tmp/diag_token2.py 验证）。
# 和上面 App 流（client_type=3、UA miHoYoBBS）混用会 405 / retcode=-100。
H5_CLIENT_TYPE = "1"
H5_APP_VERSION = "2.63.1"
UA_H5 = "Hyperion/275 CFNetwork/1402.0.8 Darwin/22.2.0"
TIMEOUT = 10
SESSION_TTL = 300.0        # 二维码有效期（§5.3：二维码即口令，5 分钟作废）
POLL_INTERVAL = 2.0        # 轮询间隔（§4.3）
DAILY_LIMIT = 5            # 每用户每天扫码次数（§4.5）
BOUND_COOLDOWN = 24 * 3600  # 绑定成功后 24h 内禁止重复发起（§4.5）
STOKEN_TTL = 30 * 86400    # stoken 有效期估计值（§6 P2；只用于到期提示，不参与判定）
RENEW_EVERY = 15 * 86400   # 惰性续期间隔：半个月用 stoken 换一次新 LToken
QR_SCALE = 6

# passport 双流 + UID 发现。takumi 与 miyoushe.HOSTS 同址，本模块独立成表：
# 取数层坏了不该拖垮登录，测试也只重定向这一张表。
HOSTS = {
    "app": "https://passport-api.mihoyo.com",
    "web": "https://passport-api.miyoushe.com",
    "takumi": "https://api-takumi.mihoyo.com",
}

WAITING = ("Created", "Scanned")
GAME_BIZ = {"hk4e_cn": "genshin", "hkrpg_cn": "starrail", "nap_cn": "zzz"}
GAME_NAMES = {"genshin": "原神", "starrail": "星铁", "zzz": "绝区零"}
GAME_ALIASES = {"原神": "genshin", "星铁": "starrail", "星穹铁道": "starrail",
                "绝区零": "zzz"}
# 只有原神的取数指令真正消费 active_uid（#体力/#深渊/#原石… 全走 genshin 那一套）。
# 星铁指令还没接（issue #42 A10）；绝区零的 #绝区零体力 读到的 ctx.uid 仍是原神 uid，
# 允许绑定只会让人以为「绑好了能用」，实际查的是错的号（#41-E4）。
BINDABLE_GAMES = ("genshin",)
QR_RETCODE_MSG = {
    1034: "米游社要求验证（验证码），请稍后再试或换设备",
}

LOG = logs_dir() / "qr_login.log"
# 每日额度落盘（#41-E1）：进程重启不该把「今天已扫 5 次」清零。
DAILY_FILE = state_dir() / "qr_login_daily.json"
STATUS_CD = 30.0           # #登录状态 的独立冷却（§4.5）
UNBIND_WINDOW = 60.0       # #解绑 两步确认的窗口
UNBIND_WORDS = ("确认", "确定", "是", "yes", "y", "ok")

# 进程内会话表：qq → {ticket, url, device_id, ...}。守护是单进程，模块级单例
# 与 miyoushe._CACHE 同一风格；poller 线程与 WS 循环共享。
SESSIONS = {}
_DAILY = collections.defaultdict(list)
_NET_ERRORS = {}           # qq → 连续网络失败次数（只用于审计限频）
_STATUS_AT = {}            # qq → 上次 #登录状态 时间戳
_UNBIND_AT = {}            # qq → 上次 #解绑 提示确认的时间戳（§4.5 两步确认）


class QrLoginError(Exception):
    """带用户可读文案的登录失败——文案直接当群回复发出。code 只给审计日志。"""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 每日额度


def _load_daily():
    """读回 24h 内的扫码额度；文件缺失/损坏按空处理（额度宁可从宽，不能卡住人）。"""
    try:
        raw = json.loads(DAILY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    now = time.time()
    out = {}
    for qq, stamps in (raw or {}).items():
        values = [float(t) for t in (stamps or [])
                  if isinstance(t, (int, float)) and now - float(t) < 86400]
        if values:
            out[str(qq)] = values
    return out


def _save_daily():
    """原子写回：崩溃不留半个 JSON（与 user_store 同一手法）。"""
    try:
        DAILY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = DAILY_FILE.with_name(DAILY_FILE.name + ".tmp")
        tmp.write_text(json.dumps({k: v for k, v in _DAILY.items() if v},
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, DAILY_FILE)
    except OSError:
        pass


def _daily_recent(qq, now):
    values = [t for t in (_DAILY.get(qq) or []) if now - t < 86400]
    _DAILY[qq] = values
    return values


def _daily_add(qq, now):
    _DAILY[qq].append(now)
    _save_daily()


def _daily_drop_last(qq):
    """回滚一次额度：二维码没发出去就不该算用过（#41-E2）。"""
    values = _DAILY.get(qq) or []
    if values:
        values.pop()
        _save_daily()


_DAILY.update(_load_daily())


# ---------------------------------------------------------------- 审计与出图


def audit(line):
    """审计日志（§5.6）：只记 QQ 号与状态，绝不记凭证字段。"""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def qr_image_segment(qr_url):
    """二维码内容 → OneBot 图片段（segno 出 PNG，base64://）。

    NapCat 支持 base64 图片段，但本项目只发过文本段——真群实测前这是 §7.2
    未验证点；不通则退 file:// 本地路径（见 doc/SLASH_COMMANDS.md）。
    """
    import segno  # 延迟导入：取数指令不依赖二维码库

    buf = io.BytesIO()
    segno.make(qr_url, error="m").save(buf, kind="png", scale=QR_SCALE)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return {"type": "image", "data": {"file": "base64://" + b64}}


# ---------------------------------------------------------------- 请求层


def _post(url, headers, payload=None):
    """POST JSON，返回 (解析后的 body, 响应头)。响应头要留给 Set-Cookie 兜底解析。"""
    body = (json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            .encode("utf-8") if payload is not None else None)
    merged = dict(headers or {})
    merged.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=merged)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return (json.loads(raw) if raw.strip() else {}), resp.headers
    except Exception as exc:  # noqa: BLE001  网络层失败统一翻译，不重试
        # code="network" 是给 poll_once 的信号：网络抖动不是「用户做错了什么」，
        # 不能清会话、不能耗额度（#41-D1）。
        raise QrLoginError("米游社登录服务无响应，稍后再试", code="network") from exc


def _get(url, headers):
    """GET（无 query、无 body），返回 (body, 响应头)。换票接口只认这个形状。"""
    req = urllib.request.Request(url, headers=dict(headers or {}))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return (json.loads(raw) if raw.strip() else {}), resp.headers
    except Exception as exc:  # noqa: BLE001  与 _post 同一套翻译
        raise QrLoginError("米游社登录服务无响应，稍后再试", code="network") from exc


def _ck_headers(auth):
    """stoken 换票的认证头：stoken/mid/stuid 三件套放进 Cookie。

    取数层的 CK 串是 `ltuid=…; ltoken=…` 那套，换票接口偏偏认 `stoken/mid/stuid`，
    所以这里单独拼，不复用 user_store.ck_string。
    """
    auth = auth if isinstance(auth, dict) else {}
    pairs = []
    for name, key in (("stoken", "stoken"), ("mid", "account_mid_v2")):
        value = str(auth.get(key) or "").strip()
        if value:
            pairs.append(f"{name}={value}")
    stuid = str(auth.get("stuid") or auth.get("ltuid") or auth.get("account_id") or "").strip()
    if stuid:
        pairs.append(f"stuid={stuid}")
    return {
        "Cookie": "; ".join(pairs),
        "x-rpc-client_type": H5_CLIENT_TYPE,
        "x-rpc-game_biz": "bbs_cn",
        "x-rpc-app_version": H5_APP_VERSION,
        "User-Agent": UA_H5,
    }


def _exchange(url, auth):
    """换票请求 → data 字典；retcode 非 0 抛 QrLoginError（沿用统一文案映射）。"""
    payload, _headers = _get(url, _ck_headers(auth))
    data = _check_retcode(payload).get("data")
    return data if isinstance(data, dict) else {}


def _app_headers(device_id):
    return {
        "x-rpc-app_id": APP_ID,
        "x-rpc-device_id": device_id,
        "x-rpc-client_type": CLIENT_TYPE,
        "x-rpc-app_version": APP_VERSION,
        "User-Agent": UA_APP,
    }


def _web_headers(device_id):
    return {"x-rpc-app_id": APP_ID, "x-rpc-device_id": device_id, "User-Agent": UA_WEB}


def _headers_for(stream, device_id):
    return _app_headers(device_id) if stream == "app" else _web_headers(device_id)


def _retcode(payload):
    if not isinstance(payload, dict):
        raise QrLoginError("米游社返回了无法解析的数据")
    return payload.get("retcode")


def _check_retcode(payload):
    code = _retcode(payload)
    if code in (0, None):
        return payload
    raise QrLoginError(QR_RETCODE_MSG.get(
        code, f"登录服务返回异常（retcode={code}），重新发 #登录 试试"), code=code)


def _find_key(node, key):
    """在响应 JSON 里递归找 key。

    Confirmed 的 stoken / LToken 位置官方没文档（§7.1/§7.4：App 流在响应体、
    Web 流在 Set-Cookie），按字段名递归兜底，上游挪位置也不用改代码。
    返回值可能是标量（stoken/ltoken）或字典（cookie），由调用方按类型处理。
    """
    if isinstance(node, dict):
        value = node.get(key)
        if value is not None and not (isinstance(value, (str, int)) and not str(value).strip()):
            return value
        for item in node.values():
            found = _find_key(item, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


def _parse_cookie_string(blob):
    out = {}
    for pair in str(blob or "").split(";"):
        name, _, value = pair.strip().partition("=")
        name = name.strip()
        if name in user_store.AUTH_FIELDS and value.strip():
            out.setdefault(name, value.strip())
    return out


def _parse_set_cookie(headers):
    """Set-Cookie → auth 字段（Web 流的主要来源，§3.2）。"""
    values = []
    if headers is not None:
        getter = getattr(headers, "get_all", None)
        if callable(getter):
            values = getter("Set-Cookie") or []
        elif headers.get("Set-Cookie"):
            values = [headers.get("Set-Cookie")]
    out = {}
    for raw in values:
        out.update(_parse_cookie_string(raw))
    return out


def _app_stream_auth(payload):
    """App 流 Confirmed 的真实形状（#41 实机 07:17 确认）。

    App 流**不下发 Cookie**，也没有叫 `stoken` 的字段：

        {"data": {"status": "Confirmed",
                  "user_info": {"aid": "…", "mid": "…"},
                  "tokens": [{"token_type": 2, "token": "…"},
                             {"token_type": 1, "token": "<stoken>"}]}}

    `token_type == 1` 的那一项才是 stoken。原来的实现只按字段名找 `stoken`，
    于是扫完明明成功了却回「没拿到登录凭证」。这里把 uid/mid 一并落成旧版字段，
    ltoken 继续交给 `_finish` 用 stoken 换（getLTokenBySToken）。
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {}
    info = data.get("user_info") if isinstance(data.get("user_info"), dict) else {}
    out = {}
    uid = str(info.get("aid") or info.get("uid") or info.get("account_id") or "").strip()
    mid = str(info.get("mid") or "").strip()
    if uid:
        out.update({"ltuid": uid, "account_id": uid, "stuid": uid})
    if mid:
        out["account_mid_v2"] = mid
    tokens = data.get("tokens") if isinstance(data.get("tokens"), list) else []
    for item in tokens:
        if not isinstance(item, dict) or str(item.get("token_type")) != "1":
            continue
        token = str(item.get("token") or "").strip()
        if token:
            out["stoken"] = token
            break
    return out


def _shape_of(node, depth=0, limit=3):
    """响应结构 → 键路径串。**只记键名不记值**，仅用于「扫完没拿到凭证」的定位。"""
    if depth >= limit or not isinstance(node, dict):
        return ""
    parts = []
    for key, value in list(node.items())[:12]:
        if isinstance(value, dict):
            inner = _shape_of(value, depth + 1, limit)
            parts.append(f"{key}{{{inner}}}" if inner else key)
        elif isinstance(value, list):
            parts.append(f"{key}[{len(value)}]")
        else:
            parts.append(key)
    return ",".join(parts)


def _extract_auth(payload, headers):
    """Confirmed 响应 → auth dict。响应头 / 响应体 / App 流三处兜底（§7.4）。"""
    auth = _parse_set_cookie(headers)
    stoken = _find_key(payload, "stoken")
    if isinstance(stoken, (str, int)) and str(stoken).strip():
        auth.setdefault("stoken", str(stoken).strip())
    body_cookie = _find_key(payload, "cookie")
    if isinstance(body_cookie, str):
        auth.update(_parse_cookie_string(body_cookie))
    elif isinstance(body_cookie, dict):
        auth.update({str(k): str(v) for k, v in body_cookie.items()})
    for key, value in _app_stream_auth(payload).items():
        auth.setdefault(key, value)
    return user_store.normalize_auth(auth)


# ---------------------------------------------------------------- passport 双流


def create_qr(stream, device_id):
    """申请二维码 → (url, ticket)。App 流优先，失败由调用方回退 Web 流。"""
    url = HOSTS[stream] + f"/account/ma-cn-passport/{stream}/createQRLogin"
    payload, _headers = _post(url, _headers_for(stream, device_id), {})
    data = _check_retcode(payload).get("data") or {}
    qr_url = str(data.get("url") or "")
    ticket = str(data.get("ticket") or "")
    if not qr_url or not ticket:
        raise QrLoginError("米游社没返回二维码，稍后再试")
    return qr_url, ticket


def query_status(stream, ticket, device_id):
    """轮询扫码状态 → (status, payload, headers)。"""
    url = HOSTS[stream] + f"/account/ma-cn-passport/{stream}/queryQRLoginStatus"
    payload, headers = _post(url, _headers_for(stream, device_id), {"ticket": ticket})
    _check_retcode(payload)
    status = str(((payload.get("data") or {}).get("status")) or "")
    return status, payload, headers


def exchange_ltoken(auth, device_id=""):
    """stoken → ltoken/ltuid（§3.3）。Phase 2 自动续期也走这条。

    实机（#41 07:22 回归）：`POST` + JSON body → `405 Method Not Allowed`，
    `GET ?stoken=` → `retcode=-100 登录状态失效`；只有 **GET + Cookie 认证**
    （见 `_ck_headers`）才回 `retcode=0`。device_id 留着只为兼容旧调用点。
    """
    auth = auth if isinstance(auth, dict) else {"stoken": str(auth or "")}
    if not str(auth.get("stoken") or "").strip():
        return {}
    data = _exchange(HOSTS["app"] + "/account/auth/api/getLTokenBySToken", auth)
    ltoken = _find_key(data, "ltoken")
    ltuid = _find_key(data, "ltuid")
    if ltoken is None:
        raise QrLoginError("stoken 没换到 LToken")
    out = {"ltoken": str(ltoken)}
    if ltuid is not None:
        out["ltuid"] = str(ltuid)
    return out


def fetch_cookie_token(auth):
    """stoken → cookie_token（+ 账号 uid）。CK 串缺这一项，取数接口判凭证失效。"""
    auth = auth if isinstance(auth, dict) else {"stoken": str(auth or "")}
    if not str(auth.get("stoken") or "").strip():
        return {}
    data = _exchange(HOSTS["web"] + "/account/auth/api/getCookieAccountInfoBySToken", auth)
    out = {}
    token = _find_key(data, "cookie_token")
    if token:
        out["cookie_token"] = str(token)
    uid = str(_find_key(data, "uid") or "").strip()
    if uid:
        # 只补缺的：账号 id 以 Confirmed 里的 user_info 为准，别用换票结果覆盖
        for key in ("account_id", "ltuid", "stuid"):
            if not str(auth.get(key) or "").strip():
                out[key] = uid
    if not out:
        raise QrLoginError("stoken 没换到 cookie_token")
    return out


def derive_ck(auth):
    """stoken → 完整 CK 串所需字段。缺哪个补哪个，单个失败不影响其他（§7.1 降级）。"""
    auth = dict(auth or {})
    if not str(auth.get("stoken") or "").strip():
        return user_store.normalize_auth(auth)
    for label, fetch in (("ltoken", exchange_ltoken), ("cookie_token", fetch_cookie_token)):
        if str(auth.get(label) or "").strip():
            continue
        try:
            for key, value in fetch(auth).items():
                auth.setdefault(key, value)
        except QrLoginError as exc:
            # 降级：能换多少换多少。缺 cookie_token 的后果是取数报「凭证失效」，
            # 由 miyoushe 的 1001/10001/10103 分支兜底提示重扫（#41-C）。
            audit(f"{label}-exchange failed code={exc.code}")
    return user_store.normalize_auth(auth)


def discover_uids(auth):
    """UID 发现（§3.3）：必须用 **Cookie 版** roles 接口。

    实机（#41）：`getUserGameRolesByStoken` 已废（`retcode=-10001 invalid request`），
    `getUserGameRolesByCookie` 用 CK 串照常返回全部游戏（原神/星铁/绝区零/崩三…）。
    失败返回空表，走「uid 手动带参」降级路径（§7.3）。
    """
    uids = {game: [] for game in user_store.GAMES}
    if isinstance(auth, str):  # 兼容旧调用点：只传 stoken
        auth = {"stoken": auth}
    ck = user_store.ck_string({"auth": auth if isinstance(auth, dict) else {}})
    if not ck:
        return uids
    url = HOSTS["takumi"] + "/binding/api/getUserGameRolesByCookie"
    headers = dict(_ck_headers(auth))
    headers["Cookie"] = ck
    try:
        payload, _headers = _get(url, headers)
        if _retcode(payload) not in (0, None):
            audit(f"uid-discover retcode={_retcode(payload)}（降级：uid 走手动带参）")
            return uids
    except QrLoginError:
        audit("uid-discover failed（降级：uid 走手动带参）")
        return uids
    for role in ((payload.get("data") or {}).get("list") or []):
        role = role or {}
        game = GAME_BIZ.get(str(role.get("game_biz") or ""))
        uid = str(role.get("game_uid") or "")
        if game and uid and uid not in uids[game]:
            uids[game].append(uid)
    return uids


# ---------------------------------------------------------------- 会话生命周期


def _qr_config(settings):
    cfg = (settings.get("miyoushe") or {}).get("qr_login") or {}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "daily_limit": int(cfg.get("daily_limit") if cfg.get("daily_limit") is not None
                           else DAILY_LIMIT),
        "session_ttl": float(cfg.get("session_ttl") or SESSION_TTL),
    }


def start_login(settings, qq, group_id):
    """#登录：限流检查 → 申请二维码 → 建会话。失败抛 QrLoginError（文案直接回群）。"""
    qq = str(qq or "").strip()
    if not user_store.safe_qq(qq):
        raise QrLoginError("识别不到你的 QQ 号，换个账号重试")
    cfg = _qr_config(settings)
    now = time.time()
    record = user_store.load_user(qq)
    if record:
        age = now - int(record.get("bound_at") or 0)
        if 0 <= age < BOUND_COOLDOWN:
            hours = max(1, int((BOUND_COOLDOWN - age) // 3600))
            raise QrLoginError(f"你已经绑定过了，直接发 #体力 就行；{hours} 小时后可重新扫码续期")
    if qq in SESSIONS:
        raise QrLoginError("你已有一个进行中的扫码，先用米游社 App 扫完；过期后重新发 #登录")
    recent = _daily_recent(qq, now)
    if cfg["daily_limit"] > 0 and len(recent) >= cfg["daily_limit"]:
        raise QrLoginError(f"今天扫码次数用完了（每天 {cfg['daily_limit']} 次），明天再试")

    device_id = uuid.uuid4().hex
    try:
        qr_url, ticket = create_qr("app", device_id)
        stream = "app"
    except QrLoginError as app_error:
        # §7.1：App 流协议变了就退 Web 流（只拿 Cookie，少 stoken 续期能力）
        audit(f"app-stream failed code={app_error.code}，回退 web 流")
        qr_url, ticket = create_qr("web", device_id)
        stream = "web"
    session = {
        "qq": qq, "group_id": str(group_id), "ticket": ticket, "url": qr_url,
        "device_id": device_id, "app_id": APP_ID, "stream": stream,
        "status": "Created", "created_at": now, "expire_at": now + cfg["session_ttl"],
    }
    SESSIONS[qq] = session
    _daily_add(qq, now)
    audit(f"create qq={qq} group={group_id} stream={stream} ttl={int(cfg['session_ttl'])}")
    return session


def _finish(session, payload, headers):
    """Confirmed：取凭据 → 补 LToken → 发现 uid → 加密落盘 → 回群通知。"""
    qq = session["qq"]
    auth = _extract_auth(payload, headers)
    # App 流只下发 stoken：ltoken/cookie_token 都得换回来，否则 CK 串不完整，
    # 取数接口会判成「凭证失效」——就是 #41 实机 07:22 那次假成功。
    auth = derive_ck(auth)
    if not auth:
        SESSIONS.pop(qq, None)
        # 只记结构不记值：下次再「扫完没凭证」时能一眼看出是不是字段名又变了。
        audit(f"fail qq={qq} reason=no-credential shape={_shape_of(payload)}")
        return [(session["group_id"], qq, "没拿到登录凭证，重新发 #登录 试试")]
    uids = discover_uids(auth)
    active = {game: (values[0] if values else "") for game, values in uids.items()}
    now = int(time.time())
    try:
        user_store.save_user(qq, {
            "qq": qq, "bound_at": now, "device_id": session["device_id"],
            "auth": auth, "uids": uids, "active_uid": active,
            # 有 stoken 才谈得上续期；Web 流只有 Cookie，到期只能重扫（§6 P2）
            "stoken_expires_at": (now + STOKEN_TTL) if auth.get("stoken") else None,
            "last_renew_at": now if auth.get("stoken") else None,
        })
    except (OSError, ValueError) as exc:
        # 磁盘满/权限/空凭证：必须让用户知道，否则会落进 poll_once 的兜底分支
        # 每 2 秒静默重试，直到二维码过期才收到「已过期」（#41-D3）。
        SESSIONS.pop(qq, None)
        audit(f"fail qq={qq} reason=save-error type={type(exc).__name__}")
        return [(session["group_id"], qq,
                 "绑定信息保存失败（磁盘或权限问题），请稍后重试；持续失败请找管理员")]
    SESSIONS.pop(qq, None)
    audit(f"success qq={qq} games={sorted(g for g, v in uids.items() if v)}")
    return [(session["group_id"], qq,
             "绑定成功！之后 #体力 / #深渊 / #原石 直接查你自己的账号，不用再带参数。"
             "解绑发 #解绑")]


def _poll_session(session, now):
    qq = session["qq"]
    if now >= session["expire_at"]:
        SESSIONS.pop(qq, None)
        audit(f"expire qq={qq}")
        return [(session["group_id"], qq,
                 "登录二维码已过期（5 分钟），重新发 #登录 再来一次")]
    status, payload, headers = query_status(
        session["stream"], session["ticket"], session["device_id"])
    if status != session.get("status"):
        session["status"] = status
        audit(f"status qq={qq} -> {status}")
    if status == "Confirmed":
        return _finish(session, payload, headers)
    if status in WAITING:
        return []
    raise QrLoginError(f"登录状态异常（{status or '未知'}），重新发 #登录 试试")


def poll_once(now=None):
    """轮询所有 pending 会话，返回 [(group_id, qq, 通知文本)] 待发。

    已知失败（retcode/状态异常）→ 通知用户并清会话；网络抖动（code=network）
    → 不清会话、不耗额度，下一轮再试，直到 expire_at 兜底（#41-D1）。
    """
    now = time.time() if now is None else now
    notices = []
    for qq, session in list(SESSIONS.items()):
        try:
            notices.extend(_poll_session(session, now))
            _NET_ERRORS.pop(qq, None)
        except QrLoginError as exc:
            if exc.code == "network":
                count = _NET_ERRORS.get(qq, 0) + 1
                _NET_ERRORS[qq] = count
                if count == 1 or count % 10 == 0:
                    audit(f"retry qq={qq} network n={count}")
                continue
            SESSIONS.pop(qq, None)
            _NET_ERRORS.pop(qq, None)
            audit(f"fail qq={qq} code={exc.code}")
            notices.append((session["group_id"], qq, str(exc)))
        except Exception as exc:  # noqa: BLE001  未预期错误不杀会话，下一轮重试
            audit(f"retry qq={qq} error={type(exc).__name__}")
    return notices


def discard_session(qq):
    """撤销一个进行中的会话并退回本次额度（#41-E2：二维码没发出去就算没过）。

    返回是否真的撤销了会话。
    """
    qq = str(qq or "")
    if not SESSIONS.pop(qq, None):
        return False
    _NET_ERRORS.pop(qq, None)
    _daily_drop_last(qq)
    audit(f"discard qq={qq} reason=delivery-failed")
    return True


def start_poller(notify, cfg_getter=None, interval=POLL_INTERVAL):
    """单轮询线程（§4.3）。notify(group_id, qq, text) 由 commands.py 接线发送。

    `cfg_getter()` 每 tick 重读 config：`qr_login.enabled` 热重载后开关立即生效
    （开→继续轮询，关→空转 sleep）。线程本身常驻，不做启停，避免重启栈才能改配置
    （#41-D2）。
    """
    def loop():
        while True:
            try:
                cfg = (cfg_getter() if cfg_getter else None) or {}
                if not cfg.get("enabled", True):
                    time.sleep(interval)
                    continue
                for group_id, qq, text in poll_once():
                    notify(group_id, qq, text)
            except Exception as exc:  # noqa: BLE001  轮询线程不许死
                audit(f"poller tick error: {type(exc).__name__}")
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="qr-login-poller", daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------- 续期与失效


def should_renew(record, now=None):
    """有 stoken 且距上次换票超过 RENEW_EVERY → 该续了（#6 §6 P2）。"""
    if not ((record or {}).get("auth") or {}).get("stoken"):
        return False
    now = time.time() if now is None else now
    last = int((record or {}).get("last_renew_at") or (record or {}).get("bound_at") or 0)
    return now - last >= RENEW_EVERY


def renew(qq, record=None, now=None):
    """惰性续期：拿 stoken 换新 LToken 并落盘。不动 bound_at（不触发 24h 冷却）。

    失败只记审计并返回 False——取数还会照常尝试，不该因为续期失败把用户挡在门外。
    """
    qq = str(qq or "")
    record = record or user_store.load_user(qq)
    stoken = ((record or {}).get("auth") or {}).get("stoken")
    if not record or not stoken:
        return False
    now = int(time.time() if now is None else now)
    auth = dict(record.get("auth") or {})
    try:
        fresh = exchange_ltoken(auth)
    except QrLoginError as exc:
        audit(f"renew fail qq={qq} code={exc.code}")
        return False
    if not fresh:
        return False
    auth.update(fresh)
    # cookie_token 也一起续：缺了它取数照样报凭证失效，重新扫码就白续了
    try:
        auth.update(fetch_cookie_token(auth))
    except QrLoginError as exc:
        audit(f"renew cookie_token fail qq={qq} code={exc.code}")
    updated = dict(record)
    updated["auth"] = auth
    updated["last_renew_at"] = now
    updated["stoken_expires_at"] = now + STOKEN_TTL
    try:
        user_store.save_user(qq, updated)
    except (OSError, ValueError) as exc:
        audit(f"renew fail qq={qq} reason=save-{type(exc).__name__}")
        return False
    audit(f"renew ok qq={qq}")
    return True


def mark_invalid(qq, now=None):
    """标记凭证失效；返回是否该提醒（24h 内最多一次，#6 §6 P2）。"""
    qq = str(qq or "")
    record = user_store.load_user(qq)
    if not record:
        return False
    now = int(time.time() if now is None else now)
    last = int(record.get("ck_invalid_at") or 0)
    updated = dict(record)
    updated["ck_invalid_at"] = now
    try:
        user_store.save_user(qq, updated)
    except (OSError, ValueError):
        return False
    return now - last >= 86400


# ---------------------------------------------------------------- 指令文案


def status_text(qq, now=None):
    """#登录状态：绑定时间、QQ 尾号、各游戏 uid（§4.5，独立 30s 冷却）。"""
    qq = str(qq or "")
    now = time.time() if now is None else now
    last = _STATUS_AT.get(qq)
    if last is not None and now - last < STATUS_CD:
        return "刚查过了，过一会儿再看"
    _STATUS_AT[qq] = now
    if len(_STATUS_AT) > 2000:  # 长期运行不攒垃圾
        for key, stamp in list(_STATUS_AT.items()):
            if now - stamp > 3600:
                _STATUS_AT.pop(key, None)
    record = user_store.load_user(qq)
    if not record:
        return "你还没有绑定米游社账号，发 #登录 扫码绑定"
    bound = time.strftime("%m-%d %H:%M",
                          time.localtime(int(record.get("bound_at") or 0)))
    parts = []
    for game, name in (("genshin", "原神"), ("starrail", "星铁"), ("zzz", "绝区零")):
        uids = (record.get("uids") or {}).get(game) or []
        if uids:
            active = user_store.active_uid(record, game)
            extra = f"（共 {len(uids)} 个 uid）" if len(uids) > 1 else ""
            parts.append(f"{name} {active}{extra}")
    tail = "；".join(parts) or "暂未发现游戏 uid（发 #体力 123456789 手动带 UID）"
    return f"绑定于 {bound}（QQ 尾号 {qq[-4:]}）\n{tail}\n解绑发 #解绑"


def unbind(qq, args="", now=None):
    """#解绑：先提示、再动真格，两步都认。

    原来只认 `#解绑 确认`（args 必须正好是「确认」），但提示文案写的是
    「再发一次 #解绑 确认」——群友读成「再发一次 #解绑」，于是每次都停在提示，
    形成死循环（实机 07:37 三连击）。现在窗口内再发一次裸 `#解绑` 也算确认，
    显式写 `#解绑 确认 / 确定 / 是` 同样一步到位。
    """
    qq = str(qq or "")
    now = time.time() if now is None else now
    if not user_store.load_user(qq):
        _UNBIND_AT.pop(qq, None)
        return "你还没有绑定，无需解绑"
    text = str(args or "").strip().lower()
    last = _UNBIND_AT.get(qq)
    if text in UNBIND_WORDS or (last is not None and now - last <= UNBIND_WINDOW):
        user_store.delete_user(qq)
        _UNBIND_AT.pop(qq, None)
        audit(f"unbind qq={qq}")
        return "已解绑，凭证文件已删除。重新绑定发 #登录"
    _UNBIND_AT[qq] = now
    if len(_UNBIND_AT) > 2000:  # 长期运行不攒垃圾
        for key, stamp in list(_UNBIND_AT.items()):
            if now - stamp > UNBIND_WINDOW:
                _UNBIND_AT.pop(key, None)
    return (f"解绑会删除你的米游社绑定凭证。{int(UNBIND_WINDOW)} 秒内再发一次 #解绑 就删"
            "（立刻发也行，或发 #解绑 确认 也行）")


def is_pending_unbind(qq, now=None):
    """#解绑 两步确认是否仍在等待第二步（issue #66）。

    仅用于冷却豁免判定：第一条 #解绑 还没把 _UNBIND_AT 写进去，不会被豁免；
    第二条进来时 _UNBIND_AT 在窗口内，豁免冷却并真正执行删除。
    """
    qq = str(qq or "")
    if now is None:
        now = time.time()
    last = _UNBIND_AT.get(qq)
    return last is not None and (now - last) <= UNBIND_WINDOW


def pending_confirmation_keys(qq, now=None):
    """当前 qq 正在等待确认的指令集合（issue #66）。

    未来新增带确认窗口的指令只需在这里追加，commands.dispatch 无需改动。
    """
    keys = set()
    if is_pending_unbind(qq, now):
        keys.add("解绑")
    return keys


def bind_uid(qq, args):
    """#绑定 [原神|星铁|绝区零] <UID>：给已扫码的账号指定/切换默认 UID。

    可行性边界：UID 只是取数参数，不是凭证——没有 CK 时单绑 UID 取不到任何数据，
    所以未扫码的用户先回 #登录 提示。归属校验拿存下来的 CK 再查一次 roles
    （`getUserGameRolesByCookie`）；换不到 / 网络失败时 owned 为空表，接受绑定，
    查询失败自然暴露。
    """
    qq = str(qq or "")
    record = user_store.load_user(qq)
    if not record:
        return "先发 #登录 扫码绑定米游社账号，再 #绑定 UID（单绑 UID 取不到数据）"
    text = (args or "").strip()
    game = "genshin"
    for alias, key in GAME_ALIASES.items():
        if text.startswith(alias):
            game = key
            text = text[len(alias):].strip()
            break
    digits = re.findall(r"\d+", text)
    uid = digits[0] if digits else ""
    if not uid or not 8 <= len(uid) <= 10:
        return "用法：#绑定 [原神|星铁|绝区零] <UID>，如 #绑定 100000000"
    if game not in BINDABLE_GAMES:
        return (f"{GAME_NAMES[game]}的取数指令还没接入，暂不支持设置默认 UID；"
                f"接好之后会通知（现在只有原神那套 #体力 / #深渊 在跑）")
    owned = discover_uids(record["auth"])
    known = owned.get(game) or []
    if known and uid not in known:
        other = next((name for name, values in owned.items() if uid in values), "")
        if other:
            return f"这是{GAME_NAMES[other]}的 UID，改成 #绑定 {GAME_NAMES[other]} {uid}"
        return (f"这个 UID 不在你扫码的米游社账号下"
                f"（该账号{GAME_NAMES[game]}的 UID：{' / '.join(known)}）")
    user_store.save_user(qq, user_store.bind_uid(record, uid, game))
    audit(f"bind-uid qq={qq} game={game} uid={uid}")
    return f"已绑定{GAME_NAMES[game]} {uid}，之后 #体力 直接查这个号（换号再发 #绑定）"
