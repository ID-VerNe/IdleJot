"""B 站视频字幕取数层：``#字幕`` 指令的后端（issue #339）。

群友在群里丢一条 B 站分享（``b23.tv`` 短链卡片、``bilibili.com/video/BV…``
链接或裸 BV/av 号），本模块把它一路问到字幕正文：

    b23.tv/<code>            → 302 Location（只跟一跳，仍须落在 B 站域）
    x/web-interface/view     → data.pages[] 里的 cid（多 P 按 p= 取）
    x/player/v2              → data.subtitle.subtitles[] + need_login_subtitle
    subtitle_url             → JSON 字幕正文（body[].from/to/content）

契约照**本机实测**钉住，不照文档猜（2026-11-01 逐个打过）：

* 短链展开是 ``GET https://b23.tv/<code>`` → ``302``，``Location`` 就是
  ``https://www.bilibili.com/video/BV…``；
* ``x/web-interface/view?bvid=`` → ``code=0``，``data.pages[] = {page, cid, part}``，
  另有 ``aid`` / ``videos`` / ``title`` / ``duration``；
* ``x/player/v2?bvid=&cid=`` → ``data.subtitle.subtitles[]``（``lan`` / ``lan_doc`` /
  ``subtitle_url``）与 ``data.need_login_subtitle``；参数坏掉时
  ``{"code": -400, "message": "请求错误"}``。

**关键负面结论（决定了本模块只能承诺「链路 + 诚实的降级」）**：匿名访问下
``subtitles`` 普遍为空。抽查 9 个不同稿件（含带「中日双语字幕」的番剧切片），
``x/player/v2`` 与 ``x/player/wbi/v2`` 两个端点、桌面与 ``mobi_app=android`` 两套
参数**全部**返回 ``subtitles: []``，其中不少同时置 ``need_login_subtitle: true``。
所以 :func:`answer` 的四句「没有」必须分开说（见 :func:`_explain_empty`）——
把「要登录」说成「UP 主没传」等于对群友撒谎。

边界（别悄悄扩）：

* **只走官方匿名接口，不落任何账号凭证**。不存 Cookie、不读浏览器 cookie、
  不扫码——本模块没有凭证这个概念。
* 不碰视频流（``playurl``）。参考项目 ``injahow/user.js`` 求的是能下载的流，
  与字幕是两条链路，且它跑在已登录网页里直接读 ``document.cookie``。
* 只读。弹幕、评论、合集批量都不在这里。
* 出站目标是**外部输入决定的**（短链 Location、``subtitle_url``），所以两处都
  有域名白名单：展开只跟一跳且须仍在 B 站域，字幕地址须在 ``hdslb.com``。
  否则这就是一个能被群友任意戳的 SSRF 入口。
"""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from wg_daemon import logger
from wg_paths import logs_dir
import wg_text

BASE = "https://api.bilibili.com"
# 四处**由外部输入决定的出站地址**的归属域，全部在这里定义；``parse_ref`` 的
# 正则也由它们现拼，换域名时改一处、判据与发请求的地方一起变（改一半就红的形状
# 让人没法「只改发送、忘了校验」）。
#
# * ``SHORT_BASE`` / ``SHORT_HOST``：``b23.tv`` 短链的请求地址与认的 host；
# * ``SITE_HOST``：展开后的 Location 必须仍在站内视频页，否则丢弃；
# * ``SUBTITLE_HOSTS``：字幕正文 CDN（``//aisubtitle.hdslb.com/bfs/…`` 是实测形态）。
SHORT_BASE = "https://b23.tv"
SHORT_HOST = "b23.tv"
SITE_HOST = "bilibili.com"
SUBTITLE_HOSTS = ("hdslb.com",)

TIMEOUT = 8
LIST_TTL = 600.0              # 同一个视频连着几个人问是常态
BODY_TTL = 3600.0             # 字幕正文基本不变，按小时缓存
CACHE_MAX = 64

REPLY_CLIP = 1200
DEFAULT_MAX_CHARS = 3000      # 字幕正文进群的长度上限，超出按条截断并明说
MAX_CUES = 400                # 一次最多拼这么多条，防止长视频把回复撑爆

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

LOG = logs_dir() / "wg_bilibili.log"
log = logger(LOG)

