"""钉住 60s 资讯 handler（issue #53 方案 A）：信封契约、query 参数、缓存、白名单、降级。

全部走 fixture_server + network_guard，不发真实请求。回归重点都是踩过的坑：
  - 天气定位参数叫 **query** 不是 city：传 city 会被上游静默忽略，永远返回
    实例所在城市的天气（2026-10-23 实测，60s.viki.moe 同样如此，属上游行为）；
  - 城市名带「市」「省」后缀会 404，normalize_city 必须剥掉；
  - allow=[] 是**全禁**不是全通（第一版写反过）；
  - need_ck 必须 False，否则未绑定群友发 #简报 收到的是绑定提示；
  - 失败结果不进缓存：一次抖动不该被放大成一整段 TTL。

    .venv/Scripts/python.exe -m scripts.check_sixty

#286：本文件曾是单文件 `scripts/check_sixty.py`（812 行，advisory 带）。按
#282（`scripts/check_qr_login/`）、#247（`scripts/check_dossier_cadence/`）与
#169 PF-2（`scripts/check_listener_rehang/`）的先例拆成包：`_fixture`
（Fake60s + route 表 + `_cfg` / `_ctx` / `_day` + 模块级状态），本 `__init__.py`
保留 docstring 与 26 条用例 + `main()`，`__main__.py` 供 `python -m`。入口名不变
（`check_sixty`），对 `gates.json` / `run_gates.py` / CI 透明。用例调用序、断言、
正文逐字搬运，零重排。

R4（issue #280）已合流：`check()` runner 委派 `_fixture` 里的
`assert_runner(markers="tick", indent="  ")`（✓/✗ 标记变体，与 #169 PE-1 的
`run_cases()` 形态不同、互不替）。`main()` 用 `_runner.summary()` 出尾行。
"""
# 导入序是 load-bearing（issue #286）：`_fixture` 必须最先——它把 `scripts/` +
# daemon 插进 `sys.path`、钉住数据根（`check_fixture_support` import 期会把它钉成
# 夹具目录）、并定义 `Fake60s` / route 表 / `_cfg` / `_ctx` / `_day` / 模块级状态
#（含 R4 的 `_runner` / `check` / `GUARD_PORTS` / `FIXTURE_BASE`）。本主模块的
# 26 条用例依赖这些名字先就位。顺序即旧文件的用例调用序——26 条契约的运行顺序
# 靠 `main()` 里那张 `fn` 元组逐字保持。禁止改成一行 `from . import _fixture`：
# 显式分行防后人误重排。
from ._fixture import (  # noqa: F401
    ANSWER, BILI, BRIEF, BRIEF_EMPTY, BAIDU, CITY_404, DOUBAN, DOUYIN, DUANZI,
    EX_RATE, FIXTURE_BASE, FUEL, FUEL_UNSUPPORTED, Fake60s, GOLD, GUARD_PORTS,
    HITOKOTO, HISTORY, ITNEWS, KFC, KFC_EMPTY, LUCK, MOYU, REDNOTE, ROUTES,
    TOUTIAO, WEATHER, WEIBO, ZHIHU, _cfg, _ctx, _day, _runner, check,
    fixture_server, network_guard, scratch_dir, temp_dir,
)
import os
import socket
import sys
import time
from pathlib import Path
from unittest.mock import patch  # noqa: E402

import commands  # noqa: E402
import sixty  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- 用例

def test_normalize_city():
    print("[normalize_city]")
    cases = [("广州（粤A）", "广州"), ("惠州市", "惠州"), ("广东省广州市", "广州"),
             ("广东省", ""), ("上海市", "上海"), ("深圳", "深圳"),
             ("", ""), (None, ""), ("  北京  ", "北京")]
    for raw, want in cases:
        check(f"{raw!r} → {want!r}", sixty.normalize_city(raw) == want,
              f"实际 {sixty.normalize_city(raw)!r}")


def test_allowed_gate():
    print("[allow 白名单闸门]")
    check("没配 sixty → 全禁", not sixty.allowed("简报", None))
    check("空 cfg → 全禁", not sixty.allowed("简报", {}))
    check("enabled=false → 全禁", not sixty.allowed("简报", {"enabled": False}))
    on = {"enabled": True}
    check("allow 缺省 → 默认名单全通",
          all(sixty.allowed(k, on) for k in sixty.DEFAULT_ALLOW))
    check("allow=[] → 全禁（不是全通）",
          not any(sixty.allowed(k, dict(on, allow=[])) for k in sixty.DEFAULT_ALLOW))
    subset = dict(on, allow=["天气"])
    check("allow 子集：名单内放行", sixty.allowed("天气", subset))
    check("allow 子集：名单外拦住", not sixty.allowed("简报", subset))
    check("allow 去空白后仍匹配", sixty.allowed("热搜", dict(on, allow=[" 热搜 "])))


