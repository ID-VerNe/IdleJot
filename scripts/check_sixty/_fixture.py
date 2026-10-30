"""check_sixty 包的夹具层（issue #286）。

从旧 `scripts/check_sixty.py` 整体搬运，按 #282（`scripts/check_qr_login/`）、
#247（`scripts/check_dossier_cadence/`）与 #169 PF-2（`scripts/check_listener_rehang/`）
的先例拆出。本模块只放 60s 替身、route 表、`_cfg` / `_ctx` / `_day` helper 与模块级
状态（`FIXTURE_BASE` / `GUARD_PORTS` / `_runner` / `check`）。26 条 `test_*` 用例与
`main()` 留主模块 `__init__.py`。

R4（issue #280）合流：本模块的 `check()` runner 委派 `assert_runner(markers="tick",
indent="  ")`（✓/✗ 标记变体，复刻原 `  ✓ ` / `  ✗ `），与 #169 PE-1 的 `run_cases()`
（`(name, fn)` case 列表族）形态不同、互不替。`__init__.py` 的 `main()` 用
`_runner.summary()` 出尾行。

与 `check_qr_login/_fixture.py` 同口径：包式门禁经 `python -m scripts.check_sixty`
调起，此时 `sys.path[0]` 是仓库根（cwd）而非 `scripts/`——后者才是
`check_fixture_support` 所在地。无法先调 `bootstrap_paths`（它自己要 import fs），故
手动把 `scripts/` 插进 `sys.path`，再进口 `check_fixture_support`，随后
`bootstrap_paths()` 收口 root + daemon。
"""
import datetime
import os
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 包式门禁经 `python -m scripts.check_sixty` 调起，此时 `sys.path[0]` 是仓库根（cwd）
# 而非 `scripts/`——后者才是 `check_fixture_support` 所在地。无法先调
# `bootstrap_paths`（它自己要 import fs），故手动把 `scripts/` 插进 `sys.path`，
# 再进口 `check_fixture_support`，随后 `bootstrap_paths()` 收口 root + daemon。
_scripts = Path(__file__).resolve().parents[1]
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

from check_fixture_support import assert_runner, network_guard, scratch_dir, temp_dir  # noqa: E402
from fixture_server import FixtureHandler, fixture_server  # noqa: E402

# 记忆库与日志必须在 import commands/sixty **之前**钉到临时目录：wg_paths 与
# memory_engine 的路径都是 import 期求值的，事后 patch 模块常量不管用。
_SCRATCH = scratch_dir(prefix="tulpa-sixty-")
os.environ["WATER_GROUP_HOME"] = str(_SCRATCH / "home")
os.environ["IDLEJOT_WG_MEMORY_DB"] = str(_SCRATCH / "mem.sqlite3")
(_SCRATCH / "home").mkdir(parents=True, exist_ok=True)

import commands  # noqa: E402
import sixty  # noqa: E402

# 即时断言 runner（issue #280 合流）：tick 标记 + 两格缩进复刻原 `  ✓ ` / `  ✗ `。
_runner = assert_runner(markers="tick", indent="  ")
check = _runner.check
GUARD_PORTS = set()


# ---------------------------------------------------------------- fixture

BRIEF = {"code": 200, "message": "ok", "data": {
    "date": "2026-10-23", "day_of_week": "星期五",
    "lunar_date": "丙午年九月十四", "tip": "寒露渐凉，静心蓄力",
    "news": [f"新闻{i}" for i in range(1, 16)]}}
BRIEF_EMPTY = {"code": 200, "message": "ok", "data": {"date": "2026-10-23", "news": []}}
WEIBO = {"code": 200, "message": "ok", "data": [
    {"title": f"话题{i}", "hot_value": 100 - i,
     "link": f"https://s.weibo.com/weibo?q=topic{i}"} for i in range(1, 9)]
    + [{"title": "", "hot_value": 1, "link": "https://empty"}, "not-a-dict"]}
WEATHER = {"code": 200, "message": "ok", "data": {
    "location": {"name": "广东广州", "province": "广东省", "city": "广州市"},
    "weather": {"condition": "多云", "temperature": 25, "humidity": 62,
                "wind_direction": "东北风", "wind_power": "4-5"},
    "air_quality": {"aqi": 24, "quality": "优"}}}
HITOKOTO = {"code": 200, "message": "ok", "data": {"hitokoto": "测试一言"}}
CITY_404 = {"code": 404, "message": "ok",
            "data": {"error": "未找到城市: 火星血。请检查城市名称拼写是否正确"}}