# 只放行这两种输入：短链 / 站内视频页。别的链接（哪怕长得像）一律当「没找到」，
# 不替用户把任意 URL 塞进 fetch。三条判据都从 ``SHORT_HOST`` / ``SITE_HOST`` /
# ``SUBTITLE_HOSTS`` 现拼，门禁换域名时与之同源，无法「只改一半」。
SHORT_RE = re.compile(r"^(?:https?://)?(?:www\.)?" + re.escape(SHORT_HOST)
                      + r"/([A-Za-z0-9]+)", re.I)
VIDEO_RE = re.compile(r"^(?:https?://)?(?:www\.|m\.)?" + re.escape(SITE_HOST)
                      + r"/video/(BV[A-Za-z0-9]+|av\d+)", re.I)
BARE_BV_RE = re.compile(r"^BV[A-Za-z0-9]+$", re.I)
BARE_AV_RE = re.compile(r"^av\d+$", re.I)
_URL_RE = re.compile(r"https?://[^\s\"'<>，。、）)】\]|]+", re.I)
_PAGE_RE = re.compile(r"[?&]p=(\d+)")
_TAG_RE = re.compile(r"<[^>]{1,40}>")


class BilibiliError(Exception):
    """取数层统一错误。``str(exc)`` 直接当回复文案，所以消息必须面向群友。"""