def test_registry_and_merge():
    print("[注册表与合并]")
    check("十四个指令键", set(sixty.HANDLERS) == {
        "简报", "热搜", "天气", "预报", "历史", "摸鱼", "运势",
        "行情", "豆瓣", "科技", "KFC", "笑话", "答案", "一言"})
    for key, (desc, fn) in sixty.HANDLERS.items():
        check(f"#{key} 免登录（need_ck=False）", getattr(fn, "need_ck", True) is False)
        check(f"#{key} 有描述", bool(str(desc).strip()))
    check("commands.HANDLERS 含米游社指令", "公告" in commands.HANDLERS)
    check("commands.HANDLERS 含资讯指令", "简报" in commands.HANDLERS)
    check("FETCH_ERRORS 含 SixtyError", sixty.SixtyError in commands.FETCH_ERRORS)
    # 模块级接线：导入没报错 + 十四条 handler 都合并进 commands（启动日志的
    # handlers/plugins 计数就靠这两个值，跑起来对不上说明进程是旧的，见 issue #68）
    plugin = commands.PLUGINS.get("sixty")
    check("sixty 导入无错", commands.PLUGINS.load_errors.get("sixty", "") == "")
    check("commands 合并到十四条资讯 handler",
          plugin is not None and len(plugin.commands) == 14
          and all(cmd.name in commands.HANDLERS for cmd in plugin.commands))
    # #KFC 全是 ASCII，手机英文键盘常打小写：匹配必须大小写不敏感
    check("#kfc 命中 #KFC", commands.match_handler("kfc") == ("KFC", ""))


def test_plugin_counts_matches_probe():
    print("[plugin_counts 与探针同口径（issue #259）]")
    # issue 现场：config 没显式配 plugins.sixty → default_enabled=False 让两边都判停用。
    # 启动行 plugin_counts 与 --status 的 status_line 必须一致，不能一个写 14、一个写 0。
    bare = commands.settings_from({})
    counts = commands.plugin_counts(bare)
    status = commands.PLUGINS.status_line(bare)
    check("未配 sixty 时 plugin_counts 写 sixty:0", "sixty:0" in counts, counts)
    check("未配 sixty 时 status_line 写 sixty 停用", "sixty 停用" in status, status)
    # 反向：显式开 sixty 时两边都得是非零活跃数
    on = commands.settings_from({"plugins": {"sixty": {
        "enabled": True, "options": {"base_url": FIXTURE_BASE}}}})
    counts_on = commands.plugin_counts(on)
    status_on = commands.PLUGINS.status_line(on)
    check("开 sixty 时 plugin_counts 写 sixty:14", "sixty:14" in counts_on, counts_on)
    check("开 sixty 时 status_line 写 sixty 已启用", "sixty 已启用" in status_on, status_on)
    # 群白名单插件在 group_id="" 时只判总开关：活跃数 = 全部指令数（不被群白名单压成 0）
    only = commands.settings_from({"plugins": {"sixty": {
        "enabled": True, "groups": ["2"],
        "options": {"base_url": FIXTURE_BASE}}}})
    counts_only = commands.plugin_counts(only)
    check("群白名单插件在启动行不计群级覆盖", "sixty:14" in counts_only, counts_only)


