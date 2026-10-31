"""钉住米游社取数 handler：DS 签名、URL 参数、缓存、错误码、格式化。

全部走 fixture_server + network_guard，不发真实请求；HOSTS 整表指到 127.0.0.1。
报告（.plan/genshin-miyoushe-api-report.md）里记名的坑在这里都是回归用例：
month 必须 YYYYMM、关键词必须 URL 编码、失败结果不进缓存。

    .venv/Scripts/python.exe scripts/check_miyoushe.py
"""
import hashlib
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

from fixture_server import FixtureHandler, fixture_server  # noqa: E402
from check_fixture_support import assert_runner, network_guard, temp_dir  # noqa: E402

import miyoushe  # noqa: E402
import commands  # noqa: E402

# 即时断言 runner（issue #280 合流）：check() 打印 + 累积，summary() 出尾行。
_runner = assert_runner(markers="ok")
check = _runner.check
CK_CTX = {"ck": ["ltuid=1; ltoken=2; cookie_token=3"], "uid": "100000000",
          "group_id": "1", "user_id": "2"}
YEAR = time.strftime("%Y")

NEWS = {"retcode": 0, "data": {"list": [
    {"post": {"post_id": "1", "subject": "公告一", "created_at": 1729500000}},
    {"post": {"post_id": "2", "subject": "公告二", "created_at": 1729400000}},
    {"post": {"post_id": "3", "subject": "公告三", "created_at": 1729300000}},
]}}
SEARCH = {"retcode": 0, "data": {"posts": [
    {"post": {"post_id": "9", "subject": "胡桃攻略"}},
    {"post": {"post_id": "8", "subject": "胡桃二创"}},
]}}
INSTANT = {"retcode": 0, "data": {"list": [
    {"post": {"post": {"structured_content":
     "{\"link\":\"https://webstatic.mihoyo.com/bbs/event/live/index.html?act_id=123456\"}"}}},
]}}
INSTANT_EMPTY = {"retcode": 0, "data": {"list": []}}
HOME = {"retcode": 0, "data": {"navigator": [
    {"name": "前瞻直播", "app_path": "https://webstatic.mihoyo.com/bbs/event/live/index.html?act_id=777"}]}}
LIVE = {"retcode": 0, "data": {"live": {"title": "4.6 前瞻特别节目", "code_ver": "1.0", "remain": 0}}}
LIVE_WAIT = {"retcode": 0, "data": {"live": {"title": "4.7 前瞻", "code_ver": "1.0", "remain": 3600}}}
CODES = {"retcode": 0, "data": {"code_list": [{"code": "AAA111"}, {"code": "BBB222"}]}}
INDEX = {"retcode": 0, "data": {
    "role": {"nickname": "旅行者", "level": 60},
    "stats": {"active_day_number": 1000, "achievement_number": 900, "avatar_number": 80,
              "full_fetter_avatar_num": 30, "spiral_abyss": "12-3",
              "precious_chest_number": 300, "luxurious_chest_number": 100,
              "exquisite_chest_number": 400, "common_chest_number": 500,
              "magic_chest_number": 10, "anemoculus_number": 66, "geoculus_number": 131},
    "world_explorations": [{"name": "蒙德", "exploration_percentage": 1000},
                           {"name": "璃月", "exploration_percentage": 800}]}}
ABYSS = {"retcode": 0, "data": {"max_floor": "12-3", "total_win_times": 12,
                                "total_battle_times": 12, "floors": [
    {"index": 12, "star": 9, "max_star": 9}, {"index": 11, "star": 9, "max_star": 9},
    {"index": 8, "star": 6, "max_star": 9}]}}
CHARACTERS = {"retcode": 0, "data": {"list": [
    {"name": "神里绫华", "rarity": 5, "level": 90, "actived_constellation_num": 0,
     "weapon": {"name": "雾切之回光", "rarity": 5, "level": 90}},
    {"name": "安柏", "rarity": 4, "level": 70, "actived_constellation_num": 2,
     "weapon": {"name": "西风弓", "rarity": 4, "level": 70}}]}}
NOTE = {"retcode": 0, "data": {
    "current_resin": 120, "max_resin": 200, "resin_recovery_time": "3600",
    "finished_task_num": 4, "total_task_num": 4,
    "expeditions": [{"status": "Finished"}, {"status": "Ongoing"}],
    "current_home_coin": 2000, "max_home_coin": 2400}}
