"""飞猪酒店价格取数层：``#酒店`` 指令的后端（issue #139 P2）。

接入方式按 P1 可行性评估结论：subprocess 调飞猪官方 `@fly-ai/flyai-cli`
的 `search-hotel` 子命令。**不复刻 MCP 签名**——官方 bundle 已混淆，
跟随私有协议不可维护，对方改算法即挂。

钉住的 CLI 契约（解包 `@fly-ai/flyai-cli@1.0.16` 的 flyai-bundle.cjs 核对）：

* 入口是 `node <bundle.cjs> search-hotel --dest-name <目的地>`，成功时
  stdout 写结构化 JSON，失败时 stderr 写 `Error: ...` 或 `MCP HTTP <code>...`
  并 exit 1；纯参数拒绝（缺 dest / 非法日期 / 非法 sort）本地直接 exit 1。
* **体验 key 内置**：`FLYAI_API_KEY` 与 `~/.flyai/config.json` 都没有值时，
  CLI 自动用 bundle 内置的体验 key 请求，返回脱敏价（如 ``¥2x``）。所以
  「没配 key」不会表现为失败，而是表现为价格残缺。本模块据此在取数前
  显式检查插件配置里的 `api_key`：未配置一律先报「未配置」，绝不把脱敏预览
  当真实价格播报（P1 硬约束 1）。
* 目的地无效时上游静默降级（P1 实测：`--dest-name 北极点xyz` 返回香港的
  酒店）。本模块在渲染前对结果集做目的地相关性校验：酒店名/地址与目的地
  零交集时判「查无结果」，不拿其他城市的酒店去凑（P1 硬约束 2）。

边界（别悄悄扩）：

* 只读。机票、景点、火车、marriott、ai-search 一律不接。
* 不发 HTTP：出站只通过 CLI 子进程，Python 侧无 urllib 调用；``winconsole``
  的抑制 helper 是唯一 spawn 口子（#39）。
* 凭证只在 `plugins.hotel.options.api_key` 配置里，落盘走 CLI 自己的
  `~/.flyai/config.json`（`config set FLYAI_API_KEY ...`）；本模块不写文件。
"""
import datetime
import json
import re
import threading
import time

from wg_daemon import logger
from wg_paths import logs_dir
import winconsole
import wg_text

# --desc 或官方 Output Example 为 `¥618`，本契约不依赖具体价格格式。
CLI_PKG = "@fly-ai/flyai-cli"
MCP_DEFAULT_URL = "https://flyai.open.fliggy.com/mcp"

TIMEOUT = 20                  # 官方 npx 热启动约 5.5s；直接 node 约 2s。20s 覆盖冷启动。
CACHE_TTL = 300.0             # 酒店价一天一价的频率，5 分钟是合理上限
CACHE_MAX = 64

REPLY_CLIP = 1200

LOG = logs_dir() / "wg_flyai.log"
log = logger(LOG)

# 排序值（CLI search-hotel 的 --sort）。
SORT_VALUES = ("distance_asc", "rate_desc", "price_asc", "price_desc", "no_rank")

# CLI search-hotel 的字符串透传类选项（--poi-name 在 answer 里也显式传）：
# 这几个走配置段，复数/连字符的 CLI 拼写与配置键不一致的地方在本模块收口。
_PASSTHROUGH = (
    ("hotel_stars", "--hotel-stars"),
    ("hotel_bed_types", "--hotel-bed-types"),
    ("hotel_types", "--hotel-types"),
    ("keywords", "--key-words"),
)

NO_KEY_HINT = ("飞猪 Key 未配置：请在 config.json 的 plugins.hotel.options.api_key "
               "填自有 Key（淘宝登录免费领），或跑 python water-group/daemon/flyai.py --status 看状态")

_DATE_FULL = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_DATE_SHORT = re.compile(r"(\d{1,2})[-/.](\d{1,2})")
_DATE_WORDS = ("今天", "今日", "明天", "明日", "后天", "大后天",
               "昨天", "昨日", "前天")
