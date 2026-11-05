"""飞猪酒店价格插件门禁（issue #139）：`#酒店` 的取数契约与配置纪律。

全部走 ``patch(winconsole.run_hidden)`` 假 runner，**不发真实请求**（本门禁
的取数通道是子进程，不是 HTTP，所以 fixture_server 不是这条的形态——
契约面是 argv 与 env）。每条用例对着一次真实故障或一次差点写错的地方：

1. **无 Key 零 spawn**。CLI 内置体验 key 会「正常」返回脱敏价 `¥2x`，
   所以「没配 Key」在取数层必须显式前置判断——门禁断言 runner 一次都没
   被调用，且文案提示未配置。
2. **脱敏价拒绝**。上游返回 `¥2x` 这类半截价格时整体拒绝，不播报。
3. **目的地静默降级反制**：`--dest-name 北极点xyz` 返回香港酒店时，
   结果与目的地零交集 → 按「查无结果」回答，不拿别的城市凑。
4. **CLI 失败分类**：exit 非 0 的 stderr 原文回、超时分类、进程不存在分类；
   失败不进缓存（一次抖动不该放大成整段 TTL）。
5. **真实失败文案诚实**：`MCP HTTP 429` 这类真实状态原样回，不冒充其他。
6. **``need_ck=False`` 与默认关闭**：与 busboss / bilibili 同口径——群友
   不需要绑定米游社账号，没配 Key 就不该出现在 `#帮助`。

    .venv/Scripts/python.exe scripts/check_hotel.py
"""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

from check_fixture_support import assert_runner, scratch_dir  # noqa: E402

# 数据根必须在 import flyai **之前**钉到临时目录（同 check_busboss）：
# LOG 是模块级常量，import 期就求值完了。
_SCRATCH = scratch_dir(prefix="tulpa-hotel-")
os.environ["WATER_GROUP_HOME"] = str(_SCRATCH / "home")
(_SCRATCH / "home").mkdir(parents=True, exist_ok=True)

import flyai  # noqa: E402
import commands  # noqa: E402

# 即时断言 runner（同 check_busboss）：check() 打印 + 累积，summary() 出尾行。
_runner = assert_runner(markers="ok")
check = _runner.check

KEY = "test-api-key-for-fixture-only"
CALLS = []          # runner 收到的 (argv, kwargs) 列表
OUTCOME = {}        # 假 runner 的行为：{"rc":..., "stdout":..., "stderr":...}


class _Completed:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def _fake_runner(argv, **kwargs):
    """假子进程：记 argv，不起进程，不出站。"""
    CALLS.append({"argv": list(argv), "kwargs": dict(kwargs)})
    mode = OUTCOME.get("mode", "ok")
    if mode == "missing":
        raise FileNotFoundError("No such file or directory: 'flyai'")
    if mode == "timeout":
        raise TimeoutError("timed out")
    if mode == "fail":
        return _Completed(rc=1, stdout="",
                          stderr=OUTCOME.get("stderr", "Error: boom"))
    return _Completed(rc=0, stdout=OUTCOME.get("stdout", "{}"), stderr="")


def reset(**kwargs):
    CALLS.clear()
    flyai.clear_cache()
    OUTCOME.clear()
    OUTCOME.update(kwargs)


GOOD_PAYLOAD = {
    "status": 0,
    "list": [
        {"hotelName": "上海外滩店", "address": "上海市黄浦区中山东一路",
         "star": 5, "price": "618", "shId": "12345",
         "url": "https://example.com/h/12345"},
        {"hotelName": "上海国宾馆", "address": "上海市黄浦区中山路",
         "star": 4, "price": "488", "shId": "54321",
         "url": "https://example.com/h/54321"},
    ],
}


# --------------------------------------------------------------------------
def test_no_key_no_spawn():
    print("[无 Key 零 spawn]")
    reset(stdout=json.dumps(GOOD_PAYLOAD, ensure_ascii=False))
    try:
        flyai.answer("上海 明天", {})
    except flyai.HotelError as exc:
        check("无 Key 给配置指引", "未配置" in str(exc), str(exc))
    else:
        check("无 Key 给配置指引", False, "没有抛错")
    check("无 Key 时不 spawn", not CALLS, str(CALLS))


