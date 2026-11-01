"""B 站字幕取数门禁（issue #339）：`#字幕` 的链路、降级与出站纪律。

全部走 ``fixture_server`` + ``network_guard``，零真实请求。每条用例对着一次
真实的判断失误或一次差点写错的地方：

1. **四种「没有」必须分开说**。匿名接口下 ``subtitles`` 普遍为空（离线抽查
   9 个稿件全空），所以这个指令平时给出的就是「没法给」；把「要登录」说成
   「UP 主没传」、把「没找到链接」说成「接口坏了」，群友就没法自己判断该
   点什么。四种文案两两不同，各有断言。
2. **短链只跟一跳，且落在白名单内**。``b23.tv`` 的 Location 是群友能间接
   控制的东西：指向 ``evil.example`` 时必须**零第二跳请求**——这是 SSRF
   判据，不是稳妥起见。
3. **``subtitle_url`` 同样受控**。``x/player/v2`` 的返回里那个地址会被直接
   fetch，不在 ``hdslb.com`` 内就丢弃（当「没有字幕」说），不许去请求它。
4. **缓存不缓存失败**。同一个视频连着几个人问是常态，但一次抖动被缓存成
   一整段 TTL，就等于把偶发故障固化。
5. **默认关闭且免绑定**。合入即静默连外网是本仓不接受的（sixty / busboss /
   repeater 同一口径）；查公开字幕不该要求绑定米游社账号。

    .venv/Scripts/python.exe scripts/check_bilibili_subtitle.py
"""
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

from check_fixture_support import assert_runner, network_guard, scratch_dir  # noqa: E402
from fixture_server import FixtureHandler, fixture_server  # noqa: E402

# 数据根必须在 import daemon 模块**之前**钉到临时目录（同 check_busboss）：
# LOG 等常量在 import 期就求值完了。
_SCRATCH = scratch_dir(prefix="tulpa-bilibili-")
os.environ["WATER_GROUP_HOME"] = str(_SCRATCH / "home")
(_SCRATCH / "home").mkdir(parents=True, exist_ok=True)

import bilibili  # noqa: E402
import commands  # noqa: E402

_runner = assert_runner(markers="ok")
check = _runner.check
PORTS = set()

# 夹具里的号码全是占位值（同本仓「仓库里不写死真实号码」的纪律）。
BVID = "BV1xx411c7mD"
CID_MAIN = 10001
CID_PART2 = 10002
SUBTITLE_PATH = "/bfs/subtitle/10001.json"

# 夹具里的站内域名由 main() 按实际端口回填（``patch`` 与用例字面量共用一份）。
SITE = ""
SHORT_LINK = ""
# 夹具端口：SSRF 用例要造「能连通、但 Host 不在白名单」的站外地址。
PORT = 0

STATE = {"requests": [], "short_target": "", "subs": [], "need_login": False,
         "view_code": 0,
         "subtitle_body": {"body": [
             {"from": 0.0, "to": 2.5, "content": "第一句字幕"},
             {"from": 2.5, "to": 5.0, "content": "<c.colorE5E5E5>第二句\n字幕</c>"},
             {"from": 5.0, "to": 8.0, "content": "第三句字幕"},
         ]}}


def reset(**kwargs):
    """用例之间复位夹具状态 + 清缓存，免得一条用例的残留当另一条的证据。"""
    STATE.update({"requests": [], "short_target": "", "subs": [],
                  "need_login": False, "view_code": 0})
    STATE.update(kwargs)
    bilibili.clear_cache()


