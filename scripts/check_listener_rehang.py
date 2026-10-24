"""钉住监听空窗：退出要说下一步，过期唤醒不许静默消失（issue #69）。

2026-10-24 一晚上 `listen.py` 掉了 3 次。它按设计「有事就退出叫人」，但退出
之后没有任何东西保证下一次挂上：Agent 回消息、读图、写 issue 的几十秒里，
@ 落进文件而没人看。三次都是用户主动问「是不是没挂上」才发现的。

这条门禁钉四个契约，任何一处退回旧行为都红：

1. `listen.py` 的每种退出（WAKE / NO_WORK / WATCH_FAILED）都以一行
   `RESTART: <python> water-group/daemon/listen.py --timeout N` 收尾，且短等待
   （`reply.py --wait 6` 这类）不透传成新的常驻周期——重挂回到 300s。
2. `reply.py` 发送成功后默认就地续挂（`--wait` 默认 240，`--no-wait` 关闭），
   已有监听时不重复 spawn。
3. `watch.py` 超时块把「因过期被丢弃的未回唤醒」计数并列出 id，不再与
   「从没来过」长得一模一样。
4. 冷启动（游标归零）仍不翻历史积压：那是 `check_pending_reply.py` 钉住的
   既有契约，过期统计不得把它变成新的唤醒源。

全程临时 `WATER_GROUP_HOME` + `IDLEJOT_WG_CONFIG`，不碰实机状态：

    .venv/Scripts/python.exe scripts/check_listener_rehang.py
"""
import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_fixture_support  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import temp_dir  # noqa: E402

DAEMON = ROOT / "water-group" / "daemon"
sys.path.insert(0, str(DAEMON))

import reply  # noqa: E402
import up  # noqa: E402

# 外部测试变量会把 state 又指回实机，这里必须清掉，只留临时数据根。
ISOLATING_VARS = ("IDLEJOT_WG_STATE", "TULPA_WG_STATE", "IDLEJOT_WG_DB",
                  "IDLEJOT_WG_MEMORY_DB")
ACCOUNT = "10001"
GROUP = "1:group:123"

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def fixture_home(home):
    """最小身份配置：watch.py 启动就校验 self_account（issue #51）。"""
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"self_account": ACCOUNT, "onebot_token": "rehang-fixture"},
                   ensure_ascii=False), encoding="utf-8")


def child_env(home):
    env = dict(os.environ)
    env["WATER_GROUP_HOME"] = str(home)
    env["IDLEJOT_WG_CONFIG"] = str(home / "config.json")
    env["PYTHONUTF8"] = "1"
    for name in ISOLATING_VARS:
        env.pop(name, None)
    return env


def run_script(home, script, *args, timeout=120):
    return subprocess.run([sys.executable, str(DAEMON / script), *args],
                          cwd=str(ROOT), env=child_env(home), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def write_events(home, rows):
    path = home / "state" / "wg_events.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")


def event(eid, age_seconds, content, mention=True):
    """一条落盘事件。age_seconds 是「多少秒前」，与 wake_expired 的口径一致。"""
    segments = ([{"type": "at", "qq": ACCOUNT}] if mention else []) + \
               [{"type": "text", "text": content}]
    return {"id": eid, "timestamp": (time.time() - age_seconds) * 1000,
            "is_self": False, "sender": "甲", "content": content,
            "segments": segments, "conversation_id": GROUP}


def lines(out):
    return [ln.strip() for ln in (out or "").splitlines() if ln.strip()]


def index_of(out, prefix):
    for idx, ln in enumerate(lines(out)):
        if ln.startswith(prefix):
            return idx
    return -1


# ---------- listen.py：退出时给出可粘贴的下一步 ----------


@case
def 静默超时后带重挂提示():
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        proc = run_script(home, "listen.py", "--cycles", "1", "--timeout", "1",
                          "--no-digest")
    out = proc.stdout
    work = index_of(out, "NO_WORK")
    restart = index_of(out, "RESTART:")
    return proc.returncode == 0 and work >= 0 and restart > work