# 脱敏价形态：`¥2x`、`¥618x`、`¥909起/晚` 不算脱敏，但体验模式落盘的是
# `¥2x` 这类「数字 + x」的半截字符串（P1 实测 2026-10-26）。
_MASKED_RE = re.compile(r"¥\s*\d+\s*x\b", re.I)
# CLI 把 JSON 打到 stdout；失败信息走 stderr，常见形态：
# `Error: --check-in-date must be in YYYY-MM-DD format` / `Error: --sort must be one of...`
# `MCP HTTP 429 ...` / `MCP error: ...`
_STDERR_PATTERNS = (
    (re.compile(r"MCP HTTP (\d{3}):\s*(.*)$", re.M), "飞猪接口返回 HTTP {code}：{detail}"),
    (re.compile(r"MCP error:\s*(.*)$", re.M), "飞猪接口报错：{detail}"),
    (re.compile(r"Error:\s*(.*)$", re.M), None),
)


class HotelError(Exception):
    """取数层统一错误。``str(exc)`` 直接当回复文案，所以消息必须面向群友。"""


class ExpiredKey(HotelError):
    """key 失效或达调用上限：调用方可以修配置重发。"""


# --------------------------------------------------------------------------
# 配置与 bundle 定位
# --------------------------------------------------------------------------
def configured_key(cfg=None):
    """从插件配置取 API Key，strip 后为空即「未配置」。

    刻意不读 CLI 自己的 ~/.flyai/config.json：插件宿主只认 Plugins 的配置段
    （与 busboss 不认 ~/.flyai 同理），CLI 那边只当执行环境。未配置时绝不
    起 subprocess——CLI 内置体验 key 会返回脱敏价，而非报错。
    """
    raw = str((cfg or {}).get("api_key") or "").strip()
    return raw or ""


def _bundle(cfg=None):
    """CLI 的调用命令：配置里 `bundle` 指向本地已安装的 flyai-bundle.cjs；
    没配置时默认用 `flyai` 命令（npm link / npm i -g 的形态）。"""
    raw = str((cfg or {}).get("bundle") or "").strip()
    if raw:
        return ["node", raw]
    return ["flyai"]


# --------------------------------------------------------------------------
# 日期
# --------------------------------------------------------------------------
def _safe_date(year, month, day):
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def parse_date(raw, today=None):
    """日期归一成 ``YYYY-MM-DD``（与 busboss 同语义的最小子集）。

    未给出日期则按默认入住窗口兜底：今天入住、明天退房。非法输入**报错不猜**。
    """
    base = today or datetime.date.today()
    text = str(raw or "").strip()
    if not text or text in ("今天", "今日"):
        return base.isoformat()
    if text in ("明天", "明日"):
        return (base + datetime.timedelta(days=1)).isoformat()
    if text == "后天":
        return (base + datetime.timedelta(days=2)).isoformat()
    if text == "大后天":
        return (base + datetime.timedelta(days=3)).isoformat()
    if text in ("昨天", "昨日", "前天"):
        raise HotelError(f"{text}已经过去了，只能查今天起的酒店")
    day = None
    m = _DATE_FULL.fullmatch(text)
    if m:
        day = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    else:
        m = _DATE_SHORT.fullmatch(text)
        if m:
            day = _safe_date(base.year, int(m.group(1)), int(m.group(2)))
    if day is None:
        raise HotelError(f"看不懂日期「{text}」，用 今天 / 明天 / 后天 / 2026-11-01 这种写法")
    if day < base:
        raise HotelError(f"{day.isoformat()} 已经过去了")
    return day.isoformat()


def looks_like_date(text):
    """参数判别：结构上像日期就返回 True（busboss 的同名口径）。"""
    t = str(text or "").strip()
    if t.lower() in _DATE_WORDS:
        return True
    return bool(_DATE_FULL.fullmatch(t) or _DATE_SHORT.fullmatch(t))