class Handler(FixtureHandler):
    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        # ``host`` 记 Host 头：站外用例靠它区分「第二跳打到哪个 host」——
        # 只看 path 的话，站内/站外两个目标在夹具里是同一个处理器（issue #349）。
        STATE["requests"].append({"path": path, "query": query,
                                  "host": self.headers.get("Host", ""),
                                  "ua": self.headers.get("User-Agent", "")})
        if path.startswith("/s/"):
            # 短链：302 到 STATE["short_target"]（用例决定指哪儿）。
            self.send_response(302)
            self.send_header("Location", STATE["short_target"])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/video/"):
            # 短链展开后会接到这里。``_get(no_redirect=True)`` 是「不跟第二跳」，
            # 不是「但凡 3xx 就算数」——给个 200 的落地页才是真实的形状。
            self.send_json({"ok": True}, status=200)
            return
        if path == "/x/web-interface/view":
            if STATE["view_code"]:
                self.send_json({"code": STATE["view_code"], "message": "请求错误"})
                return
            self.send_json({"code": 0, "data": {
                "bvid": BVID, "aid": 20002, "title": "夹具视频",
                "duration": 600, "videos": 2,
                "pages": [{"page": 1, "cid": CID_MAIN, "part": "第一 P"},
                          {"page": 2, "cid": CID_PART2, "part": "第二 P"}]}})
            return
        if path == "/x/player/v2":
            self.send_json({"code": 0, "data": {"subtitle": {
                "need_login_subtitle": STATE["need_login"],
                "subtitles": STATE["subs"]}}})
            return
        if path == SUBTITLE_PATH:
            self.send_json(STATE["subtitle_body"])
            return
        self.send_json({"code": -404, "message": "unknown path"}, status=404)

    def do_POST(self):
        self.send_json({"code": -400, "message": "请求错误"}, status=400)


def track(name, lan="zh-CN", lan_doc="中文（自动生成）", path=SUBTITLE_PATH,
          host=None):
    """一条字幕轨；``host`` 给非白名单域名时用来测丢弃。"""
    base = host or STATE["subtitle_host"]
    return {"lan": lan, "lan_doc": lan_doc, "subtitle_url": f"{base}{path}"}


def requests_to(path):
    return [item for item in STATE["requests"] if item["path"] == path]


# --------------------------------------------------------------------------
def test_parse_ref():
    print("[引用解析]")
    for text, want_id, want_page in (
            (f"http://{SITE}/video/{BVID}?p=3", BVID, 3),
            (f"看这个 http://{SITE}/video/av20002 好笑", "av20002", 1),
            (f"https://{SITE}/s/AbCdEf", "b23:AbCdEf", 1),
            (f"分享 https://{SITE}/s/AbCdEf?p=2 就行", "b23:AbCdEf", 2),
            ("直接甩个 BV1xx411c7mD", "BV1xx411c7mD", 1),
            ("（BV1xx411c7mD）", "BV1xx411c7mD", 1),
    ):
        got = bilibili.parse_ref(text)
        check(f"认出 {text[:32]!r}", got == (want_id, want_page), str(got))
    check("BV 号大小写原样保留",
          bilibili.parse_ref("BV1Xx411C7Md")[0] == "BV1Xx411C7Md")
    for text in ("", "今天天气不错", "https://example.com/video/" + BVID,
                 "https://evil.example/" + BVID):
        try:
            bilibili.parse_ref(text)
        except bilibili.BilibiliError:
            check(f"不认 {text[:24]!r}", True)
        else:
            check(f"不认 {text[:24]!r}", False, "没有抛错")


def test_happy_path():
    print("[完整链路]")
    reset(short_target=f"http://{SITE}/video/{BVID}",
          subs=[track("zh"), track("en-US", "en-US", "English")])
    text = bilibili.answer("#字幕 " + SHORT_LINK, {})
    check("短链被展开（一次请求）", len(requests_to("/s/AbCdEf")) == 1,
          str(STATE["requests"]))
    check("稿件接口按 bvid 问", bool(requests_to("/x/web-interface/view")))
    view_q = requests_to("/x/web-interface/view")
    check("稿件请求带 bvid",
          view_q and view_q[0]["query"].get("bvid") == [BVID],
          str(view_q[0]["query"]) if view_q else "无请求")
    player = requests_to("/x/player/v2")
    check("字幕清单按 bvid+cid 问",
          player and player[0]["query"].get("bvid") == [BVID]
          and player[0]["query"].get("cid") == [str(CID_MAIN)],
          str(player[0]["query"]) if player else "无请求")
    check("正文取自字幕文件", bool(requests_to(SUBTITLE_PATH)))
    check("标题进正文", "《夹具视频》字幕" in text, text[:200])
    check("贴了原视频地址",
          f"/video/{BVID}" in text, text[:200])
    check("时间戳按 mm:ss", "[00:00] 第一句字幕" in text, text[:400])
    check("字幕行内标签被剥掉", "第二句 字幕" in text and "<c." not in text, text[:400])
    check("默认挑中文轨", "中文" in text and "English" not in text, text[:200])