ZZZ = {"retcode": 0, "data": {
    "energy": {"current": 100, "max": 240, "restore": 3600},
    "vitality": {"current": 60, "max": 240, "restore": 7200},
    "bangboo": {"current": 3, "max": 4, "restore": 1800}}}
LEDGER = {"retcode": 0, "data": {"month_data": {
    "current_primogems": 12000, "current_mora": 5000000,
    "last_primogems": 10000, "last_mora": 4000000,
    "primogems_rate": 20, "mora_rate": 25,
    "group_by": [{"action": "每日委托", "num": 1800, "percent": 15},
                 {"action": "活动奖励", "num": 800, "percent": 7}]}}}
GCG_INFO = {"retcode": 0, "data": {"nickname": "旅行者"}}
GCG_DECKS = {"retcode": 0, "data": {"deck_list": [
    {"name": "第一套", "avatar_cards": [1, 2, 3], "action_cards": [1, 2]},
    {"name": "第二套", "avatar_cards": [1], "action_cards": []}]}}
FP = {"retcode": 0, "data": {"device_fp": "fp-fixture-123"}}
DEVICE_LOGIN = {"retcode": 0, "data": {"ok": True}}
DEVICE_SAVE = {"retcode": 0, "data": {"ok": True}}
FP_PATH = "/device-fp/api/getFp"
LOGIN_PATH = "/apihub/api/deviceLogin"
SAVE_PATH = "/apihub/api/saveDevice"
GACHA = {"retcode": 0, "data": {"total": "1000", "list": [
    {"time": "2024-10-01 12:00:00", "name": "神里绫华", "rank_type": "5"},
    {"time": "2024-09-30 08:00:00", "name": "笛剑", "rank_type": "4"}]}}

ROUTES = {}
RECORD = []


class FakeMihoyo(FixtureHandler):
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urllib.parse.urlparse(self.path)
        body = None
        if method == "POST" and int(self.headers.get("Content-Length", "0") or 0):
            body = self.read_json()
        RECORD.append({
            "method": method, "path": parsed.path, "raw_query": parsed.query,
            "query": urllib.parse.parse_qs(parsed.query, keep_blank_values=True),
            "body": body,
            "headers": {k.lower(): v for k, v in self.headers.items()},
        })
        payload = ROUTES.get(parsed.path)
        if payload is None:
            self.send_json({"retcode": 404, "message": "no route"}, status=404)
        else:
            self.send_json(payload)


def reset(routes):
    ROUTES.clear()
    ROUTES.update(routes)
    RECORD.clear()
    miyoushe._CACHE.clear()
    # 指纹/冷却都是跨用例的状态：#55 之后指纹落盘 7 天，不清就会串到下一个用例
    miyoushe._FP_CACHE.clear()
    miyoushe._RISK.clear()
    try:
        miyoushe.DEVICE_FILE.unlink()
    except OSError:
        pass


def hits(path):
    return [r for r in RECORD if r["path"] == path]


def raises(fn, fragment):
    try:
        fn()
    except miyoushe.MiyousheError as exc:
        return fragment in str(exc), str(exc)
    return False, "没有抛 MiyousheError"


INDEX_PATH = "/game_record/app/genshin/api/index"
NOTE_PATH = "/game_record/app/genshin/api/dailyNote"
NEWS_PATH = "/painter/wapi/getNewsList"


# ---------------------------------------------------------------- 签名与请求形状


def test_ds_and_headers():
    reset({INDEX_PATH: INDEX})
    miyoushe.exploration("100000000", CK_CTX)
    rows = hits(INDEX_PATH)
    check("index: 只发一次请求", len(rows) == 1)
    row = rows[0]
    check("index: query 带 role_id 与 server",
          row["query"].get("role_id") == ["100000000"]
          and row["query"].get("server") == ["cn_gf01"])
    ds = row["headers"].get("ds", "")
    t, r, digest = ds.split(",")
    check("index: DS 形状 t,r,md5", len(t) == 10 and len(r) == 6 and len(digest) == 32)
    expect = hashlib.md5(
        f"salt={miyoushe.SALT_CN}&t={t}&r={r}&b=&q={row['raw_query']}".encode()).hexdigest()
    check("index: DS 与请求原文对得上", digest == expect, f"{digest} != {expect}")
    check("index: Cookie/版本号/client_type",
          row["headers"].get("cookie") == CK_CTX["ck"][0]
          and row["headers"].get("x-rpc-app_version") == "2.40.1"
          and row["headers"].get("x-rpc-client_type") == "5")


