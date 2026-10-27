"""60s API 资讯 handler（issue #53 方案 A）：commands.py 动态指令的资讯后端。

数据源是 vikiboss/60s（MIT，https://github.com/vikiboss/60s ）的自建实例
接口地址由 config.json 的 `sixty.base_url` 提供。契约照**上游源码**钉住（文档缺参数表，别照文档猜）：

  - 统一响应信封 {code, message, data}，**判成功只看 code===200**。404 也返回
    JSON（data.error 带「未找到城市」这类上游原文），HTTP status 不可信。
  - 天气定位参数叫 **query**，不是 city：`?query=广州` 走腾讯城市搜索；
    `city` / `province` 只是同音多选时的消歧参数。传 `city=` 会被**静默忽略**，
    永远返回实例所在城市的天气（实测北京）——2026-10-23 踩过，别再改回去。
  - 城市名不能带「市」「省」后缀：`?query=上海市` 直接 404 未找到城市。
  - 无鉴权、无 key；`/v2/60s` 一天一更，`api_updated` 是上游的更新时间。

边界（写死在文档里，别悄悄扩）：
  - 只透传「标题 + 链接」原文：不改写、不摘要、不存储；
  - 域名是自建单点：任何失败都降级成一句话，绝不把异常抛给 dispatch；
  - 端点白名单由 commands.py 的 allow 闸门把守，新增端点先过合规评审
    （禁 ip / QQ 资料 / 二维码 / og，后者是 SSRF 面）；
  - 群友位置只从记忆库 traits.location 读、只用于拼天气请求、**不回群**。
"""
import datetime
import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from wg_daemon import logger
from wg_paths import logs_dir
import wg_text  # issue #169 PB: _clip 合流到 wg_text.render_handler_reply

BASE_URL = ""
TIMEOUT = 6
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
REPLY_CLIP = 1200
LOG = logs_dir() / "wg_sixty.log"

# TTL 按端点分档：上游更新频率差一个数量级，缓存键带 base_url + path + 参数。
TTL = {
    "/v2/60s": 300.0,
    "/v2/weibo": 180.0,
    "/v2/zhihu": 180.0,
    "/v2/baidu/hot": 180.0,
    "/v2/bili": 180.0,
    "/v2/toutiao": 180.0,
    "/v2/rednote": 180.0,
    "/v2/douyin": 180.0,
    "/v2/weather/realtime": 600.0,
    "/v2/weather/forecast": 1800.0,
    "/v2/hitokoto": 30.0,
    # 行情类：上游汇率按天、油价按小时、金价十几分钟一刷。
    "/v2/exchange-rate": 3600.0,
    "/v2/fuel-price": 3600.0,
    "/v2/gold-price": 600.0,
    "/v2/douban/weekly/movie": 3600.0,
    "/v2/it-news/rank": 600.0,
    # 随机文案类：上游每次随机一条，TTL 只用来吸收同一秒的连刷，不用于「定住内容」。
    # 给太长会让群里连着几次看到同一段 KFC 文案。
    "/v2/kfc": 60.0,
    "/v2/duanzi": 30.0,
    "/v2/answer": 30.0,
    # 下面三条是「一天一份」的数据：缓存键带日期标签（cache_tag），跨天自然换键，
    # 所以 TTL 给一天，同一天只向上游取一次。
    "/v2/moyu": 86400.0,
    "/v2/today-in-history": 86400.0,
    "/v2/luck": 86400.0,
}
DEFAULT_TTL = 120.0
CACHE_MAX = 128  # 缓存条目上限：跨天换键的条目不会命中，不淘汰会一直堆内存

# 群友位置 → 天气 query 的归一化。存储样例：「广州（粤A）」「惠州市」「广东省」。
_PROVINCE_TAIL = re.compile(r"(省|市|自治区|特别行政区)$")


# issue #11 PB: 日志助手收口到 wg_daemon（写失败吞掉的语义不变）
log = logger(LOG)