def test_settings_from():
    print("[settings_from / 插件三态]")
    bare = commands.settings_from({})
    check("缺省关闭", bare["plugins"]["sixty"]["enabled"] is False)
    check("缺省 allow=None（用默认名单）",
          bare["plugins"]["sixty"]["options"]["allow"] is None)
    check("缺省 max_items=5", bare["plugins"]["sixty"]["options"]["max_items"] == 5)
    weird = commands.settings_from({"slash_commands": {"sixty": {
        "enabled": True, "max_items": 0, "timeout": "abc", "base_url": " http://x ",
        "default_city": " 广州 ", "allow": [" 天气 ", "", "热搜"]}}})
    entry = weird["plugins"]["sixty"]
    opts = entry["options"]
    check("max_items 非正数回落 5", opts["max_items"] == 5)
    check("timeout 非数字回落 0", opts["timeout"] == 0)
    check("base_url 去空白", opts["base_url"] == "http://x")
    check("default_city 去空白", opts["default_city"] == "广州")
    check("allow 去空白丢空串", opts["allow"] == ["天气", "热搜"])
    check("enabled 透传", entry["enabled"] is True)
    check("旧键记为 plugins_legacy（--status 会提示可迁）",
          weird["plugins_legacy"] == ["sixty"], str(weird["plugins_legacy"]))
    # 群级关闭：插件总开关开着，但某个群不想看
    scoped = commands.settings_from({
        "slash_commands": {"sixty": {"enabled": True, "base_url": FIXTURE_BASE}},
        "groups": {"1": {"enabled": True, "plugins": {"sixty": False}},
                   "2": {"enabled": True}}})
    check("群级关闭归一成 group_off",
          scoped["plugins"]["sixty"]["group_off"] == ["1"],
          str(scoped["plugins"]["sixty"]["group_off"]))
    check("群级关闭的群不列 #简报", "简报" not in commands.help_text(scoped, "1"))
    check("别的群照常列 #简报", "简报" in commands.help_text(scoped, "2"))
    # 群白名单：只在这些群开
    only = commands.settings_from({"plugins": {"sixty": {
        "enabled": True, "groups": ["2"],
        "options": {"base_url": FIXTURE_BASE}}}})
    check("groups 白名单内放行", "简报" in commands.help_text(only, "2"))
    check("groups 白名单外落空", "简报" not in commands.help_text(only, "3"))


def test_gate_end_to_end():
    print("[resolve_reply 闸门端到端]")
    on = commands.settings_from({"slash_commands": {"sixty": {
        "enabled": True, "base_url": FIXTURE_BASE}}})
    off = commands.settings_from({"slash_commands": {"sixty": {"enabled": False}}})
    ev = {"user_id": "1", "group_id": "1"}
    check("开启后走到 handler", isinstance(commands.resolve_reply("简报", on, ev), str))
    check("关闭后静默落空（不吵）", commands.resolve_reply("简报", off, ev) is None)
    check("未配置 sixty 段时落空",
          commands.resolve_reply("简报", commands.settings_from({}), ev) is None)
    check("关闭时帮助不含资讯指令", "简报" not in commands.help_text(off))
    check("开启时帮助含资讯指令", "简报" in commands.help_text(on))
    check("帮助始终含米游社指令", "公告" in commands.help_text(off))
    check("粘手输入 #热搜北京 命中热搜",
          isinstance(commands.resolve_reply("热搜北京", on, ev), str))
    check("P0 指令也过同一条闸门",
          "广东广州" in commands.resolve_reply("预报", on, ev, "广州"))
    check("未绑定群友不被绑定检查拦住",
          isinstance(commands.resolve_reply("一言", on, {"user_id": "999",
                                                         "group_id": "1"}), str))


def test_fetch_envelope():
    print("[信封契约]")
    data = sixty._get("/v2/60s", _cfg())
    check("code=200 返回 data", isinstance(data, dict) and data["date"] == "2026-10-23")
    sixty._CACHE.clear()  # 前面用例的缓存会掩盖故障注入，必须清
    # 业务错误：HTTP 200 + code!=200（上游实测如此，城市找不到就是 200+404）
    Fake60s.mode["/v2/60s"] = {"code": 500, "message": "boom", "data": None}
    try:
        sixty._get("/v2/60s", _cfg())
        check("信封 code=500 → SixtyError", False)
    except sixty.SixtyError as exc:
        check("信封 code=500 → SixtyError", "code=500" in str(exc), str(exc))
    Fake60s.mode["/v2/60s"] = "garbage"
    try:
        sixty._get("/v2/60s", _cfg())
        check("非 JSON → SixtyError", False)
    except sixty.SixtyError as exc:
        check("非 JSON → 解码异常文案（不冒充断网）",
              "无法解析" in str(exc) and "断网" not in str(exc), str(exc))
    Fake60s.mode["/v2/60s"] = None
    # 传输层错误：真 HTTP 500 状态码，urllib 抛 HTTPError，必须吞成一句人话
    Fake60s.mode["/v2/hitokoto"] = 500
    try:
        sixty._get("/v2/hitokoto", _cfg())
        check("HTTP 500 → SixtyError", False)
    except sixty.SixtyError as exc:
        check("HTTP 500 → 降级文案", "暂不可用" in str(exc), str(exc))
    Fake60s.mode["/v2/hitokoto"] = None
    Fake60s.mode["/v2/weather/realtime"] = CITY_404
    try:
        sixty._get("/v2/weather/realtime", _cfg(), {"query": "火星血"})
        check("城市 404 → SixtyError", False)
    except sixty.SixtyError as exc:
        check("城市 404 → 友好文案（不带市字提示）",
              "查不到这个城市的天气" in str(exc), str(exc))
    Fake60s.mode["/v2/weather/realtime"] = None