def test_character_post_body():
    reset({"/game_record/app/genshin/api/character/list": CHARACTERS})
    miyoushe.characters("100000000", CK_CTX)
    row = hits("/game_record/app/genshin/api/character/list")[0]
    check("character: POST 且 body 精确", row["method"] == "POST"
          and row["body"] == {"role_id": "100000000", "server": "cn_gf01"})
    body_text = json.dumps(row["body"], ensure_ascii=False, separators=(",", ":"))
    t, r, digest = row["headers"]["ds"].split(",")
    expect = hashlib.md5(
        f"salt={miyoushe.SALT_CN}&t={t}&r={r}&b={body_text}&q=".encode()).hexdigest()
    check("character: DS 覆盖 body 原文", digest == expect)


# ---------------------------------------------------------------- 免登录指令


def test_news():
    for kind, code in miyoushe.NEWS_TYPES.items():
        reset({NEWS_PATH: NEWS})
        out = miyoushe.news(kind)
        check(f"news[{kind}]: 标题进回复", "公告一" in out and "公告二" in out)
        check(f"news[{kind}]: type 参数", hits(NEWS_PATH)[0]["query"].get("type") == [str(code)])
        check(f"news[{kind}]: 只带 Referer+UA 无 DS",
              hits(NEWS_PATH)[0]["headers"].get("referer") == "https://www.miyoushe.com"
              and "ds" not in hits(NEWS_PATH)[0]["headers"])


def test_search_encoding():
    reset({"/post/wapi/searchPosts": SEARCH})
    out = miyoushe.search_posts("胡桃")
    check("search: 输出含结果", "胡桃攻略" in out)
    check("search: 关键词 URL 编码（不复刻裸拼 bug）",
          hits("/post/wapi/searchPosts")[0]["raw_query"].find("keyword=%E8%83%A1%E6%A1%83") > 0)
    ok, _ = raises(lambda: miyoushe.search_posts("  "), "带上关键词")
    check("search: 空关键词被拒", ok)


def test_live_codes():
    reset({"/painter/api/user_instant/list": INSTANT,
           "/event/miyolive/index": LIVE, "/event/miyolive/refreshCode": CODES})
    out = miyoushe.live_codes("", {})
    check("live: 兑换码进回复", "AAA111" in out and "BBB222" in out)
    check("live: act_id 透传", all(
        r["headers"].get("x-rpc-act_id") == "123456"
        for r in hits("/event/miyolive/index") + hits("/event/miyolive/refreshCode")))
    reset({"/painter/api/user_instant/list": INSTANT,
           "/event/miyolive/index": LIVE_WAIT, "/event/miyolive/refreshCode": CODES})
    check("live: 未开始不吐码", "还没开始" in miyoushe.live_codes("", {}))
    reset({"/painter/api/user_instant/list": INSTANT_EMPTY,
           "/apihub/api/home/new": HOME,
           "/event/miyolive/index": LIVE, "/event/miyolive/refreshCode": CODES})
    check("live: 官方号拿不到时兜底首页导航", "AAA111" in miyoushe.live_codes("", {}))


GACHA_URL = ("https://webstatic.mihoyo.com/hk4e/event/e20190909gacha-v3/index.html"
             "?authkey_ver=1&sign_type=2&authkey=AbC%2FdEf%3D&game_biz=hk4e_cn&region=cn_gf01")


