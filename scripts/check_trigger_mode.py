"""issue #188 gate: 按群选择 natural / mention_only 触发模式。

症状（issue #188）：``collector.py:143`` 把 ``wait_chat_messages`` 请求的
``mention_only`` 写死成 ``True``，服务端存的 ``participation='natural'`` 被它覆盖，
普通闲聊不投递；``watch.py:251`` 的 ``fresh`` 又 ``and up.wake_reason(e)`` 再滤一次，
``wake_reason`` 只认 @ / 关键词 / 合并转发。两层夹击，配 ``natural`` 的群照样唤不醒。

修复后：``collector`` 按本会话群 ``trigger_mode`` 取 ``mention_only``
（natural 给 False），``wake_reason`` 对 natural 群的普通消息放行（返回 ``natural``）。

本门禁钉七件事，每条带对照组（#153 教训：只断言「natural 群出消息」会被「怎么改都出」
的坏实现骗过）：

  1. natural 群普通消息（无 @ 无关键词）→ ``wake_reason`` 返回 ``natural``、watch 出
     WAKE、``--list`` 出该条；
  2. mention_only 群普通消息 → ``wake_reason`` 返回空串、watch 不出、``--list`` 不出
     （对照组：证明 1 不是「反正什么都出」）；
  3. natural 群里带 @ 的消息仍返回 ``at``、带关键词仍返回 ``keyword:xx``，不是 ``natural``
     （对照：放行在显式判定之后，不吞掉精确理由）；
  4. natural 群自己的回显 ``wake_reason`` 仍空、``#ping`` 指令仍空（对照：is_self / 指令
     早退不破坏）；
  5. 两群不同配置：钉死 A=natural 时普通消息唤醒、钉死 B=mention_only 时不唤醒（口径同
     ``check_multigroup_isolation`` 的 conversation env 钉死）；
  6. collector 请求值来源：natural 群 ``mention_only=False``、mention_only 群 ``True``；
  7. 判据完整性自检：把 ``wake_reason`` 的 natural 放行桩掉，natural 用例必须红。

**已知边界**：不覆盖真实群消息发送（issue 明确不连真实群）；不覆盖服务端
``mcp_chat_wait.py`` 的 ``participation`` 合并（后端契约，本 PR 只改采集请求值）。

    .venv/Scripts/python.exe scripts/check_trigger_mode.py
"""
import contextlib
import io
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import check_fixture_support as fs  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import scratch_dir  # noqa: E402
fs.bootstrap_paths(scripts=False, root=False)

DAEMON = ROOT / "water-group" / "daemon"

# 数据根 / 状态目录 / 配置 / 库全部钉进临时目录，且在 import 任何 daemon 模块
# **之前**完成：wg_paths.state_dir() / wg_config.CONFIG 都是 import 期求值。
SCRATCH = scratch_dir(prefix="wg_trigger_mode_")
STATE = SCRATCH / "state"
STATE.mkdir(parents=True, exist_ok=True)
CONFIG = SCRATCH / "config.json"
DB = SCRATCH / "none.sqlite3"
os.environ["WATER_GROUP_HOME"] = str(SCRATCH)
os.environ["IDLEJOT_WG_STATE"] = str(STATE)
os.environ["TULPA_WG_STATE"] = str(STATE)
os.environ["IDLEJOT_WG_CONFIG"] = str(CONFIG)
os.environ["IDLEJOT_WG_DB"] = str(DB)
os.environ.pop("IDLEJOT_WG_CONVERSATION", None)
os.environ.pop("TULPA_WG_CONVERSATION", None)

ACCOUNT = "10001"
SELF = ACCOUNT
GROUP_A = "111"          # natural
GROUP_B = "222"          # mention_only
CONV_A = f"{ACCOUNT}:group:{GROUP_A}"
CONV_B = f"{ACCOUNT}:group:{GROUP_B}"
NOW_MS = int(time.time() * 1000)
# 每个用例一个 id 段，互不干扰。
AT_ID = 9201
KW_ID = 9202
CHAT_ID = 9203
SELF_ID = 9204
CMD_ID = 9205
AT_B_ID = 9211
CHAT_B_ID = 9212