@case
def 唤醒之后带重挂提示():
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        write_events(home, [event(1, 5, "@机器人 在吗")])
        proc = run_script(home, "listen.py", "--cycles", "1", "--timeout", "10",
                          "--no-digest")
    out = proc.stdout
    wake = index_of(out, "WAKE")
    restart = index_of(out, "RESTART:")
    return proc.returncode == 0 and wake >= 0 and restart > wake


@case
def 重挂命令可原样粘贴且回到常驻周期():
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        proc = run_script(home, "listen.py", "--cycles", "1", "--timeout", "1",
                          "--no-digest")
    hint = next((ln for ln in lines(proc.stdout) if ln.startswith("RESTART:")), "")
    # 短等待 1s 是 reply --wait 的临时边界，重挂必须回到 300s；
    # --cycles / --no-digest 同样不该透传。
    return (hint.endswith("--timeout 300") and "listen.py" in hint
            and "--no-digest" not in hint and "--cycles" not in hint)


# ---------- watch.py：过期未回唤醒不再静默 ----------


@case
def 过期未回唤醒有计数():
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        write_events(home, [event(9, 3 * 3600, "@机器人 旧话题"),
                            event(10, 1, "路过回一句", mention=False)])
        (home / "state" / "wg_watch.txt").write_text("8", encoding="utf-8")
        proc = run_script(home, "watch.py", "--timeout", "2")
    out = proc.stdout
    idx = index_of(out, "EXPIRED:")
    return (proc.returncode == 2 and idx >= 0
            and not any(ln == "WAKE" for ln in lines(out))
            and "9" in lines(out)[idx]
            and "TIMEOUT" in lines(out))


@case
def 冷启动不翻历史积压():
    with temp_dir(prefix="wg-rehang-") as home:
        fixture_home(home)
        write_events(home, [event(9, 3 * 3600, "@机器人 旧话题"),
                            event(10, 1, "路过回一句", mention=False)])
        proc = run_script(home, "watch.py", "--timeout", "2")
    out = proc.stdout
    return (proc.returncode == 2 and "TIMEOUT" in lines(out)
            and index_of(out, "EXPIRED:") < 0
            and not any(ln == "WAKE" for ln in lines(out)))


# ---------- reply.py：默认续挂 ----------


@case
def 发送后默认续挂():
    proc = subprocess.run([sys.executable, str(DAEMON / "reply.py"), "--help"],
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60,
                          env=dict(os.environ, PYTHONUTF8="1"))
    out = proc.stdout or ""
    return proc.returncode == 0 and "--no-wait" in out and "默认 240s" in out


@case
def 已有监听时不重复挂():
    originals = (up.listener_pids, reply.winconsole.run_passthrough)
    up.listener_pids = lambda *args, **kwargs: ["4321"]

    def forbid(*args, **kwargs):
        raise AssertionError("已有监听时 wait_for_next 不该再 spawn listen.py")

    reply.winconsole.run_passthrough = forbid
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = reply.wait_for_next(reply.DEFAULT_WAIT)
    finally:
        up.listener_pids, reply.winconsole.run_passthrough = originals
    return code == 0 and "已有监听在跑" in buf.getvalue()


@case
def no_wait_优先于_wait():
    """`--no-wait` 是「马上要做别的事」的出口，不能被默认值盖过去。"""
    tree = ast.parse((DAEMON / "reply.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Attribute)
                and node.test.attr == "no_wait"):
            continue
        for stmt in node.body:
            if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Attribute)
                    and stmt.targets[0].attr == "wait"
                    and isinstance(stmt.value, ast.Constant)
                    and stmt.value.value == 0):
                return True
    return False


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    bad = 0
    for fn in CASES:
        try:
            got = fn()
        except Exception as exc:  # noqa: BLE001
            got = False
            print(f"✗ {fn.__name__}: {type(exc).__name__}: {exc}")
            bad += 1
            continue
        if got:
            print(f"✓ {fn.__name__}")
        else:
            bad += 1
            print(f"✗ {fn.__name__}")
    print("全部通过" if not bad else f"{bad} 项失败")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
