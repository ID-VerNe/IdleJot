"""check_qr_login 包的夹具层（issue #282）。

从旧 `scripts/check_qr_login.py` 整体搬运，按 #169 PF-2（`scripts/check_listener_rehang/`）
与 #247（`scripts/check_dossier_cadence/`）的先例拆出。本模块只放 passport 替身、
7 条 route、`wire_routes` / `settings_for` / `reset` / `raises` helper 与模块级
状态（`RESPONSES` / `RECORD` / `QUERY_STATE` / `FAILED` / 7 个 URL 常量 /
`COOKIE_FIELDS`）。28 条 `test_*` 用例与 `main()` 留主模块 `__init__.py`。

与 `check_listener_rehang/_common.py` 同口径：包式门禁经
`python -m scripts.check_qr_login` 调起，此时 `sys.path[0]` 是仓库根（cwd）而非
`scripts/`——后者才是 `check_fixture_support` 所在地。无法先调
`bootstrap_paths`（它自己要 import fs），故手动把 `scripts/` 插进 `sys.path`，
再进口 `check_fixture_support`，随后 `bootstrap_paths()` 收口 root + daemon。
"""
import json
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 包式门禁经 `python -m scripts.check_qr_login` 调起，此时 `sys.path[0]` 是
# 仓库根（cwd）而非 `scripts/`——后者才是 `check_fixture_support` 所在地。无法先调
# `bootstrap_paths`（它自己要 import fs），故手动把 `scripts/` 插进 `sys.path`，
# 再进口 `check_fixture_support`，随后 `bootstrap_paths()` 收口 root + daemon。
_scripts = Path(__file__).resolve().parents[1]
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

from fixture_server import FixtureHandler, fixture_server  # noqa: E402
from check_fixture_support import network_guard, temp_dir  # noqa: E402

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