class SixtyError(Exception):
    """取数层统一错误。str(exc) 直接当回复文案，所以消息必须面向群友。"""


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(path, params=None, base=None, timeout=None):
    """GET 一个端点，返回 data。信封异常与网络异常都翻成 SixtyError。"""
    url = (base or BASE_URL).rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    try:
        with _opener().open(req, timeout=timeout or TIMEOUT) as resp:
            raw = resp.read()
    except Exception as exc:  # noqa: BLE001  网络层失败统一翻译，类型不外抛
        log(f"fetch {path} failed: {type(exc).__name__}")
        raise SixtyError("资讯源暂不可用（超时或断网），稍后再试") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError as exc:  # 含 UnicodeDecodeError / JSONDecodeError
        # 反代 502 的 HTML 错误页走到这里：报文收得回来，但根本不是 JSON。
        # 以前和网络异常共用一句「超时或断网」，排查会被带到网络层（2026-10-23 修）。
        log(f"fetch {path} decode failed: {type(exc).__name__}")
        raise SixtyError("资讯源返回了无法解析的数据，稍后再试") from exc
    if not isinstance(payload, dict):
        raise SixtyError("资讯源返回了无法解析的数据")
    code = payload.get("code")
    if code != 200:
        data = payload.get("data")
        detail = str(data.get("error") or "") if isinstance(data, dict) else ""
        if code == 404 and "未找到城市" in detail:
            raise SixtyError("查不到这个城市的天气，换个写法试试（不用带「市」字）")
        log(f"fetch {path} -> code={code} {detail or payload.get('message')}")
        raise SixtyError(f"资讯源返回错误（code={code}），稍后再试")
    return payload.get("data")


_CACHE = {}
_LOCK = threading.Lock()


def cached(key, fetch_fn, ttl):
    """TTL 缓存；**失败结果不进缓存**——失败落缓存会把一次抖动放大成一整段
    TTL（米游社 cached() 同款教训，见 miyoushe.py:177）。"""
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    data = fetch_fn()  # 抛异常则什么都不写进缓存
    with _LOCK:
        _store(key, now + ttl, data)
    return data


def _store(key, expires, data):
    """写缓存顺带淘汰：先清过期的，仍超上限就按到期时间踢最早的一批。

    键里带日期标签的条目（#摸鱼 / #历史 / #运势）跨天后不再命中，只靠 TTL
    永远等不到清理时机，所以写入时主动收。
    """
    if len(_CACHE) >= CACHE_MAX:
        now = time.monotonic()
        for k in [k for k, (exp, _v) in _CACHE.items() if exp <= now]:
            _CACHE.pop(k, None)
        overflow = len(_CACHE) - CACHE_MAX + 1
        if overflow > 0:
            for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:overflow]:
                _CACHE.pop(k, None)
    _CACHE[key] = (expires, data)


def _get(path, cfg, params=None, cache_tag=""):
    """带 base_url 覆盖、超时与 TTL 缓存的 GET。cfg 是 ctx["sixty"]。

    cache_tag 只参与缓存键、**不进请求**：按日期钉住当日数据的条目靠它跨天换键。
    """
    cfg = cfg or {}
    base = str(cfg.get("base_url") or "").strip() or BASE_URL
    if not base:
        raise SixtyError("资讯源未配置 base_url，请在 config.json 的 sixty 段设置")
    timeout = cfg.get("timeout") or TIMEOUT
    key = f"{base}|{path}|{urllib.parse.urlencode(params or {})}|{cache_tag}"
    return cached(key,
                  lambda: fetch(path, params, base=base, timeout=timeout),
                  TTL.get(path, DEFAULT_TTL))


def normalize_city(raw):
    """「广州（粤A）」→「广州」；「广东省广州市」→「广州」；「广州市」→「广州」。

    腾讯城市搜索对「市 / 省」后缀直接 404，所以剥后缀是**功能必需**，不是美化。
    剥不干净的（「广东」这种省级、或者乱打的词）原样返回，让上游报错说人话。
    """
    city = re.split(r"[（(]", str(raw or ""))[0].strip()
    if not city:
        return ""
    if "省" in city:  # 「广东省广州市」→ 取省名后面那段
        city = city.rsplit("省", 1)[-1].strip()
    return _PROVINCE_TAIL.sub("", city).strip()