def _day(offset):
    return (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()


# 上游第 0 条是昨天（2026-10-23 实测）：过滤逻辑必须靠 fixture 里的昨天那条钉住。
FORECAST = {"code": 200, "message": "ok", "data": {
    "location": {"name": "广东广州", "province": "广东省", "city": "广州市"},
    "hourly_forecast": [],
    "daily_forecast": [
        {"date": _day(-1), "day_condition": "多云", "night_condition": "晴",
         "max_temperature": 29, "min_temperature": 21},
        *[{"date": _day(i), "day_condition": "晴", "night_condition": "晴",
           "max_temperature": 30 + i, "min_temperature": 20 + i} for i in range(7)],
    ],
    "sunrise_sunset": {"sunrise": "06:24", "sunset": "17:55"}}}
HISTORY = {"code": 200, "message": "ok", "data": {
    "date": "10-23", "month": 10, "day": 23,
    "items": [{"title": f"事件{i}", "year": str(1900 + i), "description": "长描述",
               "event_type": "event", "link": f"https://baike.baidu.com/item/{i}"}
              for i in range(1, 13)]}}
MOYU = {"code": 200, "message": "ok", "data": {
    "date": {"gregorian": "2026-10-23", "weekday": "星期五", "dayOfWeek": 5,
             "lunar": {"yearCN": "二零二六", "monthCN": "九月", "dayCN": "十四",
                       "zodiac": "马", "yearGanZhi": "丙午"}},
    "today": {"isWeekend": False, "isHoliday": False, "isWorkday": True,
              "holidayName": None, "solarTerm": "霜降", "lunarFestivals": []},
    "progress": {"week": {"passed": 5, "total": 7, "percentage": 71},
                 "month": {"passed": 23, "total": 31, "percentage": 74},
                 "year": {"passed": 296, "total": 365, "percentage": 81}},
    "currentHoliday": None, "nextHoliday": None,
    "nextWeekend": {"date": "2026-10-24", "weekday": "星期六", "daysUntil": 1},
    "countdown": {"toWeekEnd": 1, "toFriday": 0, "toMonthEnd": 8, "toYearEnd": 69},
    "moyuQuote": "能摸鱼绝不工作。"}}
LUCK = {"code": 200, "message": "ok", "data": {
    "luck_desc": "末吉", "luck_rank": 5, "luck_tip": "条件不好也是对将来的投资",
    "luck_tip_index": 22}}
ZHIHU = {"code": 200, "message": "ok", "data": [
    {"title": f"知乎问题{i}", "hot_value_desc": f"{100 - i} 万热度",
     "detail": "长摘要", "link": f"https://www.zhihu.com/question/{i}"}
    for i in range(1, 7)]}
BAIDU = {"code": 200, "message": "ok", "data": [
    {"rank": i, "title": f"百度热榜{i}", "score_desc": f"{90 - i}.0w",
     "desc": "长描述", "url": f"https://www.baidu.com/s?wd={i}"}
    for i in range(1, 7)]}
BILI = {"code": 200, "message": "ok", "data": [
    {"title": f"B站热搜{i}", "link": f"https://search.bilibili.com/all?keyword={i}"}
    for i in range(1, 7)]}
TOUTIAO = {"code": 200, "message": "ok", "data": [
    {"title": f"头条热榜{i}", "hot_value": 1000000 + i,
     "link": f"https://www.toutiao.com/trending/{i}"} for i in range(1, 7)]}
REDNOTE = {"code": 200, "message": "ok", "data": [
    {"rank": i, "title": f"小红书热点{i}", "score": f"{90 - i}.0w",
     "link": f"https://www.xiaohongshu.com/search_result?keyword={i}"}
    for i in range(1, 7)]}
DOUYIN = {"code": 200, "message": "ok", "data": [
    {"title": f"抖音热榜{i}", "hot_value": 12000000 + i,
     "link": f"https://www.douyin.com/search/{i}"} for i in range(1, 7)]}
EX_RATE = {"code": 200, "message": "ok", "data": {
    "base_code": "CNY", "updated": "2026/10/23 08:02:31",
    "rates": [{"currency": "CNY", "rate": 1}, {"currency": "USD", "rate": 0.15},
              {"currency": "EUR", "rate": 0.125}, {"currency": "JPY", "rate": 25.0},
              {"currency": "HKD", "rate": 1.1}, {"currency": "GBP", "rate": 0.1},
              {"currency": "AUD", "rate": 0.2}, {"currency": "KRW", "rate": 200.0}]}}
GOLD = {"code": 200, "message": "ok", "data": {
    "date": "2026-10-23",
    "metals": [
        {"name": "今日金价", "sell_price": "861.78", "today_price": "864.78",
         "unit": "元/克"},
        {"name": "黄金价格", "sell_price": "861.78", "today_price": "864.78",
         "unit": "元/克"},
        {"name": "黄金_9999", "sell_price": "863.16", "today_price": "864",
         "unit": "元/克"},
        {"name": "黄金_T+D", "sell_price": "863.3", "today_price": "863.8",
         "unit": "元/克"},
        {"name": "伦敦金(现货黄金)", "sell_price": "4011.64",
         "today_price": "4011.99", "unit": "美元/盎司"},
        {"name": "纽约黄金(美国)", "sell_price": "1", "today_price": "1",
         "unit": "美元/盎司"},
        {"name": "白银价格", "sell_price": "10.5", "today_price": "10.5", "unit": "元/克"},
        {"name": "铂金价格", "sell_price": "300", "today_price": "300", "unit": "元/克"},
        {"name": "钯金价格", "sell_price": "300", "today_price": "300", "unit": "元/克"}],
    "stores": [{"brand": "周大福", "product": "足金", "price": "1100", "unit": "元/克"}],
    "banks": [], "recycle": []}}
FUEL = {"code": 200, "message": "ok", "data": {
    "region": "广东广州",
    "trend": {"description": "下次调价时间: 10月15日24时，预计搁浅（不调整）"},
    "items": [{"name": "92#汽油", "price": 8.63, "price_desc": "8.63 元/升"},
              {"name": "95#汽油", "price": 9.35, "price_desc": "9.35 元/升"},
              {"name": "98#汽油", "price": 11.35, "price_desc": "11.35 元/升"},
              {"name": "0#柴油", "price": 8.31, "price_desc": "8.31 元/升"}],
    "link": "http://www.qiyoujiage.com/guangdong/guangzhou.shtml",
    "updated": "2026/10/23 16:12:33"}}
# 不认识的地区：上游是 HTTP 200 + code=400（信封里的业务错，不是真 404）
FUEL_UNSUPPORTED = {"code": 400, "message": "暂不支持 火星 区域查询", "data": None}
DOUBAN = {"code": 200, "message": "ok", "data": [
    {"rank": i, "title": f"电影{i}", "rating": 8.0 - i / 10, "rating_count": 1000 * i,
     "url": f"https://movie.douban.com/subject/{i}/",
     "card_subtitle": "2026 / 美国 / 剧情", "description": "长简介"}
    for i in range(1, 11)]}
ITNEWS = {"code": 200, "message": "ok", "data": [
    {"title": f"科技新闻{i}", "link": f"https://www.ithome.com/1/016/{i}.htm"}
    for i in range(1, 13)]}
# 随机文案三条：上游把正文放在单独字段里，另外还带 index / answer_en 之类的附加字段
KFC = {"code": 200, "message": "ok", "data": {
    "index": 41, "kfc": "今天肯德基疯狂星期四，v我50吃炸鸡"}}
KFC_EMPTY = {"code": 200, "message": "ok", "data": {"index": -1, "kfc": ""}}
DUANZI = {"code": 200, "message": "ok", "data": {
    "index": 7, "duanzi": "我问老板能不能涨工资，他说他也在等答案"}}
ANSWER = {"code": 200, "message": "ok", "data": {
    "id": 3, "answer": "放手去做", "answer_en": "Go for it", "index": 3}}
ROUTES = {"/v2/60s": BRIEF, "/v2/weibo": WEIBO, "/v2/zhihu": ZHIHU,
          "/v2/baidu/hot": BAIDU, "/v2/bili": BILI, "/v2/toutiao": TOUTIAO,
          "/v2/rednote": REDNOTE, "/v2/douyin": DOUYIN,
          "/v2/weather/realtime": WEATHER, "/v2/weather/forecast": FORECAST,
          "/v2/today-in-history": HISTORY, "/v2/moyu": MOYU, "/v2/luck": LUCK,
          "/v2/exchange-rate": EX_RATE, "/v2/fuel-price": FUEL,
          "/v2/gold-price": GOLD, "/v2/douban/weekly/movie": DOUBAN,
          "/v2/it-news/rank": ITNEWS,
          "/v2/kfc": KFC, "/v2/duanzi": DUANZI, "/v2/answer": ANSWER,
          "/v2/hitokoto": HITOKOTO}


class Fake60s(FixtureHandler):
    """60s 的替身：路由表 + 命中计数 + 最后一次 query + 故障注入。

    mode 里放整数 = 注入该 HTTP 状态码的错误信封；放 "garbage" = 返回非 JSON。
    """

    hits = {}
    last_query = {}
    mode = {}

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        type(self).hits[path] = type(self).hits.get(path, 0) + 1
        type(self).last_query = query
        injected = type(self).mode.get(path)
        if injected == "garbage":
            # HTTP 200 + 非 JSON：反代「成功」回一张 HTML 错误页才是解码分支的真实场景。
            # 5xx 的 HTML 在 urllib 层就抛 HTTPError，算网络异常，另有用例覆盖。
            body = b"<html>502 Bad Gateway</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if isinstance(injected, int):
            self.send_json({"code": injected, "message": "boom", "data": None},
                           status=injected)
            return
        if injected == "luck-id-404":
            # 只对带 id 的请求回 404：用来钉住「id 越界时退化成随机签」这条回退。
            if path == "/v2/luck" and "id" in query:
                self.send_json({"code": 404, "message": "not found",
                                "data": {"error": "未找到ID为 99 的运势"}})
            else:
                self.send_json(ROUTES[path])
            return
        if isinstance(injected, dict):  # 整封信封替换（如城市 404）
            self.send_json(injected)
            return
        self.send_json(ROUTES.get(path, {"code": 404, "message": "no route",
                                         "data": None}))


def _cfg(**extra):
    cfg = {"base_url": FIXTURE_BASE, "timeout": 6, "max_items": 5}
    cfg.update(extra)
    return cfg


def _ctx(**extra):
    ctx = {"group_id": "1", "user_id": "1", "sixty": _cfg()}
    ctx.update(extra)
    return ctx


FIXTURE_BASE = ""