def test_gacha():
    reset({"/gacha_info/api/getGachaLog": GACHA})
    out = miyoushe.gacha_log(GACHA_URL, {})
    row = hits("/gacha_info/api/getGachaLog")[0]
    check("gacha: authkey 解码后再编码进 URL",
          "authkey=AbC%2FdEf%3D" in row["raw_query"]
          and row["query"].get("authkey") == ["AbC/dEf="])
    check("gacha: 默认角色池 301 与区服",
          row["query"].get("gacha_type") == ["301"] and row["query"].get("region") == ["cn_gf01"])
    check("gacha: 星级与名字进回复", "★神里绫华" in out and "笛剑" in out)
    reset({"/gacha_info/api/getGachaLog": GACHA})
    miyoushe.gacha_log("武器 " + GACHA_URL, {})
    check("gacha: 武器前缀切 302",
          hits("/gacha_info/api/getGachaLog")[0]["query"].get("gacha_type") == ["302"])
    ok, _ = raises(lambda: miyoushe.gacha_log("没有链接", {}), "没解析到 authkey")
    check("gacha: 无链接被拒", ok)
    ok, _ = raises(lambda: miyoushe.gacha_log(GACHA_URL.replace("cn_gf01", "os_usa"), {}), "国际服")
    check("gacha: 国际服区服被拒", ok)


# ---------------------------------------------------------------- 账号类指令


def test_exploration_and_abyss():
    reset({INDEX_PATH: INDEX, "/game_record/app/genshin/api/spiralAbyss": ABYSS})
    out = miyoushe.exploration("100000000", CK_CTX)
    check("exploration: 面板行", "旅行者 Lv.60" in out and "活跃 1000 天" in out)
    check("exploration: 千分比转百分比", "蒙德 100%" in out)
    out2 = miyoushe.abyss("100000000", CK_CTX)
    check("abyss: 最深楼层与战绩", "12-3" in out2 and "12/12 胜" in out2)
    check("abyss: 高层逐层列星", "12层 ★9/9" in out2 and "8层 ★6/9" in out2)


def test_daily_note_fp():
    reset({NOTE_PATH: NOTE, FP_PATH: FP,
           LOGIN_PATH: DEVICE_LOGIN, SAVE_PATH: DEVICE_SAVE})
    out = miyoushe.daily_note("100000000", CK_CTX)
    fp_rows = hits(FP_PATH)
    check("note: 先取一次设备指纹", len(fp_rows) == 1)
    body = json.dumps(fp_rows[0]["body"] if fp_rows[0]["method"] == "POST" else {})
    check("note: getFp 是 POST 且带 app_name", fp_rows[0]["method"] == "POST" and "bbs_cn" in body)
    check("note: device_fp 进 header",
          hits(NOTE_PATH)[0]["headers"].get("x-rpc-device_fp") == "fp-fixture-123")
    check("note: 树脂与回满时间", "120/200" in out and "1 小时回满" in out)
    check("note: 委托与派遣", "委托 4/4" in out and "派遣 1/2" in out)
    miyoushe.daily_note("100000000", CK_CTX)
    check("note: 指纹缓存，不重复取", len(hits(FP_PATH)) == 1)


def test_device_fp_body():
    """#55-A：getFp 必须是完整设备画像，device_id 必须持久（不能每次随机）。"""
    reset({FP_PATH: FP, LOGIN_PATH: DEVICE_LOGIN, SAVE_PATH: DEVICE_SAVE})
    ck = CK_CTX["ck"][0]
    check("fp: 取到指纹", miyoushe.device_fp(ck) == "fp-fixture-123")

    body = hits(FP_PATH)[0]["body"]
    check("fp: platform 用 2（旧实现是 1）",
          str(body.get("platform")) == "2", str(body.get("platform")))
    ext = json.loads(body.get("ext_fields") or "{}")
    need = ("deviceName", "productName", "screenSize", "sdCapacity", "romCapacity",
            "aaid", "oaid", "cpuType", "osVersion", "packageName", "sdkVersion",
            "buildType", "networkType", "hardware", "board")
    missing = [key for key in need if key not in ext]
    check("fp: ext_fields 是完整设备画像", not missing, f"缺 {missing}")
    did = body.get("device_id") or ""
    check("fp: device_id 是 16 位 hex", bool(re.fullmatch(r"[0-9a-f]{16}", did)), did)
    check("fp: seed_id 与 device_id 同源", body.get("seed_id") == did)

    miyoushe._FP_CACHE.clear()
    miyoushe.device_fp("ltuid=999; ltoken=x")
    check("fp: 换账号仍用同一台设备", hits(FP_PATH)[-1]["body"].get("device_id") == did)
    miyoushe._FP_CACHE.clear()
    miyoushe.device_fp("ltuid=777; ltoken=y")
    check("fp: 清缓存（模拟重启）后 device_id 不变",
          hits(FP_PATH)[-1]["body"].get("device_id") == did)


