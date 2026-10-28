"""Always-on waiter for the water-group session.

Holds wait_chat_messages continuously so @ mentions are captured the moment they
arrive instead of waiting for an agent to re-arm a 180 s tool call. Every delivered
group message is appended to wg_events.jsonl and the newest payload lands in
wg_state.json, so a disconnected agent can replay whatever it missed.

Our own send receipts travel in the same batch but are dropped before disk: they
wake nobody (up.wake_reason already ignores is_self) and only inflate the stream,
which every --status / --list / watch call parses in full. chat_inbox keeps the
complete copy, so reply.py --context still shows them as 「我: …」.

The loop body is fully guarded: a bad payload, a write race, an MCP timeout or a
revoked grant must never kill the listener, because the agent is not what keeps it
alive. The session id lives in wg_session.txt and is rebuilt by session.ensure()
whenever the grant behind it rotates.

Control files, all inside the state directory:
  wg_ack.txt      cursor the agent has finished handling; the collector acks it next loop
  wg_pause        pause marker; judged by wg_pause (owner + deadline), never by existence
  wg_heartbeat    unix time of the last completed loop, watched by supervisor.py
  wg_session.txt  active session id
"""
import json
import os
import sys
import time
from pathlib import Path

import mcp_client
import session
import wg_gap  # noqa: E402  (停机窗口缺口账本，issue #114)
import wg_images
import wg_pause  # noqa: E402  (暂停标记的判定与清理，issue #189)

ROOT = Path(__file__).resolve().parents[2]
from wg_daemon import beater, logger
from wg_paths import logs_dir, state_dir
STATE_DIR = state_dir()
ACK = STATE_DIR / "wg_ack.txt"
STATE = STATE_DIR / "wg_state.json"
EVENTS = STATE_DIR / "wg_events.jsonl"
LOG = logs_dir() / "wg_collector.log"
HEARTBEAT = STATE_DIR / "wg_heartbeat"

SESSION_ERRORS = ("denied", "不存在", "未连接", "source_unavailable")


def read_ack():
    try:
        return int(ACK.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


# issue #11 PB: 日志/心跳助手收口到 wg_daemon（旧本件缺 mkdir，现在恒建父目录）
log = logger(LOG)
beat = beater(HEARTBEAT)


def load_seen():
    seen = set()
    if EVENTS.exists():
        for raw in EVENTS.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(raw)["id"])
            except (ValueError, KeyError):
                continue
    return seen


def capture_events(msgs, seen):
    """把一批消息写进唤醒流，返回实际落盘条数。

    自己的发送回执（is_self）随 @ 批次一起下发，但唤醒链路本来就不认它
    （up.wake_reason 首行就排除了），落盘只是撑大 wg_events.jsonl，让
    --status / --list / watch 每次全量解析更慢。这里在写盘前就丢掉。

    seen 仍按整批推进，包括被丢弃的回显：游标和去重记的是「这批处理过了」，
    不是「这行写进文件了」；否则同一批回显会在下一轮被反复判定。写盘失败时
    只把已经写成的行记进 seen，异常冒泡给外层，下一轮补写剩下的。
    """
    fresh = [m for m in msgs if m.get("id") not in seen]
    group = [m for m in fresh if not m.get("is_self")]
    if group:
        with open(EVENTS, "a", encoding="utf-8") as fh:
            for m in group:
                fh.write(json.dumps(m, ensure_ascii=False) + "\n")
                seen.add(m["id"])
    seen.update(m["id"] for m in fresh)
    return len(group)