def test_second_page():
    print("[多 P 取 p=]")
    reset(subs=[track("zh")])
    text = bilibili.answer(f"http://{SITE}/video/{BVID}?p=2", {})
    player = requests_to("/x/player/v2")
    check("p=2 问的是第二 P 的 cid",
          player and player[0]["query"].get("cid") == [str(CID_PART2)],
          str(player[0]["query"]) if player else "无请求")
    check("正文说出是哪一 P", "第 2 P：第二 P" in text, text[:300])
    reset(subs=[track("zh")])
    try:
        bilibili.answer(f"http://{SITE}/video/{BVID}?p=99", {})
    except bilibili.BilibiliError as exc:
        check("越界的 p 明说只有几 P", "只有 2 P" in str(exc) and "第 99 P" in str(exc),
              str(exc))
    else:
        check("越界的 p 明说只有几 P", False, "没有抛错")


def test_language():
    print("[语言选择]")
    reset(subs=[track("zh-CN"), track("en-US", "en-US", "English")])
    text = bilibili.answer(BVID + " en-US", {})
    check("指定语言挑得到", "English" in text, text[:200])
    reset(subs=[track("zh-CN"), track("en-US", "en-US", "English")])
    try:
        bilibili.answer(BVID + " ja", {})
    except bilibili.BilibiliError as exc:
        check("指定语言没有时列出可选", "没有「ja」字幕" in str(exc) and "中文" in str(exc),
              str(exc))
    else:
        check("指定语言没有时列出可选", False, "没有抛错")


def test_four_kinds_of_nothing():
    print("[四种「没有」分开说]")
    messages = {}
    try:
        bilibili.answer("今天天气不错", {})
    except bilibili.BilibiliError as exc:
        messages["no_link"] = str(exc)
    reset(short_target="https://evil.example/video/" + BVID)
    try:
        bilibili.answer(SHORT_LINK, {})
    except bilibili.BilibiliError as exc:
        messages["bad_link"] = str(exc)
    reset(subs=[], need_login=True)
    try:
        bilibili.answer(BVID, {})
    except bilibili.NeedsLogin as exc:
        messages["login"] = str(exc)
    reset(subs=[], need_login=False)
    try:
        bilibili.answer(BVID, {})
    except bilibili.BilibiliError as exc:
        messages["no_sub"] = str(exc)
    reset(view_code=-404)
    try:
        bilibili.answer(BVID, {})
    except bilibili.BilibiliError as exc:
        messages["api"] = str(exc)

    check("四种说法都在", set(messages) == {"no_link", "bad_link", "login", "no_sub", "api"},
          str(sorted(messages)))
    check("没有链接说「没找到链接」", "找不到" in messages.get("no_link", "")
          or "没在这条消息里找到" in messages.get("no_link", ""),
          messages.get("no_link", ""))
    check("坏链接不说成「UP 主没传」",
          "打不开" in messages.get("bad_link", "")
          and "没传" not in messages.get("bad_link", ""), messages.get("bad_link", ""))
    check("要登录说「要登录」", "登录" in messages.get("login", ""), messages.get("login", ""))
    check("真没有说「没字幕」", "没有字幕" in messages.get("no_sub", ""),
          messages.get("no_sub", ""))
    check("登录与真没有是两句话",
          messages.get("login") != messages.get("no_sub"),
          f"{messages.get('login')!r} vs {messages.get('no_sub')!r}")
    check("视频不存在说「不存在或已删除」",
          "不存在" in messages.get("api", ""), messages.get("api", ""))
    check("四句两两不同", len(set(messages.values())) == len(messages), str(messages))