def test_device_register():
    """#55-B：拿到指纹后补发 deviceLogin/saveDevice；7 天、按账号分桶缓存。"""
    reset({FP_PATH: FP, LOGIN_PATH: DEVICE_LOGIN, SAVE_PATH: DEVICE_SAVE})
    miyoushe.device_fp("ltuid=111; ltoken=a")
    check("fp: 注册两个请求都发了",
          len(hits(LOGIN_PATH)) == 1 and len(hits(SAVE_PATH)) == 1,
          str([r["path"] for r in RECORD]))
    reg = hits(LOGIN_PATH)[0]["body"] or {}
    check("fp: 注册体带 device_id/registration_id/Android",
          bool(reg.get("device_id")) and len(str(reg.get("registration_id"))) == 19
          and reg.get("platform") == "Android", str(reg))

    miyoushe.device_fp("ltuid=111; ltoken=a")
    check("fp: 命中缓存不再取指纹也不再注册",
          len(hits(FP_PATH)) == 1 and len(hits(LOGIN_PATH)) == 1)
    miyoushe.device_fp("ltuid=222; ltoken=b")
    check("fp: 不同账号各取一次", len(hits(FP_PATH)) == 2)

    reset({FP_PATH: FP})          # 注册接口 404：不能把取数拖死
    check("fp: 注册失败仍返回指纹",
          miyoushe.device_fp("ltuid=333; ltoken=c") == "fp-fixture-123")


def test_risk_cooldown():
    """#55-C：1034/5003 是账号级状态，命中后按账号冷却，冷却期内不再发请求。"""
    ck = CK_CTX["ck"][0]
    base = {FP_PATH: FP, LOGIN_PATH: DEVICE_LOGIN, SAVE_PATH: DEVICE_SAVE}
    reset(dict(base, **{NOTE_PATH: {"retcode": 1034, "message": ""}}))
    ok, msg = raises(lambda: miyoushe.daily_note("100000000", CK_CTX), "人机验证")
    check("risk: 1034 文案引导去 App 过验证", ok, msg)
    reset(dict(base, **{NOTE_PATH: {"retcode": 5003, "message": ""}}))
    ok, msg = raises(lambda: miyoushe.daily_note("100000000", CK_CTX), "风控")
    check("risk: 5003 文案说明是账号风控", ok, msg)

    check("risk: 未命中时可直接查", miyoushe.risk_wait(ck, now=1000) == 0)
    miyoushe.note_risk(ck, 1034, now=1000)
    check("risk: 命中后进入整段冷却",
          miyoushe.risk_wait(ck, now=1000) == miyoushe.RISK_COOLDOWN)
    check("risk: 冷却按账号隔离",
          miyoushe.risk_wait("ltuid=999; ltoken=x", now=1000) == 0)
    check("risk: 到期自动放行",
          miyoushe.risk_wait(ck, now=1000 + miyoushe.RISK_COOLDOWN + 1) == 0)

    reset({FP_PATH: FP, LOGIN_PATH: DEVICE_LOGIN, SAVE_PATH: DEVICE_SAVE})
    miyoushe.device_fp(ck)
    miyoushe.note_risk(ck, 5003, now=2000)
    miyoushe.device_fp(ck)
    check("risk: 命中后丢掉被标记的指纹，下次重取", len(hits(FP_PATH)) == 2)


def test_commands_risk_guard():
    """#55-C 接线：冷却期内 run_handler 直接回文案，连 handler 都不进。"""
    ctx = {"ck": [CK_CTX["ck"][0]], "uid": "100000000", "group_id": "1", "user_id": "2"}
    check("guard: 无风控时放行", commands._risk_blocked(ctx) == "")
    commands._note_risk(ctx, miyoushe.MiyousheError("x", code=1034))
    blocked = commands._risk_blocked(ctx)
    check("guard: 命中后拦下并给分钟数", "风控" in blocked and "分钟" in blocked, blocked)
    other = {"ck": ["ltuid=999; ltoken=x"], "uid": ""}
    check("guard: 别的账号不受影响", commands._risk_blocked(other) == "")
    commands._note_risk({"ck": ["ltuid=888; ltoken=y"]},
                        miyoushe.MiyousheError("x", code=10101))
    check("guard: 限额类错误不触发冷却",
          commands._risk_blocked({"ck": ["ltuid=888; ltoken=y"]}) == "")