# --------------------------------------------------------------------------
# 参数切分
# --------------------------------------------------------------------------
def split_args(args):
    """``目的地 [景点|筛选词] [入住日期] [退房日期]`` → ``(dest, poi, check_in, check_out)``。

    按内容判别：像日期的就是日期，剩下的归景点/关键词。
    """
    tokens = [t for t in re.split(r"[\s\u3000]+", str(args or "").strip()) if t]
    if not tokens:
        raise HotelError("用法：#酒店 目的地 [景点] [入住日期] [退房日期]\n"
                         "例：#酒店 上海 11-01 11-02")
    if len(tokens) > 4:
        raise HotelError("参数太多，最多四段：目的地 景点 入住日期 退房日期")
    dest = tokens[0]
    poi, dates = "", []
    for token in tokens[1:]:
        if looks_like_date(token) and len(dates) < 2:
            dates.append(token)
        elif not poi:
            poi = token
        else:
            raise HotelError("参数重复了，景点只写一个，日期最多两个")
    check_in = dates[0] if dates else ""
    check_out = dates[1] if len(dates) > 1 else ""
    return dest, poi, check_in, check_out


# --------------------------------------------------------------------------
# CLI 调用
# --------------------------------------------------------------------------
def _run_cli(argv, cfg=None):
    """跑一次 CLI：成功返回解析后的 dict；任何失败都抛 HotelError。

    子进程走 `winconsole.run_hidden`（#39 的唯一 spawn 口子）。超时 / 进程
    不存在 / exit 非 0 三条失败都给群友一句能看的话，原文只进日志。
    """
    timeout = int((cfg or {}).get("timeout") or TIMEOUT)
    env = None  # 继承父进程 env，CLI 自己读 FLYAI_API_KEY 与 ~/.flyai/config.json
    try:
        result = winconsole.run_hidden(
            argv,
            stdout=__import__("subprocess").PIPE,
            stderr=__import__("subprocess").PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        log(f"cli not found: {argv[0]}")
        raise HotelError("未安装飞猪 CLI：npm i -g @fly-ai/flyai-cli，"
                         "或在 plugins.hotel.options.bundle 写 bundle.cjs 路径") from exc
    except TimeoutError as exc:
        log(f"cli timeout: {' '.join(argv[:2])} timeout={timeout}")
        raise HotelError("飞猪 CLI 超时，稍后再试") from exc
    except Exception as exc:  # noqa: BLE001  子进程层任何异常都翻成可读文案
        log(f"cli failed: {type(exc).__name__}: {exc}")
        raise HotelError("飞猪 CLI 暂时不可用，稍后再试") from exc
    if result.returncode != 0:
        detail = _stderr_detail(result.stderr or "")
        log(f"cli exit {result.returncode}: {result.stderr or ''}")
        raise HotelError(detail or "飞猪 CLI 调用失败，稍后再试")
    out = (result.stdout or "").strip()
    if not out:
        raise HotelError("飞猪 CLI 返回为空，稍后再试")
    try:
        payload = json.loads(out)
    except ValueError as exc:
        log(f"decode failed cli: {out[:120]!r}")
        raise HotelError("飞猪 CLI 返回了无法解析的数据") from exc
    if not isinstance(payload, dict):
        raise HotelError("飞猪 CLI 返回了无法解析的数据")
    return payload


def _stderr_detail(stderr):
    """从 CLI 的 stderr 里取一句面向群友的话。

    CLI 的形态只有两种值得信：`MCP HTTP <code>`（真实 HTTP 状态）和
    `Error: <原文>`（本地参数拒绝）。后者原样回，避免冒充。
    """
    for pattern, template in _STDERR_PATTERNS:
        m = pattern.search(stderr or "")
        if not m:
            continue
        if template is None:
            return m.group(1).strip()
        return template.format(code=m.group(1).strip(), detail=(m.group(2) or "").strip())
    return ""


def fetch_hotels(dest, poi, check_in, check_out, cfg=None):
    """调 CLI `search-hotel`：返回解析出的酒店列表。

    `cfg["api_key"]` 为空时**不**调用 CLI（体验 key 会返回脱敏价），而是
    直接抛带配置指引的错误。
    """
    cfg = cfg or {}
    if not configured_key(cfg):
        raise HotelError(NO_KEY_HINT)
    argv = [*_bundle(cfg), "search-hotel", "--dest-name", dest]
    if poi:
        argv += ["--poi-name", poi]
    if check_in:
        argv += ["--check-in-date", check_in]
    if check_out:
        argv += ["--check-out-date", check_out]
    for cfg_key, flag in _PASSTHROUGH:
        value = str(cfg.get(cfg_key) or "").strip()
        if value:
            argv += [flag, value]
    max_price = cfg.get("max_price")
    try:
        max_price_num = int(max_price) if max_price is not None else 0
    except (TypeError, ValueError):
        max_price_num = 0
    if max_price_num > 0:
        argv += ["--max-price", str(max_price_num)]
    sort = str(cfg.get("sort") or "").strip()
    if sort:
        argv += ["--sort", sort]
    payload = _run_cli(argv, cfg)

    # 上游信封：`status != 0` 视作上游失败（配合 stderr 的可分类信息，
    # CLI 非零 exit 不存在 status 包装时这边会优先于 stderr）。
    status = payload.get("status", 0)
    if status not in (0, "", None):
        message = str(payload.get("systemMessage") or payload.get("message") or "").strip()
        log(f"cli status={status}: {message}")
        if status == -1 or "DateTimeParseException" in message:
            raise HotelError(message or "日期格式有问题")
        raise HotelError(f"飞猪接口返回错误（status {status}）{': ' + message if message else ''}，稍后再试")

    items = _extract_items(payload)
    if not items:
        raise HotelError(f"目的地「{dest}」暂无酒店")

    # P1 硬约束 1：体验 key 的脱敏价绝不能当真实价格播报。任何一条酒店的
    # 价格里出现 `¥2x` 这类半截形态都整体拒绝——报「体验 key」或「未配置」。
    for item in items:
        price = str(item.get("price") or "")
        if _MASKED_RE.search(price):
            log(f"masked price returned: {price!r}")
            raise HotelError("当前返回的价格已脱敏（体验 Key 或 Key 已失效），"
                             "请在 plugins.hotel.options.api_key 配置自有 Key")

    # P1 硬约束 2：上游无效目的地静默降级。结果与目的地零交集时判查无结果。
    if not _relevant_items(dest, poi, items):
        raise HotelError(f"没有查到「{dest}」的酒店（返回的是其他城市的结果）")

    return items


def _extract_items(payload):
    """从 CLI 输出里取酒店列表：字段名以 P1 结论「酒店名、地址、星级、起价、
    shId、跳转链接」为准，形态上容忍三种包法。
    """
    # 最常见的形态：`{"status":0, "list":[...]}` / `{"status":0,"hotels":[...]}`。
    for key in ("list", "hotels", "items", "result"):
        rows = payload.get(key)
        if isinstance(rows, list) and rows:
            return [row for row in rows if isinstance(row, dict)]
    # 上游直接给 `{"status":0, "data":[...]}`。
    data = payload.get("data")
    if isinstance(data, list) and data:
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("list", "hotels", "items"):
            rows = data.get(key)
            if isinstance(rows, list) and rows:
                return [row for row in rows if isinstance(row, dict)]
    return []


def _relevant_items(dest, poi, items):
    """目的地/景点相关性：酒店名或地址命中目的地/景点字符串即相关。

    `poi` 为空时仅看目的地；目的地命中 OR 景点命中即相关。避免把香港的
    酒店塞给查北极点的人。
    """
    wanted = [_norm(dest)]
    if poi:
        wanted.append(_norm(poi))
    kept = []
    for item in items:
        name = _norm(str(item.get("hotelName") or item.get("hotel_name") or item.get("name") or ""))
        address = _norm(str(item.get("address") or item.get("destName") or item.get("district") or ""))
        if any(w and (w in name or w in address) for w in wanted):
            kept.append(item)
    return kept


def _norm(text):
    return re.sub(r"[\s\u3000]+", "", str(text or ""))


def relevant_filter(dest, poi, items):
    """供门禁单独钉的相关性口径（与 fetch_hotels 内部同一函数）。"""
    return _relevant_items(dest, poi, items)


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
            for stale in [k for k, (exp, _v) in _CACHE.items() if exp <= now]:
                _CACHE.pop(stale, None)
            overflow = len(_CACHE) - CACHE_MAX + 1
            if overflow > 0:
                for stale in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:overflow]:
                    _CACHE.pop(stale, None)
        _CACHE[key] = (now + ttl, data)
    return data