def _member_city(qq):
    """群友位置记忆：member_profiles.traits.location（画像卡「地域:」同款字段）。

    懒加载 + 异常全吞：记忆库锁死/损坏不能拖垮 # 指令。reply.py:284-299
    同款模式，那边的原话是 "a locked or missing database must not take the
    whole reply down with it"。
    """
    if not qq or qq == "0":
        return ""
    try:
        root = str(Path(__file__).resolve().parents[2])
        if root not in sys.path:
            sys.path.insert(0, root)
        from water_group.memory_engine import get_memory_engine

        prof = get_memory_engine().find_profile(str(qq))
        traits = json.loads((prof or {}).get("traits") or "{}")
        return normalize_city(traits.get("location") or traits.get("地域"))
    except Exception as exc:  # noqa: BLE001  位置是锦上添花，不是指令的前提
        log(f"member city lookup failed: {type(exc).__name__}")
        return ""


def _items(cfg, key, default):
    try:
        value = int(cfg.get(key) or 0)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _clip(text, limit=REPLY_CLIP):
    """issue #169 PB：截断的唯一归宿是 wg_text.render_handler_reply。

    保留这个薄壳是为了 14 个调用点（含 `_bounded` 注册表）的改动最小化——它们都
    `_clip("\n".join(lines))` / `_clip(fn(args, ctx))`，换函数名要逐处改且容易漏。
    实现只剩一行委托，墓碑判据（check_text_truncation）不再命中：没有裸切片、
    没有自拼尾巴。三份 `_clip` 合流后净输出不再超过 REPLY_CLIP（#169 复现 B 的修绿）。
    """
    return wg_text.render_handler_reply(text, limit)


def _heat(value):
    """热度 → 一行短文本。纯数字上万折成「x.x万」；知乎/百度/小红书这些已经
    带单位或带「万热度」字样的字符串原样透传。"""
    if value is None or value == "":
        return ""
    try:
        num = int(float(value))
    except (TypeError, ValueError):
        return str(value).strip()
    if num >= 10000:
        return f"{num / 10000:.1f}万".replace(".0万", "万")
    return str(num)


def _today_key():
    """缓存标签用的本地日期。按天钉住的数据跨天换键，不靠 TTL 撞运气。"""
    return time.strftime("%Y-%m-%d")


_WEEKDAYS = "一二三四五六日"


def _weekday(date_str):
    """'2026-10-24' → '周六'；解析不了就返回空串（不为了让一行格式完美而炸）。"""
    try:
        day = datetime.date.fromisoformat(str(date_str)[:10])
    except ValueError:
        return ""
    return "周" + _WEEKDAYS[day.weekday()]


def _resolve_city(args, ctx):
    """城市三级回退（#天气 / #预报 共用，DRY）：参数 → 位置记忆 → default_city。"""
    cfg = ctx.get("sixty") or {}
    city = (args or "").strip() or _member_city(ctx.get("user_id")) \
        or str(cfg.get("default_city") or "").strip()
    return normalize_city(city) or city


CITY_HINT = "带上城市名再查，如 #天气 北京（配置里也能设 default_city 兜底）"


# ---------------------------------------------------------------- 指令实现


def brief(args, ctx):
    """#简报：每日 60 秒。"""
    cfg = ctx.get("sixty") or {}
    data = _get("/v2/60s", cfg) or {}
    news = [str(n).strip() for n in (data.get("news") or []) if str(n).strip()]
    if not news:
        return "今天的简报还是空的，稍后再试"
    head = " ".join(x for x in ("【每日简报", str(data.get("date") or "").strip(),
                                str(data.get("day_of_week") or "").strip()) if x) + "】"
    lines = [head]
    lines += [f"{i}. {n}" for i, n in
              enumerate(news[:_items(cfg, "max_items", 5)], 1)]
    tail = " ".join(x for x in (str(data.get("lunar_date") or "").strip(),
                                str(data.get("tip") or "").strip()) if x)
    if tail:
        lines.append(f"（{tail}）")
    return _clip("\n".join(lines))