def test_ledger_month():
    reset({"/event/ys_ledger/monthInfo": LEDGER})
    out = miyoushe.ledger("10月", CK_CTX)
    check("ledger: month 补成 YYYYMM（不复刻 1-12 bug）",
          hits("/event/ys_ledger/monthInfo")[0]["query"].get("month") == [f"{YEAR}10"])
    check("ledger: 原石摩拉与占比", "原石 12000" in out and "每日委托 1800（15%）" in out)
    reset({"/event/ys_ledger/monthInfo": LEDGER})
    miyoushe.ledger("", CK_CTX)
    check("ledger: 缺省当月",
          hits("/event/ys_ledger/monthInfo")[0]["query"].get("month") == [time.strftime("%Y%m")])


def test_zzz_and_gcg():
    reset({"/event/game_record_zzz/api/zzz/note": ZZZ, "/device-fp/api/getFp": FP})
    out = miyoushe.zzz_note("123456789", CK_CTX)
    check("zzz: 电量活力邦布", "电量 100/240" in out and "活力 60/240" in out and "邦布 3/4" in out)
    check("zzz: 区服 prod_gf_cn",
          hits("/event/game_record_zzz/api/zzz/note")[0]["query"].get("server") == ["prod_gf_cn"])
    reset({"/game_record/app/genshin/api/gcg/basicInfo": GCG_INFO,
           "/game_record/app/genshin/api/gcg/deckList": GCG_DECKS})
    out2 = miyoushe.seven_saints("100000000", CK_CTX)
    check("gcg: 牌组统计", "牌组 2 套" in out2 and "第一套" in out2)


def test_weapons():
    reset({"/game_record/app/genshin/api/character/list": CHARACTERS})
    out = miyoushe.weapons("100000000", CK_CTX)
    check("weapons: 只收五星", "雾切之回光 Lv.90" in out and "西风弓" not in out)


# ---------------------------------------------------------------- 缓存、错误码、边界


def test_cache():
    reset({NEWS_PATH: NEWS})
    miyoushe.news("公告")
    miyoushe.news("公告")
    check("cache: 300s 内不重复请求", len(hits(NEWS_PATH)) == 1)
    reset({NEWS_PATH: {"retcode": 10101}})
    ok, msg = raises(lambda: miyoushe.news("公告"), "今日查询次数已达上限")
    check("cache: 失败结果不进缓存", ok, msg)
    reset({NEWS_PATH: NEWS})
    miyoushe.news("公告")
    check("cache: 失败后换成功可再请求", len(hits(NEWS_PATH)) == 1)


def test_retcode_map():
    for code in (-100, 1001, 1008, 10101, 10102, 5003, 1034, 10307):
        reset({NEWS_PATH: {"retcode": code, "message": "x"}})
        ok, msg = raises(lambda: miyoushe.news("公告"), miyoushe.RETCODE_MSG[code])
        check(f"retcode {code} → {miyoushe.RETCODE_MSG[code]}", ok, msg)


def test_cn_only_and_missing_ck():
    reset({NOTE_PATH: NOTE, "/device-fp/api/getFp": FP})
    ok, msg = raises(lambda: miyoushe.daily_note("600000000", CK_CTX), "国际服")
    check("uid 6 开头判国际服并拒绝", ok, msg)
    ok, msg = raises(lambda: miyoushe.daily_note("100000000", {"ck": [], "uid": ""}), "未配置米游社 CK")
    check("无 CK 时提示配置路径", ok, msg)
    ok, msg = raises(lambda: miyoushe.exploration("", {"ck": ["x"], "uid": ""}), "请带上游戏 UID")
    check("无 UID 时提示用法", ok, msg)


def test_clip():
    long_news = {"retcode": 0, "data": {"list": [
        {"post": {"post_id": str(i), "subject": "标" * 200, "created_at": 1729500000}}
        for i in range(5)]}}
    reset({NEWS_PATH: long_news})
    out = miyoushe.HANDLERS["公告"][1]("", {})  # 走注册表才带 _bounded 截断
    check("clip: 超长回复截断", len(out) <= miyoushe.REPLY_CLIP + 12 and "已截断" in out)


