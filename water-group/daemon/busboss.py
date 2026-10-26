"""车盈网（busboss）城际班次取数层：``#班次`` 指令的后端（issue #103）。

平台是「广州市悦到信息科技有限公司 / 车盈网」，广达出行等小程序都是它的白标租户：
同一个后端按查询参数 ``appId`` 区分租户，**不是请求头**。所以这里把 ``appid``
做成参数，默认值只是一个已验证过的租户。

契约照**抓包 + 解包源码**钉住，不照文档猜（该平台没有对外文档）：

* 鉴权是 OpenIddict / OAuth2，客户端固定 ``client_id=WeChatMiniProgram``，
  ``offline_access`` 已授权。换 token 只能走微信 ``code`` 或 ``refresh_token``，
  外部造不出来——凭证由部署者本机抓一次包取得，**不入库**。
* ``access_token`` 是 RS256 JWT，**1 小时**有效；refresh 分支**开启轮换**，
  每次刷新都会换一个新的 ``refresh_token``。必须写回，否则下次失效。
* 节点参数 ``value`` 是**嵌套对象**，不是标量：

      {"type": "Region", "value": {"id": "4401", "level": 1}}   ✓
      {"type": "Region", "value": "4401"}                        ✗ → totalCount=0

  第二种**不报错、静默返回空列表**。2026-10-26 在这上面误判过一整轮
  「这条城际没班次」，所以本模块只暴露 :func:`node` 一个构造点，任何调用方
  都不许手写这两个键。

边界（别悄悄扩）：

* 只读：班次 / 日历 / 城市树。下单、支付、锁座一律不碰。
* 站点级筛选是**客户端行为**：``type="Station"`` 直传 nodeId 上游返回 0 条，
  平铺 ``startNodeId`` 会被静默忽略。正确做法是拉全量后在本地对
  ``startNodeList`` 求交集——小程序自己也是这么做的。
* 群消息有长度上限：渲染层按行截断，宁可少列几条也不让 OneBot 段超长。
"""
import datetime
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from wg_daemon import logger
from wg_paths import logs_dir, state_dir

BASE = "https://sale-api-public.dz.busboss.cn"
AUTH_URL = "https://sale-auth.dz.busboss.cn/connect/token"
APPID = "wxbe8356b6339fcc41"
CLIENT_ID = "WeChatMiniProgram"
SCOPE = ("openid email address phone profile offline_access WeChatService "
         "CustomBusService FileManagementService ByteDanceService UnifiedPaymentService")

TIMEOUT = 8
REFRESH_MARGIN = 300          # 提前 5 分钟续期，别卡在请求中间过期
CLASS_TTL = 60.0              # 同一条线路连着几个人问是常态
CITY_TTL = 86400.0
CACHE_MAX = 64
MAX_CLASSES = 200             # 单次查询最多翻这么多班，防止被上游拖死
PAGE_SIZE = 50
MAX_DAYS = 15                 # 日历接口只给 15 天

REPLY_CLIP = 1200

TOKEN_FILE = state_dir() / "busboss_tokens.json"
CITY_FILE = state_dir() / "busboss_cities.json"
LOG = logs_dir() / "wg_busboss.log"

log = logger(LOG)

NO_TOKEN_HINT = ("车盈网凭证未配置：把抓到的 token 写进 state/busboss_tokens.json，"
                 "或跑 python water-group/daemon/busboss.py --status 看状态")

_PROVINCE_TAIL = re.compile(r"(省|市|自治区|特别行政区|自治州|地区|盟)$")
_DATE_FULL = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_DATE_SHORT = re.compile(r"(\d{1,2})[-/.](\d{1,2})")


class BusbossError(Exception):
    """取数层统一错误。``str(exc)`` 直接当回复文案，所以消息必须面向群友。"""