# 热搜多源（issue #53 P1）。各源的 JSON 字段各不相同，用一张表把差异压成同一行
# 格式，免得七个 if 分支各拼一遍。字段名照上游 module 源码，别照文档猜。
HOT_SOURCES = {
    "微博": {"path": "/v2/weibo", "title": "title", "hot": "hot_value", "link": "link"},
    "知乎": {"path": "/v2/zhihu", "title": "title", "hot": "hot_value_desc", "link": "link"},
    "百度": {"path": "/v2/baidu/hot", "title": "title", "hot": "score_desc", "link": "url"},
    "B站": {"path": "/v2/bili", "title": "title", "hot": "", "link": "link"},
    "头条": {"path": "/v2/toutiao", "title": "title", "hot": "hot_value", "link": "link"},
    "小红书": {"path": "/v2/rednote", "title": "title", "hot": "score", "link": "link"},
    "抖音": {"path": "/v2/douyin", "title": "title", "hot": "hot_value", "link": "link"},
}
HOT_ALIASES = {
    "微博": "微博", "weibo": "微博", "wb": "微博",
    "知乎": "知乎", "zhihu": "知乎",
    "百度": "百度", "baidu": "百度",
    "b站": "B站", "bili": "B站", "哔哩哔哩": "B站",
    "头条": "头条", "今日头条": "头条", "toutiao": "头条",
    "小红书": "小红书", "rednote": "小红书", "xhs": "小红书",
    "抖音": "抖音", "douyin": "抖音", "dy": "抖音",
}


def hot(args, ctx):
    """#热搜 [源]：多源热搜榜，无参默认微博。只透传标题 + 热度 + 链接。"""
    cfg = ctx.get("sixty") or {}
    raw = (args or "").strip()
    name = HOT_ALIASES.get(raw.lower())
    if raw and not name:  # 打错的源名给可选列表，比静默回微博好排查
        return "热搜源支持：" + " / ".join(HOT_SOURCES) + "（如 #热搜 知乎）"
    name = name or "微博"
    spec = HOT_SOURCES[name]
    data = _get(spec["path"], cfg) or []
    lines = [f"【{name}热搜】"]
    count = 0
    for row in data:
        if not isinstance(row, dict):
            continue
        title = str(row.get(spec["title"]) or "").strip()
        if not title:
            continue
        heat = _heat(row.get(spec["hot"])) if spec["hot"] else ""
        link = str(row.get(spec["link"]) or "").strip()
        count += 1
        lines.append(f"{count}. {title}"
                     + (f"（{heat}）" if heat else "")
                     + (f" {link}" if link else ""))
        if count >= _items(cfg, "max_items", 5):
            break
    if not count:
        return f"{name}热搜暂时是空的，稍后再试"
    return _clip("\n".join(lines))


def weather(args, ctx):
    """#天气 [城市]：无参走三级回退（issue #53 第二评）。

    ① 发件人 traits.location（归一化）→ ② config default_city → ③ 让群友带参数。
    """
    cfg = ctx.get("sixty") or {}
    query = _resolve_city(args, ctx)
    if not query:
        return CITY_HINT
    data = _get("/v2/weather/realtime", cfg, {"query": query}) or {}
    loc = data.get("location") or {}
    name = str(loc.get("name") or query)
    w = data.get("weather") or {}
    air = data.get("air_quality") or {}
    lines = [f"【{name}】{w.get('condition') or '—'} {w.get('temperature')}℃"]
    detail = []
    if w.get("wind_direction") or w.get("wind_power"):
        detail.append(f"{w.get('wind_direction') or ''} "
                      f"{w.get('wind_power') or ''}级".strip())
    if w.get("humidity"):
        detail.append(f"湿度 {w.get('humidity')}%")
    if air.get("aqi"):
        detail.append(f"空气{air.get('quality') or ''}（AQI {air.get('aqi')}）")
    if detail:
        lines.append(" · ".join(detail))
    return _clip("\n".join(lines))


