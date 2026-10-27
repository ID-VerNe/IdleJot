"""米游社取数 handler（issue 方案 D）：commands.py 动态指令的取数后端。

接口契约全部对照 Miao-Yunzai 的逆向报告实现（`.plan/genshin-miyoushe-api-report.md`，
2026-10-22）：DS 签名、请求头、设备指纹 getFp、300s 缓存、retcode 错误码翻译。
报告里记名的三个坑按修好的写法实现：month 补成 YYYYMM（不再传 1-12）、searchPosts
关键词走 urlencode（不再裸拼）、只缓存 retcode===0（失败结果不污染 5 分钟）。

边界（写死在文档里，别悄悄扩）：
  - 只做国服（cn_gf01 / cn_qd01 / prod_gf_cn）；国际服 host/salt/UA 另一套，未接入。
  - CK 是账号凭证：只从 water-group/config.json（gitignore）的
    slash_commands.miyoushe.ck 读，由主人配置；不落日志、不进群、不写任何文件。
  - 输出纯文本：Yunzai 那些 puppeteer 渲染图不在本模块职责内。
  - 未接入：每用户 CK 绑定、充值记录档位对账、养成计算（需角色名→ID 映射）、
    抽卡统计/导出（需多页+历史存储）、星铁、国际服。理由见 doc/SLASH_COMMANDS.md。
"""
import hashlib
import json
import os
import random
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from wg_paths import state_dir
import wg_text  # issue #169 PB: _clip 合流到 wg_text.render_handler_reply

SALT_CN = "xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs"
APP_VERSION = "2.40.1"
CLIENT_TYPE = "5"
UA_CN = ("Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 "
         "Chrome/99.0.4844.73 Mobile Safari/537.36 miHoYoBBS/2.40.1")
UA_WEB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "Chrome/124.0.0.0 Safari/537.36")
CACHE_TTL = 300.0
TIMEOUT = 10
REPLY_CLIP = 480

# 测试把整张表指到 127.0.0.1 的 fixture；生产用真实 host。
HOSTS = {
    "record": "https://api-takumi-record.mihoyo.com",
    "bbs_static": "https://bbs-api-static.miyoushe.com",
    "bbs": "https://bbs-api.miyoushe.com",
    "bbs_mihoyo": "https://bbs-api.mihoyo.com",
    "takumi": "https://api-takumi.mihoyo.com",
    "takumi_static": "https://api-takumi-static.mihoyo.com",
    "hk4e": "https://hk4e-api.mihoyo.com",
    "public_data": "https://public-data-api.mihoyo.com",
    "gacha": "https://public-operation-hk4e.mihoyo.com",
}

NEWS_TYPES = {"公告": 1, "活动": 2, "资讯": 3}
GACHA_KINDS = {"角色": "301", "武器": "302", "常驻": "200", "集录": "500"}
CN_SERVERS = ("cn_gf01", "cn_qd01")

# getFp 的设备画像（issue #55-A）。字段名与 Axiu-Plugin deviceFp.js 的 ext_fields 对齐，
# 少一个都可能被判成异常环境；这些值本身不重要，重要的是"像个真机"且**长期不变**。
DEVICE_PROFILE = {
    "brand": "Sony", "model": "XQ-BC52", "device": "XQ-BC52", "board": "lahaina",
    "product": "XQ-BC52", "os_version": "13", "sdk": "33",
    "screen": "1096x2434", "cpu": "arm64-v8a",
    "package": "com.mihoyo.hyperion", "package_version": "2.35.0",
    "build": "Sony/XQ-BC52_EEA/XQ-BC52:13/61.2.A.0.472A/"
             "061002A0000472A0046651803:user/release-keys",
}

RETCODE_MSG = {
    -1: "CK 已失效，请重新配置", -100: "CK 已失效，请重新配置",
    1001: "CK 已失效，请重新配置", 10001: "CK 已失效，请重新配置",
    10103: "CK 已失效，请重新配置",
    1008: "米游社未绑定游戏角色，先去米游社绑定",
    10101: "今日查询次数已达上限，明天再试",
    10102: "米游社数据未公开（隐私开关没开）",
    5003: "米游社账号异常（风控），先停一会儿再查", 10041: "米游社账号异常（风控），先停一会儿再查",
    1034: "米游社要求人机验证：去 App「我的 → 我的角色」过一次验证",
    10035: "米游社要求人机验证：去 App「我的 → 我的角色」过一次验证",
    10307: "版本维护中，暂停查询",
}