def test_weather_query_param():
    print("[天气参数：query 不是 city]")
    Fake60s.hits.clear()
    sixty.HANDLERS["天气"][1]("广州", _ctx())
    check("用 query 参数", Fake60s.last_query.get("query") == ["广州"],
          str(Fake60s.last_query))
    check("没有传 city 参数", "city" not in Fake60s.last_query)
    check("打到 realtime 端点", Fake60s.hits.get("/v2/weather/realtime") == 1)
    out = sixty.HANDLERS["天气"][1]("上海市", _ctx())
    check("带市后缀也被归一化救回", "广东广州" in out, out)


def test_cache():
    print("[TTL 缓存]")
    cfg = _cfg()
    Fake60s.hits.clear()
    sixty._CACHE.clear()
    sixty._get("/v2/60s", cfg)
    sixty._get("/v2/60s", cfg)
    check("TTL 内第二次不发请求", Fake60s.hits.get("/v2/60s") == 1)
    key = next(k for k in sixty._CACHE if "|/v2/60s|" in k)
    expire, payload = sixty._CACHE[key]
    sixty._CACHE[key] = (time.monotonic() - 1, payload)
    sixty._get("/v2/60s", cfg)
    check("过期后重发", Fake60s.hits.get("/v2/60s") == 2)
    Fake60s.hits.clear()
    Fake60s.mode["/v2/60s"] = 500
    try:
        sixty._get("/v2/60s", cfg)
    except sixty.SixtyError:
        pass
    Fake60s.mode["/v2/60s"] = None
    data = sixty._get("/v2/60s", cfg)
    check("失败结果不进缓存", isinstance(data, dict) and data.get("date") == "2026-10-23")


def test_cache_eviction():
    print("[缓存淘汰]")
    sixty._CACHE.clear()
    for i in range(sixty.CACHE_MAX + 7):
        sixty._CACHE[f"expired|{i}"] = (time.monotonic() - 1, None)
    sixty._get("/v2/60s", _cfg())
    check("写入时清掉过期键", len(sixty._CACHE) == 1, str(len(sixty._CACHE)))
    sixty._CACHE.clear()
    now = time.monotonic()
    for i in range(sixty.CACHE_MAX):
        sixty._CACHE[f"live|{i}"] = (now + 100 + i, None)
    sixty._get("/v2/60s", _cfg())
    check("满容量时仍受上限约束", len(sixty._CACHE) == sixty.CACHE_MAX,
          str(len(sixty._CACHE)))
    check("淘汰的是最早到期的",
          "live|0" not in sixty._CACHE and "live|1" in sixty._CACHE)
    sixty._CACHE.clear()


def test_p0_forecast():
    print("[#预报]")
    sixty._CACHE.clear()
    Fake60s.hits.clear()
    out = sixty.HANDLERS["预报"][1]("广州", _ctx())
    check("标题带定位", "广东广州" in out, out)
    check("上游昨天的行被过滤", _day(-1)[5:] not in out, out)
    check("首行是今天", _day(0)[5:] in out, out)
    check("默认 5 天（标题 + 5 行）", out.count("\n") == 5, out)
    check("带星期与温度区间", "周" in out and "20~30℃" in out, out)
    check("用 query 参数", Fake60s.last_query.get("query") == ["广州"],
          str(Fake60s.last_query))
    check("打 forecast 端点", Fake60s.hits.get("/v2/weather/forecast") == 1)
    seven = sixty.HANDLERS["预报"][1]("广州", _ctx(sixty=_cfg(max_items=7)))
    check("max_items 调大后 7 天", seven.count("\n") == 7, seven)
    check("无城市无兜底给提示",
          "带上城市名" in sixty.HANDLERS["预报"][1]("", _ctx()))
    check("带市后缀也能归一化",
          "广东广州" in sixty.HANDLERS["预报"][1]("广州市", _ctx()))