class NeedsLogin(BilibiliError):
    """``need_login_subtitle`` 为真：匿名接口拿不到，但视频多半确实有字幕。"""


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """把 3xx 原样交回去，不跟过去（issue #349）。

    ``no_redirect=True`` 原本只是「捕获 ``HTTPError`` 时读 ``Location``」——可 urllib
    的默认处理器**会自动跟随跳转**，302 早被跟完、``resp.geturl()`` 已经是终点地址，
    那句 except 分支根本轮不到。于是短链的「只跟一跳」只写在注释里：群友贴的
    ``b23.tv`` 若指向站外，请求**已经发出去了**，``_allowed()`` 只是事后核对字符串。
    这里返回 ``None`` 让处理器拒绝重定向，3xx 变成 ``HTTPError``，由 ``_get`` 交回。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener(no_redirect=False):
    """不走系统代理：本机装了代理时直连更稳（同 busboss / sixty）。

    ``no_redirect=True`` 时装一个不跟随跳转的处理器——出站纪律靠它，不靠调用方
    自觉（issue #349）。**只给需要它的那次请求装**：其余接口（稿件 / 字幕清单 /
    字幕正文）跟随时跳转是正常的，全局禁跳会把「地址搬了家」当成失败。
    """
    handlers = [urllib.request.ProxyHandler({})]
    if no_redirect:
        handlers.append(_NoRedirect())
    return urllib.request.build_opener(*handlers)


def _get(req, timeout, no_redirect=False):
    """GET 并读回原始字节。``no_redirect`` 时把 302 当结果交回去而不是跟过去。"""
    opener = _opener(no_redirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.geturl(), resp.read()
    except urllib.error.HTTPError as exc:
        if no_redirect and exc.code in (301, 302, 303, 307, 308):
            return exc.headers.get("Location") or "", b""
        log(f"http {exc.code} {_short_url(req.full_url)}")
        raise BilibiliError(f"B 站接口暂时不可用（HTTP {exc.code}），稍后再试") from exc
    except BilibiliError:
        raise
    except Exception as exc:  # noqa: BLE001  网络层失败统一翻译
        log(f"http failed {_short_url(req.full_url)}: {type(exc).__name__}")
        raise BilibiliError("B 站接口暂时不可用（超时或断网），稍后再试") from exc


def _short_url(url):
    """日志里的 URL 去掉查询串：``subtitle_url`` 带签名，不该原样落盘。"""
    return str(url).split("?", 1)[0]


def _api(path, params, timeout):
    """B 站 JSON 接口：信封 ``{code, message, data}``，``code != 0`` 就是失败。"""
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://www.bilibili.com/"})
    _final, raw = _get(req, timeout)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        log(f"decode failed {path}")
        raise BilibiliError("B 站返回了无法解析的数据，稍后再试") from exc
    if not isinstance(payload, dict):
        raise BilibiliError("B 站返回了无法解析的数据，稍后再试")
    code = payload.get("code")
    if code != 0:
        message = str(payload.get("message") or "").strip()
        log(f"api {path} code={code} message={message}")
        if code == -404:
            raise BilibiliError("这个视频不存在或已被 UP 主删除")
        raise BilibiliError(f"B 站接口返回错误（code {code}{(' ' + message) if message else ''}），"
                            "稍后再试")
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------
# 引用解析：消息文本 / 卡片 JSON → (bvid|avid, page)
# --------------------------------------------------------------------------
def parse_ref(text):
    """从一段文本里认出 B 站视频引用，返回 ``(id, page)``；认不出抛错。

    ``page`` 是 ``?p=`` 参数（1 起），缺省 1。``id`` 保留大小写原样（BV 号对
    大小写敏感，``BV1xx411c7mD`` 与 ``bv1xx411c7md`` 不是同一条）。

    只认三种形态：``b23.tv/<code>``、``bilibili.com/video/<BV|av>``、裸 ``BV…`` /
    ``av…``。别的链接**不认**——不替用户猜，猜错会去问一个不相干的视频。
    """
    raw = str(text or "")
    for part in _split(raw):
        if "://" in part:
            short = SHORT_RE.match(part)
            if short:
                return ("b23:" + short.group(1), _page_of(part))
            video = VIDEO_RE.match(part)
            if video:
                return (video.group(1), _page_of(part))
            continue
        if BARE_BV_RE.match(part) or BARE_AV_RE.match(part):
            return (part, 1)
    raise BilibiliError("没在这条消息里找到 B 站视频链接（认 b23.tv 短链、"
                        "bilibili.com/video/BV… 与裸 BV 号）")


_EDGE = '，。、）」』】"\'（）()【】《》〔〕,.;：:！!？?'


def _split(raw):
    """把一段话切成「URL 原样 / 其余按空白切开的词」，并剥掉两侧标点。"""
    out, rest = [], []
    pos = 0
    for match in _URL_RE.finditer(raw):
        rest.append(raw[pos:match.start()])
        out.append(match.group(0).rstrip(".,;"))
        pos = match.end()
    rest.append(raw[pos:])
    for chunk in rest:
        out.extend(word.strip(_EDGE) for word in re.split(r"[\s\u3000]+", chunk))
    return [item for item in out if item]


def language_of(text):
    """``#字幕 BV… en-US`` 里的语言后缀，没有就空串。

    语言**只从裸 ``BV…`` / ``av…`` 后面那个词取**：URL 后面跟的词多半是群友
    的话（「这个好看」），当成语言去查会得到一句「没有这个语言」的责怪。
    """
    parts = _split(text)
    for index, part in enumerate(parts[:-1]):
        if "://" not in part and (BARE_BV_RE.match(part) or BARE_AV_RE.match(part)):
            nxt = parts[index + 1]
            return "" if "://" in nxt else nxt
    return ""


def _page_of(url):
    found = _PAGE_RE.search(url)
    try:
        return max(1, int(found.group(1))) if found else 1
    except (TypeError, ValueError):
        return 1


def expand(code, timeout=TIMEOUT):
    """``b23.tv/<code>`` → ``(bvid|avid, page)``，只跟 **一跳** 302。

    SSRF 的两道闸：不跟第二跳（展开后的地址只解析、不再请求），且解析出的
    host 必须仍在 ``SITE_HOST`` / ``SHORT_HOST`` 内。指向别处的 302 一律当
    「链接打不开」——这是群友能贴进群的输入，不能拿它当任意 URL 的取数入口。
    """
    url = f"{SHORT_BASE.rstrip('/')}/{code}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    location = ""
    try:
        final, _raw = _get(req, timeout, no_redirect=True)
        location = final
    except BilibiliError:
        raise BilibiliError("这个分享链接打不开（短链服务没有给出新地址）")
    if not _allowed(location):
        log(f"expand rejected host: {_short_url(location) or '(empty)'}")
        raise BilibiliError("这个分享链接打不开（指向了 B 站以外的地址）")
    match = VIDEO_RE.match(location)
    if not match:
        raise BilibiliError("这个分享链接打开后不是视频页（可能是动态、专栏或直播）")
    return match.group(1), _page_of(location)


def _allowed(url):
    """host 是否在 ``SITE_HOST`` / ``SHORT_HOST`` 之内（读**此刻**的常量）。

    ``SITE_HOST`` 是 ``"bilibili.com"`` 这样的裸域名；门禁会把它换成
    ``"127.0.0.1:<port>"``，所以有端口时按「整个 host:port」比，没有端口才按
    域名后缀比——否则 ``hostname`` 里的裸 IP 永远比不上带端口的白名单项。
    """
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    site, short = SITE_HOST.lower(), SHORT_HOST.lower()
    if ":" in site or ":" in short:
        hostport = f"{host}:{parts.port}" if parts.port else host
        return any(hostport == item or hostport.endswith("." + item)
                   for item in (site, short))
    return any(host == item or host.endswith("." + item) for item in (site, short))



def resolve(ref, timeout=TIMEOUT):
    """``parse_ref`` 的产物 → ``(bvid_or_avid, page)``，短链在这里才真的展开。"""
    ident, page = ref
    if ident.startswith("b23:"):
        return expand(ident[4:], timeout)
    return ident, page


def video_url(ident, page=1):
    """给回复里贴的原视频地址（多 P 带上 ``?p=``）。"""
    suffix = f"?p={int(page)}" if int(page or 1) > 1 else ""
    return f"https://www.bilibili.com/video/{ident}{suffix}"


# --------------------------------------------------------------------------
# 稿件 / 字幕
# --------------------------------------------------------------------------
def view(ident, timeout=TIMEOUT):
    """稿件信息：``{bvid, aid, title, duration, pages: [{page, cid, part}]}``。"""
    key = "bvid" if str(ident).lower().startswith("bv") else "aid"
    data = _api("/x/web-interface/view", {key: str(ident)}, timeout)
    pages = []
    for item in data.get("pages") or []:
        if not isinstance(item, dict) or not item.get("cid"):
            continue
        pages.append({"page": int(item.get("page") or len(pages) + 1),
                      "cid": int(item["cid"]),
                      "part": str(item.get("part") or "")})
    if not pages:
        raise BilibiliError("这个视频没有可用的分 P 信息（可能是番剧或直播回放）")
    return {"bvid": str(data.get("bvid") or ident),
            "aid": data.get("aid"),
            "title": str(data.get("title") or ""),
            "duration": int(data.get("duration") or 0),
            "pages": pages}


def pick_page(info, page):
    """按 ``p=`` 取分 P；越界时报「只有 N P」而不是静默退回第一 P。"""
    pages = info["pages"]
    wanted = int(page or 1)
    for item in pages:
        if item["page"] == wanted:
            return item
    raise BilibiliError(f"这个视频只有 {len(pages)} P，没有第 {wanted} P")


def subtitles(bvid, cid, timeout=TIMEOUT):
    """字幕清单：``(list, need_login)``。每项 ``{lan, lan_doc, url}``。"""
    data = _api("/x/player/v2", {"bvid": str(bvid), "cid": int(cid)}, timeout)
    subtitle = data.get("subtitle") if isinstance(data.get("subtitle"), dict) else {}
    need_login = bool(subtitle.get("need_login_subtitle") or data.get("need_login_subtitle"))
    found = []
    for item in subtitle.get("subtitles") or []:
        if not isinstance(item, dict):
            continue
        url = _subtitle_url(str(item.get("subtitle_url") or ""))
        if not url:
            continue
        found.append({"lan": str(item.get("lan") or ""),
                      "lan_doc": str(item.get("lan_doc") or ""),
                      "url": url})
    return found, need_login


def _subtitle_url(raw):
    """``//aisubtitle.hdslb.com/…`` → ``https://…``，并校验域名在白名单里。

    这是第二个由外部输入决定的出站地址，和短链展开同一个纪律：不在
    ``hdslb.com`` 内就丢弃这一条（调用方据此当「没有字幕」说，不会去请求它）。
    """
    text = str(raw or "").strip()
    if text.startswith("//"):
        text = "https:" + text
    if not text:
        return ""
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not any(
            host == item or host.endswith("." + item) for item in SUBTITLE_HOSTS):
        log(f"subtitle_url rejected host: {host or '(empty)'}")
        return ""
    return text


def fetch_subtitle(url, timeout=TIMEOUT):
    """字幕正文：``[{from, to, content}]``。上游是 ``{"body": [...]}`` 信封。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    _final, raw = _get(req, timeout)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        log("decode failed subtitle body")
        raise BilibiliError("字幕文件解析不了（B 站返回的不是 JSON）") from exc
    body = payload.get("body") if isinstance(payload, dict) else None
    if not isinstance(body, list):
        raise BilibiliError("字幕文件里没有正文（body 字段缺失）")
    cues = []
    for item in body:
        if not isinstance(item, dict):
            continue
        text = _plain(str(item.get("content") or ""))
        if not text:
            continue
        cues.append({"from": _num(item.get("from")), "to": _num(item.get("to")),
                     "content": text})
    if not cues:
        raise BilibiliError("字幕文件是空的")
    return cues