# 进程级缓存：守护是单线程 recv 循环，无需加锁。key → (过期时刻, payload)。
_CACHE = {}

# ---------------------------------------------------------------- 设备指纹与风控冷却
#
# record / 活动接口要求合法的设备指纹，否则回 1034（验证码）或 5003（账号异常）。
# 三个坑全部对标 Yunzai 生态的实现（issue #55）：
#   1. getFp 的 body 必须是**完整设备画像**——Miao-Yunzai #272 原话「旧版获取fp的
#      body 亲测原神无效」，只塞三个键的 ext_fields 拿不到有效指纹；
#   2. device_id 必须**持久**：每次进程启动换一个新的，等于每次都是陌生设备；
#   3. 拿到指纹后要补发 deviceLogin / saveDevice 把设备**登记**上（Axiu-Plugin
#      deviceFp.js），未注册的指纹容易被判成异常环境（TravelerNote README 的 5003）。
# 指纹缓存 7 天，按 ltuid 分桶——不同账号不同桶，别串用。
FP_SEED = "38d805c20d53d"          # getFp 请求体里的初始 fp，社区通用常量
FP_TTL = 7 * 86400
DEVICE_FILE = state_dir() / "device.json"
_FP_CACHE = {}                     # bucket → 指纹（进程内快层；落盘持久层见 DEVICE_FILE）

# 风控冷却：出过验证码/账号异常的账号，冷却期内不再发任何上游请求——越查越久
# （miao-plugin #396 的做法就是「把出验证的 ck 暂时屏蔽掉」）。
RISK_CODES = (1034, 10035, 5003, 10041)
RISK_COOLDOWN = 30 * 60
_RISK = {}                         # bucket → 冷却到期时间戳


class MiyousheError(Exception):
    """带用户可读文案的取数失败——文案直接当群回复发出。

    `code` 保留米游社 retcode：commands.py 靠它区分「凭证失效」与别的失败，
    决定要不要提醒本人重新扫码（#41-C）。
    """

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 基础请求层