def test_p0_history():
    print("[#历史]")
    sixty._CACHE.clear()
    Fake60s.hits.clear()
    out = sixty.HANDLERS["历史"][1]("", _ctx())
    check("标题带日期", "历史上的今天" in out and "10-23" in out, out)
    check("默认 5 条（标题 + 5 行）", out.count("\n") == 5, out)
    check("带年份与链接", "1901 事件1" in out and "baike.baidu.com/item/1" in out, out)
    check("不透传长描述", "长描述" not in out, out)
    sixty.HANDLERS["历史"][1]("", _ctx())
    check("同日第二次走缓存", Fake60s.hits.get("/v2/today-in-history") == 1,
          str(Fake60s.hits))


def test_p0_moyu():
    print("[#摸鱼]")
    sixty._CACHE.clear()
    out = sixty.HANDLERS["摸鱼"][1]("", _ctx())
    check("公历 + 星期 + 农历",
          "2026-10-23 星期五" in out and "农历九月十四" in out, out)
    check("状态行带节气", "工作日" in out and "霜降" in out, out)
    check("三档进度", "本周 71%（5/7）" in out and "本年 81%（296/365）" in out, out)
    check("倒计时", "距周末 1 天" in out and "距年底 69 天" in out, out)
    check("无假期时退到周末", "下个周末 星期六" in out, out)
    check("格言", "能摸鱼绝不工作" in out, out)


def test_p0_luck():
    print("[#运势]")
    sixty._CACHE.clear()
    Fake60s.hits.clear()
    out = sixty.HANDLERS["运势"][1]("", _ctx())
    check("出签", "今日运势" in out and "末吉" in out and "投资" in out, out)
    check("请求带按日期定住的 id", "id" in Fake60s.last_query,
          str(Fake60s.last_query))
    again = sixty.HANDLERS["运势"][1]("", _ctx())
    check("同日结果一致", out == again, f"{out!r} != {again!r}")
    check("同日只请求一次", Fake60s.hits.get("/v2/luck") == 1, str(Fake60s.hits))
    # 上游 luck.json 若收缩条目，带 id 的请求会 404：必须退化成随机签而不是报错
    sixty._CACHE.clear()
    Fake60s.hits.clear()
    Fake60s.mode["/v2/luck"] = "luck-id-404"
    fallback = sixty.HANDLERS["运势"][1]("", _ctx())
    check("id 404 时退化成随机签", "末吉" in fallback, fallback)
    check("回退确实重发了一次", Fake60s.hits.get("/v2/luck") == 2, str(Fake60s.hits))
    Fake60s.mode["/v2/luck"] = None


def test_p1_hot_sources():
    print("[#热搜 多源]")
    sixty._CACHE.clear()
    cases = [
        ("", "【微博热搜】", "话题1", "/v2/weibo"),
        ("微博", "【微博热搜】", "话题1", "/v2/weibo"),
        ("知乎", "【知乎热搜】", "知乎问题1", "/v2/zhihu"),
        ("zhihu", "【知乎热搜】", "知乎问题1", "/v2/zhihu"),
        ("百度", "【百度热搜】", "百度热榜1", "/v2/baidu/hot"),
        ("B站", "【B站热搜】", "B站热搜1", "/v2/bili"),
        ("b站", "【B站热搜】", "B站热搜1", "/v2/bili"),
        ("头条", "【头条热搜】", "头条热榜1", "/v2/toutiao"),
        ("小红书", "【小红书热搜】", "小红书热点1", "/v2/rednote"),
        ("抖音", "【抖音热搜】", "抖音热榜1", "/v2/douyin"),
    ]
    for arg, label, first, path in cases:
        sixty._CACHE.clear()  # 别名用例会命中上一条的缓存，命中计数就没了
        Fake60s.hits.clear()
        out = sixty.HANDLERS["热搜"][1](arg, _ctx())
        check(f"{arg or '默认'} → {label}",
              label in out and first in out, out)
        check(f"{arg or '默认'} 打到 {path}", Fake60s.hits.get(path) == 1,
              str(Fake60s.hits))
    sixty._CACHE.clear()
    out = sixty.HANDLERS["热搜"][1]("", _ctx())
    check("默认 5 条", out.count("\n") == 5, out)
    check("微博热度带括号", "（99）" in out, out)
    check("知乎热度原样透传（已带单位）", "（99 万热度）" in sixty.HANDLERS["热搜"][1](
        "知乎", _ctx()), out)
    check("百度热度透传 score_desc", "（89.0w）" in sixty.HANDLERS["热搜"][1](
        "百度", _ctx()), out)
    check("头条大数折成万", "（100万）" in sixty.HANDLERS["热搜"][1](
        "头条", _ctx()), out)
    check("B站没有热度字段就不硬拼括号", "（" not in sixty.HANDLERS["热搜"][1](
        "B站", _ctx()).split("\n", 1)[1])
    check("不透传 detail/desc", "长摘要" not in sixty.HANDLERS["热搜"][1](
        "知乎", _ctx()) and "长描述" not in sixty.HANDLERS["热搜"][1]("百度", _ctx()))
    unknown = sixty.HANDLERS["热搜"][1]("贴吧", _ctx())
    check("未知源给可选列表", "热搜源支持" in unknown and "知乎" in unknown, unknown)