def test_happy_path():
    print("[完整链路：argv 契约]")
    reset(stdout=json.dumps(GOOD_PAYLOAD, ensure_ascii=False))
    text = flyai.answer("上海 明天", {"api_key": KEY})
    check("正常返回酒店列表", "上海外滩店" in text and "618" in text, text[:400])
    check("CLI 被调用一次", len(CALLS) == 1, str(CALLS))
    argv = CALLS[0]["argv"] if CALLS else []
    check("argv 是 node bundle + search-hotel",
          argv[:3] == ["flyai", "search-hotel", "--dest-name"], str(argv))
    check("目的地透传", "上海" in argv, str(argv))
    check("入住日期填上", "--check-in-date" in argv, str(argv))
    kw = CALLS[0]["kwargs"] if CALLS else {}
    check("子进程带超时", kw.get("timeout") == flyai.TIMEOUT, str(kw.get("timeout")))
    check("子进程不继承输出到控制台", kw.get("text") is True and kw.get("encoding") == "utf-8", str(kw))


def test_cli_args_passthrough():
    print("[CLI 参数透传]")
    # 用远期日期避开「已过去」剥离；cache_key 不含景点/日期差异时，用 clear_cache 隔离。
    reset(stdout=json.dumps(GOOD_PAYLOAD, ensure_ascii=False))
    flyai.answer("上海 外滩 2030-11-01", {"api_key": KEY, "sort": "price_asc",
                                           "max_price": 500, "hotel_stars": "3,4"})
    argv = CALLS[0]["argv"] if CALLS else []
    check("景点透传", "--poi-name" in argv and "外滩" in argv, str(argv))
    check("排序透传", "--sort" in argv and "price_asc" in argv, str(argv))
    check("最高价透传", "--max-price" in argv and "500" in argv, str(argv))
    check("星级透传", "--hotel-stars" in argv and "3,4" in argv, str(argv))


def test_masked_price_rejected():
    print("[脱敏价拒绝]")
    payload = {"status": 0, "list": [
        {"hotelName": "上海外滩店", "address": "上海市黄浦区", "price": "¥2x"}]}
    reset(stdout=json.dumps(payload, ensure_ascii=False))
    try:
        flyai.answer("上海 明天", {"api_key": KEY})
    except flyai.HotelError as exc:
        check("脱敏价不播报，提示体验 Key", "脱敏" in str(exc) and "¥2x" not in str(exc), str(exc))
    else:
        check("脱敏价不播报，提示体验 Key", False, "没有抛错")


def test_dest_zero_overlap():
    print("[目的地零交集]")
    payload = {"status": 0, "list": [
        {"hotelName": "九龙东帝苑酒店", "address": "香港九龙",
         "price": "¥618"}]}
    reset(stdout=json.dumps(payload, ensure_ascii=False))
    try:
        flyai.answer("北极点 明天", {"api_key": KEY})
    except flyai.HotelError as exc:
        check("零交集说查无结果", "查无结果" not in str(exc) and "没有查到" in str(exc)
              and "北极点" in str(exc), str(exc))
        check("其余城市的酒店不外播", "九龙东帝苑" not in str(exc), str(exc))
    else:
        check("零交集说查无结果", False, "没有抛错")
    # 对照：命中目的地就必须放行
    reset(stdout=json.dumps(GOOD_PAYLOAD, ensure_ascii=False))
    text = flyai.answer("上海 明天", {"api_key": KEY})
    check("对照组：命中目的地放行", "上海外滩店" in text, text[:200])


def test_failure_classification():
    print("[失败分类]")
    # exit 非 0：stderr 原文回
    reset(mode="fail", stderr="Error: --check-in-date must be in YYYY-MM-DD format")
    try:
        flyai.answer("上海 明天", {"api_key": KEY})
    except flyai.HotelError as exc:
        check("本地拒绝原文回", "YYYY-MM-DD" in str(exc), str(exc))
    else:
        check("本地拒绝原文回", False, "没有抛错")
    # MCP HTTP 真实状态原样回
    reset(mode="fail", stderr="MCP HTTP 429: Too Many Requests")
    try:
        flyai.answer("上海 明天", {"api_key": KEY})
    except flyai.HotelError as exc:
        check("真实 HTTP 状态原样回", "429" in str(exc), str(exc))
    else:
        check("真实 HTTP 状态原样回", False, "没有抛错")
    # 超时
    reset(mode="timeout")
    try:
        flyai.answer("上海 明天", {"api_key": KEY})
    except flyai.HotelError as exc:
        check("超时文案", "超时" in str(exc), str(exc))
    else:
        check("超时文案", False, "没有抛错")
    # 进程不存在
    reset(mode="missing")
    try:
        flyai.answer("上海 明天", {"api_key": KEY})
    except flyai.HotelError as exc:
        check("未安装 CLI 的指引", "未安装" in str(exc) and "npm i -g" in str(exc), str(exc))
    else:
        check("未安装 CLI 的指引", False, "没有抛错")


