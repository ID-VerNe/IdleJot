"""车盈网班次插件门禁（issue #103）：`#班次` 的取数契约与凭证生命周期。

全部走 ``fixture_server`` + ``network_guard``，不发真实请求。每条契约都对应
一次真实故障或一次差点写错的地方：

1. **节点参数 ``value`` 必须是嵌套对象**。传字符串时上游**不报错、返回 0 条**
   （2026-10-26 在这上面误判过一整轮「这条城际没班次」）。取数层只留
   ``node()`` 一个构造点，门禁从**实际发出的请求体**上断言形状，而不是断言
   某个函数返回了什么——断言实现细节会让人改实现时顺手改断言。
2. **对照组**：fixture 复刻上游行为——收到字符串形态就返回 0 条。所以
   「0 条」既可能真没班次，也可能参数错；只有形状断言能区分这两件事。
3. **日期结构判别与可查性分开**：``昨天`` / 超窗口日期**像日期**但不可查，
   必须报日期错误而不是掉进「站点关键词」分支回一句「没有含『昨天』的站点」。
4. **凭证轮换必须写回**：refresh 分支开启轮换，新 ``refresh_token`` 不落盘
   下一次就失效。
5. **续期失败不抹凭证**：一次网络抖动不该把还能用的东西删掉。
6. **失败结果不进缓存**：一次抖动不该被放大成一整段 TTL。
7. **无凭证零请求**：配置没做就不该打上游，也不该把 token 打进日志。
8. **``need_ck=False``**：群友不需要绑定米游社账号也能查班次，否则未绑定的人
   收到的是绑定提示。

    .venv/Scripts/python.exe scripts/check_busboss.py
"""
import atexit
import datetime
import json
import os
import shutil
import sys
import tempfile
import urllib.parse
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

from check_fixture_support import network_guard  # noqa: E402
from fixture_server import FixtureHandler, fixture_server  # noqa: E402

# 路径必须在 import busboss **之前**钉到临时目录：TOKEN_FILE / CITY_FILE 是
# 模块级常量，import 期就求值完了（与 check_sixty 同款约束）。
_SCRATCH = Path(tempfile.mkdtemp(prefix="tulpa-busboss-"))
atexit.register(shutil.rmtree, _SCRATCH, True)
os.environ["WATER_GROUP_HOME"] = str(_SCRATCH / "home")
(_SCRATCH / "home").mkdir(parents=True, exist_ok=True)

import busboss  # noqa: E402
import commands  # noqa: E402

FAILED = []
PORTS = set()

GOOD_TOKEN = "Bearer good"
STATE = {"requests": [], "token_mode": "ok", "zero_for_string": True}

CITIES = [
    {"provinceCode": "44", "provinceName": "广东省",
     "firstTagName": "广州市", "firstTagCode": "4401", "secondTagList": []},
    {"provinceCode": "44", "provinceName": "广东省",
     "firstTagName": "珠海市", "firstTagCode": "4404", "secondTagList": []},
    {"provinceCode": "44", "provinceName": "广东省",
     "firstTagName": "深圳市", "firstTagCode": "4403", "secondTagList": []},
]

CLASSES = [
    {"id": "c1", "lineId": "l1", "lineName": "广州（区庄）-珠海市区",
     "classDate": "2026-10-27", "classTime": "06:30:00",
     "lineClassDayPrice": 13.8, "canSaleCount": 24,
     "startNodeList": [{"nodeName": "区庄地铁站C口前方公交站", "classTime": "06:30:00"},
                       {"nodeName": "区庄地铁站C口前方公交站(公路路政门口)",
                        "classTime": "06:32:00"}],
     "arrivalNodeList": [{"nodeName": "北理工", "classTime": "08:25:00"},
                         {"nodeName": "拱北公交站（口岸）", "classTime": "09:20:00"}]},
    {"id": "c2", "lineId": "l2", "lineName": "广州白云至珠海A线",
     "classDate": "2026-10-27", "classTime": "07:00:00",
     "lineClassDayPrice": 35.0, "canSaleCount": 41,
     "startNodeList": [{"nodeName": "广州白云站南平台3楼进站口3路边",
                        "classTime": "07:00:00"}],
     "arrivalNodeList": [{"nodeName": "北理工公交站", "classTime": "09:00:00"}]},
]