WATCH = DAEMON / "watch.py"
REPLY = DAEMON / "reply.py"

ID_RE = re.compile(r"^id=(\d+)\b")


def write_config(groups, **extra):
    payload = {"self_account": ACCOUNT, "onebot_token": "trigger-mode-fixture",
               "self_names": ["小鲸鱼"], "default_trigger_mode": "mention_only",
               "groups": groups}
    payload.update(extra)
    CONFIG.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def natural_groups():
    """A=natural、B=mention_only，两组都 enabled。"""
    return {GROUP_A: {"enabled": True, "trigger_mode": "natural"},
            GROUP_B: {"enabled": True, "trigger_mode": "mention_only"}}


def only_natural():
    return {GROUP_A: {"enabled": True, "trigger_mode": "natural"}}


def only_mention_only():
    return {GROUP_B: {"enabled": True, "trigger_mode": "mention_only"}}


def event(event_id, conversation_id, content, *, at=False, is_self=False):
    segs = [{"type": "at", "qq": SELF}] if at else []
    return {"id": event_id, "conversation_id": conversation_id, "sender": "群友",
            "content": content, "timestamp": NOW_MS, "is_self": is_self,
            "segments": segs, "onebot_message_id": f"m{event_id}"}


def seed(rows):
    (STATE / "wg_events.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")


def clear_state():
    for name in ("wg_events.jsonl", "wg_replied.jsonl", "wg_watch.txt",
                 "wg_conversation.txt", "wg_session.txt", "wg_state.json"):
        (STATE / name).unlink(missing_ok=True)


def child_env(conversation=None):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["WATER_GROUP_HOME"] = str(SCRATCH)
    env["IDLEJOT_WG_STATE"] = str(STATE)
    env["TULPA_WG_STATE"] = str(STATE)
    env["IDLEJOT_WG_CONFIG"] = str(CONFIG)
    env["IDLEJOT_WG_DB"] = str(DB)
    for name in ("IDLEJOT_WG_CONVERSATION", "TULPA_WG_CONVERSATION"):
        env.pop(name, None)
    if conversation:
        env["IDLEJOT_WG_CONVERSATION"] = conversation
    return env