def forecast(args, ctx):
    """#预报 [城市]：逐日预报。上游第 0 条常是**昨天**，必须按日期过滤。"""
    cfg = ctx.get("sixty") or {}
    query = _resolve_city(args, ctx)
    if not query:
        return CITY_HINT
    data = _get("/v2/weather/forecast", cfg, {"query": query}) or {}
    loc = data.get("location") or {}
    name = str(loc.get("name") or query)
    today = datetime.date.today().isoformat()
    rows = [d for d in (data.get("daily_forecast") or [])
            if isinstance(d, dict) and str(d.get("date") or "") >= today]
    if not rows:
        return "预报数据还没更新，稍后再试"
    lines = [f"【{name} 天气预报】"]
    for d in rows[:_items(cfg, "max_items", 5)]:
        day = str(d.get("day_condition") or "—").strip()
        night = str(d.get("night_condition") or "").strip()
        cond = day if (not night or night == day) else f"{day}转{night}"
        low, high = d.get("min_temperature"), d.get("max_temperature")
        temp = f"{low}~{high}℃" if low is not None and high is not None else "气温未知"
        lines.append(f"{str(d.get('date'))[5:]} {_weekday(d.get('date'))} "
                     f"{cond} {temp}")
    return _clip("\n".join(lines))


def history(args, ctx):
    """#历史：历史上的今天，只透传年份 + 标题 + 链接。"""
    cfg = ctx.get("sixty") or {}
    data = _get("/v2/today-in-history", cfg, cache_tag=_today_key()) or {}
    rows = [r for r in (data.get("items") or []) if isinstance(r, dict)
            and str(r.get("title") or "").strip()]
    if not rows:
        return "今天的历史条目还没更新，稍后再试"
    date = str(data.get("date") or "").strip()
    lines = [f"【历史上的今天{f' {date}' if date else ''}】"]
    for i, r in enumerate(rows[:_items(cfg, "max_items", 5)], 1):
        year = str(r.get("year") or "").strip()
        title = str(r.get("title")).strip()
        link = str(r.get("link") or "").strip()
        lines.append(f"{i}. " + (f"{year} " if year else "") + title
                     + (f" {link}" if link else ""))
    return _clip("\n".join(lines))


def moyu(args, ctx):
    """#摸鱼：打工人日历（日期 / 状态 / 进度 / 倒计时 / 格言）。"""
    cfg = ctx.get("sixty") or {}
    data = _get("/v2/moyu", cfg, cache_tag=_today_key()) or {}
    date = data.get("date") or {}
    lunar = date.get("lunar") or {}
    today = data.get("today") or {}
    if not date.get("gregorian"):
        return "摸鱼日历暂时拿不到，稍后再试"
    stamp = " ".join(str(x) for x in (date.get("gregorian"), date.get("weekday")) if x)
    lunar_text = "".join(str(x) for x in (lunar.get("monthCN"), lunar.get("dayCN"))
                         if x)
    lines = [f"【摸鱼办 {stamp}" + (f" · 农历{lunar_text}" if lunar_text else "") + "】"]
    current = data.get("currentHoliday") or {}
    if current.get("name"):
        status = (f"假期中：{current.get('name')}（第 {current.get('dayOfHoliday')} 天，"
                  f"还剩 {current.get('daysRemaining')} 天）")
    elif today.get("isWeekend"):
        status = "周末，愉快摸鱼"
    elif today.get("isHoliday"):
        status = f"假日：{today.get('holidayName') or '—'}，尽情摸鱼"
    elif today.get("isWorkday"):
        status = "工作日，低调摸鱼，注意老板"
    else:
        status = ""
    if today.get("solarTerm"):
        status = (status + " · " if status else "") + f"节气：{today.get('solarTerm')}"
    if status:
        lines.append(status)
    progress = data.get("progress") or {}
    bars = []
    for key, label in (("week", "本周"), ("month", "本月"), ("year", "本年")):
        seg = progress.get(key) or {}
        if seg.get("percentage") is not None:
            bars.append(f"{label} {seg.get('percentage')}%"
                        f"（{seg.get('passed')}/{seg.get('total')}）")
    if bars:
        lines.append(" · ".join(bars))
    countdown = data.get("countdown") or {}
    cd = []
    if countdown.get("toWeekEnd") is not None:
        left = countdown.get("toWeekEnd")
        cd.append("距周末：今天就是周末" if not left else f"距周末 {left} 天")
    if countdown.get("toMonthEnd") is not None:
        cd.append(f"距月底 {countdown.get('toMonthEnd')} 天")
    if countdown.get("toYearEnd") is not None:
        cd.append(f"距年底 {countdown.get('toYearEnd')} 天")
    if cd:
        lines.append(" · ".join(cd))
    nxt = data.get("nextHoliday") or {}
    weekend = data.get("nextWeekend") or {}
    if nxt.get("name"):
        lines.append(f"下个假期 {nxt.get('name')}（{nxt.get('date') or '—'}，"
                     f"{nxt.get('until', '?')} 天后）")
    elif weekend.get("date"):
        lines.append(f"下个周末 {weekend.get('weekday') or ''} {weekend.get('date')}"
                     f"（{weekend.get('daysUntil', '?')} 天后）")
    quote = str(data.get("moyuQuote") or "").strip()
    if quote:
        lines.append(f"格言：{quote}")
    return _clip("\n".join(lines))