def _num(value):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _plain(text):
    """字幕行常带 ``<c.colorE5E5E5>`` 一类的标签与行内换行，去掉再拼。"""
    return " ".join(_TAG_RE.sub("", text).split())


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
def stamp(seconds):
    """秒 → ``[mm:ss]``；超过一小时用 ``[h:mm:ss]``。"""
    total = int(max(0.0, _num(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"[{hours}:{minutes:02d}:{secs:02d}]"
    return f"[{minutes:02d}:{secs:02d}]"


def pick_track(tracks, lang=""):
    """按 ``lang`` 挑一条字幕轨道；没指定时优先中文，再退回第一条。"""
    wanted = str(lang or "").strip().lower()
    if wanted:
        for track in tracks:
            if wanted in (track["lan"].lower(), track["lan_doc"].lower()):
                return track
        # ``zh-CN`` / ``zh`` / ``中文（简体）`` 这种写不全的后缀也要认。
        for track in tracks:
            if wanted in track["lan"].lower() or wanted in track["lan_doc"].lower():
                return track
        names = "、".join(f"{t['lan_doc'] or t['lan']}" for t in tracks)
        raise BilibiliError(f"这个视频没有「{lang}」字幕，可选：{names}")
    for track in tracks:
        if track["lan"].lower().startswith("zh") or "中文" in track["lan_doc"]:
            return track
    return tracks[0]


def render(cues, title="", source="", limit=DEFAULT_MAX_CHARS, note=""):
    """把字幕拼成一段回群文字：按 ``MAX_CUES`` 与 ``limit`` 双上限截断并明说。

    ``note`` 是多 P 视频的「第几 P」，单独占一行——不带它，群友看着一段和
    标题对不上的字幕没法判断是自己要的那一 P，还是贴错链接了。
    """
    lines = []
    if title:
        lines.append(f"《{title}》字幕")
    if note:
        lines.append(note)
    if source:
        lines.append(source)
    shown, used = 0, 0
    for cue in cues[:MAX_CUES]:
        line = f"{stamp(cue['from'])} {cue['content']}"
        if used + len(line) + 1 > limit:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    if shown < len(cues):
        lines.append(f"……后面还有 {len(cues) - shown} 条，正文只贴到这里")
    return "\n".join(lines)


def _explain_empty(need_login, page_note=""):
    """``subtitles`` 为空时，把两种截然不同的事实分开说。"""
    suffix = f"（{page_note}）" if page_note else ""
    if need_login:
        raise NeedsLogin(f"这个视频的字幕要登录才看得到，匿名接口拿不到{suffix}——"
                         "自己点开视频开着字幕看吧")
    raise BilibiliError(f"这个视频（用匿名接口能问到的范围内）没有字幕{suffix}——"
                        "UP 主可能没传 CC 字幕，或者字幕还在生成中")


def answer(text, cfg=None):
    """``#字幕`` 的完整流程：解析引用 → 取稿件 → 取字幕清单 → 取正文 → 渲染。

    报错一律走 :class:`BilibiliError`（``str()`` 就是给群友的一句话），四种
    「没有」各有各的说法，见模块 docstring 与 :func:`_explain_empty`。
    """
    cfg = cfg or {}
    timeout = int(cfg.get("timeout") or TIMEOUT)
    limit = int(cfg.get("max_chars") or DEFAULT_MAX_CHARS)
    ident, page = resolve(parse_ref(text), timeout)

    info = cached(("view", ident), lambda: view(ident, timeout), LIST_TTL)
    target = pick_page(info, page)
    note = f"第 {target['page']} P：{target['part']}" if len(info["pages"]) > 1 else ""

    tracks, need_login = cached(
        ("subs", info["bvid"], target["cid"]),
        lambda: subtitles(info["bvid"], target["cid"], timeout),
        LIST_TTL)
    if not tracks:
        _explain_empty(need_login, note)
    track = pick_track(tracks, cfg.get("lang") or language_of(text))
    cues = cached(("body", track["url"]),
                  lambda: fetch_subtitle(track["url"], timeout), BODY_TTL)
    source = f"{video_url(ident, target['page'])} · {track['lan_doc'] or track['lan']}"
    return wg_text.render_handler_reply(
        render(cues, info.get("title") or "", source, limit, note), REPLY_CLIP)