def test_ssrf_one_hop():
    print("[出站纪律]")
    # 302 目标必须**真的能连到夹具**，否则「没有第二跳」在任何实现下都成立
    # （issue #349：原先指向 `evil.example`，DNS 解析不到 + `network_guard` 先拦，
    # 把 `_allowed()` 换成恒 `True` 也照样绿）。这里用同一端口的 `localhost`：
    # 它解析到 loopback、守卫放行，但 Host 头是 `localhost:<port>`，不是白名单项
    # `127.0.0.1:<port>`——对 `_allowed()` 而言就是站外。
    out_of_site = f"http://localhost:{PORT}/video/" + BVID
    reset(short_target=out_of_site)
    text = ""
    try:
        text = bilibili.answer(SHORT_LINK, {})
    except bilibili.BilibiliError as exc:
        text = str(exc)
    # 两道闸（正则 / 白名单）任一条挡下都算对，这里只钉「确实没发第二跳」+
    # 「说法是打不开」。**具体是哪道闸**由 test_site_whitelist_alone 单独钉。
    check("指向站外的 302 不做第二跳",
          len(STATE["requests"]) == 1, str(STATE["requests"]))
    check("指向站外的 302 说「打不开」不说成没字幕",
          "打不开" in text and "没有字幕" not in text, text)
    # 对照：同一个夹具、同一台服务器，把 302 换成站内地址就**必须**走完链路。
    # 没有这条，「零第二跳」也可能只是夹具不好使。
    reset(short_target=f"http://{SITE}/video/" + BVID, subs=[track("zh")])
    text = ""
    try:
        text = bilibili.answer(SHORT_LINK, {})
    except bilibili.BilibiliError as exc:
        text = str(exc)
    check("对照组：站内 302 真的走完了链路",
          [item["path"] for item in STATE["requests"]][:1] == ["/s/AbCdEf"]
          and "/x/web-interface/view" in [item["path"] for item in STATE["requests"]]
          and "第一句字幕" in text,
          f"{[item['path'] for item in STATE['requests']]} | {text[:80]}")

    # 字幕地址白名单：站外地址一次都不许请求，也不许被当成「有字幕」进缓存。
    reset(short_target=f"http://{SITE}/video/" + BVID, subs=[track("zh")])
    STATE["subs"] = [{"lan": "zh-CN", "lan_doc": "中文",
                      "subtitle_url": f"http://localhost:{PORT}/steal.json"}]
    text = ""
    try:
        text = bilibili.answer(SHORT_LINK, {})
    except bilibili.BilibiliError as exc:
        text = str(exc)
    check("非 hdslb 的字幕地址一次都不请求",
          not any(item["path"] == "/steal.json" for item in STATE["requests"]),
          str([item["path"] for item in STATE["requests"]]))
    # 与「没有字幕」同口径：说「没有字幕」，不说成接口坏了、也不说成要登录。
    check("被丢弃的地址与「没有字幕」同口径",
          "没有字幕" in text and "登录" not in text and "暂时不可用" not in text, text)
    # 同一句结论要稳定：再问一次仍说「没有字幕」，不会因为第一次失败而变成
    # 「接口不可用」或半截正文。字幕清单本身是成功的（拿到 tracks），缓存它是对的；
    # 被丢弃的只是那条**地址**，它从来没被请求过，也就没有「缓存失败」这回事。
    again = ""
    try:
        again = bilibili.answer(SHORT_LINK, {})
    except bilibili.BilibiliError as exc:
        again = str(exc)
    check("被丢弃的地址每次都给同一句「没有字幕」",
          again == text, f"{text!r} vs {again!r}")