# 上游 luck.json 的条目数（2026-10-23 核对）。按日期取模定住当日那一签；
# 上游若收缩条目，撞到 404 时 luck() 会退化成不传 id 的随机签。
LUCK_SLOTS = 19


def luck(args, ctx):
    """#运势：按日期钉住当日运势（上游每次请求都随机，不钉住同一群会刷出满屏不同签）。"""
    cfg = ctx.get("sixty") or {}
    day = _today_key()
    try:
        slot = int(day.replace("-", "")) % LUCK_SLOTS
        data = _get("/v2/luck", cfg, {"id": slot}, cache_tag=day)
    except SixtyError as exc:
        if "code=404" not in str(exc):  # 只有 id 越界才回退，网络错误照常上浮
            raise
        data = _get("/v2/luck", cfg, cache_tag=day)
    data = data or {}
    desc = str(data.get("luck_desc") or "").strip()
    tip = str(data.get("luck_tip") or "").strip()
    if not desc and not tip:
        return "运势签筒空了，稍后再试"
    return "【今日运势】" + desc + (f"\n{tip}" if tip else "")


# ---------------------------------------------------------------- 行情 / 榜单

# #行情 一个关键词带三条线：汇率（默认）/ 油价 / 金价
MARKET_KINDS = {
    "汇率": "exchange", "外汇": "exchange", "rate": "exchange",
    "油价": "fuel", "油": "fuel", "fuel": "fuel",
    "金价": "gold", "黄金": "gold", "gold": "gold",
}
# 上游 rates 是「1 CNY 兑多少外币」，这里只取常看的几种；单位太小的币种按 100 报，
# 否则一行全是 0.0x 的小数。
CURRENCY_LABELS = (("USD", "美元"), ("EUR", "欧元"), ("JPY", "日元"),
                   ("HKD", "港币"), ("GBP", "英镑"), ("AUD", "澳元"))
CURRENCY_PER = {"JPY": 100}
# 金价上游有 9 种（含伦敦金/纽约黄金），群里只报常用的几档。名字照上游原文。
GOLD_PICKS = ("今日金价", "黄金_9999", "伦敦金(现货黄金)", "白银价格", "铂金价格")


def exchange(args, ctx):
    """#行情 汇率：把上游「1 CNY 兑 X 外币」翻成「1（或 100）外币兑多少人民币」。"""
    cfg = ctx.get("sixty") or {}
    data = _get("/v2/exchange-rate", cfg) or {}
    rates = {}
    for row in data.get("rates") or []:
        if isinstance(row, dict):
            rates[str(row.get("currency") or "").upper()] = row.get("rate")
    if not rates:
        return "汇率数据暂时拿不到，稍后再试"
    lines = ["【人民币汇率】"]
    for code, label in CURRENCY_LABELS:
        try:
            rate = float(rates.get(code))
        except (TypeError, ValueError):
            continue
        if rate <= 0:
            continue
        unit = CURRENCY_PER.get(code, 1)
        lines.append(f"{unit} {label}({code}) ≈ {unit / rate:.2f} 元")
    updated = str(data.get("updated") or "").strip()
    if updated:
        lines.append(f"更新：{updated}")
    return _clip("\n".join(lines))