def _ds(query, body):
    t = int(time.time())
    r = random.randint(100000, 999999)
    digest = hashlib.md5(
        f"salt={SALT_CN}&t={t}&r={r}&b={body}&q={query}".encode("utf-8")).hexdigest()
    return f"{t},{r},{digest}"


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch(url, headers, body=None):
    req = urllib.request.Request(url, data=body.encode("utf-8") if body else None,
                                 headers=headers)
    try:
        with _opener().open(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except MiyousheError:
        raise
    except Exception as exc:  # noqa: BLE001  网络层失败统一翻译
        raise MiyousheError(f"米游社接口无响应（{type(exc).__name__}），稍后再试")


def web_get(url, extra=None):
    """BBS / 静态接口：不走 DS，只要 Referer + UA。"""
    headers = {"Referer": "https://www.miyoushe.com", "User-Agent": UA_WEB}
    if extra:
        headers.update(extra)
    return _fetch(url, headers)


class Client:
    """带 DS 签名的国服请求客户端。一个实例对应一个 CK。"""

    def __init__(self, ck="", uid=""):
        self.ck = ck
        self.uid = str(uid or "")

    def _signed(self, url, query, body, extra):
        headers = {
            "x-rpc-app_version": APP_VERSION,
            "x-rpc-client_type": CLIENT_TYPE,
            "User-Agent": UA_CN,
            "Referer": "https://webstatic.mihoyo.com/",
            "Origin": "https://webstatic.mihoyo.com/",
            "DS": _ds(query, body),
        }
        if self.ck:
            headers["Cookie"] = self.ck
        if extra:
            headers.update(extra)
        return _fetch(url, headers, body)

    def get(self, host, path, query="", extra=None):
        url = HOSTS[host] + path + (("?" + query) if query else "")
        return self._signed(url, query, "", extra)

    def post(self, host, path, query="", payload=None, extra=None):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) \
            if payload is not None else ""
        url = HOSTS[host] + path + (("?" + query) if query else "")
        headers = {"Content-Type": "application/json"}
        if extra:
            headers.update(extra)
        return self._signed(url, query, body, headers)

    def device_fp(self):
        """体力类接口不带指纹大概率吃验证码。缓存分发见模块级 device_fp()。"""
        return device_fp(self.ck, self.uid)

    def _fp_body(self, device_id_value):
        """getFp 请求体：完整设备画像（issue #55-A，对标 Axiu-Plugin deviceFp.js）。"""
        p = DEVICE_PROFILE
        model, brand = p["model"], p["brand"]
        ext = {
            "proxyStatus": "1", "isRoot": "0", "romCapacity": "768", "romRemain": "727",
            "deviceName": model, "productName": p["product"], "hostname": "BuildHost",
            "screenSize": p["screen"], "isTablet": 0, "model": model, "brand": brand,
            "hardware": "qcom", "deviceType": p["device"], "devId": "REL",
            "serialNumber": "unknown", "sdCapacity": 224845, "sdRemain": 218214,
            "buildTime": "1692775759000", "buildUser": "BuildUser",
            "buildTags": "release-keys", "buildType": "user",
            "deviceInfo": p["build"], "display": p["build"].split("/")[3],
            "osVersion": p["os_version"], "sdkVersion": p["sdk"],
            "cpuType": p["cpu"], "packageName": p["package"],
            "packageVersion": p["package_version"],
            "aaid": device_id_value, "oaid": device_id_value, "vaid": device_id_value,
            "appUpdateTimeDiff": 1740498108042, "appInstallTimeDiff": 1740498108042,
            "appMemory": "768", "ramCapacity": "224845", "ramRemain": "218344",
            "simState": 1, "networkType": "WiFi", "chargeStatus": 1, "batteryStatus": 88,
            "isMockLocation": 0, "isAirMode": 0, "ringMode": 2, "emulatorStatus": 0,
            "debugStatus": 1, "hasKeyboard": 0, "ui_mode": "UI_MODE_TYPE_NORMAL",
            "accelerometer": "-1.588236x6.8404818x6.999604",
            "magnetometer": "-47.04375x51.3375x137.96251",
            "gyroscope": "-0.22601996x-0.09453133x0.09040799",
            "manufacturer": brand, "vendor": "unknown", "board": p["board"],
        }
        return json.dumps({
            "app_name": "bbs_cn",
            "bbs_device_id": device_id_value,
            "device_fp": FP_SEED,
            "device_id": device_id_value,
            "ext_fields": json.dumps(ext, separators=(",", ":")),
            "platform": "2",
            "seed_id": device_id_value,
            "seed_time": str(int(time.time() * 1000)),
        }, separators=(",", ":"), ensure_ascii=False)

    def fetch_device_fp(self):
        """现取一次指纹 → fp 字符串（不带缓存）。取不到抛 MiyousheError。"""
        did = device_id()
        url = HOSTS["public_data"] + "/device-fp/api/getFp"
        payload = self._signed(url, "", self._fp_body(did),
                               {"Content-Type": "application/json"})
        fp = str(((payload.get("data") or {}).get("device_fp")) or "")
        if not fp:
            raise MiyousheError("设备指纹获取失败，稍后再试")
        return fp

    def register_device(self):
        """把设备登记到账号上（best-effort）。

        Axiu-Plugin deviceFp.js 的做法：getFp 成功后补发 deviceLogin + saveDevice，
        否则新指纹对服务端仍是「没见过这台机器」。**未在真实风控态下验证过**，
        所以失败只吞掉：登记不上最多回到修之前的行为，不该让取数整个挂掉。
        """
        if not self.ck:
            return False
        body = json.dumps({
            "app_version": "2.73.1",
            "device_id": device_id(),
            "device_name": DEVICE_PROFILE["brand"] + DEVICE_PROFILE["model"],
            "os_version": DEVICE_PROFILE["sdk"],
            "platform": "Android",
            "registration_id": os.urandom(10).hex()[:19],
        }, separators=(",", ":"))
        ok = False
        for path in ("/apihub/api/deviceLogin", "/apihub/api/saveDevice"):
            try:
                self._signed(HOSTS["bbs"] + path, "", body,
                             {"Content-Type": "application/json"})
                ok = True
            except Exception:  # noqa: BLE001  登记非关键，网络/接口变了都不影响取数
                continue
        return ok


def cached(key, fetch):
    """300s TTL；只缓存 retcode===0——失败结果进缓存会连带污染 5 分钟。"""
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    payload = fetch()
    if isinstance(payload, dict) and payload.get("retcode") == 0:
        _CACHE[key] = (now + CACHE_TTL, payload)
    return payload


def check(payload):
    """retcode → data；失败按 §13.1 映射成用户可读文案。"""
    if not isinstance(payload, dict):
        raise MiyousheError("米游社返回了无法解析的数据")
    code = payload.get("retcode")
    if code == 0 or (code is None and payload.get("data") is not None):
        return payload.get("data") or {}
    if code in RETCODE_MSG:
        raise MiyousheError(RETCODE_MSG[code], code=code)
    raise MiyousheError(f"米游社查询失败（retcode={code} {payload.get('message') or ''}）",
                        code=code)