def test_site_whitelist_alone():
    """单独把 `_allowed()` 钉在靶子上（issue #349 复核）。

    `test_ssrf_one_hop` 其实测不到这道闸：`_patched()` 把 `SITE_HOST` 与
    `VIDEO_RE` 一起换成了 `127.0.0.1:<port>`，于是 `localhost:<port>/video/BV…`
    在**正则**那一道就被挡掉——把 `_allowed()` 换成恒 `True`，那条用例照样绿。
    实测：`_allowed` 恒真 + `VIDEO_RE` 不认 host 时，整套门禁全绿而第二跳真的
    发出去了（`/video/BV…` 进了夹具的请求表）。

    所以这里换个 302 目标：**同样长得像视频页**（能过 `VIDEO_RE`）但 host 不在
    白名单里。默认判据下它必须被 `_allowed()` 拦下，且在发请求之前就拦下。

    为了让这条用例真的落到 `_allowed()` 上，用例期间把 `VIDEO_RE` 临时放宽成
    「不认 host」——否则 `localhost:<port>` 又会被正则挡掉，测到的还是正则。
    放宽只影响这条路，对照组在同一 `SITE_HOST` 下照常走完链路，证明放宽没把
    链路弄坏。
    """
    print("[站内白名单]")
    probe = f"http://localhost:{PORT}/video/" + BVID
    lenient = re.compile(r"^(?:https?://)?(?:www\.|m\.)?" + r"[^/]+"
                         + r"/video/(BV[A-Za-z0-9]+|av\d+)", re.I)
    with patch.object(bilibili, "VIDEO_RE", lenient):
        # 对照组不可省：证明「放宽后的正则」确实放行这个地址，否则下面那条
        # 「被白名单挡下」在任何实现下都成立（又变回假绿）。
        check("对照组：放宽正则后这个地址确实过了正则",
              bool(bilibili.VIDEO_RE.match(probe)), probe)
        reset(short_target=probe)
        text = ""
        try:
            text = bilibili.answer(SHORT_LINK, {})
        except bilibili.BilibiliError as exc:
            text = str(exc)
        check("指向站外的 302 被白名单挡下（正则已放行）",
              "打不开" in text and "不是视频页" not in text, text)
        check("被白名单挡下的地址一次都没被请求",
              [item["path"] for item in STATE["requests"]] == ["/s/AbCdEf"],
              str([item["path"] for item in STATE["requests"]]))
    # 对照组：把同一个地址的 host 换成白名单项，就必须放行并走完链路。
    reset(short_target=f"http://{SITE}/video/" + BVID, subs=[track("zh")])
    passed = ""
    try:
        passed = bilibili.answer(SHORT_LINK, {})
    except bilibili.BilibiliError as exc:
        passed = str(exc)
    check("对照组：白名单内的同形态地址放行并取到正文",
          "/x/web-interface/view" in [i["path"] for i in STATE["requests"]]
          and "第一句字幕" in passed,
          f"{[i['path'] for i in STATE['requests']]} | {passed[:80]}")


def test_redirect_guard():
    print("[禁跳]")
    # `no_redirect=True` 必须真的不跟：实现层靠 `_NoRedirect` 拒绝 3xx（issue #349）。
    # 这里直接对夹具的短链发一个 `no_redirect` 请求，确认拿到的是 Location 原文、
    # 且夹具**只收到一条**（若被跟随，第二条会打到 302 目标）。
    reset(short_target=f"http://{SITE}/video/" + BVID)
    req = urllib.request.Request(f"http://{SITE}/s/AbCdEf",
                                 headers={"User-Agent": "fixture"})
    final, raw = bilibili._get(req, 5, no_redirect=True)
    check("no_redirect 交回 Location 而不是终点地址",
          final == f"http://{SITE}/video/{BVID}", final)
    check("no_redirect 时夹具只收到一条（302 没被跟随）",
          [item["path"] for item in STATE["requests"]] == ["/s/AbCdEf"],
          str([item["path"] for item in STATE["requests"]]))
    # 对照组：默认情况下**必须**跟随，否则上面那条「没跟随」可能只是根本不通。
    reset(short_target=f"http://{SITE}/video/" + BVID)
    req = urllib.request.Request(f"http://{SITE}/s/AbCdEf",
                                 headers={"User-Agent": "fixture"})
    final, _raw = bilibili._get(req, 5)
    check("对照组：默认会跟随 302",
          final.endswith(f"/video/{BVID}")
          and [item["path"] for item in STATE["requests"]]
          == ["/s/AbCdEf", f"/video/{BVID}"],
          f"{final} | {[item['path'] for item in STATE['requests']]}")