def fuel(args, ctx):
    """#行情 油价 [地区]：上游按 region **后缀**匹配（广州 → 广东广州）。"""
    cfg = ctx.get("sixty") or {}
    region = _resolve_city(args, ctx)
    params = {"region": region} if region else None  # 不带 region 时上游默认北京
    try:
        data = _get("/v2/fuel-price", cfg, params) or {}
    except SixtyError as exc:
        if "code=400" not in str(exc):  # 400 是「暂不支持该区域」，换城市能救
            raise
        return f"暂不支持「{region}」的油价，换个写法试试（如 #行情 油价 广州）"
    items = [i for i in (data.get("items") or []) if isinstance(i, dict)]
    if not items:
        return "油价数据暂时拿不到，稍后再试"
    lines = [f"【今日油价 {data.get('region') or region or '默认地区'}】"]
    for item in items[:6]:  # 上游只有 92/95/98/0# 四档，上限留给未来扩品
        name = str(item.get("name") or "").strip()
        price = str(item.get("price_desc") or "").strip()
        if name:
            lines.append(f"{name}：{price}" if price else name)
    trend = data.get("trend") or {}
    desc = str(trend.get("description") or "").strip()
    if desc:
        lines.append(desc)
    return _clip("\n".join(lines))


def gold(args, ctx):
    """#行情 金价：只取几种常用的金属报价，stores/banks/recycle 不搬进群里。"""
    cfg = ctx.get("sixty") or {}
    data = _get("/v2/gold-price", cfg) or {}
    metals = [m for m in (data.get("metals") or []) if isinstance(m, dict)]
    if not metals:
        return "金价数据暂时拿不到，稍后再试"
    by_name = {str(m.get("name") or "").strip(): m for m in metals}
    picked = [by_name[n] for n in GOLD_PICKS if n in by_name] or metals[:5]
    date = str(data.get("date") or "").strip()
    lines = [f"【今日金价 {date}】" if date else "【今日金价】"]
    for metal in picked:
        price = str(metal.get("sell_price") or metal.get("today_price") or "").strip()
        unit = str(metal.get("unit") or "").strip()
        lines.append(f"{str(metal.get('name') or '').strip()}：{price} {unit}".strip("： "))
    return _clip("\n".join(lines))


def market(args, ctx):
    """#行情 [汇率|油价|金价]：三条行情共用一个关键词，省得占三个槽位。"""
    parts = (args or "").split()
    kind = "exchange"
    rest = ""
    if parts:
        mapped = MARKET_KINDS.get(parts[0].lower())
        if not mapped:
            return "行情支持：汇率 / 油价 / 金价（如 #行情 油价 广州）"
        kind = mapped
        rest = " ".join(parts[1:]).strip()
    return {"exchange": exchange, "fuel": fuel, "gold": gold}[kind](rest, ctx)


def douban(args, ctx):
    """#豆瓣：一周口碑电影榜（上游另有剧集/综艺榜，暂不铺开）。"""
    cfg = ctx.get("sixty") or {}
    data = _get("/v2/douban/weekly/movie", cfg) or []
    rows = [r for r in data if isinstance(r, dict)
            and str(r.get("title") or "").strip()]
    if not rows:
        return "豆瓣榜单暂时是空的，稍后再试"
    lines = ["【豆瓣一周口碑电影榜】"]
    for i, r in enumerate(rows[:_items(cfg, "max_items", 5)], 1):
        rating = str(r.get("rating") or "").strip()
        link = str(r.get("url") or "").strip()
        lines.append(f"{i}. {str(r.get('title')).strip()}"
                     + (f"（{rating}分）" if rating and rating != "0" else "（暂无评分）")
                     + (f" {link}" if link else ""))
    return _clip("\n".join(lines))


def itnews(args, ctx):
    """#科技：IT 之家日榜（上游 /it-news/rank 的 data 是数组，不是 {type,list}）。"""
    cfg = ctx.get("sixty") or {}
    data = _get("/v2/it-news/rank", cfg, {"type": "day"}) or []
    rows = [r for r in data if isinstance(r, dict)
            and str(r.get("title") or "").strip()]
    if not rows:
        return "科技榜单暂时是空的，稍后再试"
    lines = ["【IT 之家日榜】"]
    for i, r in enumerate(rows[:_items(cfg, "max_items", 5)], 1):
        link = str(r.get("link") or "").strip()
        lines.append(f"{i}. {str(r.get('title')).strip()}"
                     + (f" {link}" if link else ""))
    return _clip("\n".join(lines))