def check(name, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


class Handler(FixtureHandler):
    def do_POST(self):
        path = self.path.split("?")[0]
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        if path == "/connect/token":
            form = urllib.parse.parse_qs(raw.decode("utf-8"))
            STATE["requests"].append({"path": path, "form": form,
                                      "auth": self.headers.get("Authorization", "")})
            rt = (form.get("refresh_token") or [""])[0]
            if STATE["token_mode"] == "fail" or rt.startswith("bad"):
                self.send_json({"error": "invalid_grant",
                                "error_description": "The refresh token is not valid."}, status=400)
                return
            self.send_json({"access_token": "new-at", "refresh_token": "new-rt",
                            "expires_in": 3600})
            return
        body = json.loads(raw.decode("utf-8") or "{}")
        STATE["requests"].append({"path": path, "body": body,
                                  "auth": self.headers.get("Authorization", "")})
        if self.headers.get("Authorization", "") not in (GOOD_TOKEN, "Bearer new-at"):
            self.send_json({"error": {"code": "Volo.Authorization:010001",
                                      "message": "未授权"}}, status=401)
            return
        if path.endswith("/standard-node/v2/start"):
            self.send_json(CITIES)
            return
        if path.endswith("/line-class-day/v2/list"):
            start = (body.get("start") or {})
            # 对照组：复刻上游对「value 传字符串」的处理——200 + 空列表，不报错。
            if STATE["zero_for_string"] and not isinstance(start.get("value"), dict):
                self.send_json({"filterNodes": {}, "totalCount": 0, "items": []})
                return
            self.send_json({"filterNodes": {"startNode": [], "arrivalNode": []},
                            "totalCount": len(CLASSES), "items": CLASSES})
            return
        self.send_json({"error": {"message": "unknown path"}}, status=404)


# --------------------------------------------------------------------------
def test_node_shape():
    print("[节点参数形状]")
    check("Region 包成 {id, level}",
          busboss.node("4401") == {"type": "Region", "value": {"id": "4401", "level": 1}},
          str(busboss.node("4401")))
    check("level 可指定",
          busboss.node("4401", 2)["value"]["level"] == 2)
    check("Station 走直通分支",
          busboss.node("n1", 1, "Station") == {"type": "Station", "value": "n1"},
          str(busboss.node("n1", 1, "Station")))
    try:
        busboss.node("")
    except busboss.BusbossError:
        check("空节点报错", True)
    else:
        check("空节点报错", False, "没有抛错")


def test_parse_date():
    print("[日期]")
    base = datetime.date(2026, 10, 26)
    cases = [("", "2026-10-26"), ("今天", "2026-10-26"), ("明天", "2026-10-27"),
             ("后天", "2026-10-28"), ("大后天", "2026-10-29"),
             ("2026-10-30", "2026-10-30"), ("2026/11/02", "2026-11-02"),
             ("11-05", "2026-11-05")]
    for raw, want in cases:
        got = busboss.parse_date(raw, base)
        check(f"parse_date({raw!r}) → {want}", got == want, got)
    for raw in ("昨天", "前天", "2026-10-01", "2027-01-01", "下周一", "10-40"):
        try:
            busboss.parse_date(raw, base)
        except busboss.BusbossError:
            check(f"parse_date({raw!r}) 报错", True)
        else:
            check(f"parse_date({raw!r}) 报错", False, "没有抛错")
    check("昨天算「像日期」", busboss.looks_like_date("昨天"))
    check("超窗口日期算「像日期」", busboss.looks_like_date("2027-01-01"))
    check("火星不算日期", not busboss.looks_like_date("火星"))
    check("站点名不算日期", not busboss.looks_like_date("区庄"))


def test_split_args():
    print("[参数切分]")
    check("三段日期",
          busboss.split_args("广州 珠海 明天") == ("广州", "珠海", "明天", ""),
          str(busboss.split_args("广州 珠海 明天")))
    check("三段站点",
          busboss.split_args("广州 珠海 区庄") == ("广州", "珠海", "", "区庄"),
          str(busboss.split_args("广州 珠海 区庄")))
    check("四段站点+日期",
          busboss.split_args("广州 珠海 区庄 明天") == ("广州", "珠海", "明天", "区庄"),
          str(busboss.split_args("广州 珠海 区庄 明天")))
    for bad in ("广州", "广州 珠海 明天 区庄 多余"):
        try:
            busboss.split_args(bad)
        except busboss.BusbossError:
            check(f"split_args({bad!r}) 报错", True)
        else:
            check(f"split_args({bad!r}) 报错", False, "没有抛错")


def test_normalize_city():
    print("[城市名归一]")
    for raw, want in (("广州市", "广州"), ("广东省", "广东"), (" 珠海 ", "珠海"),
                      ("广州市\u3000", "广州"), ("阿坝藏族羌族自治州", "阿坝藏族羌族")):
        check(f"normalize_city({raw!r}) → {want}", busboss.normalize_city(raw) == want,
              busboss.normalize_city(raw))


def test_render():
    print("[渲染]")
    text = busboss.render_summary("广州", "珠海", "2026-10-27", CLASSES, 2)
    check("概览有班次总数", "共 2 班" in text, text)
    check("概览有首末班", "首 06:30" in text and "末 07:00" in text, text)
    check("概览有票价区间", "¥13.8~35" in text, text)
    check("概览按时间升序", text.index("06:30") < text.index("07:00"), text)
    empty = busboss.render_summary("广州", "珠海", "2026-10-27", [], 0)
    check("空结果说「暂无班次」", "暂无班次" in empty, empty)
    station = busboss.render_station("广州", "珠海", "2026-10-27",
                                     "区庄地铁站C口前方公交站", CLASSES[:1], side="start")
    check("站点级标「上车」", "上车" in station, station)
    check("站点级有到达时刻", "09:20" in station, station)
    arrival = busboss.render_station("广州", "珠海", "2026-10-27",
                                     "北理工", CLASSES[:1], side="arrival")
    check("终点站标「下车」", "下车" in arrival, arrival)
    check("终点站取该站到达时刻", "08:25" in arrival, arrival)


def test_filter_station():
    print("[站点筛选]")
    name, kept, cand = busboss.filter_by_station(CLASSES, "北理工", "arrival")
    check("整名优先于前缀（北理工 vs 北理工公交站）", name == "北理工" and len(kept) == 1,
          f"{name} {len(kept)}")
    name, kept, cand = busboss.filter_by_station(CLASSES, "区庄", "start")
    check("前缀命中多个给候选", not name and len(cand) == 2, f"{name} {cand}")
    check("候选按班次数再按长度排序", cand[0] == "区庄地铁站C口前方公交站", str(cand))
    name, kept, cand = busboss.filter_by_station(CLASSES, "火星", "start")
    check("对不上返回空而不是全量", not name and not kept and not cand)
    name, kept, _cand = busboss.filter_by_station(CLASSES, "区庄地铁站C口前方公交站", "start")
    check("写全名不再歧义", name == "区庄地铁站C口前方公交站", name)


def test_tokens():
    print("[凭证生命周期]")
    tok = _SCRATCH / "tok.json"
    busboss.save_tokens({"access_token": "old-at", "refresh_token": "old-rt",
                         "exp": 0, "appid": "wx", "client_id": "c"}, tok)
    got = busboss.access_token(path=tok)
    check("过期自动续期", got == "new-at", got)
    saved = json.loads(tok.read_text(encoding="utf-8"))
    check("轮换后的 refresh_token 写回", saved["refresh_token"] == "new-rt", str(saved))
    check("新 access_token 写回", saved["access_token"] == "new-at", str(saved))
    check("exp 被刷新", saved["exp"] > 0, str(saved))

    bad = _SCRATCH / "bad.json"
    busboss.save_tokens({"access_token": "old-at", "refresh_token": "bad-rt",
                         "exp": 0, "appid": "wx"}, bad)
    before = bad.read_text(encoding="utf-8")
    try:
        busboss.access_token(path=bad)
    except busboss.BusbossError:
        check("续期失败抛 BusbossError", True)
    else:
        check("续期失败抛 BusbossError", False, "没有抛错")
    check("续期失败不抹掉原凭证", bad.read_text(encoding="utf-8") == before, "文件被改了")

    missing = _SCRATCH / "none.json"
    check("缺文件返回 None", busboss.load_tokens(missing) is None)
    try:
        busboss.access_token(path=missing)
    except busboss.BusbossError as exc:
        check("缺凭证给配置指引", "未配置" in str(exc), str(exc))
    else:
        check("缺凭证给配置指引", False, "没有抛错")


def test_query_end_to_end():
    print("[端到端：形状 + 缓存]")
    tok = _SCRATCH / "good.json"
    busboss.save_tokens({"access_token": "good", "refresh_token": "rt",
                         "exp": int(__import__("time").time()) + 3600,
                         "appid": "wx", "client_id": "c"}, tok)
    city = _SCRATCH / "cities.json"
    busboss.clear_cache()
    STATE["requests"].clear()
    text = busboss.answer("广州 珠海 2026-10-27", {}, token_path=tok, city_path=city,
                          today=datetime.date(2026, 10, 26))
    check("查询有结果", "共 2 班" in text, text)

    lists = [r for r in STATE["requests"] if r["path"].endswith("/v2/list")]
    check("发出了一次班次请求", len(lists) == 1, str(len(lists)))
    body = lists[0]["body"] if lists else {}
    start = body.get("start") or {}
    value = start.get("value")
    check("请求体 start.value 是对象", isinstance(value, dict), str(start))
    check("start.value 带 id", isinstance(value, dict) and value.get("id") == "4401", str(start))
    check("start.value 带 level", isinstance(value, dict) and value.get("level") == 1, str(start))
    check("请求体没有字符串形态的 value", not isinstance(value, str), str(start))
    check("classDate 正确", body.get("classDate") == "2026-10-27", str(body))
    check("鉴权头带 Bearer", lists[0]["auth"].startswith("Bearer "), lists[0]["auth"])

    before = len(STATE["requests"])
    busboss.answer("广州 珠海 2026-10-27", {}, token_path=tok, city_path=city,
                   today=datetime.date(2026, 10, 26))
    check("同参数命中缓存不再打上游", len(STATE["requests"]) == before,
          f"{before} → {len(STATE['requests'])}")


def test_zero_is_not_error():
    print("[对照组：0 条 ≠ 报错]")
    tok = _SCRATCH / "good.json"
    busboss.clear_cache()
    with patch.object(busboss, "fetch_classes",
                      lambda *a, **k: {"items": [], "total": 0}):
        text = busboss.answer("广州 珠海 2026-10-27", {}, token_path=tok,
                              city_path=_SCRATCH / "cities.json",
                              today=datetime.date(2026, 10, 26))
    check("0 条渲染成「暂无班次」而不是抛错", "暂无班次" in text, text)


def test_no_credential_no_request():
    print("[无凭证零请求]")
    busboss.clear_cache()
    STATE["requests"].clear()
    missing = _SCRATCH / "absent.json"
    try:
        busboss.answer("广州 珠海 2026-10-27", {}, token_path=missing,
                       city_path=_SCRATCH / "cities-none.json",
                       today=datetime.date(2026, 10, 26))
    except busboss.BusbossError as exc:
        check("无凭证报配置指引", "未配置" in str(exc), str(exc))
    else:
        check("无凭证报配置指引", False, "没有抛错")
    check("无凭证时不打上游", not STATE["requests"], str(STATE["requests"]))


def test_plugin_registry():
    print("[插件注册]")
    owner = commands.PLUGINS.owner("班次")
    check("#班次 归属 busboss 插件", owner is not None and owner.name == "busboss",
          getattr(owner, "name", "None"))
    check("插件加载无错误", commands.PLUGINS.load_errors == {},
          str(commands.PLUGINS.load_errors))
    plugin = commands.PLUGINS.get("busboss")
    check("插件默认关闭（没配凭证时不该出现在 #帮助）",
          plugin is not None and plugin.default_enabled is False)
    settings = commands.settings_from({})
    check("默认配置下 #班次 不可用", not commands.PLUGINS.available("班次", settings, ""))
    on = commands.settings_from({"plugins": {"busboss": {"enabled": True}}})
    check("开启后 #班次 可用", commands.PLUGINS.available("班次", on, ""))
    check("开启后出现在 #帮助", "#班次" in commands.help_text(on, ""),
          commands.help_text(on, "")[:200])
    handler = commands.HANDLERS.get("班次")
    check("handler 不需要绑定凭证", handler is not None and getattr(handler[1], "need_ck", True) is False)
    opts = commands.plugin_options(on, "busboss")
    check("配置归一有默认值",
          opts.get("max_lines") == 8 and opts.get("max_classes") == 200, str(opts))
    bad = commands.plugin_options(
        commands.settings_from({"plugins": {"busboss": {
            "enabled": True, "options": {"max_lines": -1, "cache_seconds": "x"}}}}),
        "busboss")
    check("坏配置回落默认值", bad.get("max_lines") == 8 and bad.get("cache_seconds") == 60,
          str(bad))


def _run(fn):
    """一段炸了不许把后面的断言一起吞掉。

    变异验证时踩过：``node()`` 改成字符串形态后，形状段直接 TypeError 中断，
    端到端那几条「请求体 start.value 是对象」根本没跑到——门禁红了，但红的是
    最容易看见的那条，真正守着请求体的那条没出声。
    """
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        check(f"{fn.__name__} 跑完不炸", False, f"{type(exc).__name__}: {exc}")


def main():
    with fixture_server(Handler, ports=PORTS) as server:
        base = f"http://127.0.0.1:{server.server_port}"
        with patch.object(busboss, "BASE", base), \
                patch.object(busboss, "AUTH_URL", base + "/connect/token"), \
                patch.object(busboss, "TOKEN_FILE", _SCRATCH / "default.json"), \
                patch.object(busboss, "CITY_FILE", _SCRATCH / "cities.json"):
            restrict, _calls = network_guard(PORTS)
            with patch("socket.create_connection", restrict):
                for fn in (test_node_shape, test_parse_date, test_split_args,
                           test_normalize_city, test_render, test_filter_station,
                           test_tokens, test_query_end_to_end, test_zero_is_not_error,
                           test_no_credential_no_request, test_plugin_registry):
                    _run(fn)
    print()
    if FAILED:
        print(f"{len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