def test_cache():
    print("[缓存]")
    reset(subs=[track("zh")])
    bilibili.answer(BVID, {})
    first = len(STATE["requests"])
    text = bilibili.answer(BVID, {})
    check("第二次问同一视频不再发请求", len(STATE["requests"]) == first,
          f"{first} → {len(STATE['requests'])}")
    check("命中缓存照样给正文", "第一句字幕" in text, text[:200])
    reset(view_code=-400)
    for _ in range(2):
        try:
            bilibili.answer(BVID, {})
        except bilibili.BilibiliError:
            pass
    check("失败结果不进缓存（两次都真的问了）",
          len(requests_to("/x/web-interface/view")) == 2,
          str(len(requests_to("/x/web-interface/view"))))


def test_render_limits():
    print("[渲染限额]")
    cues = [{"from": float(i), "to": float(i + 1), "content": f"第{i}句"} for i in range(50)]
    text = bilibili.render(cues, "标题", "来源", limit=200)
    check("超限明说还剩几条", "后面还有" in text, text[-80:])
    body = [line for line in text.splitlines() if line.startswith("[")]
    check("正文没超 limit", sum(len(line) + 1 for line in body) <= 200,
          str(sum(len(line) + 1 for line in body)))
    text = bilibili.render(cues[:3], "标题", "")
    check("没超限时不打截断话", "后面还有" not in text, text)
    check("单条上限 MAX_CUES 老实地截",
          bilibili.render([{"from": 0.0, "to": 1.0, "content": "x"}] * (bilibili.MAX_CUES + 5),
                          "", "", limit=10 ** 6).count("\n") == bilibili.MAX_CUES)
    check("时间戳过一小时带小时位", bilibili.stamp(3725) == "[1:02:05]", bilibili.stamp(3725))
    check("时间戳零值", bilibili.stamp(0) == "[00:00]", bilibili.stamp(0))


def test_handle_usage():
    print("[handler 与注册]")
    handler = commands.HANDLERS.get("字幕")
    check("#字幕 已注册", handler is not None, str(list(commands.HANDLERS)[:10]))
    if handler is not None:
        check("handler 不需要绑定凭证", getattr(handler[1], "need_ck", True) is False)
    owner = commands.PLUGINS.owner("字幕")
    check("#字幕 归属 bilibili 插件", owner is not None and owner.name == "bilibili",
          getattr(owner, "name", "None"))
    check("插件加载无错误", commands.PLUGINS.load_errors == {},
          str(commands.PLUGINS.load_errors))
    plugin = commands.PLUGINS.get("bilibili")
    check("插件默认关闭", plugin is not None and plugin.default_enabled is False)
    settings = commands.settings_from({})
    check("默认配置下 #字幕 不可用",
          not commands.PLUGINS.available("字幕", settings, ""))
    check("默认配置下不在 #帮助里", "#字幕" not in commands.help_text(settings, ""),
          commands.help_text(settings, "")[:200])
    on = commands.settings_from({"plugins": {"bilibili": {"enabled": True}}})
    check("开启后 #字幕 可用", commands.PLUGINS.available("字幕", on, ""))
    check("开启后出现在 #帮助", "#字幕" in commands.help_text(on, ""),
          commands.help_text(on, "")[:200])
    # 免凭证要靠 ``need_ck=False`` 真的过掉宿主那道闸（``host.resolve_credentials``）：
    # 只用未被任何用户绑定的合成 user_id，回「用法」说明没被拦去发 #登录 提示。
    reply = commands.run_handler("字幕", commands.HANDLERS["字幕"][1],
                                 on, {"group_id": "1", "user_id": "339000001"}, "")
    check("空参数回用法而不是「指令执行出错」",
          "用法" in reply and "出错" not in reply, reply)
    check("免凭证指令没被拦去要绑定", "登录" not in reply, reply)
    opts = commands.plugin_options(on, "bilibili")
    check("配置归一有默认值", opts.get("max_chars") == 3000 and opts.get("lang") == "",
          str(opts))