# ---------------------------------------------------------------- 设备状态与冷却


def _device_state():
    """读 state/device.json（device_id + 各账号指纹）。缺失/损坏按空处理。

    返回值恒为 dict 且恒有 "fp" 键——首次运行（文件不存在）也走同一条路径，
    否则调用方要各自判空（真踩过：全新安装第一句 #体力 就 KeyError）。
    """
    try:
        raw = json.loads(DEVICE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"fp": {}}
    if not isinstance(raw, dict):
        return {"fp": {}}
    if not isinstance(raw.get("fp"), dict):
        raw["fp"] = {}
    return raw


def _save_device_state(state):
    """原子写回：崩溃不留半个 JSON（与 user_store / qr_login 同一手法）。"""
    try:
        DEVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = DEVICE_FILE.with_name(DEVICE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, DEVICE_FILE)
    except OSError:
        pass


def device_id():
    """16 位 hex 设备 ID，**持久化**。

    换设备 ID 等于换一台新机器——原来每次进程启动都随机生成一个，
    每重启一次守护就等于让风控看见一台陌生设备（issue #55-A）。
    """
    state = _device_state()
    value = str(state.get("device_id") or "").strip()
    if re.fullmatch(r"[0-9a-f]{16}", value):
        return value
    value = os.urandom(8).hex()
    state["device_id"] = value
    _save_device_state(state)
    return value


def fp_bucket(ck):
    """指纹/冷却按账号分桶：ltuid → account_id → stuid → uid → 整串哈希。

    风控是**账号级**状态，不同账号必须分开：一个号被挡不该把另一个号也锁住。
    """
    text = str(ck or "")
    for name in ("ltuid", "account_id", "stuid"):
        found = re.search(rf"(?:^|;\s*){name}=([^;\s]+)", text)
        if found:
            return found.group(1)
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12] if text else ""


def device_fp(ck, uid=""):
    """设备指纹：进程内 → 落盘（7 天）→ 现取。取到后顺手登记一次设备。"""
    bucket = fp_bucket(ck) or "anon"
    hit = _FP_CACHE.get(bucket)
    if hit:
        return hit
    state = _device_state()
    saved = state["fp"].get(bucket) or {}
    now = time.time()
    if saved.get("value") and now - float(saved.get("at") or 0) < FP_TTL:
        _FP_CACHE[bucket] = saved["value"]
        return saved["value"]

    client = Client(ck, uid)
    fresh = client.fetch_device_fp()
    _FP_CACHE[bucket] = fresh
    state = _device_state()          # 重新读：device_id() 可能刚写过一次
    state.setdefault("fp", {})[bucket] = {"value": fresh, "at": int(now)}
    _save_device_state(state)
    client.register_device()
    return fresh


def fp_fresh(ck):
    """强制丢弃该账号的指纹缓存（风控命中后调用，下次换一个新的）。"""
    bucket = fp_bucket(ck) or "anon"
    _FP_CACHE.pop(bucket, None)
    state = _device_state()
    if state["fp"].pop(bucket, None) is not None:
        _save_device_state(state)


def note_risk(ck, code, now=None):
    """命中风控 → 该账号进冷却，返回冷却秒数（issue #55-C）。"""
    bucket = fp_bucket(ck)
    if not bucket:
        return 0
    now = time.time() if now is None else now
    _RISK[bucket] = now + RISK_COOLDOWN
    fp_fresh(ck)                      # 下次换个指纹，别拿被标记的那个继续撞
    if len(_RISK) > 2000:             # 长期运行不攒垃圾
        for key, until in list(_RISK.items()):
            if until <= now:
                _RISK.pop(key, None)
    return RISK_COOLDOWN


def risk_wait(ck, now=None):
    """还要等多少秒；0 表示现在可以查。"""
    bucket = fp_bucket(ck)
    if not bucket:
        return 0
    now = time.time() if now is None else now
    until = _RISK.get(bucket) or 0
    if until <= now:
        _RISK.pop(bucket, None)
        return 0
    return int(until - now)


# ---------------------------------------------------------------- 小工具


def server_of(uid):
    head = str(uid)[:-8]
    return {"5": "cn_qd01", "6": "os_usa", "7": "os_euro", "18": "os_asia",
            "8": "os_asia", "9": "os_cht"}.get(head, "cn_gf01")


def zzz_server_of(uid):
    head = str(uid)[:2]
    return {"10": "prod_gf_us", "15": "prod_gf_eu", "13": "prod_gf_jp",
            "17": "prod_gf_sg"}.get(head, "prod_gf_cn")