def test_status_envelope():
    print("[上游 status 信封]")
    reset(stdout=json.dumps({"status": 1, "systemMessage": "[SYS001] DateTimeParseException"}, ensure_ascii=False))
    try:
        flyai.answer("上海 明天", {"api_key": KEY})
    except flyai.HotelError as exc:
        check("status=1 透传 message", "DateTimeParseException" in str(exc), str(exc))
    else:
        check("status=1 透传 message", False, "没有抛错")


def test_cache():
    print("[缓存]")
    reset(stdout=json.dumps(GOOD_PAYLOAD, ensure_ascii=False))
    flyai.answer("上海 明天", {"api_key": KEY})
    first = len(CALLS)
    flyai.answer("上海 明天", {"api_key": KEY})
    check("同参数命中缓存不再 spawn", len(CALLS) == first, f"{first} → {len(CALLS)}")
    reset(mode="fail", stderr="Error: boom")
    for _ in range(2):
        try:
            flyai.answer("上海 明天", {"api_key": KEY})
        except flyai.HotelError:
            pass
    check("失败结果不进缓存（两次都 spawn）", len(CALLS) == 2, str(len(CALLS)))


def test_render_truncation():
    print("[渲染截断]")
    items = [{"hotelName": f"上海测试酒店{i}", "address": "上海市黄浦区",
              "price": "618"} for i in range(40)]
    text = flyai.render_hotels("上海", "", "2026-11-06", "2026-11-07", items, {})
    check("截断了说「另有 N 家」", "另有" in text, text[-120:])
    check("回复不超过 REPLY_CLIP", len(text) <= flyai.REPLY_CLIP, str(len(text)))


def test_split_and_dates():
    print("[参数切分与日期]")
    check("三段日期", flyai.split_args("上海 明天") == ("上海", "", "明天", ""),
          str(flyai.split_args("上海 明天")))
    check("四段景点+日期", flyai.split_args("上海 外滩 2026-11-01 2026-11-02")
          == ("上海", "外滩", "2026-11-01", "2026-11-02"),
          str(flyai.split_args("上海 外滩 2026-11-01 2026-11-02")))
    check("纯目的地", flyai.split_args("上海") == ("上海", "", "", ""))
    try:
        flyai.split_args("")
    except flyai.HotelError:
        check("空参报错", True)
    else:
        check("空参报错", False, "没有抛错")


def test_plugin_registry():
    print("[插件注册]")
    owner = commands.PLUGINS.owner("酒店")
    check("#酒店 归属 hotel 插件", owner is not None and owner.name == "hotel",
          getattr(owner, "name", "None"))
    check("插件加载无错误", commands.PLUGINS.load_errors == {},
          str(commands.PLUGINS.load_errors))
    plugin = commands.PLUGINS.get("hotel")
    check("插件默认关闭（没配 Key 时不该出现在 #帮助）",
          plugin is not None and plugin.default_enabled is False)
    settings = commands.settings_from({})
    check("默认配置下 #酒店 不可用", not commands.PLUGINS.available("酒店", settings, ""))
    on = commands.settings_from({"plugins": {"hotel": {"enabled": True}}})
    check("开启后 #酒店 可用", commands.PLUGINS.available("酒店", on, ""))
    check("开启后出现在 #帮助", "#酒店" in commands.help_text(on, ""),
          commands.help_text(on, "")[:200])
    handler = commands.HANDLERS.get("酒店")
    check("handler 不需要绑定凭证", handler is not None and getattr(handler[1], "need_ck", True) is False)
    opts = commands.plugin_options(on, "hotel")
    check("配置归一有默认值",
          opts.get("max_hotels") == 8 and opts.get("cache_seconds") == 300, str(opts))
    bad = commands.plugin_options(
        commands.settings_from({"plugins": {"hotel": {
            "enabled": True, "options": {"max_hotels": -1, "cache_seconds": "x",
                                          "sort": "乱写的排序"}}}}),
        "hotel")
    check("坏配置回落默认值", bad.get("max_hotels") == 8 and bad.get("cache_seconds") == 300
          and bad.get("sort") == "", str(bad))


def _run(fn):
    """一段炸了不许把后面的断言一起吞掉（同 check_busboss）。"""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        check(f"{fn.__name__} 跑完不炸", False, f"{type(exc).__name__}: {exc}")


def main():
    with patch.object(flyai.winconsole, "run_hidden", _fake_runner):
        for fn in (test_no_key_no_spawn, test_happy_path, test_cli_args_passthrough,
                   test_masked_price_rejected, test_dest_zero_overlap,
                   test_failure_classification, test_status_envelope,
                   test_cache, test_render_truncation, test_split_and_dates,
                   test_plugin_registry):
            _run(fn)
    return _runner.summary()


if __name__ == "__main__":
    sys.exit(main())