def test_answer_clipped():
    print("[回复长度]")
    reset(subs=[track("zh")],
          subtitle_body={"body": [{"from": float(i), "to": float(i + 1),
                                   "content": "很长的字幕内容" * 8} for i in range(200)]})
    text = bilibili.answer(BVID, {"max_chars": 10 ** 6})
    check("再长的字幕也受 REPLY_CLIP 约束", len(text) <= bilibili.REPLY_CLIP,
          str(len(text)))


def _run(fn):
    """一段炸了不许把后面的断言一起吞掉（同 check_busboss）。"""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        check(f"{fn.__name__} 跑完不炸", False, f"{type(exc).__name__}: {exc}")


def _patched(server_port):
    """把四处出站地址整体挪到本机夹具。

    ``SHORT_HOST`` / ``SITE_HOST`` 同时是 ``SHORT_RE`` / ``VIDEO_RE`` 的**原料**
    （模块里由它们 ``re.escape`` 现拼），所以换域名时正则必须跟着换——门禁换的
    是域名，不是判据。四条一起换才是「换域名」，只换一半会在第一次请求前就被
    正则挡住，用例红得莫名其妙。

    ``subtitle_url`` 的 host 白名单单独换（``SUBTITLE_HOSTS``），这样才能在同
    一个夹具上既测「白名单内的地址会去请求」又测「白名单外的地址不请求」。
    """
    import re
    base = f"http://127.0.0.1:{server_port}"
    host = "127.0.0.1"
    # 站内域名也挪到本机：短链展开后要再判一次「仍在站内」，夹具的 Location
    # 指回 127.0.0.1，不换 SITE_HOST 就会在第二道闸被当「指向站外」挡掉——
    # 那样测不到链路，只测到了闸门。
    site = f"{host}:{server_port}"
    return [
        patch.object(bilibili, "BASE", base),
        patch.object(bilibili, "SHORT_BASE", base + "/s"),
        patch.object(bilibili, "SHORT_HOST", site),
        patch.object(bilibili, "SITE_HOST", site),
        patch.object(bilibili, "SUBTITLE_HOSTS", (host,)),
        patch.object(bilibili, "SHORT_RE", re.compile(
            r"^(?:https?://)?(?:www\.)?" + re.escape(site)
            + r"/s/([A-Za-z0-9]+)", re.I)),
        patch.object(bilibili, "VIDEO_RE", re.compile(
            r"^(?:https?://)?(?:www\.|m\.)?" + re.escape(site)
            + r"/video/(BV[A-Za-z0-9]+|av\d+)", re.I)),
    ]


def main():
    from contextlib import ExitStack
    global SITE, SHORT_LINK, PORT
    with fixture_server(Handler, ports=PORTS) as server:
        PORT = server.server_port
        SITE = f"127.0.0.1:{PORT}"
        SHORT_LINK = f"http://{SITE}/s/AbCdEf"
        STATE["subtitle_host"] = f"http://{SITE}"
        reset()
        with ExitStack() as stack:
            for manager in _patched(server.server_port):
                stack.enter_context(manager)
            restrict, _calls = network_guard(PORTS)
            stack.enter_context(patch("socket.create_connection", restrict))
            for fn in (test_parse_ref, test_happy_path, test_second_page,
                       test_language, test_four_kinds_of_nothing,
                       test_ssrf_one_hop, test_site_whitelist_alone,
                       test_redirect_guard, test_cache,
                       test_render_limits,
                       test_handle_usage, test_answer_clipped):
                _run(fn)
    return _runner.summary()


if __name__ == "__main__":
    sys.exit(main())