def hitokoto(args, ctx):
    """#一言。"""
    data = _get("/v2/hitokoto", ctx.get("sixty")) or {}
    text = str(data.get("hitokoto") or "").strip()
    return text or "一言池子空了，稍后再试"


# ---------------------------------------------------------------- 随机文案


def _random_text(path, field, cfg):
    """三条随机文案指令共用的取数骨架：上游把正文放在单个字段里。

    只有字段名不同（kfc / duanzi / answer），不各写一遍「取数 → 取字段 → 去空白」。
    返回空串表示取不到，兜底文案由各指令自己定（标题前缀不一样）。
    """
    data = _get(path, cfg) or {}
    return str(data.get(field) or "").strip()


def kfc(args, ctx):
    """#KFC：疯狂星期四文案（上游从 v50 文案库里随机一条）。"""
    text = _random_text("/v2/kfc", "kfc", ctx.get("sixty"))
    return f"【疯狂星期四】\n{text}" if text else "疯狂星期四文案暂时取不到，稍后再试"


def duanzi(args, ctx):
    """#笑话：随机段子。"""
    return _random_text("/v2/duanzi", "duanzi", ctx.get("sixty")) \
        or "段子库存空了，稍后再试"


def answer(args, ctx):
    """#答案：答案之书，随机给一句。英文原文不上屏（群是中文语境）。"""
    text = _random_text("/v2/answer", "answer", ctx.get("sixty"))
    return f"【答案之书】{text}" if text else "答案之书翻不开，稍后再试"


# ---------------------------------------------------------------- 注册表


def _bounded(fn):
    """包一层截断，并标记免登录（commands.run_handler 的绑定检查只看 need_ck）。

    issue #169 PB：与 miyoushe._bounded 同一段包装器复制两份，合流到
    wg_text.bounded_handler。本模块业务上限 REPLY_CLIP=1200、全部指令免凭证（need_ck
    固定 False），闭包只绑这两个数；need_ck 默认值差异（米游社 True / 60s False）
    是各模块的隐私策略，留在各自的 `_bounded` 默认里，不在共享装饰器里写死。
    """
    return wg_text.bounded_handler(fn, limit=REPLY_CLIP, need_ck=False)


HANDLERS = {
    "简报": ("今日 60 秒简报", _bounded(brief)),
    "热搜": ("热搜榜（#热搜 知乎/百度/B站/头条/小红书/抖音）", _bounded(hot)),
    "天气": ("实时天气（#天气 城市）", _bounded(weather)),
    "预报": ("未来几天天气预报（#预报 城市）", _bounded(forecast)),
    "历史": ("历史上的今天", _bounded(history)),
    "摸鱼": ("打工人摸鱼日历", _bounded(moyu)),
    "运势": ("今日运势（当天固定）", _bounded(luck)),
    "行情": ("汇率 / 油价 / 金价（#行情 油价 广州）", _bounded(market)),
    "豆瓣": ("豆瓣一周口碑电影榜", _bounded(douban)),
    "科技": ("IT 之家日榜", _bounded(itnews)),
    "KFC": ("疯狂星期四文案", _bounded(kfc)),
    "笑话": ("随机段子", _bounded(duanzi)),
    "答案": ("答案之书（随机一句）", _bounded(answer)),
    "一言": ("随机一句话", _bounded(hitokoto)),
}

# 默认放行的指令（allow 缺省时用）。新增指令先过合规评审再进这里。
DEFAULT_ALLOW = ("简报", "热搜", "天气", "预报", "历史", "摸鱼", "运势",
                 "行情", "豆瓣", "科技", "KFC", "笑话", "答案", "一言")


def allowed(key, cfg):
    """白名单闸门（commands.resolve_reply / help_text 调用）。

    没配 / enabled=false → 全禁：PR 合入但没开配置时指令静默落空，而不是
    列在帮助里却不回。allow 为空数组 = 全禁；缺省 allow = DEFAULT_ALLOW。
    """
    if not cfg or not cfg.get("enabled"):
        return False
    allow = cfg.get("allow")
    if allow is None:
        return key in DEFAULT_ALLOW
    names = [str(k).strip() for k in allow if str(k).strip()]
    return bool(names) and key in names