def _cn_only(server):
    if server not in CN_SERVERS:
        raise MiyousheError("国际服账号暂未接入（只做了国服）")


def _pick_uid(args, ctx):
    m = re.search(r"\d{9,10}", args or "")
    uid = m.group(0) if m else (ctx.get("uid") or "")
    if not uid:
        raise MiyousheError("请带上游戏 UID，如 #体力 123456789；或让主人配置默认 uid")
    return uid


def _client(ctx, need_ck=True):
    cks = ctx.get("ck") or []
    ck = cks[0] if cks else ""
    if need_ck and not ck:
        raise MiyousheError("未配置米游社 CK（config.json → slash_commands.miyoushe.ck），"
                            "私聊主人配置后才能查账号数据")
    return Client(ck, ctx.get("uid") or "")


def _dur(seconds):
    seconds = int(seconds or 0)
    if seconds <= 0:
        return ""
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分"
    if hours:
        return f"{hours} 小时"
    return f"{minutes} 分钟"


def _clip(text, limit=REPLY_CLIP):
    """issue #169 PB：截断的唯一归宿是 wg_text.render_handler_reply（见 sixty._clip 注释）。"""
    return wg_text.render_handler_reply(text, limit)


def _parse_authkey(text):
    m = re.search(r"authkey=([^&\s]+)", text or "")
    if not m:
        return "", ""
    rm = re.search(r"region=([a-z0-9_]+)", text or "")
    return urllib.parse.unquote(m.group(1)), (rm.group(1) if rm else "cn_gf01")


# ---------------------------------------------------------------- 免登录指令


def news(kind):
    t = NEWS_TYPES[kind]
    query = urllib.parse.urlencode({"gids": 2, "page_size": 5, "type": t})
    payload = cached(f"news{t}", lambda: web_get(
        f"{HOSTS['bbs_static']}/painter/wapi/getNewsList?{query}"))
    posts = [item.get("post") or {} for item in (check(payload).get("list") or [])]
    if not posts:
        return f"暂时没有{kind}"
    lines = [f"【原神{kind}】"]
    for p in posts[:5]:
        stamp = time.strftime("%m-%d", time.localtime(int(p.get("created_at") or 0)))
        lines.append(f"{stamp} {p.get('subject') or '(无标题)'}")
    return "\n".join(lines)


def search_posts(keyword, label=None):
    if not keyword.strip():
        raise MiyousheError("带上关键词再搜，如 #米游社胡桃")
    query = urllib.parse.urlencode({"gids": 2, "size": 5, "keyword": keyword})
    payload = cached(f"search{keyword}", lambda: web_get(
        f"{HOSTS['bbs']}/post/wapi/searchPosts?{query}"))
    posts = [item.get("post") or {} for item in (check(payload).get("posts") or [])]
    if not posts:
        return f"米游社没搜到「{keyword}」"
    lines = [f"【米游社 · {label or keyword}】"]
    for p in posts[:5]:
        lines.append(f"· {p.get('subject') or '(无标题)'}")
    return "\n".join(lines)


def _find_act_id():
    """前瞻直播入口：先翻官方号动态，兜底翻首页导航。"""
    query = urllib.parse.urlencode({"offset": 0, "size": 20, "uid": "75276539"})
    blob = ""
    try:
        blob = json.dumps(cached("act_id", lambda: web_get(
            f"{HOSTS['bbs_mihoyo']}/painter/api/user_instant/list?{query}")),
            ensure_ascii=False)
    except MiyousheError:
        pass
    m = re.search(r"act_id=(\d+)", blob)
    if m:
        return m.group(1)
    query2 = urllib.parse.urlencode({"gids": 2, "parts": "1,3,4"})
    try:
        blob2 = json.dumps(web_get(
            f"{HOSTS['bbs']}/apihub/api/home/new?{query2}"), ensure_ascii=False)
    except MiyousheError:
        return ""
    m2 = re.search(r"act_id=(\d+)", blob2)
    return m2.group(1) if m2 else ""


