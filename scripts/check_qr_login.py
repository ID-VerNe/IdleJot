"""钉住每用户扫码登录（issue #6）：passport 双流、凭据兜底解析、DPAPI、限流、P0 隐私。

全部走 fixture_server + network_guard，不发真实请求；qr_login.HOSTS 整表指到
127.0.0.1。users 目录与审计日志改道临时目录，不碰 ~/.water-group 真实数据。

    .venv/Scripts/python.exe scripts/check_qr_login.py
"""
import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

from fixture_server import FixtureHandler, fixture_server  # noqa: E402
from check_fixture_support import network_guard  # noqa: E402

import commands  # noqa: E402
import miyoushe  # noqa: E402  (域异常由插件声明，commands 不再转出类对象)
import qr_login  # noqa: E402
import user_store  # noqa: E402

FAILED = []
RECORD = []
RESPONSES = {}

APP_CREATE = "/account/ma-cn-passport/app/createQRLogin"
APP_QUERY = "/account/ma-cn-passport/app/queryQRLoginStatus"
WEB_CREATE = "/account/ma-cn-passport/web/createQRLogin"
WEB_QUERY = "/account/ma-cn-passport/web/queryQRLoginStatus"
LTOKEN = "/account/auth/api/getLTokenBySToken"
COOKIE_INFO = "/account/auth/api/getCookieAccountInfoBySToken"
ROLES = "/binding/api/getUserGameRolesByCookie"

# 每个 ticket 的查询响应；测试按需改写。
QUERY_STATE = {}
COOKIE_FIELDS = ("ltuid=12345; Path=/; Expires=Fri, 01 Jan 2027 00:00:00 GMT",
                 "ltoken=WEB-LTOKEN; Path=/",
                 "cookie_token=WEB-COOKIE; Path=/",
                 "account_id=98765; Path=/",
                 "account_mid_v2=mid-abc; Path=/")