def bootstrap_session():
    sid = ""
    while not sid:
        try:
            sid = session.ensure()
        except Exception as exc:  # noqa: BLE001
            log(f"session bootstrap failed: {type(exc).__name__}: {exc}")
        if not sid:
            time.sleep(10)
    return sid


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    seen = load_seen()
    log(f"collector start, {len(seen)} known events, ack={read_ack()}")
    beat()
    sid = bootstrap_session()
    log(f"session {sid}")
    beat()
    # issue #114：collector 随栈重启而重启——夜间关机、断电、换网后早上拉起，停机
    # 窗口内的群消息永久不进 chat_inbox（NapCat 不重放断线期间事件，游标只前进）。
    # 这里算一次「本地 chat_inbox 最后一条 → 拉起时刻」与 get_group_msg_history 的
    # 差，落 state/wg_gap.json：up.py --status / reply.py --list / listen.py NO_WORK
    # 才能事后报出「这段时间漏了什么」，不再依赖人肉翻历史。fetcher 默认走
    # wg_up_wake.group_msg_history，延迟 import 避免循环（wg_gap 默认参数里也走它）。
    try:
        import wg_up_wake  # noqa: PLC0415  延迟到 session 建好后再拉 OneBot
        gap = wg_gap.compute_and_record(fetch=wg_up_wake.group_msg_history)
        total = sum((e.get("missing") or 0)
                    for e in (gap.get("groups") or {}).values()
                    if isinstance(e, dict) and not e.get("unreachable"))
        at_me = sum((e.get("at_me") or 0)
                    for e in (gap.get("groups") or {}).values()
                    if isinstance(e, dict) and not e.get("unreachable"))
        log(f"gap disclosure: missing {total}, at_me {at_me}")
    except Exception as exc:  # noqa: BLE001  缺口计算挂了不该让 collector 挂
        log(f"gap disclosure failed: {type(exc).__name__}: {exc}")

    while True:
        try:
            # issue #189：暂停以前只看「文件在不在」，于是任意一个手工塞进来的
            # `wg_pause`（现场内容是 `placeholder`）都能永久停采：这一分支既不采
            # 也不写心跳，supervisor 300 秒判死重启，重启后第一轮又回到这里。
            # 现在判定交给 wg_pause：有主（owner 存活）+ 有时限才算数，失效的标记
            # 当轮删掉继续采；**暂停期间照写心跳**——不写就等着被判死，而重启救不了。
            hold, note = wg_pause.gate()
            if note:
                log(note)
            if hold:
                beat()
                time.sleep(2)
                continue
            ack = read_ack()
            try:
                data, blocks = mcp_client.call_tool_raw(
                    "wait_chat_messages",
                    {"session_id": sid, "timeout_seconds": 170,
                     "acknowledge_through_id": ack, "mention_only": True},
                    timeout=200,
                )
            except Exception as exc:  # noqa: BLE001
                log(f"wait failed: {type(exc).__name__}: {exc}")
                beat()
                time.sleep(5)
                continue

            error = str(data.get("error") or "")
            if error:
                log(f"wait error: {error} {data.get('error_code', '')}")
                if "read_through_id" in error:
                    # The cursor is per session: a rebuilt session starts at zero.
                    ACK.write_text("0", encoding="utf-8")
                    log("ack cursor reset for the new session")
                    beat()
                    time.sleep(2)
                    continue
                if any(token in error for token in SESSION_ERRORS):
                    try:
                        fresh = session.ensure()
                    except Exception as exc:  # noqa: BLE001
                        log(f"session rebuild failed: {type(exc).__name__}: {exc}")
                        fresh = ""
                    if fresh and fresh != sid:
                        sid = fresh
                        log(f"session switched to {sid}")
                beat()
                time.sleep(5)
                continue

            STATE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            if data.get("event") == "stopped":
                # Someone stopped the session from the app; a wait on it returns
                # instantly forever, so rebuild instead of spinning.
                log("session reported stopped, rebuilding")
                try:
                    fresh = session.ensure()
                except Exception as exc:  # noqa: BLE001
                    log(f"session rebuild failed: {type(exc).__name__}: {exc}")
                    fresh = ""
                if fresh and fresh != sid:
                    sid = fresh
                    log(f"session switched to {sid}")
                beat()
                time.sleep(5)
                continue
            msgs = data.get("messages") or []
            # 已交付的直接图与引用图像素先落盘，Agent 侧才有可打开的路径；
            # deferred 项只记元数据，留给 look.py 按需补读。必须在写唤醒流之前，
            # 落盘会把 local_path 回填到消息段上。
            try:
                wg_images.ingest(data, blocks)
            except Exception as exc:  # noqa: BLE001
                log(f"image save failed: {type(exc).__name__}: {exc}")
            captured = capture_events(msgs, seen)
            if captured:
                log(f"captured {captured} new events, read_through={data.get('read_through_id')}")
            beat()

            if msgs:
                # Acknowledge on arrival, not on reply. The cursor records that the
                # batch was handled — group messages reached disk, our own echoes
                # were deliberately kept out of it — so holding the cursor open
                # until an agent composes a reply left the listener deaf for up to
                # two minutes after every batch. The group half is preserved in
                # wg_events.jsonl, the echo half in chat_inbox, so nothing is lost.
                ACK.write_text(str(data.get("read_through_id") or ack), encoding="utf-8")
                beat()
            else:
                time.sleep(2)
        except Exception as exc:  # noqa: BLE001
            log(f"loop error: {type(exc).__name__}: {exc}")
            beat()
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