class AuthExpired(BusbossError):
    """401：token 过期或被吊销。调用方可以续期一次再重试。"""


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _opener():
    """不走系统代理：本机装了代理时，直连比走代理更稳（同 sixty）。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(req, timeout):
    try:
        with _opener().open(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        detail = _abp_message(raw)
        if exc.code == 401:
            raise AuthExpired("凭证失效，需要重新获取") from exc
        log(f"http {exc.code} {req.full_url.split('?')[0]}: {detail}")
        raise BusbossError(detail or f"车盈网接口返回 {exc.code}") from exc
    except BusbossError:
        raise
    except Exception as exc:  # noqa: BLE001  网络层失败统一翻译
        log(f"http failed {req.full_url.split('?')[0]}: {type(exc).__name__}")
        raise BusbossError("车盈网接口暂时不可用（超时或断网），稍后再试") from exc


def _abp_message(raw):
    """ABP 错误信封 ``{"error":{"message":...}}`` → 一句能看的话。"""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(payload, dict):
        return ""
    err = payload.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or "").strip()
    return str(payload.get("message") or "").strip()


def _post_json(path, body, token, cfg=None, appid=None):
    cfg = cfg or {}
    url = f"{BASE.rstrip('/')}{path}?appId={appid or APPID}"
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "platform": "mp-weixin",
                 "Accept": "application/json", "Authorization": f"Bearer {token}"})
    raw = _request(req, cfg.get("timeout") or TIMEOUT)
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        log(f"decode failed {path}")
        raise BusbossError("车盈网返回了无法解析的数据") from exc


def _post_form(url, fields, cfg=None):
    cfg = cfg or {}
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    raw = _request(req, cfg.get("timeout") or TIMEOUT)
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise BusbossError("鉴权服务返回了无法解析的数据") from exc


# --------------------------------------------------------------------------
# 凭证
# --------------------------------------------------------------------------
def _exp_of(jwt):
    """读 JWT 的 exp，不验签。解不出来就返回 0（当作已过期）。"""
    try:
        part = jwt.split(".")[1]
        part += "=" * (-len(part) % 4)
        import base64  # noqa: PLC0415  只在续期路径上用
        return int(json.loads(base64.urlsafe_b64decode(part)).get("exp") or 0)
    except Exception:  # noqa: BLE001
        return 0


def load_tokens(path=None):
    """读凭证。文件缺失/损坏都返回 None——调用方据此给配置指引，不抛异常。"""
    p = Path(path or TOKEN_FILE)
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("access_token") else None


def save_tokens(record, path=None):
    """原子写 + 0600。轮换后的 refresh_token 必须靠这里落盘。"""
    p = Path(path or TOKEN_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, p)
    return p


def refresh_tokens(record, cfg=None, path=None):
    """用 refresh_token 续期，**成功才写回**。

    失败时原文件保持不动：一次网络抖动不该把还能用的凭证抹掉。
    轮换开启，所以返回的是全新的一对；上游万一没给新的 refresh_token
    （ReUse 模式）就沿用旧的。
    """
    rt = str((record or {}).get("refresh_token") or "").strip()
    if not rt:
        raise BusbossError("凭证缺少 refresh_token，需要重新获取")
    payload = _post_form(AUTH_URL, {
        "grant_type": "refresh_token", "client_id": CLIENT_ID,
        "scope": SCOPE, "appid": (record or {}).get("appid") or APPID,
        "refresh_token": rt,
    }, cfg)
    access = str(payload.get("access_token") or "")
    if not access:
        raise BusbossError("续期没有返回 access_token，需要重新获取")
    new = dict(record or {})
    new.update({
        "access_token": access,
        "refresh_token": str(payload.get("refresh_token") or rt),
        "exp": int(payload.get("expires_in") or 3600) + int(time.time()),
        "appid": new.get("appid") or APPID,
        "client_id": CLIENT_ID,
    })
    save_tokens(new, path)
    log("token refreshed, expires_in=%s" % payload.get("expires_in"))
    return new


def access_token(cfg=None, force=False, path=None):
    """拿一个可用的 access_token，必要时先续期。"""
    record = load_tokens(path)
    if not record:
        raise BusbossError(NO_TOKEN_HINT)
    exp = int(record.get("exp") or 0) or _exp_of(str(record.get("access_token") or ""))
    if force or exp - int(time.time()) < REFRESH_MARGIN:
        record = refresh_tokens(record, cfg, path)
        exp = int(record.get("exp") or 0) or _exp_of(str(record["access_token"]))
    if exp and exp <= int(time.time()):
        raise BusbossError("凭证已过期且续期失败，需要重新获取")
    return str(record["access_token"])


def _call(path, body, cfg=None, appid=None, token_path=None):
    """带一次自动续期的调用：401 时续期重试一次，第二次仍失败就认输。"""
    token = access_token(cfg, path=token_path)
    try:
        return _post_json(path, body, token, cfg, appid)
    except AuthExpired:
        log("401 on %s, refreshing once" % path)
        token = access_token(cfg, force=True, path=token_path)
        return _post_json(path, body, token, cfg, appid)


# --------------------------------------------------------------------------
# 节点参数：唯一构造点
# --------------------------------------------------------------------------
def node(node_id, level=1, node_type="Region"):
    """构造一个节点参数。

    ``Region`` 一类必须包成 ``{"id","level"}``；``Station``/``Line`` 一类走直通
    分支（``value`` 是标量）。这个分支照上游前端 ``formatNodeParams`` 抄的，
    写成一份是为了让「手写字符串 value」这件事在代码里没有落脚点。
    """
    text = str(node_id or "").strip()
    if not text:
        raise BusbossError("节点参数为空")
    if node_type in ("Station", "Line", "LineId", "TagName", "Group"):
        return {"type": node_type, "value": text}
    return {"type": node_type, "value": {"id": text, "level": int(level)}}


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------
_CACHE = {}
_LOCK = threading.Lock()


def cached(key, fetch_fn, ttl):
    """TTL 缓存；**失败结果不进缓存**——一次抖动不该被放大成一整段 TTL。"""
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    data = fetch_fn()
    with _LOCK:
        if len(_CACHE) >= CACHE_MAX:
            for k in [k for k, (exp, _v) in _CACHE.items() if exp <= now]:
                _CACHE.pop(k, None)
            overflow = len(_CACHE) - CACHE_MAX + 1
            if overflow > 0:
                for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:overflow]:
                    _CACHE.pop(k, None)
        _CACHE[key] = (now + ttl, data)
    return data


def clear_cache():
    with _LOCK:
        _CACHE.clear()


# --------------------------------------------------------------------------
# 城市树
# --------------------------------------------------------------------------
def normalize_city(raw):
    """「广州市」→「广州」；「广东省」→「广东」；空白与全角空格一并去掉。"""
    text = re.sub(r"[\s\u3000]+", "", str(raw or ""))
    return _PROVINCE_TAIL.sub("", text)


def _fetch_cities(cfg=None, token_path=None):
    """拉城市树（省 → 市 → 区）。区级只用于展示，v1 不按区查询。"""
    payload = _call("/api/custombus/standard-node/v2/start",
                    {"withDetail": False, "type": "Region"}, cfg, token_path=token_path)
    rows = payload if isinstance(payload, list) else []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("firstTagCode") or "").strip()
        name = str(row.get("firstTagName") or "").strip()
        if not code or not name:
            continue
        out.append({"code": code, "name": name,
                    "province": str(row.get("provinceName") or "").strip(),
                    "province_code": str(row.get("provinceCode") or "").strip()})
    if not out:
        raise BusbossError("城市列表为空，车盈网接口可能改了")
    return out


def cities(cfg=None, path=None, force=False, token_path=None):
    """城市列表，落盘缓存 24 小时。``path`` 是缓存文件，``token_path`` 是凭证。"""
    city_path = Path(path or CITY_FILE)
    now = time.time()
    if not force:
        try:
            blob = json.loads(city_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            blob = None
        if isinstance(blob, dict) and blob.get("exp", 0) > now and blob.get("items"):
            return blob["items"]
    items = _fetch_cities(cfg, token_path)
    city_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        city_path.write_text(json.dumps({"exp": now + CITY_TTL, "items": items},
                                        ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log(f"city cache write failed: {exc}")
    return items


def resolve_city(raw, cfg=None, path=None, token_path=None):
    """城市名 → (code, 显示名)。重名/查不到都报候选，绝不随便挑一个。"""
    want = normalize_city(raw)
    if not want:
        raise BusbossError("城市名不能为空")
    items = cities(cfg, path, token_path=token_path)
    exact = [it for it in items if normalize_city(it["name"]) == want]
    if len(exact) == 1:
        return exact[0]["code"], exact[0]["name"]
    if len(exact) > 1:
        names = "、".join(f"{it['name']}（{it['province']}）" for it in exact[:5])
        raise BusbossError(f"「{want}」有多个同名城市：{names}，请写全一点")
    fuzzy = [it for it in items if want in normalize_city(it["name"])]
    if len(fuzzy) == 1:
        return fuzzy[0]["code"], fuzzy[0]["name"]
    if fuzzy:
        names = "、".join(it["name"] for it in fuzzy[:6])
        raise BusbossError(f"「{want}」能对上多个城市：{names}，请写具体一点")
    raise BusbossError(f"车盈网没有「{want}」这个城市，换个写法试试（不用带「市」字）")


# --------------------------------------------------------------------------
# 日期
# --------------------------------------------------------------------------
_DATE_WORDS = ("今天", "今日", "明天", "明日", "后天", "大后天",
               "昨天", "昨日", "前天", "today", "tomorrow")


def parse_date(raw, today=None):
    """日期归一成 ``YYYY-MM-DD``。

    支持 ``今天/明天/后天/大后天``、``YYYY-MM-DD``、``MM-DD``。非法输入**报错
    不猜**：猜错日期比不回答更坏。超出 15 天日历窗口同样报错。
    """
    base = today or datetime.date.today()
    text = str(raw or "").strip()
    if not text or text in ("今天", "今日", "today"):
        return base.isoformat()
    if text in ("明天", "明日", "tomorrow"):
        return (base + datetime.timedelta(days=1)).isoformat()
    if text in ("后天",):
        return (base + datetime.timedelta(days=2)).isoformat()
    if text in ("大后天",):
        return (base + datetime.timedelta(days=3)).isoformat()
    if text in ("昨天", "昨日", "前天"):
        raise BusbossError(f"{text}已经过去了，只能查今天起 {MAX_DAYS} 天内的班次")
    day = None
    m = _DATE_FULL.fullmatch(text)
    if m:
        day = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    else:
        m = _DATE_SHORT.fullmatch(text)
        if m:
            day = _safe_date(base.year, int(m.group(1)), int(m.group(2)))
    if day is None:
        raise BusbossError(f"看不懂日期「{text}」，用 今天 / 明天 / 后天 / 2026-10-27 这种写法")
    delta = (day - base).days
    if delta < 0:
        raise BusbossError(f"{day.isoformat()} 已经过去了")
    if delta >= MAX_DAYS:
        raise BusbossError(f"只能查今天起 {MAX_DAYS} 天内的班次")
    return day.isoformat()


def _safe_date(year, month, day):
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def looks_like_date(text):
    """参数判别用：**结构上**像日期就返回 True。

    注意这里不判「能不能查」——``昨天`` / 超窗口的日期也算像日期，只是
    ``parse_date`` 会带着明确原因报错。若按「能解析」判，``#班次 广州 珠海
    昨天`` 会掉进站点关键词分支，回一句「没有含『昨天』的站点」，把用户带偏。
    """
    t = str(text or "").strip()
    if t.lower() in _DATE_WORDS:
        return True
    return bool(_DATE_FULL.fullmatch(t) or _DATE_SHORT.fullmatch(t))


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------
def fetch_classes(origin_code, dest_code, class_date, cfg=None, token_path=None):
    """拉某个城市对某一天的全部班次，返回 ``(items, total_count)``。

    上游分页，单页 50。翻到 ``totalCount`` 或 ``MAX_CLASSES`` 为止——群消息
    里用不到 200 条以后的班次，多翻只是把上游拖慢。
    """
    cache_key = f"classes|{origin_code}|{dest_code}|{class_date}"
    ttl = float((cfg or {}).get("cache_seconds") or CLASS_TTL)

    def _load():
        items, total, skip = [], 0, 0
        cap = int((cfg or {}).get("max_classes") or MAX_CLASSES)
        while skip < cap:
            payload = _call("/api/custombus/line-class-day/v2/list", {
                "skipCount": skip, "maxResultCount": min(PAGE_SIZE, cap - skip),
                "classDate": class_date,
                "start": node(origin_code), "end": node(dest_code),
            }, cfg, token_path=token_path)
            if not isinstance(payload, dict):
                raise BusbossError("车盈网返回了无法解析的班次数据")
            batch = payload.get("items") or []
            total = int(payload.get("totalCount") or 0)
            items.extend(x for x in batch if isinstance(x, dict))
            if not batch or len(items) >= total:
                break
            skip += len(batch)
        return {"items": items, "total": total}

    return cached(cache_key, _load, ttl)


def _price(item):
    try:
        return float(item.get("lineClassDayPrice") or 0)
    except (TypeError, ValueError):
        return 0.0


def _hm(stamp):
    """``06:30:00`` → ``06:30``；异常值原样短截。"""
    return str(stamp or "")[:5]


def _station_names(item, side):
    key = "startNodeList" if side == "start" else "arrivalNodeList"
    return [n for n in (item.get(key) or []) if isinstance(n, dict)]


def filter_by_station(items, keyword, side="start"):
    """按上/下车站关键词过滤，返回 ``(命中的站点名, 班次列表, 候选站点名)``。

    关键词对不上唯一站点时返回候选，由渲染层原样回给群友——静默空列表会
    让人以为「这条线路没有这个站」。
    """
    want = re.sub(r"[\s\u3000]+", "", str(keyword or ""))
    if not want:
        return "", items, []
    hits = {}
    for item in items:
        for station in _station_names(item, side):
            name = str(station.get("nodeName") or "")
            if want in re.sub(r"[\s\u3000]+", "", name):
                hits[name] = hits.get(name, 0) + 1
    if not hits:
        return "", [], []
    # 整名命中优先：`区庄地铁站C口前方公交站` 是另一个站
    # `区庄地铁站C口前方公交站(公路路政门口)` 的前缀，只按包含关系判会让
    # 「写全名反而更歧义」，与直觉相反。
    exact = [name for name in hits if re.sub(r"[\s\u3000]+", "", name) == want]
    if len(exact) == 1:
        name = exact[0]
    elif not exact and len(hits) == 1:
        name = next(iter(hits))
    else:
        pool = exact or list(hits)
        return "", [], sorted(pool, key=lambda k: (-hits[k], len(k), k))
    kept = [it for it in items
            if any(str(s.get("nodeName") or "") == name for s in _station_names(it, side))]
    return name, kept, []


def _pick_station(item, name, side):
    for station in _station_names(item, side):
        if str(station.get("nodeName") or "") == name:
            return station
    return {}


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def _lines_budget(cfg):
    try:
        return max(1, int((cfg or {}).get("max_lines") or 8))
    except (TypeError, ValueError):
        return 8


def _clip(text, limit=REPLY_CLIP):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def render_summary(origin, dest, class_date, items, total, cfg=None):
    """城市级概览：班次数、首末班、票价区间、主要线路。"""
    if not items:
        return f"{origin} → {dest} {class_date}\n该日期暂无班次"
    items = sorted(items, key=lambda x: (str(x.get("classTime") or ""), _price(x)))
    prices = [p for p in (_price(x) for x in items) if p > 0]
    cheapest = min(items, key=_price)
    lines = {}
    for item in items:
        name = str(item.get("lineName") or "未命名线路")
        bucket = lines.setdefault(name, {"count": 0, "price": _price(item)})
        bucket["count"] += 1
        bucket["price"] = min(bucket["price"], _price(item))
    top = sorted(lines.items(), key=lambda kv: (-kv[1]["count"], kv[1]["price"]))

    head = f"{origin} → {dest} {class_date}"
    span = (f"共 {total or len(items)} 班 · 首 {_hm(items[0].get('classTime'))}"
            f" · 末 {_hm(items[-1].get('classTime'))}")
    if prices:
        span += f" · ¥{min(prices):g}~{max(prices):g}"
    span += f" · {len(lines)} 条线路"
    out = [head, span,
           f"最便宜 {_hm(cheapest.get('classTime'))} "
           f"{cheapest.get('lineName')} ¥{_price(cheapest):g} "
           f"余{cheapest.get('canSaleCount') or 0}"]
    out.append("主要线路")
    for name, info in top[:min(_lines_budget(cfg), 6)]:
        out.append(f"· {name} {info['count']}班 ¥{info['price']:g}")
    if len(top) > 6:
        out.append(f"…另有 {len(top) - 6} 条线路")
    out.append("看某站上车时刻：#班次 起点 终点 站点名")
    return _clip("\n".join(out))


def render_station(origin, dest, class_date, station, items, cfg=None, side="start"):
    """站点级：某个上/下车站当天各班的时刻 + 票价 + 余票。

    ``side="start"`` 是「从这个站上车」，左列是该站的候车时刻；``side="arrival"``
    是「到这个站下车」，右列是该站的到达时刻。群友说「广州 珠海 拱北」时想要
    的多半是后者，所以 ``answer`` 在起点侧对不上时会自动改看终点侧。
    """
    if not items:
        return f"{origin} → {dest}「{station}」{class_date}\n该站当天没有班次"
    items = sorted(items, key=lambda x: str(x.get("classTime") or ""))
    rows = []
    for item in items:
        starts = _station_names(item, "start")
        if side == "start":
            left = _hm(_pick_station(item, station, "start").get("classTime")
                       or item.get("classTime"))
            arrive = _station_names(item, "arrival")
            right = _hm(arrive[-1].get("classTime")) if arrive else "—"
        else:
            left = _hm(starts[0].get("classTime")) if starts else _hm(item.get("classTime"))
            right = _hm(_pick_station(item, station, "arrival").get("classTime")) or "—"
        rows.append(f"{left} → {right} ¥{_price(item):g} "
                    f"余{item.get('canSaleCount') or 0} {item.get('lineName')}")
    budget = _lines_budget(cfg)
    verb = "上车" if side == "start" else "下车"
    out = [f"{origin} → {dest}「{station}」（{verb}）{class_date}",
           f"{len(items)} 班 · 首 {_hm(items[0].get('classTime'))}"
           f" · 末 {_hm(items[-1].get('classTime'))}"]
    out.extend(rows[:budget])
    if len(rows) > budget:
        out.append(f"…另有 {len(rows) - budget} 班")
    return _clip("\n".join(out))


def split_args(args):
    """``起点 终点 [日期|站点] [日期]`` → ``(origin, dest, date_text, station)``。

    第三、四段按**内容**判别而不是按位置：能解析成日期就当日期，否则当上车站
    关键词。这样 ``#班次 广州 珠海 明天`` 与 ``#班次 广州 珠海 区庄`` 都不用
    额外记语法。
    """
    tokens = [t for t in re.split(r"[\s\u3000]+", str(args or "").strip()) if t]
    if len(tokens) < 2:
        raise BusbossError("用法：#班次 起点 终点 [日期] [上车站]\n"
                           "例：#班次 广州 珠海 明天")
    if len(tokens) > 4:
        raise BusbossError("参数太多，最多四段：起点 终点 日期 上车站")
    origin, dest = tokens[0], tokens[1]
    date_text, station = "", ""
    for token in tokens[2:]:
        if not date_text and looks_like_date(token):
            date_text = token
        elif not station:
            station = token
        else:
            raise BusbossError("参数重复了，日期和上车站各写一次就够")
    return origin, dest, date_text, station


def answer(args, cfg=None, token_path=None, city_path=None, today=None):
    """``#班次`` 的完整流程：解析 → 查城市 → 拉班次 → 渲染。

    ``token_path`` 与 ``city_path`` 分开：一个是凭证、一个是城市缓存，混用会
    出现「拿 token 文件当城市缓存读」这种只在测试里才暴露的错。
    """
    cfg = cfg or {}
    origin_raw, dest_raw, date_text, station_kw = split_args(args)
    class_date = parse_date(date_text, today)
    origin_code, origin = resolve_city(origin_raw, cfg, city_path, token_path)
    dest_code, dest = resolve_city(dest_raw, cfg, city_path, token_path)
    if origin_code == dest_code:
        raise BusbossError("起点和终点是同一个城市")
    payload = fetch_classes(origin_code, dest_code, class_date, cfg, token_path)
    items, total = payload["items"], payload["total"]
    if not station_kw:
        return render_summary(origin, dest, class_date, items, total, cfg)
    # 先看起点侧（「从这个站上车」），对不上再看终点侧（「到这个站下车」）：
    # 群友写「广州 珠海 拱北」时想要的多半是后者。
    for side in ("start", "arrival"):
        station, kept, candidates = filter_by_station(items, station_kw, side)
        if candidates:
            names = "、".join(candidates[:4])
            raise BusbossError(f"「{station_kw}」能对上多个{'上' if side == 'start' else '下'}车站："
                               f"{names}，写具体一点（例如 {candidates[0]}）")
        if station:
            return render_station(origin, dest, class_date, station, kept, cfg, side)
    raise BusbossError(f"{origin} ↔ {dest} 两侧都没有含「{station_kw}」的站点。"
                       f"去掉关键词看全部班次：#班次 {origin_raw} {dest_raw} {class_date}")


# --------------------------------------------------------------------------
# CLI：凭证自检与导入（部署者在本机用，不进群）
# --------------------------------------------------------------------------
def _status():
    record = load_tokens()
    if not record:
        print(f"未配置凭证：{TOKEN_FILE} 不存在")
        return 1
    exp = int(record.get("exp") or 0) or _exp_of(str(record.get("access_token") or ""))
    left = exp - int(time.time())
    print(f"凭证文件: {TOKEN_FILE}")
    print(f"appid    : {record.get('appid') or APPID}")
    print(f"access   : {'有' if record.get('access_token') else '缺'}"
          f"  剩余 {left // 60} 分钟")
    print(f"refresh  : {'有' if record.get('refresh_token') else '缺'}")
    if left < REFRESH_MARGIN:
        print("（下次调用会自动续期）")
    return 0


def _import(path):
    """从抓包记录或裸 JSON 导入凭证。抓包格式见 doc/SLASH_COMMANDS.md。"""
    raw = Path(path).read_text(encoding="utf-8")
    record = None
    try:
        blob = json.loads(raw)
    except ValueError:
        blob = None
    if isinstance(blob, dict) and blob.get("access_token"):
        record = blob
    else:
        rows = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        for row in reversed(rows):
            body = row.get("resp_body") if isinstance(row, dict) else None
            if not body:
                continue
            try:
                payload = json.loads(body)
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get("access_token"):
                record = payload
                break
    if not record:
        print("导入失败：没找到 access_token")
        return 1
    new = {"access_token": record["access_token"],
           "refresh_token": record.get("refresh_token") or "",
           "exp": int(record.get("expires_in") or 3600) + int(time.time()),
           "appid": APPID, "client_id": CLIENT_ID}
    save_tokens(new)
    print(f"已写入 {TOKEN_FILE}（refresh {'有' if new['refresh_token'] else '缺'}）")
    return 0


def main(argv=None):
    import argparse  # noqa: PLC0415
    parser = argparse.ArgumentParser(description="车盈网班次取数层（#班次 后端）")
    parser.add_argument("--status", action="store_true", help="看凭证状态")
    parser.add_argument("--import", dest="import_path", metavar="FILE",
                        help="从抓包记录或裸 JSON 导入凭证")
    parser.add_argument("--query", metavar="ARGS", help="按 #班次 的参数查一次并打印")
    parser.add_argument("--refresh", action="store_true", help="强制续期一次")
    args = parser.parse_args(argv)
    try:
        if args.status:
            return _status()
        if args.import_path:
            return _import(args.import_path)
        if args.refresh:
            record = load_tokens()
            if not record:
                print(NO_TOKEN_HINT)
                return 1
            refresh_tokens(record)
            print("续期完成")
            return 0
        if args.query:
            print(answer(args.query))
            return 0
    except BusbossError as exc:
        print(str(exc))
        return 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
