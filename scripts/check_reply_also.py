"""issue #302 gate: `reply.py --also` 不遮蔽外层事件 `ref`、不崩、两条都进账本。

症状：`water-group/daemon/reply.py` 里 `for ref in args.also:` 的循环变量与**外层事件
`Ref` 同名**。`--also` 存在时循环把外层 `ref` 遮蔽成 `args.also` 里最后一个元素的字符串，
循环结束后 `if ref` 恒真，`:893` 对字符串调 `ref.describe()` 抛
`AttributeError: 'str' object has no attribute 'describe'`——回一条连着多条的事一用
`--also` 就整条回复流程中断，连「还有 N 条没回」都打不出来。

本门禁钉三件事：

  1. **`--also` 不崩 + `target=` 行仍带 `src=`**：`run("1","正文","--also","2")`
     → `exit=0`、`target=1` 那行带 `ref.describe()` 的来源标记、`SENT`。这是回归点：
     修前这条会 `AttributeError`、`exit=1`。
  2. **无 `--also` 对照组**：`run("1","正文")` → 同样带 `src=`、`exit=0`。防反方向
     回归——把 `ref` 整个去掉、`src=` 消失时第 1 条照样绿，要这条钉住「src 必须还在」。
  3. **`--also` 两条都进账本**：`reply.py 1 ... --also 2` 发送成功后
     `wg_replied.jsonl` 有 1 和 2 的记账行（`answered = [target, *also_event]`，
     `record(answered, ...)` 一次记全）。防「`--also` 的目标事件被标记成 [待回]」。

真跑 `reply.py` 子进程 + loopback OneBot 假服务器 + 隔离数据根，不发任何真实消息：

    .venv/Scripts/python.exe scripts/check_reply_also.py
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_fixture_support  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import pin_state_env, scratch_dir  # noqa: E402

from fixture_server import FixtureHandler, fixture_server  # noqa: E402

REPLY = ROOT / "water-group" / "daemon" / "reply.py"

HOME = scratch_dir(prefix="wg_reply_also_")
STATE = HOME / "state"
STATE.mkdir(parents=True)
(HOME / "data").mkdir()
CONFIG = HOME / "config.json"
GROUP = "123"
SELF = "10001"

CALLS = []


class OneBot(FixtureHandler):
    """只认 `/send_group_msg`：别的路径被调用就是这次回复走错了出口。"""

    def do_POST(self):
        body = self.read_json()
        assert self.path == "/send_group_msg", self.path
        assert self.headers.get("Authorization") == f"Bearer {TOKEN}", "token 没带上"
        CALLS.append(body)
        self.send_json({"status": "ok", "retcode": 0,
                        "data": {"message_id": 4000 + len(CALLS)}})


TOKEN = "reply-also-fixture"
LIVE_URL = ""


def write_config(url):
    CONFIG.write_text(json.dumps({
        "self_account": SELF,
        "onebot_token": TOKEN,
        "onebot_http_url": url,
        "self_names": ["小鲸鱼"],
    }, ensure_ascii=False), encoding="utf-8")


def child_env():
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    pin_state_env(STATE, home=HOME, env=env)  # WATER_GROUP_HOME + state 两腿（#281）
    env["IDLEJOT_WG_DB"] = str(HOME / "data" / "mcp-access.sqlite3")
    env["IDLEJOT_WG_CONFIG"] = str(CONFIG)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                 "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        env.pop(name, None)
    return env


def run(*args):
    proc = subprocess.run([sys.executable, str(REPLY), *args],
                          cwd=str(ROOT), env=child_env(), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)
    return proc


def seed_events(count):
    """每条用例一个 event id，带 onebot_message_id。

    时间戳取「此刻」：过期唤醒会被 `reply.py` 默认拒绝，固定时间戳过几小时就红在
    「拒绝：id=N 是 … 前的唤醒」上——那是时钟问题，不是本门禁要测的东西。
    """
    now = int(time.time() * 1000)
    rows = [{"id": n, "sender": "群友甲", "content": f"@小鲸鱼 第 {n} 条",
             "timestamp": now, "is_self": False,
             "conversation_id": f"{SELF}:group:{GROUP}",
             "onebot_message_id": str(700000 + n)} for n in range(1, count + 1)]
    (STATE / "wg_events.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")


def ledger_ids():
    path = STATE / "wg_replied.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["id"] for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def build_fixtures():
    (STATE / "wg_session.txt").write_text("session-fixture", encoding="utf-8")
    seed_events(3)


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def also_存在不崩且带src():
    """回归点：修前 `--also` 让 `ref` 变字符串，`ref.describe()` 崩 AttributeError。"""
    CALLS.clear()
    proc = run("1", "连着回", "--also", "2", "--no-wait", "--no-followup")
    if proc.returncode != 0:
        return proc.stdout + proc.stderr
    ok = (proc.returncode == 0
          and "target=1 " in proc.stdout
          and "src=" in proc.stdout
          and "SENT" in proc.stdout)
    return ok or proc.stdout + proc.stderr


@case
def 无also对照组也带src():
    """反方向回归：`ref` 被整个删掉、`src=` 消失时，上一条照样绿，这条钉住。"""
    CALLS.clear()
    proc = run("1", "单回", "--no-wait", "--no-followup")
    if proc.returncode != 0:
        return proc.stdout + proc.stderr
    ok = (proc.returncode == 0
          and "target=1 " in proc.stdout
          and "src=" in proc.stdout
          and "SENT" in proc.stdout)
    return ok or proc.stdout + proc.stderr


@case
def also_两条都进账本():
    """`--also` 的目标事件不能被标记成 [待回]：`answered` 含 2，`record` 一次记全。"""
    CALLS.clear()
    proc = run("1", "连着回", "--also", "2", "--no-wait", "--no-followup")
    if proc.returncode != 0:
        return proc.stdout + proc.stderr
    ids = ledger_ids()
    ok = (1 in ids and 2 in ids)
    return ok or proc.stdout + proc.stderr


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    build_fixtures()
    bad = 0
    global LIVE_URL
    try:
        with fixture_server(OneBot) as server:
            LIVE_URL = f"http://127.0.0.1:{server.server_port}"
            write_config(LIVE_URL)
            for fn in CASES:
                try:
                    ok = fn()
                except AssertionError as exc:
                    ok = f"断言失败：{exc}"
                except Exception as exc:  # noqa: BLE001
                    ok = f"{type(exc).__name__}: {exc}"
                if ok is True:
                    print(f"✓ {fn.__name__}")
                else:
                    print(f"✗ {fn.__name__}: {ok}")
                    bad += 1
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
    print("全部通过" if not bad else f"{bad} 项失败")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())