def test_p1_market():
    print("[#行情]")
    sixty._CACHE.clear()
    Fake60s.hits.clear()
    out = sixty.HANDLERS["行情"][1]("", _ctx())
    check("默认是汇率", "人民币汇率" in out, out)
    check("美元换算", "1 美元(USD) ≈ 6.67 元" in out, out)
    check("日元按 100 报", "100 日元(JPY) ≈ 4.00 元" in out, out)
    check("带更新时间", "更新：2026/10/23 08:02:31" in out, out)
    check("打 exchange-rate", Fake60s.hits.get("/v2/exchange-rate") == 1)
    check("外汇别名", "人民币汇率" in sixty.HANDLERS["行情"][1]("外汇", _ctx()))
    out = sixty.HANDLERS["行情"][1]("油价 广州", _ctx())
    check("油价区名用上游回填", "今日油价 广东广州" in out, out)
    check("四档油价", "92#汽油：8.63 元/升" in out and "0#柴油：8.31 元/升" in out, out)
    check("带调价预告", "搁浅" in out, out)
    check("region 参数（归一化后）", Fake60s.last_query.get("region") == ["广州"],
          str(Fake60s.last_query))
    out = sixty.HANDLERS["行情"][1]("金价", _ctx())
    check("金价标题带日期", "今日金价 2026-10-23" in out, out)
    check("常用金属报价", "今日金价：861.78 元/克" in out
          and "伦敦金(现货黄金)：4011.64 美元/盎司" in out, out)
    check("不搬门店/银行/回收", "周大福" not in out, out)
    check("黄金别名", "今日金价" in sixty.HANDLERS["行情"][1]("黄金", _ctx()))
    unknown = sixty.HANDLERS["行情"][1]("楼盘", _ctx())
    check("未知类别给可选", "行情支持" in unknown and "油价" in unknown, unknown)
    # 区域不支持：上游 HTTP 200 + code=400，不能当成「稍后再试」的抖动
    sixty._CACHE.clear()
    Fake60s.mode["/v2/fuel-price"] = FUEL_UNSUPPORTED
    out = sixty.HANDLERS["行情"][1]("油价 火星", _ctx())
    check("区域不支持提示换城市", "暂不支持「火星」" in out, out)
    Fake60s.mode["/v2/fuel-price"] = None


def test_p1_douban():
    print("[#豆瓣]")
    sixty._CACHE.clear()
    Fake60s.hits.clear()
    out = sixty.HANDLERS["豆瓣"][1]("", _ctx())
    check("标题", "豆瓣一周口碑电影榜" in out, out)
    check("默认 5 条（标题 + 5 行）", out.count("\n") == 5, out)
    check("评分 + 链接", "（7.9分）" in out and "subject/1" in out, out)
    check("不透传长简介", "长简介" not in out and "card_subtitle" not in out, out)
    check("打 movie 榜", Fake60s.hits.get("/v2/douban/weekly/movie") == 1)


def test_p1_itnews():
    print("[#科技]")
    sixty._CACHE.clear()
    Fake60s.hits.clear()
    out = sixty.HANDLERS["科技"][1]("", _ctx())
    check("标题", "IT 之家日榜" in out, out)
    check("默认 5 条（标题 + 5 行）", out.count("\n") == 5, out)
    check("带链接", "ithome.com/1/016/1.htm" in out, out)
    check("type=day", Fake60s.last_query.get("type") == ["day"],
          str(Fake60s.last_query))
    check("打 rank 端点", Fake60s.hits.get("/v2/it-news/rank") == 1)