def clear_cache():
    with _LOCK:
        _CACHE.clear()


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def _price_of(item):
    raw = str(item.get("price") or "").strip()
    if not raw:
        return ""
    return raw


def _lines_budget(cfg):
    try:
        return max(1, int((cfg or {}).get("max_hotels") or 8))
    except (TypeError, ValueError):
        return 8


def render_hotels(dest, poi, check_in, check_out, items, cfg=None):
    """把酒店列表拼成回群文本：名 / 地址 / 星级 / 起价 / shId / 链接。"""
    budget = _lines_budget(cfg)
    out = [f"{dest} {check_in or ''} → {check_out or ''}".strip()]
    if poi:
        out.append(f"景点：{poi}")
    out.append(f"共 {len(items)} 家")
    for item in items[:budget]:
        name = str(item.get("hotelName") or item.get("name") or "未命名酒店")
        stars = str(item.get("star") or item.get("stars") or item.get("hotelStars") or "")
        address = str(item.get("address") or "")
        price = _price_of(item)
        row = f"· {name}"
        if stars:
            row += f" {stars}星"
        if price:
            row += f" ¥{price}" if not price.startswith("¥") else f" {price}"
        out.append(row)
        if address:
            out.append(f"  {address}")
        ext = []
        if item.get("shId") or item.get("id"):
            ext.append(f"shId={item.get('shId') or item.get('id')}")
        for key in ("url", "jumpUrl", "link"):
            if item.get(key):
                ext.append(str(item[key]))
                break
        if ext:
            out.append("  " + " · ".join(ext))
    if len(items) > budget:
        out.append(f"…另有 {len(items) - budget} 家未列出")
    return wg_text.render_handler_reply("\n".join(out), REPLY_CLIP)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def answer(args, cfg=None, today=None):
    """``#酒店`` 的完整流程：切参数 → 配 key → CLI → 相关性校验 → 渲染。"""
    cfg = cfg or {}
    dest, poi, check_in_text, check_out_text = split_args(args)
    base = today or datetime.date.today()
    check_in = parse_date(check_in_text, base) if check_in_text else ""
    check_out = parse_date(check_out_text, base) if check_out_text else ""
    if check_in and not check_out:
        check_out = (datetime.date.fromisoformat(check_in) + datetime.timedelta(days=1)).isoformat()
    cache_key = f"hotels|{dest}|{poi}|{check_in}|{check_out}|{cfg.get('sort') or ''}"
    ttl = float(cfg.get("cache_seconds") or CACHE_TTL)
    items = cached(cache_key, lambda: fetch_hotels(dest, poi, check_in, check_out, cfg), ttl)
    # 渲染前再按目的地/景点过滤一遍：上游降级返回香港酒店的零交集情况由
    # fetch_hotels 里判定拦截，这里保留最全的结果供渲染，相关性已在那里钉死。
    return render_hotels(dest, poi, check_in, check_out, items, cfg)


# --------------------------------------------------------------------------
# CLI：部署者在本机用（不进群）
# --------------------------------------------------------------------------
def _status(cfg=None):
    cfg = cfg or {}
    from wg_paths import state_dir  # noqa: PLC0415  按 busboss 形态延迟取
    has_key = bool(configured_key(cfg))
    bundle = cfg.get("bundle") or ""
    print(f"api_key : {'有' if has_key else '缺'}")
    print(f"bundle  : {bundle or '(用 flyai 命令)'}")
    print(f"state   : {state_dir()}")
    print(f"mcp     : {MCP_DEFAULT_URL}")
    if not has_key:
        print(NO_KEY_HINT)
    return 0 if has_key else 1


def main(argv=None):
    import argparse  # noqa: PLC0415  同 busboss
    parser = argparse.ArgumentParser(description="飞猪酒店价格取数层（#酒店 后端）")
    parser.add_argument("--status", action="store_true", help="看 Key 是否配置")
    parser.add_argument("--query", metavar="ARGS", help="按 #酒店 的参数查一次并打印")
    args = parser.parse_args(argv)
    try:
        if args.status:
            return _status()
        if args.query:
            print(answer(args.query))
            return 0
    except HotelError as exc:
        print(str(exc))
        return 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