def live_codes(_args, _ctx):
    act_id = _find_act_id()
    if not act_id:
        return "没找到前瞻直播入口，稍后再试"
    payload = cached(f"live{act_id}", lambda: web_get(
        f"{HOSTS['takumi']}/event/miyolive/index",
        extra={"x-rpc-act_id": act_id}))
    live = check(payload).get("live") or {}
    if live.get("remain"):
        return f"「{live.get('title') or '前瞻直播'}」还没开始，暂无兑换码"
    ver = str(live.get("code_ver") or "1.0")
    query = urllib.parse.urlencode({"version": ver, "time": int(time.time())})
    payload2 = web_get(f"{HOSTS['takumi_static']}/event/miyolive/refreshCode?{query}",
                       extra={"x-rpc-act_id": act_id})
    codes = [c.get("code") for c in (check(payload2).get("code_list") or []) if c.get("code")]
    if not codes:
        return "暂未发放兑换码"
    return "\n".join([f"【前瞻兑换码 · {live.get('title') or ''}】"] + codes)


def _split_gacha_args(args):
    kind = "角色"
    rest = (args or "").strip()
    for name, code in GACHA_KINDS.items():
        if rest.startswith(name):
            return code, rest[len(name):].strip()
    return GACHA_KINDS[kind], rest