def test_p2_random_texts():
    print("[#KFC]")
    sixty._CACHE.clear()
    Fake60s.hits.clear()
    out = sixty.HANDLERS["KFC"][1]("", _ctx())
    check("标题", out.startswith("【疯狂星期四】"), out)
    check("文案上屏", "v我50" in out, out)
    check("index 不上屏", "41" not in out, out)
    check("打 kfc 端点", Fake60s.hits.get("/v2/kfc") == 1)
    # 上游返回空文案（v50 文案库拉取失败会回空数组 → index=-1）：给兜底而不是空回复
    Fake60s.mode["/v2/kfc"] = KFC_EMPTY
    sixty._CACHE.clear()
    try:
        out = sixty.HANDLERS["KFC"][1]("", _ctx())
        check("空文案有兜底", "取不到" in out and "【" not in out, out)
    finally:
        Fake60s.mode["/v2/kfc"] = None

    print("[#笑话]")
    sixty._CACHE.clear()
    Fake60s.hits.clear()
    out = sixty.HANDLERS["笑话"][1]("", _ctx())
    check("段子上屏", "老板" in out, out)
    check("不加标题前缀", not out.startswith("【"), out)
    check("打 duanzi 端点", Fake60s.hits.get("/v2/duanzi") == 1)

    print("[#答案]")
    sixty._CACHE.clear()
    Fake60s.hits.clear()
    out = sixty.HANDLERS["答案"][1]("", _ctx())
    check("标题", out.startswith("【答案之书】"), out)
    check("中文答案上屏", "放手去做" in out, out)
    check("英文原文不上屏", "answer_en" not in out and "Go for it" not in out, out)
    check("打 answer 端点", Fake60s.hits.get("/v2/answer") == 1)

    # 随机文案的 TTL 只吸收连刷：30s/60s 内不再打上游，过期后重发
    sixty._CACHE.clear()
    Fake60s.hits.clear()
    sixty.HANDLERS["笑话"][1]("", _ctx())
    sixty.HANDLERS["笑话"][1]("", _ctx())
    check("30s 内连刷只发一次", Fake60s.hits.get("/v2/duanzi") == 1)
    check("TTL 是短档", sixty.TTL["/v2/duanzi"] == 30.0
          and sixty.TTL["/v2/kfc"] == 60.0 and sixty.TTL["/v2/answer"] == 30.0)


def test_network_down():
    print("[网络层降级]")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead = probe.getsockname()[1]
    probe.close()
    GUARD_PORTS.add(dead)  # 放行这个端口，让连接真的被拒绝而不是被护栏拦
    try:
        sixty.fetch("/v2/60s", base=f"http://127.0.0.1:{dead}")
        check("连不上应抛 SixtyError", False)
    except sixty.SixtyError as exc:
        check("连不上 → 一句人话", "暂不可用" in str(exc), str(exc))


def test_member_city():
    print("[群友位置记忆]")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from water_group.memory_engine import get_memory_engine
    engine = get_memory_engine()  # 指向进程启动时钉的临时库
    engine.save_profile("100000001", "群友A", traits={"location": "广州（粤A）"})
    engine.save_profile("100000002", "群友B", traits={"location": "惠州（粤L）"})
    check("位置记忆归一化后可用", sixty._member_city("100000001") == "广州")
    check("另一个群友", sixty._member_city("100000002") == "惠州")
    check("没有画像 → 空", sixty._member_city("1") == "")
    check("合成事件 user_id=0 → 空", sixty._member_city("0") == "")
    db = Path(os.environ["IDLEJOT_WG_MEMORY_DB"])
    backup = db.read_bytes()
    db.write_bytes(b"this is definitely not a sqlite database")
    check("记忆库损坏 → 降级空串（不炸）", sixty._member_city("100000001") == "")
    db.write_bytes(backup)
    check("恢复后又能读到", sixty._member_city("100000001") == "广州")


def test_weather_fallback_chain():
    print("[天气三级回退]")
    check("无参 + 有位置 → 用位置",
          "广东广州" in sixty.HANDLERS["天气"][1](
              "", _ctx(user_id="100000001")))
    check("无参 + 无位置 + 有 default_city → 用配置",
          "广东广州" in sixty.HANDLERS["天气"][1](
              "", _ctx(user_id="1", sixty=_cfg(default_city="广州"))))
    check("全落空 → 提示带参数",
          "带上城市名" in sixty.HANDLERS["天气"][1](
              "", _ctx(user_id="1", sixty={"base_url": FIXTURE_BASE})))
    # 带参时以参数为准：fixture 不管 query 是什么都回广州，所以断言请求本身
    Fake60s.hits.clear()
    sixty.HANDLERS["天气"][1]("深圳", _ctx(user_id="100000001"))
    check("带参优先于位置记忆（请求里是 query=深圳）",
          Fake60s.last_query.get("query") == ["深圳"], str(Fake60s.last_query))