class FakePassport(FixtureHandler):
    """passport / takumi 的替身。

    换票与 roles 走 GET（#41 实机：POST 会 405），二维码那三个走 POST。
    两条路共用 `_respond`，测试只需要维护 RESPONSES 表。
    """

    def _respond(self, body):
        path = urllib.parse.urlparse(self.path).path
        RECORD.append({"path": path, "method": self.command, "body": body,
                       "headers": {k.lower(): v for k, v in self.headers.items()}})
        spec = RESPONSES.get(path)
        if spec is None:
            self.send_json({"retcode": 404, "message": "no route"}, status=404)
            return
        if callable(spec):
            spec = spec(body)
        raw = json.dumps(spec.get("payload") or {}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for cookie in spec.get("cookies") or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        self._respond(json.loads(self.rfile.read(length)) if length else {})

    def do_GET(self):
        self._respond({})


def check(name, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def reset():
    RESPONSES.clear()
    RECORD.clear()
    QUERY_STATE.clear()
    qr_login.SESSIONS.clear()
    qr_login._DAILY.clear()
    wire_routes()


def raises(fn, fragment):
    try:
        fn()
    except qr_login.QrLoginError as exc:
        return fragment in str(exc), str(exc)
    return False, "没有抛 QrLoginError"


def app_create(_body):
    return {"payload": {"retcode": 0, "data": {"url": "https://qr.example/app", "ticket": "T-APP"}}}


def web_create(_body):
    return {"payload": {"retcode": 0, "data": {"url": "https://qr.example/web", "ticket": "T-WEB"}}}


def app_query(body):
    status, extra = QUERY_STATE.get(body.get("ticket"), ("Created", {}))
    data = {"status": status}
    data.update(extra)
    return {"payload": {"retcode": 0, "data": data}}


def web_query(body):
    status, cookies = QUERY_STATE.get(body.get("ticket"), ("Created", []))
    return {"payload": {"retcode": 0, "data": {"status": status}}, "cookies": cookies}


def lt_exchange(_body):
    return {"payload": {"retcode": 0, "data": {"ltoken": "LT-2", "ltuid": "12345"}}}


def cookie_info(_body):
    return {"payload": {"retcode": 0, "data": {"cookie_token": "COOKIE-2", "uid": "12345"}}}


def roles(_body):
    return {"payload": {"retcode": 0, "data": {"list": [
        {"game_biz": "hk4e_cn", "game_uid": "100000001", "nickname": "旅行者"},
        {"game_biz": "hkrpg_cn", "game_uid": "200000002"},
        {"game_biz": "bh3_cn", "game_uid": "999999999"}]}}}


def wire_routes():
    RESPONSES.update({APP_CREATE: app_create, WEB_CREATE: web_create,
                      APP_QUERY: app_query, WEB_QUERY: web_query,
                      LTOKEN: lt_exchange, COOKIE_INFO: cookie_info, ROLES: roles})


def settings_for(**mys):
    base = {"ck": ["ltuid=GLOBAL; ltoken=GLOBAL"], "uid": "100000000"}
    base.update(mys)
    return commands.settings_from({
        "self_account": "10001",
        "groups": {"123456789": {"enabled": True}},
        "slash_commands": {"enabled": True, "miyoushe": base},
    })


# ---------------------------------------------------------------- user_store


def test_user_store(tmp):
    qq = "123456789"
    check("store: 非法 QQ 保存被拒", _value_error(lambda: user_store.save_user(
        "../x", {"auth": {"ltoken": "1"}})))
    check("store: 非法 QQ 读取返回 None", user_store.load_user("12/34") is None)
    check("store: 空 auth 拒绝保存", _value_error(lambda: user_store.save_user(
        qq, {"auth": {}})))

    record = {"qq": qq, "bound_at": 1760000000, "device_id": "dev1",
              "auth": {"ltuid": "1", "ltoken": "LT-X", "cookie_token": "CK-X",
                       "stoken": "ST-X", "evil": "x"},
              "uids": {"genshin": ["100000001"], "starrail": [], "zzz": []},
              "active_uid": {"genshin": "100000001"}}
    user_store.save_user(qq, record)
    path = tmp / f"{qq}.json"
    raw = path.read_text(encoding="utf-8")
    check("store: 文件落在 users/<qq>.json", path.is_file())
    check("store: auth 加密落盘（明文不出现）",
          "LT-X" not in raw and "ST-X" not in raw and "CK-X" not in raw)
    check("store: auth_encrypted 标记", json.loads(raw).get("auth_encrypted") is True)
    loaded = user_store.load_user(qq)
    check("store: 往返解密", loaded["auth"]["ltoken"] == "LT-X"
          and loaded["auth"]["stoken"] == "ST-X")
    check("store: ck_string 拼装",
          user_store.ck_string(loaded) == "ltuid=1; ltoken=LT-X; cookie_token=CK-X; stoken=ST-X",
          user_store.ck_string(loaded))
    check("store: active_uid 直读", user_store.active_uid(loaded) == "100000001")
    check("store: active_uid 缺省退回唯一 uid",
          user_store.active_uid({"uids": {"genshin": ["777"]}, "active_uid": {}}) == "777")
    check("store: 未知字段不落盘", "evil" not in loaded["auth"])
    check("store: delete 后读不到", user_store.delete_user(qq)
          and user_store.load_user(qq) is None)
    check("store: 重复 delete 返回 False", not user_store.delete_user(qq))

    path.write_text("{not json", encoding="utf-8")
    check("store: 损坏文件按未绑定处理", user_store.load_user(qq) is None)
    path.write_text(json.dumps({"qq": qq, "auth": "bm90LWRwYXBp"}), encoding="utf-8")
    check("store: 解密失败按未绑定处理", user_store.load_user(qq) is None)
    path.unlink()


def _value_error(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


# ---------------------------------------------------------------- 扫码流程


def test_app_stream_happy(tmp):
    reset()
    QUERY_STATE["T-APP"] = ("Confirmed", {"stoken": "STOKEN-1"})
    session = qr_login.start_login(settings_for(), "222333444", 123456789)
    check("app: 会话建在 app 流", session["stream"] == "app" and session["ticket"] == "T-APP")
    check("app: 二维码内容来自 createQRLogin", session["url"] == "https://qr.example/app")
    check("app: 会话表登记", qr_login.SESSIONS.get("222333444") is session)

    notices = qr_login.poll_once()
    check("app: Confirmed 触发成功通知", len(notices) == 1
          and "绑定成功" in notices[0][2], str(notices))
    check("app: 通知带群号与 QQ", notices[0][0] == "123456789" and notices[0][1] == "222333444")
    check("app: 会话出清", "222333444" not in qr_login.SESSIONS)

    record = user_store.load_user("222333444")
    check("app: 凭证落盘", record is not None and record["auth"].get("stoken") == "STOKEN-1")
    check("app: stoken 换到 LToken", record["auth"].get("ltoken") == "LT-2"
          and record["auth"].get("ltuid") == "12345")
    check("app: uid 发现（原神/星铁，崩三忽略）",
          record["uids"].get("genshin") == ["100000001"]
          and record["uids"].get("starrail") == ["200000002"]
          and "999999999" not in json.dumps(record["uids"]))
    check("app: active_uid 取首个", record["active_uid"].get("genshin") == "100000001")
    check("app: 审计日志不出现凭证",
          all(secret not in qr_login.LOG.read_text(encoding="utf-8")
              for secret in ("STOKEN-1", "LT-2", "WEB-COOKIE")))


def test_web_fallback(tmp):
    reset()
    RESPONSES[APP_CREATE] = lambda _b: {"payload": {"retcode": 500, "message": "app down"}}
    QUERY_STATE["T-WEB"] = ("Confirmed", COOKIE_FIELDS)
    session = qr_login.start_login(settings_for(), "222333445", 123456789)
    check("web: app 流失败自动回退", session["stream"] == "web" and session["ticket"] == "T-WEB")
    notices = qr_login.poll_once()
    check("web: Set-Cookie 取到凭据", "绑定成功" in notices[0][2], str(notices))
    record = user_store.load_user("222333445")
    auth = (record or {}).get("auth") or {}
    check("web: 五个 Cookie 字段全收",
          all(auth.get(k) for k in ("ltuid", "ltoken", "cookie_token",
                                    "account_id", "account_mid_v2")), str(auth))
    check("web: 已有 ltoken 不再换", "LT-2" not in json.dumps(auth))


def test_body_cookie(_tmp):
    auth = qr_login._extract_auth(
        {"data": {"status": "Confirmed", "cookie": {"ltuid": "1", "ltoken": "2"}}}, None)
    check("解析: 响应体 cookie 字典兜底", auth == {"ltuid": "1", "ltoken": "2"}, str(auth))
    auth2 = qr_login._extract_auth(
        {"data": {"status": "Confirmed", "cookie": "ltuid=3; ltoken=4; Path=/"}}, None)
    check("解析: 响应体 cookie 字符串兜底", auth2 == {"ltuid": "3", "ltoken": "4"}, str(auth2))
    check("解析: 未知字段丢弃",
          qr_login._extract_auth({"data": {"cookie": {"evil": "x"}}}, None) == {})


def test_waiting_and_expire(tmp):
    reset()
    QUERY_STATE["T-APP"] = ("Scanned", {})
    qr_login.start_login(settings_for(), "222333446", 123456789)
    check("waiting: Scanned 不产生通知", qr_login.poll_once() == [])
    check("waiting: 会话保留", "222333446" in qr_login.SESSIONS)
    session = qr_login.SESSIONS["222333446"]
    notices = qr_login.poll_once(now=session["expire_at"] + 1)
    check("expire: 过期通知", len(notices) == 1 and "过期" in notices[0][2])
    check("expire: 会话出清", "222333446" not in qr_login.SESSIONS)


def test_retcode_1034(tmp):
    reset()
    QUERY_STATE["T-APP"] = ("Created", {})
    qr_login.start_login(settings_for(), "222333447", 123456789)
    RESPONSES[APP_QUERY] = lambda _b: {"payload": {"retcode": 1034, "message": "captcha"}}
    notices = qr_login.poll_once()
    check("retcode 1034: 明确告知验证码", len(notices) == 1 and "验证码" in notices[0][2])
    check("retcode 1034: 会话出清", "222333447" not in qr_login.SESSIONS)


def test_limits(tmp):
    reset()
    qr_login.start_login(settings_for(), "222333448", 123456789)
    ok, msg = raises(lambda: qr_login.start_login(
        settings_for(), "222333448", 123456789), "进行中")
    check("limit: 同人同时只允许 1 个 pending", ok, msg)
    qr_login.SESSIONS.clear()

    qq = "222333450"
    limited = settings_for(qr_login={"daily_limit": 1})
    qr_login.start_login(limited, qq, 123456789)
    qr_login.SESSIONS.clear()
    ok, msg = raises(lambda: qr_login.start_login(limited, qq, 123456789), "次数用完")
    check("limit: daily_limit 配置生效（1 次后拒绝）", ok, msg)

    qq = "222333453"
    for _ in range(5):
        qr_login.start_login(settings_for(), qq, 123456789)
        qr_login.SESSIONS.clear()
    ok, msg = raises(lambda: qr_login.start_login(settings_for(), qq, 123456789), "次数用完")
    check("limit: 默认每天 5 次后拒绝", ok, msg)

    bound = "222333451"
    user_store.save_user(bound, {"auth": {"ltuid": "1", "ltoken": "2"},
                                 "bound_at": int(time.time())})
    ok, msg = raises(lambda: qr_login.start_login(settings_for(), bound, 123456789), "已经绑定")
    check("limit: 绑定后 24h 内拒绝重复扫码", ok, msg)
    user_store.save_user(bound, {"auth": {"ltuid": "1", "ltoken": "2"},
                                 "bound_at": int(time.time()) - 25 * 3600})
    check("limit: 超过 24h 允许重新扫码续期",
          qr_login.start_login(settings_for(), bound, 123456789)["ticket"] == "T-APP")

    ok, msg = raises(lambda: qr_login.start_login(settings_for(), "abc", 1), "QQ")
    check("limit: 非法 QQ 拒绝", ok, msg)


def test_network_resilience(_tmp):
    """网络抖动不是用户的错：会话保留、不上报、恢复后继续（#41-D1）。"""
    reset()
    QUERY_STATE["T-APP"] = ("Created", {})
    qq = "222333475"
    qr_login.start_login(settings_for(), qq, 123456789)
    real_post = qr_login._post

    def dead(*_a, **_k):
        raise qr_login.QrLoginError("米游社登录服务无响应，稍后再试", code="network")

    qr_login._post = dead
    try:
        notices = qr_login.poll_once()
    finally:
        qr_login._post = real_post
    check("network: 抖动不上报、不打扰用户", notices == [], str(notices))
    check("network: 抖动不清会话", qq in qr_login.SESSIONS)
    QUERY_STATE["T-APP"] = ("Confirmed", {"stoken": "S-NET"})
    notices = qr_login.poll_once()
    check("network: 恢复后照常完成绑定",
          len(notices) == 1 and "绑定成功" in notices[0][2], str(notices))


def test_save_failure(_tmp):
    """落盘失败要立刻告诉用户，而不是每 2 秒静默重试到过期（#41-D3）。"""
    reset()
    QUERY_STATE["T-APP"] = ("Confirmed", {"stoken": "S-SAVE"})
    qq = "222333476"
    qr_login.start_login(settings_for(), qq, 123456789)
    real_save = user_store.save_user

    def disk_full(*_a, **_k):
        raise OSError("disk full")

    user_store.save_user = disk_full
    try:
        notices = qr_login.poll_once()
    finally:
        user_store.save_user = real_save
    check("save: 失败有明确文案", len(notices) == 1 and "保存失败" in notices[0][2], str(notices))
    check("save: 失败后会话出清（不静默重试）", qq not in qr_login.SESSIONS)


def test_daily_persisted(_tmp):
    """额度落盘：守护重启不清零（#41-E1）。"""
    reset()
    qq = "222333477"
    limited = settings_for(qr_login={"daily_limit": 1})
    qr_login.start_login(limited, qq, 123456789)
    qr_login.SESSIONS.clear()
    qr_login._DAILY.clear()                        # 模拟进程重启：内存丢
    qr_login._DAILY.update(qr_login._load_daily())  # 从磁盘恢复
    ok, msg = raises(lambda: qr_login.start_login(limited, qq, 123456789), "次数用完")
    check("limit: 额度落盘，重启不清零", ok, msg)


def test_discard_session(_tmp):
    """二维码没发出去 → 撤销会话并退还额度（#41-E2）。"""
    reset()
    qq = "222333478"
    limited = settings_for(qr_login={"daily_limit": 1})
    qr_login.start_login(limited, qq, 123456789)
    check("discard: 撤销成功", qr_login.discard_session(qq) is True)
    check("discard: 会话出清", qq not in qr_login.SESSIONS)
    check("discard: 未知会话返回 False", qr_login.discard_session(qq) is False)
    ticket = qr_login.start_login(limited, qq, 123456789)["ticket"]
    check("discard: 额度已退还，可重新发起", ticket == "T-APP", ticket)


def test_poller_switch(_tmp):
    """enabled 热重载立即生效，不需要重启栈（#41-D2）。"""
    reset()
    qr_login.start_login(settings_for(), "222333479", 123456789)
    cfg = {"enabled": False}
    calls = []
    real_poll = qr_login.poll_once

    def fake_poll(*_a, **_k):
        calls.append(1)
        return []

    qr_login.poll_once = fake_poll
    qr_login.start_poller(lambda *_a: None, cfg_getter=lambda: cfg, interval=0.02)
    time.sleep(0.15)
    off = len(calls)
    cfg["enabled"] = True
    time.sleep(0.15)
    on = len(calls)
    cfg["enabled"] = False
    time.sleep(0.05)
    qr_login.poll_once = real_poll
    check("poller: enabled=False 时整轮空转", off == 0, f"{off}")
    check("poller: 热重载开启后开始轮询", on > 0, f"{on}")
    qr_login.SESSIONS.clear()


def test_renew(_tmp):
    """惰性续期：半个月用 stoken 换新 LToken，不动 bound_at（#41-C）。"""
    reset()
    qq = "222333482"
    user_store.save_user(qq, {"auth": {"stoken": "S-OLD", "ltoken": "LT-OLD", "ltuid": "1"},
                              "bound_at": 1760000000, "device_id": "dev",
                              "last_renew_at": 1760000000})
    old = user_store.load_user(qq)
    check("renew: 未到间隔不触发",
          qr_login.should_renew(old, now=1760000000 + 86400) is False)
    check("renew: 到期该触发",
          qr_login.should_renew(old, now=1760000000 + 16 * 86400) is True)
    check("renew: 换票成功", qr_login.renew(qq, now=1760000000 + 16 * 86400) is True)
    record = user_store.load_user(qq)
    check("renew: 新 LToken 落盘", record["auth"]["ltoken"] == "LT-2", str(record.get("auth")))
    check("renew: bound_at 不变（不触发 24h 冷却）", record["bound_at"] == 1760000000)
    check("renew: last_renew_at 刷新", record["last_renew_at"] == 1760000000 + 16 * 86400)

    user_store.save_user("222333483", {"auth": {"ltoken": "X"}, "bound_at": 1760000000})
    check("renew: 没有 stoken 跳过（Web 流只能重扫）", qr_login.renew("222333483") is False)


def test_mark_invalid(_tmp):
    """凭证失效提醒：24h 内最多一次（#41-C）。"""
    reset()
    qq = "222333484"
    user_store.save_user(qq, {"auth": {"ltoken": "X"}, "bound_at": 1760000000})
    check("invalid: 首次提醒", qr_login.mark_invalid(qq, now=1760000000) is True)
    check("invalid: 24h 内不重复提醒", qr_login.mark_invalid(qq, now=1760000000 + 3600) is False)
    check("invalid: 隔天再提醒", qr_login.mark_invalid(qq, now=1760000000 + 90000) is True)


def test_ck_invalid_notify(_tmp):
    """10001 触发失效提醒；别的错误码不打扰用户。"""
    reset()
    qq = "222333485"
    user_store.save_user(qq, {"auth": {"ltoken": "X", "ltuid": "1"},
                              "bound_at": 1760000000, "last_renew_at": int(time.time())})

    def boom(_args, _ctx):
        raise miyoushe.MiyousheError("CK 已失效", code=10001)

    def boom_quota(_args, _ctx):
        raise miyoushe.MiyousheError("今日查询次数已达上限", code=10101)

    real = dict(commands.HANDLERS)
    commands.HANDLERS["坏探针"] = ("坏探针", boom)
    commands.HANDLERS["限额探针"] = ("限额探针", boom_quota)
    sent = []
    real_notify = commands.notify_login
    commands.notify_login = lambda _s, gid, target, text: sent.append((gid, target, text))
    try:
        S = settings_for()
        event = {"user_id": qq, "group_id": 123456789, "message_id": 9}
        out = commands.resolve_reply("坏探针", S, event, "")
        check("invalid: 文案照旧回给用户", out == "CK 已失效", out)
        check("invalid: 主动提醒本人重扫",
              len(sent) == 1 and "失效" in sent[0][2] and sent[0][1] == qq, str(sent))
        commands.resolve_reply("坏探针", S, event, "")
        check("invalid: 同一天第二次不再提醒", len(sent) == 1, str(sent))
        commands.resolve_reply("限额探针", S, event, "")
        check("invalid: 10101 限额不触发失效提醒", len(sent) == 1, str(sent))
    finally:
        commands.HANDLERS = real
        commands.notify_login = real_notify


def test_app_confirmed_tokens(tmp):
    """App 流 Confirmed 的真实形状：user_info + tokens[token_type=1]（#41 实机回归）。

    2026-10-23 07:17 真机扫码：Scanned → Confirmed 后回「没拿到登录凭证」，
    因为原实现只按字段名找 `stoken`，而 App 流把它放在 tokens 数组里。
    """
    reset()
    qq = "222333486"
    QUERY_STATE["T-APP"] = ("Confirmed", {
        "user_info": {"aid": "123456789", "mid": "MID-XYZ"},
        "tokens": [{"token_type": 2, "token": "S-OTHER"},
                   {"token_type": 1, "token": "S-APP-REAL"}],
    })
    qr_login.start_login(settings_for(), qq, 123456789)
    notices = qr_login.poll_once()
    check("app 流: 扫码后能绑定（不再 no-credential）",
          len(notices) == 1 and "绑定成功" in notices[0][2], str(notices))
    record = user_store.load_user(qq) or {}
    auth = record.get("auth") or {}
    check("app 流: token_type=1 才是 stoken", auth.get("stoken") == "S-APP-REAL", str(auth))
    check("app 流: user_info.mid → account_mid_v2",
          auth.get("account_mid_v2") == "MID-XYZ", str(auth))
    check("app 流: stoken 换到 ltoken", auth.get("ltoken") == "LT-2", str(auth))
    # 07:22 回归：绑定「成功」了但取数回 CK 失效——App 流只给 stoken，
    # ltoken/cookie_token 都得换回来，否则 CK 串不完整。
    check("app 流: cookie_token 也补上", auth.get("cookie_token") == "COOKIE-2", str(auth))
    check("app 流: CK 串字段齐全",
          all(auth.get(k) for k in ("ltuid", "stuid", "ltoken", "cookie_token")), str(auth))

    exchange = [r for r in RECORD if r["path"] in (LTOKEN, COOKIE_INFO)]
    check("换票: 两个接口都是 GET（POST 会 405）",
          len(exchange) == 2 and all(r["method"] == "GET" for r in exchange),
          str([(r["path"], r["method"]) for r in RECORD]))
    check("换票: Cookie 带 stoken/mid/stuid",
          all("stoken=S-APP-REAL" in r["headers"].get("cookie", "")
              and "mid=MID-XYZ" in r["headers"].get("cookie", "")
              and "stuid=123456789" in r["headers"].get("cookie", "")
              for r in exchange), str([r["headers"].get("cookie") for r in exchange]))
    roles_req = [r for r in RECORD if r["path"] == ROLES]
    check("uid 发现: 用 Cookie 版 roles 接口（ByStoken 已废）",
          len(roles_req) == 1 and "ltoken=LT-2" in roles_req[0]["headers"].get("cookie", "")
          and not any("ByStoken" in r["path"] for r in RECORD),
          str([(r["path"], r["headers"].get("cookie")) for r in RECORD]))

    shape = qr_login._shape_of({"retcode": 0, "data": {
        "status": "Confirmed", "tokens": [1, 2]}})
    check("诊断: shape 只含键名不含值",
          "Confirmed" not in shape and "tokens[2]" in shape, shape)


def test_derive_ck_degrade(tmp):
    """换票失败不能挡住绑定：拿不到 ltoken/cookie_token 也先存 stoken（§7.1 降级）。

    取数层随后会回 1001/10001/10103，走 #41-C 的「凭证失效 → 重新扫码」文案，
    这比扫码当场报错更好定位（用户确实扫成功了）。
    """
    reset()
    qq = "222333487"
    QUERY_STATE["T-APP"] = ("Confirmed", {
        "user_info": {"aid": "123456789", "mid": "MID-DEG"},
        "tokens": [{"token_type": 1, "token": "S-DEG"}]})
    real_get = qr_login._get

    def dead(*_a, **_k):
        raise qr_login.QrLoginError("米游社登录服务无响应，稍后再试", code="network")

    qr_login._get = dead
    try:
        qr_login.start_login(settings_for(), qq, 123456789)
        notices = qr_login.poll_once()
    finally:
        qr_login._get = real_get
    check("降级: 换票失败仍完成绑定",
          len(notices) == 1 and "绑定成功" in notices[0][2], str(notices))
    record = user_store.load_user(qq) or {}
    auth = record.get("auth") or {}
    check("降级: 只落了 stoken", auth.get("stoken") == "S-DEG"
          and not auth.get("ltoken") and not auth.get("cookie_token"), str(auth))
    check("降级: uid 空表（走手动带参）",
          not (record.get("active_uid") or {}).get("genshin"), str(record.get("active_uid")))
    check("降级: 审计记录换票失败",
          "ltoken-exchange failed" in qr_login.LOG.read_text(encoding="utf-8"))


def test_qr_image(_tmp):
    seg = qr_login.qr_image_segment("https://qr.example/app")
    check("qr: 图片段形状", seg["type"] == "image" and seg["data"]["file"].startswith("base64://"))
    raw = base64.b64decode(seg["data"]["file"][len("base64://"):])
    check("qr: 输出是 PNG", raw[:8] == b"\x89PNG\r\n\x1a\n")


def test_bind_uid(tmp):
    check("bind: 未扫码先提示 #登录",
          "先发 #登录" in qr_login.bind_uid("222333480", "100000001"))
    user_store.save_user("222333480", {"auth": {"stoken": "S", "ltuid": "1"},
                                       "bound_at": int(time.time()) - 30 * 3600,
                                       "uids": {}, "active_uid": {}})
    check("bind: 格式不对回用法",
          "用法" in qr_login.bind_uid("222333480", "12"))
    check("bind: 只有游戏没有 UID 也回用法",
          "用法" in qr_login.bind_uid("222333480", "原神"))

    out = qr_login.bind_uid("222333480", "100000001")
    check("bind: 本人账号下的 UID 绑定成功", "已绑定原神 100000001" in out, out)
    record = user_store.load_user("222333480")
    check("bind: active_uid 落盘", record["active_uid"].get("genshin") == "100000001")
    check("bind: uids 落盘", "100000001" in (record["uids"].get("genshin") or []))
    check("bind: 绑定后 #体力 用的就是它", user_store.active_uid(record) == "100000001")

    out = qr_login.bind_uid("222333480", "200000002")
    check("bind: 别的游戏的 UID 提示加前缀", "这是星铁的 UID" in out, out)
    out = qr_login.bind_uid("222333480", "星铁 200000002")
    check("bind: 星铁指令未接入，拒绝绑定", "还没接入" in out, out)
    record = user_store.load_user("222333480")
    check("bind: 被拒后不写 active_uid",
          record["active_uid"].get("genshin") == "100000001"
          and "starrail" not in (record["active_uid"] or {}), str(record.get("active_uid")))
    out = qr_login.bind_uid("222333480", "绝区零 10123456")
    check("bind: 绝区零同样拒绝（uid 消费链路未接）", "还没接入" in out, out)

    out = qr_login.bind_uid("222333480", "123456789")
    check("bind: 不属于该账号的 UID 被拒", "不在你扫码的米游社账号下" in out, out)
    check("bind: 被拒后 active 不变",
          user_store.load_user("222333480")["active_uid"].get("genshin") == "100000001")

    user_store.save_user("222333481", {"auth": {"ltuid": "9", "ltoken": "9"},
                                       "bound_at": int(time.time()) - 30 * 3600})
    check("bind: 没有 stoken 时接受未校验绑定",
          "已绑定原神 100000001" in qr_login.bind_uid("222333481", "100000001"))


def test_status_and_unbind(tmp):
    check("status: 未绑定提示", "还没有绑定" in qr_login.status_text("222333452", now=0))
    user_store.save_user("222333452", {
        "auth": {"ltuid": "1", "ltoken": "2"}, "bound_at": 1760000000,
        "uids": {"genshin": ["100000001"]}, "active_uid": {"genshin": "100000001"}})
    check("status: 30s 内重复查询走冷却",
          "刚查过" in qr_login.status_text("222333452", now=10))
    text = qr_login.status_text("222333452", now=31)
    check("status: 绑定信息", "尾号 3452" in text and "原神 100000001" in text, text)

    # 07:37 实机三轮 #解绑 都停在提示：文案说「再发一次 #解绑」，代码却只认「确认」。
    first = qr_login.unbind("222333452", "", now=100)
    check("unbind: 首次只提示不删",
          "确认" in first and user_store.load_user("222333452") is not None, first)
    check("unbind: 窗口内再发一次就真删",
          "已解绑" in qr_login.unbind("222333452", "", now=130)
          and user_store.load_user("222333452") is None)
    check("unbind: 未绑定再解绑", "还没有绑定" in qr_login.unbind("222333452", "", now=200))

    # 显式词仍可一步到位；窗口过期则要重新提示（防误触不能失效）
    user_store.save_user("222333453", {"auth": {"ltuid": "1", "ltoken": "2"},
                                       "bound_at": 1760000000})
    check("unbind: 显式 #解绑 确认 一步删",
          "已解绑" in qr_login.unbind("222333453", " 确认 ")
          and user_store.load_user("222333453") is None)
    user_store.save_user("222333454", {"auth": {"ltuid": "1", "ltoken": "2"},
                                       "bound_at": 1760000000})
    qr_login.unbind("222333454", "", now=1000)
    check("unbind: 超过窗口要重新提示",
          "确认" in qr_login.unbind("222333454", "", now=1000 + qr_login.UNBIND_WINDOW + 1)
          and user_store.load_user("222333454") is not None)
    check("unbind: 未绑定时不残留待确认",
          "还没有绑定" in qr_login.unbind("222333452", "", now=300))


# ---------------------------------------------------------------- commands 接线


def test_commands_privacy(tmp):
    user_store.save_user("222333460", {
        "auth": {"ltuid": "U", "ltoken": "USER-LT"}, "bound_at": int(time.time()) - 30 * 3600,
        "uids": {"genshin": ["100000009"]}, "active_uid": {"genshin": "100000009"}})

    def probe(_args, ctx):
        return json.dumps({"ck": ctx["ck"], "uid": ctx["uid"]}, ensure_ascii=False)

    def free_probe(_args, ctx):
        """模拟 need_ck=False 的免登录 handler（#公告 那一类）。"""
        return json.dumps({"ck": ctx["ck"], "uid": ctx["uid"]}, ensure_ascii=False)
    free_probe.need_ck = False

    real = dict(commands.HANDLERS)
    commands.HANDLERS["探针"] = ("ctx 探针", probe)
    commands.HANDLERS["免登探针"] = ("免登探针", free_probe)
    try:
        S = settings_for(admin_qq=["999999"])
        unbound = commands.resolve_reply(
            "探针", S, {"user_id": 222333461, "group_id": 1}, "")
        check("privacy: 未绑定不再回落全局 CK", unbound == commands.BIND_HINT, unbound)

        free = json.loads(commands.resolve_reply(
            "免登探针", S, {"user_id": 222333461, "group_id": 1}, ""))
        check("privacy: 未绑定 + 免登录 handler 不拦截", free == {"ck": [], "uid": ""}, str(free))

        FREE_KEYS = ("公告", "活动", "资讯", "兑换码", "米游社", "攻略", "抽卡记录")
        CK_KEYS = ("探索", "深渊", "角色", "武器", "体力", "树脂", "原石", "札记",
                   "绝区零体力", "七圣召唤")
        check("privacy: 免登录指令都标了 need_ck=False",
              all(getattr(commands.HANDLERS[k][1], "need_ck", None) is False
                  for k in FREE_KEYS if k in commands.HANDLERS),
              str([k for k in FREE_KEYS if k in commands.HANDLERS
                   and getattr(commands.HANDLERS[k][1], "need_ck", None) is not False]))
        check("privacy: 账号类指令保持 need_ck=True（缺省即拦截）",
              all(getattr(commands.HANDLERS[k][1], "need_ck", True) is True
                  for k in CK_KEYS if k in commands.HANDLERS))

        bound = json.loads(commands.resolve_reply(
            "探针", S, {"user_id": 222333460, "group_id": 1}, ""))
        check("privacy: 绑定用户用自己的 CK",
              bound["ck"] == ["ltuid=U; ltoken=USER-LT"] and bound["uid"] == "100000009", str(bound))

        admin = json.loads(commands.resolve_reply(
            "探针", S, {"user_id": 999999, "group_id": 1}, ""))
        check("privacy: admin_qq 白名单用全局 CK", admin["ck"] == ["ltuid=GLOBAL; ltoken=GLOBAL"])

        test_path = json.loads(commands.resolve_reply("探针", S, None, ""))
        check("privacy: --test 合成事件用全局 CK", test_path["ck"] == ["ltuid=GLOBAL; ltoken=GLOBAL"])
        zero = json.loads(commands.resolve_reply("探针", S, {"user_id": 0}, ""))
        check("privacy: user_id=0 视作测试口", zero["ck"] == ["ltuid=GLOBAL; ltoken=GLOBAL"])

        # 凭证链路在宿主里（issue #4 PA）：user_store 的落点是 plugins.host。
        import plugins.host as plugin_host
        saved = plugin_host.user_store
        plugin_host.user_store = None
        try:
            fallback = json.loads(commands.resolve_reply(
                "探针", S, {"user_id": 222333461, "group_id": 1}, ""))
            check("privacy: 存储模块缺失时退回旧行为（不掐死取数）",
                  fallback["ck"] == ["ltuid=GLOBAL; ltoken=GLOBAL"])
        finally:
            plugin_host.user_store = saved
    finally:
        commands.HANDLERS = real


def test_commands_login_replies(tmp):
    S = settings_for()
    event = {"user_id": 222333470, "group_id": 123456789, "message_id": 1}
    reply = commands.resolve_reply("登录", S, event, "")
    check("commands: #登录 回二维码段",
          isinstance(reply, dict) and reply["image"]["type"] == "image"
          and reply["image"]["data"]["file"].startswith("base64://")
          and "5 分钟" in reply["text"], str(reply)[:120])
    check("commands: #登录 建了会话", "222333470" in qr_login.SESSIONS)
    check("commands: #登录状态 可用",
          "还没有绑定" in commands.resolve_reply("登录状态", S, event, ""))
    # 07:37 实机死循环回归：文案让「再发一次 #解绑」，那第二次就必须真的解掉。
    user_store.save_user("222333470", {"auth": {"ltuid": "1", "ltoken": "2"},
                                       "bound_at": int(time.time()) - 30 * 3600})
    qr_login._UNBIND_AT.pop("222333470", None)
    first = commands.resolve_reply("解绑", S, event, "")
    second = commands.resolve_reply("解绑", S, event, "")
    check("commands: #解绑 第一次只提示",
          "确认" in first and "已解绑" not in first, first)
    check("commands: #解绑 第二次真的解掉",
          "已解绑" in second and user_store.load_user("222333470") is None, second)
    check("commands: #帮助 列出登录指令",
          "#登录" in commands.resolve_reply("帮助", S)
          and "#绑定" in commands.resolve_reply("帮助", S))
    check("commands: #绑定 未扫码提示先登录",
          "先发 #登录" in commands.resolve_reply(
              "绑定", S, {"user_id": 222333471, "group_id": 123456789}, "100000001"))
    check("commands: 合成事件不能绑定",
          "群友本人" in commands.resolve_reply("绑定", S, {"user_id": 0}, "100000001"))
    check("commands: 合成事件不能扫码",
          "群友本人" in commands.resolve_reply("登录", S, {"user_id": 0}, ""))
    disabled = settings_for(qr_login={"enabled": False})
    check("commands: qr_login.enabled=false 时拒绝",
          "未开启" in commands.resolve_reply("登录", disabled, event, ""))

    segs = commands.segments_for({"text": "扫我", "image": "base64://aa"}, 7)
    check("commands: 二维码消息段顺序 reply/image/text",
          [s["type"] for s in segs] == ["reply", "image", "text"])
    check("commands: 纯文本消息段不变",
          [s["type"] for s in commands.segments_for("pong", 7)] == ["reply", "text"])


def _seg_text(segs):
    for seg in segs or []:
        if isinstance(seg, dict) and seg.get("type") == "text":
            return str((seg.get("data") or {}).get("text") or "")
    return ""


def test_commands_dispatch_unbind_cooldown(tmp):
    # issue #66：#解绑 两步确认不能被 5s 冷却闸门吃掉，且冷却不再跨用户串台。
    user_store.save_user("222333490", {"auth": {"ltuid": "1", "ltoken": "2"},
                                       "bound_at": int(time.time()) - 30 * 3600})
    qr_login._UNBIND_AT.pop("222333490", None)
    S = settings_for()
    sent = []
    real_send = commands.send_group_msg
    commands.send_group_msg = lambda s, g, segs: (sent.append((g, segs)) or {"data": {"message_id": 1}})
    try:
        state = commands.new_state()
        ev1 = {"post_type": "message", "message_type": "group",
               "group_id": 123456789, "user_id": 222333490,
               "message_id": 9001, "message": [{"type": "text", "data": {"text": "#解绑"}}]}
        ev2 = dict(ev1, message_id=9002)
        r1 = commands.dispatch(ev1, S, set(), state)
        r2 = commands.dispatch(ev2, S, set(), state)  # 间隔 < 5s，修复前会被静默丢弃
    finally:
        commands.send_group_msg = real_send
    check("dispatch #66: 第一次只提示",
          r1 is True and "确认" in _seg_text(sent[0][1]) and "已解绑" not in _seg_text(sent[0][1]),
          str(sent[:1]))
    check("dispatch #66: 第二次真正解绑（<5s 也不被冷却吃掉）",
          r2 is True and len(sent) >= 2 and "已解绑" in _seg_text(sent[1][1])
          and user_store.load_user("222333490") is None, str(sent))
    check("dispatch #66: 提示文案说明立刻可发", "立刻发也行" in _seg_text(sent[0][1]))

    # 跨用户：A 的 #解绑 冷却不能掐掉 B 的 #体力（B 未绑定，走 BIND_HINT，不联网）
    qr_login._UNBIND_AT.pop("222333491", None)
    sent2 = []
    commands.send_group_msg = lambda s, g, segs: (sent2.append((g, segs)) or {"data": {"message_id": 2}})
    try:
        state2 = commands.new_state()
        evA = {"post_type": "message", "message_type": "group",
               "group_id": 123456789, "user_id": 222333490,
               "message_id": 9011, "message": [{"type": "text", "data": {"text": "#解绑"}}]}
        evB = {"post_type": "message", "message_type": "group",
               "group_id": 123456789, "user_id": 222333491,
               "message_id": 9012, "message": [{"type": "text", "data": {"text": "#体力"}}]}
        commands.dispatch(evA, S, set(), state2)
        rB = commands.dispatch(evB, S, set(), state2)
    finally:
        commands.send_group_msg = real_send
    check("dispatch #66: 跨用户冷却不串台（B 的 #体力 不被 A 吃掉）",
          rB is True and len(sent2) == 2, str(sent2))


def test_settings(_tmp):
    S = settings_for(admin_qq="123", qr_login={"daily_limit": 0})
    mys = S["plugins"]["miyoushe"]["options"]
    check("settings: admin_qq 字符串归一成列表", mys["admin_qq"] == ["123"])
    check("settings: daily_limit=0 保留（不限流）", mys["qr_login"]["daily_limit"] == 0)
    check("settings: 缺省 qr_login 段",
          commands.settings_from({"slash_commands": {"miyoushe": {}}})
          ["plugins"]["miyoushe"]["options"]["qr_login"]
          == {"enabled": True, "daily_limit": 5, "session_ttl": 300})


def test_degraded_login(_tmp):
    """qr_login 导入失败时旧行为等价：四条指令仍在、回「暂不可用」（issue #4 PC）。

    子进程跑：``sys.modules['qr_login'] = None`` 让 import 直接失败，比 reload
    干净——不会把半改的模块状态留在本进程里。
    """
    daemon = str(ROOT / "water-group" / "daemon")
    code = (
        "import sys\n"
        "sys.modules['qr_login'] = None\n"
        f"sys.path.insert(0, {daemon!r})\n"
        "import commands\n"
        "assert commands.PLUGINS.load_errors == {}, commands.PLUGINS.load_errors\n"
        "keys = ['登录', '登录状态', '绑定', '解绑']\n"
        "assert all(k in commands.HANDLERS for k in keys), sorted(commands.HANDLERS)\n"
        "settings = commands.settings_from({})\n"
        "out = commands.resolve_reply('登录', settings, "
        "{'user_id': '1', 'group_id': '1'}, '')\n"
        "print('REPLY:' + str(out))\n"
        "print('HELP:' + ('OK' if all('#' + k in commands.help_text(settings) for k in keys) else 'MISSING'))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    check("降级：子进程正常退出", proc.returncode == 0, (proc.stderr or "")[-400:])
    check("降级：#登录 回「暂不可用」", "REPLY:登录功能暂不可用" in (proc.stdout or ""),
          (proc.stdout or "")[-200:])
    check("降级：四条指令仍在 #帮助", "HELP:OK" in (proc.stdout or ""), (proc.stdout or "")[-200:])


def main():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        real_users_dir = user_store.users_dir
        real_log = qr_login.LOG
        real_daily = qr_login.DAILY_FILE
        user_store.users_dir = lambda: tmp
        qr_login.LOG = tmp / "qr_login.log"
        qr_login.DAILY_FILE = tmp / "qr_login_daily.json"
        qr_login._DAILY.clear()          # 别把生产 ~/.water-group/state 的额度带进测试
        commands.LOG = tmp / "wg_commands.log"
        wire_routes()
        with fixture_server(FakePassport, ports=set()) as server:
            real_hosts = dict(qr_login.HOSTS)
            base = f"http://127.0.0.1:{server.server_port}"
            qr_login.HOSTS = {key: base for key in real_hosts}
            restrict, _calls = network_guard({server.server_port})
            from unittest.mock import patch
            try:
                with patch("socket.create_connection", restrict):
                    for fn in (test_user_store, test_app_stream_happy, test_web_fallback,
                               test_body_cookie, test_waiting_and_expire, test_retcode_1034,
                               test_limits, test_network_resilience, test_save_failure,
                               test_daily_persisted, test_discard_session, test_poller_switch,
                               test_qr_image, test_bind_uid, test_app_confirmed_tokens,
                               test_derive_ck_degrade,
                               test_renew, test_mark_invalid, test_ck_invalid_notify,
                               test_status_and_unbind, test_commands_privacy,
                               test_commands_login_replies, test_commands_dispatch_unbind_cooldown,
                                test_settings, test_degraded_login):
                        fn(tmp)
            finally:
                qr_login.HOSTS = real_hosts
                user_store.users_dir = real_users_dir
                qr_login.LOG = real_log
                qr_login.DAILY_FILE = real_daily
    print()
    if FAILED:
        print(f"{len(FAILED)} 项失败: {', '.join(FAILED)}")
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