def run(script, *args, conversation=None, timeout=120):
    import subprocess
    return subprocess.run([sys.executable, str(script), *args], cwd=str(ROOT),
                          env=child_env(conversation), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def listed_ids(out):
    """唤醒块里的 id 集合。正则捕获组，不用子串（#151 教训）。"""
    ids = set()
    for line in out.splitlines():
        match = ID_RE.match(line.strip())
        if match:
            ids.add(int(match.group(1)))
    return ids


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# ---------- 1/2：natural 放行 / mention_only 不放行 ----------


@case
def natural群普通消息被唤醒():
    """natural 群里一条无 @ 无关键词的群友消息：wake_reason 返回 'natural'，
    watch --replay 出 WAKE、--list 出该条。"""
    import up

    clear_state()
    write_config(only_natural())
    seed([event(CHAT_ID, CONV_A, "今天天气不错")])
    reason = up.wake_reason(event(CHAT_ID, CONV_A, "今天天气不错"))
    if reason != "natural":
        return f"wake_reason={reason!r}（期望 'natural'）"
    proc = run(WATCH, "--timeout", "3", "--replay", conversation=CONV_A)
    ids = listed_ids(proc.stdout or "")
    list_proc = run(REPLY, "--list", conversation=CONV_A)
    list_ids = listed_ids(list_proc.stdout or "")
    ok = (CHAT_ID in ids and "WAKE" in (proc.stdout or "")
          and CHAT_ID in list_ids)
    return ok or f"watch_ids={ids}\nlist_ids={list_ids}\n{proc.stdout}\n{list_proc.stdout}"


@case
def mention_only群普通消息不唤醒():
    """对照组（改前行为）：mention_only 群同一普通消息 wake_reason 返回空串、
    watch 不出、--list 不出。"""
    import up

    clear_state()
    write_config(only_mention_only())
    seed([event(CHAT_B_ID, CONV_B, "今天天气不错")])
    reason = up.wake_reason(event(CHAT_B_ID, CONV_B, "今天天气不错"))
    if reason != "":
        return f"wake_reason={reason!r}（期望空串）"
    proc = run(WATCH, "--timeout", "3", "--replay", conversation=CONV_B)
    ids = listed_ids(proc.stdout or "")
    list_proc = run(REPLY, "--list", conversation=CONV_B)
    list_ids = listed_ids(list_proc.stdout or "")
    # watch 没有唤醒块就回到 NO_WORK-ish；--list 不出该条。
    ok = (CHAT_B_ID not in ids and CHAT_B_ID not in list_ids)
    return ok or f"watch_ids={ids}\nlist_ids={list_ids}\n{proc.stdout}"


# ---------- 3：@ 与关键词优先级 ----------


@case
def natural群里_at_仍返回at():
    """natural 群里带 @ 的消息 wake_reason 仍返回 'at'，不是 'natural'。"""
    import up

    clear_state()
    write_config(only_natural())
    reason = up.wake_reason(event(AT_ID, CONV_A, "@机器人 在吗", at=True))
    if reason != "at":
        return f"wake_reason={reason!r}（期望 'at'）"
    return True


@case
def natural群里关键词仍返回keyword():
    """natural 群里正文含唤醒词的消息 wake_reason 仍返回 'keyword:...'。"""
    import up

    clear_state()
    write_config(only_natural())
    reason = up.wake_reason(event(KW_ID, CONV_A, "小鲸鱼 你说说"))
    if not reason.startswith("keyword:"):
        return f"wake_reason={reason!r}（期望 'keyword:...'）"
    return True


# ---------- 4：is_self / 指令早退 ----------


@case
def natural群里自己回显仍不唤醒():
    """natural 群里自己的回显 wake_reason 仍空（is_self 早退先于 natural 放行）。"""
    import up

    clear_state()
    write_config(only_natural())
    reason = up.wake_reason(event(SELF_ID, CONV_A, "我说一句", is_self=True))
    if reason != "":
        return f"wake_reason={reason!r}（期望空串）"
    return True


@case
def natural群里指令仍不唤醒():
    """natural 群里 `#ping` 指令 wake_reason 仍空（归 commands.py，不进唤醒）。"""
    import up

    clear_state()
    write_config(only_natural())
    reason = up.wake_reason(event(CMD_ID, CONV_A, "#ping"))
    if reason != "":
        return f"wake_reason={reason!r}（期望空串）"
    return True


# ---------- 5：两群不同配置 ----------


@case
def 两群不同配置按群生效():
    """A=natural、B=mention_only：钉死 A 普通消息唤醒，钉死 B 不唤醒——证明按群读取。"""
    import up

    clear_state()
    write_config(natural_groups())
    seed([event(CHAT_ID, CONV_A, "随便聊聊"), event(CHAT_B_ID, CONV_B, "随便聊聊")])
    # 钉死 A
    os.environ["IDLEJOT_WG_CONVERSATION"] = CONV_A
    try:
        reason_a = up.wake_reason(event(CHAT_ID, CONV_A, "随便聊聊"))
        proc_a = run(WATCH, "--timeout", "3", "--replay", conversation=CONV_A)
        ids_a = listed_ids(proc_a.stdout or "")
        # 钉死 B
        os.environ["IDLEJOT_WG_CONVERSATION"] = CONV_B
        reason_b = up.wake_reason(event(CHAT_B_ID, CONV_B, "随便聊聊"))
        proc_b = run(WATCH, "--timeout", "3", "--replay", conversation=CONV_B)
        ids_b = listed_ids(proc_b.stdout or "")
    finally:
        os.environ.pop("IDLEJOT_WG_CONVERSATION", None)
    ok = (reason_a == "natural" and CHAT_ID in ids_a
          and reason_b == "" and CHAT_B_ID not in ids_b)
    return ok or (f"reason_a={reason_a!r} ids_a={ids_a}\n"
                  f"reason_b={reason_b!r} ids_b={ids_b}\n"
                  f"{proc_a.stdout}\n{proc_b.stdout}")


# ---------- 6：collector 请求值来源 ----------


@case
def collector请求值按群取():
    """natural 群 collector 请求 mention_only=False、mention_only 群 True。

    直接调 trigger_mode() —— 它是 collector 取值的唯一来源（collector.py:144）。
    断言这一份，就等于断言请求体里没有硬编码 True。
    """
    import wg_up_wake

    clear_state()
    write_config(natural_groups())
    os.environ["IDLEJOT_WG_CONVERSATION"] = CONV_A
    try:
        natural_only = wg_up_wake.trigger_mode() != "natural"
    finally:
        os.environ.pop("IDLEJOT_WG_CONVERSATION", None)
    os.environ["IDLEJOT_WG_CONVERSATION"] = CONV_B
    try:
        mention_only_val = wg_up_wake.trigger_mode() != "natural"
    finally:
        os.environ.pop("IDLEJOT_WG_CONVERSATION", None)
    if natural_only is not False:
        return f"natural 群 mention_only={natural_only}（期望 False）"
    if mention_only_val is not True:
        return f"mention_only 群 mention_only={mention_only_val}（期望 True）"
    return True


# ---------- 7：判据完整性自检 ----------


def inprocess_watch():
    """进程内跑一次 watch.main()（--replay 让游标归零），返回 (退出码, 输出)。

    自检必须在进程内：wake_reason 的退化桩打在本进程模块全局上，子进程读不到
    （check_multigroup_isolation.inprocess_watch 同款理由）。
    """
    import watch

    saved = sys.argv
    sys.argv = ["watch.py", "--timeout", "3", "--replay"]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = watch.main()
    finally:
        sys.argv = saved
    return code, buf.getvalue()


@case
def 自检_去掉natural放行必须翻转():
    """把 wake_reason 的 natural 放行桩成空串：natural 群普通消息用例必须红。

    只看「natural 群出消息」的实现会在这里露馅：它测的是「反正出」，不测「为什么出」。
    进程内跑 watch，让桩真正生效（子进程读不到本进程的模块全局）。
    """
    import up
    import wg_up_wake

    clear_state()
    write_config(only_natural())
    seed([event(CHAT_ID, CONV_A, "今天天气不错")])
    real = up.wake_reason
    real_wake = wg_up_wake.wake_reason

    def stub(event):
        # 复制 wake_reason 的显式判定，去掉末尾的 natural 放行。
        if event.get("is_self"):
            return ""
        account = wg_up_wake.self_account()
        for seg in event.get("segments") or []:
            if seg.get("type") == "at" and str(seg.get("qq")) == account:
                return "at"
        text = event.get("content") or ""
        if wg_up_wake.is_command(text):
            return ""
        for word in wg_up_wake.wake_words():
            if word in text:
                return f"keyword:{word}"
        return ""

    up.wake_reason = stub
    wg_up_wake.wake_reason = stub
    try:
        reason = up.wake_reason(event(CHAT_ID, CONV_A, "今天天气不错"))
        _code, watch_out = inprocess_watch()
    finally:
        up.wake_reason = real
        wg_up_wake.wake_reason = real_wake
    ids = listed_ids(watch_out)
    # 去掉放行后：reason 应空、watch 不出该条。
    bad = (reason == "natural" or CHAT_ID in ids)
    return (not bad) or f"reason={reason!r} ids={ids}（桩掉后仍放行）"


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
        if got is True:
            print(f"✓ {fn.__name__}")
        else:
            bad += 1
            print(f"✗ {fn.__name__}")
            if isinstance(got, str):
                print(f"    {got}")
    print("全部通过" if not bad else f"{bad} 项失败")
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