def gacha_log(args, _ctx):
    kind, rest = _split_gacha_args(args)
    authkey, region = _parse_authkey(rest)
    if not authkey:
        raise MiyousheError("没解析到 authkey，请把游戏内「祈愿记录」页面的完整链接发过来")
    _cn_only(region)
    query = urllib.parse.urlencode({
        "authkey_ver": 1, "lang": "zh-cn", "gacha_type": kind, "page": 1,
        "size": 6, "end_id": 0, "authkey": authkey, "region": region})
    payload = web_get(f"{HOSTS['gacha']}/gacha_info/api/getGachaLog?{query}")
    data = check(payload)
    items = data.get("list") or []
    if not items:
        return "这个卡池还没有记录"
    kind_name = next(k for k, v in GACHA_KINDS.items() if v == kind)
    lines = [f"【{kind_name}抽卡记录】共 {data.get('total')} 抽，最近："]
    for it in items[:6]:
        star = "★" * int(it.get("rank_type") or 0)
        lines.append(f"{(it.get('time') or '')[:10]} {star}{it.get('name')}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 账号类指令（CK）


def _index(uid, client):
    query = urllib.parse.urlencode({"role_id": uid, "server": server_of(uid)})
    payload = cached(f"index{query}", lambda: client.get(
        "record", "/game_record/app/genshin/api/index", query))
    return check(payload)


def exploration(args, ctx):
    uid = _pick_uid(args, ctx)
    data = _index(uid, _client(ctx))
    role = data.get("role") or {}
    stats = data.get("stats") or {}
    lines = [f"【{role.get('nickname') or '旅行者'} Lv.{role.get('level')}】"
             f"活跃 {stats.get('active_day_number')} 天"]
    lines.append(f"成就 {stats.get('achievement_number')} · 角色 {stats.get('avatar_number')}"
                 f" · 满羁绊 {stats.get('full_fetter_avatar_num')}")
    lines.append(f"深渊 {stats.get('spiral_abyss')}")
    chests = [("珍贵", stats.get("precious_chest_number")), ("华丽", stats.get("luxurious_chest_number")),
              ("精美", stats.get("exquisite_chest_number")), ("普通", stats.get("common_chest_number")),
              ("奇珍", stats.get("magic_chest_number"))]
    lines.append("宝箱 " + " ".join(f"{n} {v}" for n, v in chests if v is not None))
    oculi = [("风", stats.get("anemoculus_number")), ("岩", stats.get("geoculus_number")),
             ("雷", stats.get("electroculus_number")), ("草", stats.get("dendroculus_number")),
             ("水", stats.get("hydroculus_number")), ("火", stats.get("pyroculus_number")),
             ("冰", stats.get("iceculus_number")), ("月", stats.get("moonoculus_number"))]
    lines.append("神瞳 " + " ".join(f"{n} {v}" for n, v in oculi if v is not None))
    regions = sorted(data.get("world_explorations") or [],
                     key=lambda e: -(e.get("exploration_percentage") or 0))[:3]
    if regions:
        lines.append("探索 " + " ".join(
            f"{e.get('name')} {(e.get('exploration_percentage') or 0) / 10:.0f}%" for e in regions))
    return "\n".join(lines)


def abyss(args, ctx):
    uid = _pick_uid(args, ctx)
    _cn_only(server_of(uid))
    client = _client(ctx)
    query = urllib.parse.urlencode(
        {"role_id": uid, "schedule_type": 1, "server": server_of(uid)})
    payload = cached(f"abyss{query}", lambda: client.get(
        "record", "/game_record/app/genshin/api/spiralAbyss", query))
    data = check(payload)
    lines = [f"【深渊·本期】{data.get('max_floor')}"
             f"（{data.get('total_win_times')}/{data.get('total_battle_times')} 胜）"]
    floors = [f for f in (data.get("floors") or []) if (f.get("index") or 0) >= 8]
    for f in floors:
        lines.append(f"{f.get('index')}层 ★{f.get('star')}/{f.get('max_star')}")
    if not floors:
        lines.append("还没有高层记录")
    return "\n".join(lines)


def _avatars(uid, client):
    server = server_of(uid)
    _cn_only(server)
    query = urllib.parse.urlencode({"role_id": uid, "server": server})
    payload = cached(f"character{query}", lambda: client.post(
        "record", "/game_record/app/genshin/api/character/list",
        payload={"role_id": str(uid), "server": server}))
    data = check(payload)
    return data.get("avatars") or data.get("list") or []


def characters(args, ctx):
    uid = _pick_uid(args, ctx)
    avatars = _avatars(uid, _client(ctx))
    five = [a for a in avatars if a.get("rarity") == 5]
    lines = [f"【角色】共 {len(avatars)} 名，五星 {len(five)} 名"]
    for a in sorted(five, key=lambda x: -(x.get("level") or 0))[:10]:
        lines.append(f"{a.get('name')} Lv.{a.get('level')} {a.get('actived_constellation_num')}命")
    return "\n".join(lines)


def weapons(args, ctx):
    uid = _pick_uid(args, ctx)
    avatars = _avatars(uid, _client(ctx))
    best = {}
    for a in avatars:
        w = a.get("weapon") or {}
        if w.get("rarity") == 5 and w.get("name"):
            best[w["name"]] = max(best.get(w["name"], 0), w.get("level") or 0)
    if not best:
        return "没有拿到五星武器"
    lines = [f"【五星武器】{len(best)} 把"]
    for name, level in sorted(best.items(), key=lambda kv: -kv[1])[:10]:
        lines.append(f"{name} Lv.{level}")
    return "\n".join(lines)


def daily_note(args, ctx):
    uid = _pick_uid(args, ctx)
    _cn_only(server_of(uid))
    client = _client(ctx)
    fp = client.device_fp()
    query = urllib.parse.urlencode({"role_id": uid, "server": server_of(uid)})
    payload = cached(f"note{query}", lambda: client.get(
        "record", "/game_record/app/genshin/api/dailyNote", query,
        extra={"x-rpc-device_fp": fp}))
    data = check(payload)
    resin, mx = data.get("current_resin"), data.get("max_resin")
    recovery = _dur(data.get("resin_recovery_time"))
    lines = [f"【体力】{resin}/{mx}" + (f"（{recovery}回满）" if recovery else "（已回满）")]
    if data.get("total_task_num") is not None:
        lines.append(f"委托 {data.get('finished_task_num')}/{data.get('total_task_num')}")
    expeditions = data.get("expeditions") or []
    if expeditions:
        done = sum(1 for e in expeditions if e.get("status") == "Finished")
        lines.append(f"派遣 {done}/{len(expeditions)} 已探索完成")
    if data.get("current_home_coin") is not None:
        lines.append(f"洞天圆票 {data.get('current_home_coin')}/{data.get('max_home_coin')}")
    return "\n".join(lines)


def zzz_note(args, ctx):
    uid = _pick_uid(args, ctx)
    server = zzz_server_of(uid)
    if server != "prod_gf_cn":
        raise MiyousheError("绝区零国际服暂未接入")
    client = _client(ctx)
    fp = client.device_fp()
    query = urllib.parse.urlencode({"role_id": uid, "server": server})
    payload = cached(f"zzz{query}", lambda: client.get(
        "record", "/event/game_record_zzz/api/zzz/note", query,
        extra={"x-rpc-device_fp": fp}))
    data = check(payload)
    lines = ["【绝区零】"]
    for key, name in (("energy", "电量"), ("vitality", "活力"), ("bangboo", "邦布")):
        item = data.get(key) or {}
        if item.get("max"):
            recovery = _dur(item.get("restore"))
            lines.append(f"{name} {item.get('current')}/{item.get('max')}"
                         + (f"（{recovery}回满）" if recovery else "（已回满）"))
    return "\n".join(lines)


def ledger(args, ctx):
    uid = _pick_uid(args, ctx)
    _cn_only(server_of(uid))
    client = _client(ctx)
    month = _month_of(args)
    query = urllib.parse.urlencode(
        {"month": month, "bind_uid": uid, "bind_region": server_of(uid)})
    payload = cached(f"ledger{query}", lambda: client.get(
        "hk4e", "/event/ys_ledger/monthInfo", query))
    md = check(payload).get("month_data") or {}
    lines = [f"【札记 {month[:4]}年{int(month[4:])}月】"]
    lines.append(f"原石 {md.get('current_primogems')}"
                 f"（上月 {md.get('last_primogems')}，{md.get('primogems_rate')}%）")
    lines.append(f"摩拉 {md.get('current_mora')}"
                 f"（上月 {md.get('last_mora')}，{md.get('mora_rate')}%）")
    for g in (md.get("group_by") or [])[:3]:
        lines.append(f"· {g.get('action')} {g.get('num')}（{g.get('percent')}%）")
    return "\n".join(lines)


def _month_of(args):
    m = re.search(r"(?:(\d{4})\s*年?\s*)?(1[0-2]|[1-9])\s*月", args or "")
    if not m:
        return time.strftime("%Y%m")
    year = m.group(1) or time.strftime("%Y")
    return f"{year}{int(m.group(2)):02d}"


def seven_saints(args, ctx):
    uid = _pick_uid(args, ctx)
    _cn_only(server_of(uid))
    client = _client(ctx)
    query = urllib.parse.urlencode({"role_id": uid, "server": server_of(uid)})
    info = check(cached(f"gcg{query}", lambda: client.get(
        "record", "/game_record/app/genshin/api/gcg/basicInfo", query)))
    decks = check(client.get(
        "record", "/game_record/app/genshin/api/gcg/deckList", query))
    deck_list = decks.get("deck_list") or []
    lines = [f"【七圣召唤 · {info.get('nickname') or '旅行者'}】牌组 {len(deck_list)} 套"]
    for d in deck_list[:5]:
        lines.append(f"· {d.get('name')}（角色牌 {len(d.get('avatar_cards') or [])} "
                     f"行动牌 {len(d.get('action_cards') or [])}）")
    return "\n".join(lines)


# ---------------------------------------------------------------- 注册表


def _bounded(fn, need_ck=True):
    """包一层截断，并标记该 handler 是否需要本人凭证（issue #41-A）。

    issue #169 PB：与 sixty._bounded 合流到 wg_text.bounded_handler。本模块业务上限
    REPLY_CLIP=480，`need_ck` 默认 True 是**刻意的**——新加的 handler 忘了标，宁可
    被绑定检查拦住，也不能悄悄绕过隐私拦截。`need_ck` 只在这里落属性、不改 HANDLERS
    元组形状：commands.py 与测试里有 6 处按 `v[1]` 解包，加一列会连带改一圈。
    """
    return wg_text.bounded_handler(fn, limit=REPLY_CLIP, need_ck=need_ck)


def _news_handler(kind):
    return lambda _args, _ctx: news(kind)


def _search_handler(suffix):
    return lambda args, _ctx: search_posts((args or "").strip() + suffix,
                                           label=(args or "").strip() + suffix)


HANDLERS = {
    # need_ck=False：BBS / 静态接口，免登录（issue #2 附录 A §4、§6.2、§7）
    "公告": ("原神公告列表", _bounded(_news_handler("公告"), need_ck=False)),
    "活动": ("原神活动列表", _bounded(_news_handler("活动"), need_ck=False)),
    "资讯": ("原神资讯列表", _bounded(_news_handler("资讯"), need_ck=False)),
    "兑换码": ("前瞻直播兑换码", _bounded(live_codes, need_ck=False)),
    "米游社": ("米游社帖子搜索", _bounded(lambda args, _ctx: search_posts(args), need_ck=False)),
    "攻略": ("角色攻略搜索", _bounded(_search_handler("攻略"), need_ck=False)),
    "抽卡记录": ("抽卡记录（发游戏内链接）", _bounded(gacha_log, need_ck=False)),
    "探索": ("探索度统计（需 CK）", _bounded(exploration)),
    "深渊": ("本期深渊战绩（需 CK）", _bounded(abyss)),
    "角色": ("角色列表（需 CK）", _bounded(characters)),
    "武器": ("五星武器（需 CK）", _bounded(weapons)),
    "体力": ("原神树脂（需 CK）", _bounded(daily_note)),
    "树脂": ("同 #体力（需 CK）", _bounded(daily_note)),
    "原石": ("札记原石统计（需 CK）", _bounded(ledger)),
    "札记": ("同 #原石（需 CK）", _bounded(ledger)),
    "绝区零体力": ("绝区零电量（需 CK）", _bounded(zzz_note)),
    "七圣召唤": ("七圣召唤牌组（需 CK）", _bounded(seven_saints)),
}