def test_formatting():
    print("[输出格式]")
    out = sixty.HANDLERS["简报"][1]("", _ctx())
    check("简报带日期", "2026-10-23" in out)
    check("简报默认 5 条（head+5+tail 共 7 行）", out.count("\n") == 6, out)
    check("简报带农历和 tip", "丙午年九月十四" in out and "寒露渐凉" in out)
    out15 = sixty.HANDLERS["简报"][1]("", _ctx(sixty=_cfg(max_items=15)))
    check("max_items 生效（15 条）", out15.count("新闻") == 15)
    hot = sixty.HANDLERS["热搜"][1]("", _ctx())
    check("热搜默认 5 条带链接", hot.count("https://") == 5)
    check("热搜跳过无标题行", "话题9" not in hot)
    check("一言透传", sixty.HANDLERS["一言"][1]("", _ctx()) == "测试一言")
    check("clip 截断带标记", sixty._clip("x" * (sixty.REPLY_CLIP + 100))
          .endswith("…（已截断）"))


def test_empty_and_error_surfacing():
    print("[空数据与错误上浮]")
    Fake60s.mode["/v2/60s"] = BRIEF_EMPTY
    sixty._CACHE.clear()
    check("简报空列表有兜底文案", "空的" in sixty.HANDLERS["简报"][1]("", _ctx()))
    Fake60s.mode["/v2/60s"] = None
    Fake60s.mode["/v2/weather/realtime"] = CITY_404
    settings = commands.settings_from({"slash_commands": {"sixty": {
        "enabled": True, "base_url": FIXTURE_BASE}}})
    ev = {"user_id": "1", "group_id": "1"}
    check("run_handler 把 SixtyError 翻成回复",
          commands.resolve_reply("天气", settings, ev, "火星血")
          == "查不到这个城市的天气，换个写法试试（不用带「市」字）")
    check("错误回复不带堆栈", "Traceback" not in str(
        commands.resolve_reply("天气", settings, ev, "火星血")))
    Fake60s.mode["/v2/weather/realtime"] = None


def main():
    # FIXTURE_BASE 在 _fixture 里是模块级变量；`main()` 拿到夹具端口后改写它。
    # `_cfg()` 定义在 _fixture 里、读的是 _fixture 自己模块的 FIXTURE_BASE；而本主
    # 模块用例里直接引用的 `FIXTURE_BASE`（test_gate_end_to_end /
    # test_weather_fallback_chain / test_empty_and_error_surfacing 等）读的是
    # `__init__` 的全局。旧单文件 `global FIXTURE_BASE` 一处改写两边都见，因为
    # _cfg 与用例同在一个模块；拆包后两边各属一模块，故 `global` 改写本模块的
    # 同时必须同步 `_fixture.FIXTURE_BASE`，_cfg 才读到真值。这条线即旧文件的
    # `global FIXTURE_BASE`。
    global FIXTURE_BASE
    from . import _fixture
    Fake60s.hits.clear()
    Fake60s.mode.clear()
    sixty._CACHE.clear()
    with temp_dir() as tmp:
        real_cmd_log = commands.LOG
        real_sixty_log = sixty.LOG
        commands.LOG = tmp / "wg_commands.log"
        sixty.LOG = tmp / "wg_sixty.log"
        with fixture_server(Fake60s, ports=GUARD_PORTS) as server:
            FIXTURE_BASE = f"http://127.0.0.1:{server.server_port}"
            _fixture.FIXTURE_BASE = FIXTURE_BASE
            restrict, _calls = network_guard(GUARD_PORTS)
            try:
                with patch("socket.create_connection", restrict):
                    for fn in (test_normalize_city, test_allowed_gate,
                               test_registry_and_merge,
                               test_plugin_counts_matches_probe,
                               test_settings_from,
                               test_gate_end_to_end, test_fetch_envelope,
                               test_weather_query_param, test_cache,
                               test_cache_eviction, test_p0_forecast,
                               test_p0_history, test_p0_moyu, test_p0_luck,
                               test_p1_hot_sources, test_p1_market,
                               test_p1_douban, test_p1_itnews,
                               test_p2_random_texts,
                               test_network_down, test_member_city,
                               test_weather_fallback_chain, test_formatting,
                               test_empty_and_error_surfacing):
                        fn()
            finally:
                commands.LOG = real_cmd_log
                sixty.LOG = real_sixty_log
    if _runner.summary():
        sys.exit(1)