def test_handlers_registry():
    expected = {"公告", "活动", "资讯", "兑换码", "米游社", "攻略", "抽卡记录",
                "探索", "深渊", "角色", "武器", "体力", "树脂", "原石", "札记",
                "绝区零体力", "七圣召唤"}
    check("HANDLERS: 指令表全覆盖", expected <= set(miyoushe.HANDLERS),
          f"缺 {expected - set(miyoushe.HANDLERS)}")
    check("HANDLERS: 每项有描述与可调用体",
          all(len(v) == 2 and v[0] and callable(v[1]) for v in miyoushe.HANDLERS.values()))
    ok, msg = raises(lambda: miyoushe.HANDLERS["体力"][1]("", {"ck": [], "uid": "100000000"}), "未配置")
    check("HANDLERS: 体力有 UID 无 CK 走友好文案", ok, msg)


def test_commands_wiring():
    reset({"/post/wapi/searchPosts": SEARCH})
    settings = commands.settings_from({
        "self_account": "100000000",
        "groups": {"123456789": {"enabled": True}},
        "slash_commands": {"enabled": True, "commands": {"ping": {"reply": "pong"}},
                           "miyoushe": {"ck": [], "uid": ""}}},
    )
    out = commands.resolve_reply("体力", settings, None, "100000000")
    check("commands: #体力 带 UID 无 CK 时回配置提示（不联网）", "未配置米游社 CK" in out)
    check("commands: 静态词不受影响", commands.resolve_reply("ping", settings) == "pong")
    check("commands: #米游社胡桃 粘手输入切出参数并取数",
          "胡桃攻略" in commands.resolve_reply("米游社胡桃", settings, None, ""))
    check("commands: 搜索标题用关键词不用 ctx（回归：label 串参）",
          "【米游社 · 胡桃】" in commands.resolve_reply("米游社 胡桃", settings, None, ""))
    check("commands: #帮助 列出动态指令",
          "#体力" in commands.resolve_reply("帮助", settings)
          and "#公告" in commands.resolve_reply("命令", settings))
    check("commands: ck 字符串归一成列表",
          commands.settings_from({"slash_commands": {"miyoushe": {"ck": "a=b"}}})
          ["plugins"]["miyoushe"]["options"]["ck"] == ["a=b"])
    check("commands: 空 ck 归一成空列表",
          commands.settings_from({"slash_commands": {"miyoushe": {"ck": "  "}}})
          ["plugins"]["miyoushe"]["options"]["ck"] == [])


def main():
    with fixture_server(FakeMihoyo, ports=set()) as server:
        real = dict(miyoushe.HOSTS)
        miyoushe.HOSTS = {k: f"http://127.0.0.1:{server.server_port}" for k in real}
        # device.json 是**真**状态文件（7 天指纹 + device_id），
        # 用例里要 unlink/回写，必须整表指到临时目录，别动 ~/.water-group/state
        real_device_file = miyoushe.DEVICE_FILE
        # commands.LOG 同理：_note_risk 会打「risk cooldown」——写进真的守护日志，
        # 以后排查风控时会看到一堆根本没发生过的命中（与 check_qr_login 同一手法）
        real_cmd_log = commands.LOG
        with temp_dir() as tmpdir:
            miyoushe.DEVICE_FILE = tmpdir / "device.json"
            commands.LOG = tmpdir / "wg_commands.log"
            restrict, _ = network_guard({server.server_port})
            try:
                with patch("socket.create_connection", restrict):
                    for fn in (test_ds_and_headers, test_character_post_body, test_news,
                               test_search_encoding, test_live_codes, test_gacha,
                               test_exploration_and_abyss, test_daily_note_fp, test_ledger_month,
                               test_zzz_and_gcg, test_weapons, test_cache, test_retcode_map,
                               test_cn_only_and_missing_ck, test_clip, test_handlers_registry,
                               test_commands_wiring, test_device_fp_body, test_device_register,
                               test_risk_cooldown, test_commands_risk_guard):
                        fn()
            finally:
                miyoushe.HOSTS = real
                miyoushe.DEVICE_FILE = real_device_file
                commands.LOG = real_cmd_log
    if _runner.summary():
        sys.exit(1)


if __name__ == "__main__":
    main()
