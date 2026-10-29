"""Block until someone wakes the bot, then print it and exit.

The collector already captures a mention within about a second of it arriving.
What used to add a minute was the agent: it had no way to be told, so it looked
at wg_events.jsonl on its own schedule and the gap between two looks was dead
time. This closes that gap — the call returns the moment a wake lands, so the
only remaining delay is composing the reply.

Two wake paths meet here: @ and keywords arrive in wg_events.jsonl, and while a
follow-up window is open (追问窗口插件，issue #4 PD2a) ordinary chatter after the
last reply counts too, read straight from chat_inbox. They never overlap — a
mention always wins — and the window closes itself once the group goes quiet.

A wake line says who spoke and what they said; it also carries what the bot already
knows about them (issue #163): the sender's dossier card -- aliases, XP, the freshest
memory, this group's latest gossip -- plus a card for anyone the text names, and one
line of this group's terms (black talk, shop names, conventions) per wake block. Only
people actually identified get a card, each card is one clipped line, and the body's
80-character budget is computed before the cards are appended, so the dossier never
eats the message (see `wg_dossiers`).

A wake that aged out while nobody was listening is not reported as a wake (the
cold start would replay days of dead mentions), but it is no longer silent
either: the timeout block ends with an ``EXPIRED:`` count and its ids, so
"nobody watched for two hours" and "the group said nothing" stop looking the
same (issue #69).

A wake that was scanned but never delivered is picked back up (issue #182).
The listener lives one round, so when the host cuts the call before the wake
block reaches the agent, the cursor has already moved past those ids and every
later listener starts after them -- they sit in ``[待回]`` forever. A default
start now rewinds the cursor to just before the oldest mention that is still
unanswered and unexpired, reprints it, and says ``BACKFILL:`` why. Answered and
expired wakes are left behind (the same judgment as ``reply.py --list``).

While it waits it also refreshes the listener's liveness beat (issue #112): "was
anyone on watch" has to be answerable from a file afterwards, and this poll loop
is the only thing running during the long quiet stretches. Bare ``watch.py`` runs
(no listener started the record) leave the ledger untouched.

The ledger can also hold an unresolved mute (issue #192). A bot muted for ten
minutes is never woken by the mute ending -- the group said nothing, and the wake
it needs is a state change, not a message. So the poll loop re-checks the mute
every ``PROBE_INTERVAL`` and prints ``UNMUTED:`` plus the usual closing ``WAKE``
the moment it is lifted, which sends the caller back to re-hang the listener and
pick up whatever piled up meanwhile. Only a confirmed release is printed: "still
muted" once a minute would bury the lines worth reading.

While that mute is in force the follow-up window is left unscanned (issue #197):
nothing said there can be answered, so waking up for it only burns a round and
resets the quiet counter. The window keeps its cursor and expires on its own; the
moment the mute lifts the next listener finds it again. A mute does not silence
a real mention -- somebody is looking for the bot, and `--wait-unmute` can wait
inside the budget.

Exit codes:
  0  a wake was printed
  2  timed out with nothing new (call again)
  1  the event stream is unreadable
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import plugins  # noqa: E402  (事件型能力派发口：追问窗口，issue #4 PD2a)
import up  # noqa: E402  (wake_reason / events / replied_ids live there)
import wg_budget  # noqa: E402  (等待预算，issue #83)
import wg_aliases  # noqa: E402  (别名指向渲染，issue #30)
import wg_clock  # noqa: E402  (「现在」的唯一出口，issue #95)
import wg_dossiers  # noqa: E402  (群友画像 / 群级术语，issue #163)
import wg_images  # noqa: E402  (图片标记渲染)
import wg_videos  # noqa: E402  (视频标记渲染，issue #200)
import wg_isolation  # noqa: E402  (本会话只管一个群，issue #164 PA)
import wg_listener_state  # noqa: E402  (监听存亡账本的续心跳，issue #112)
import wg_quotes  # noqa: E402  (引用原文渲染，issue #94)
import wg_send_failure  # noqa: E402  (禁言账本的现查与解禁唤醒，issue #192)
import wg_text  # noqa: E402  (正文渲染：段占位不占额度，issue #32)
import winconsole  # noqa: E402  (stdout 编码 + 控制台代码页，一次收口)

ROOT = HERE.parents[1]
from wg_paths import state_dir
STATE = state_dir()
CURSOR = STATE / "wg_watch.txt"
POLL = 1.5
# 裸跑 `watch.py`（不带 --timeout）时的等待上限。与 listen / reply 同一个预算：
# 超过它，宿主工具超时会把本进程杀掉，而那条路径上不会打印任何东西（issue #83）。
DEFAULT_TIMEOUT = wg_budget.WAIT_BUDGET
# 过期未回唤醒的报告里最多列几个 id：一行报得下，又不至于把整块输出撑成清单。
EXPIRED_SHOWN = 5


def read_cursor():
    try:
        return int(CURSOR.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def write_cursor(value):
    try:
        CURSOR.write_text(str(value), encoding="utf-8")
    except OSError:
        pass


def latest_id(rows):
    return max((r.get("id") or 0) for r in rows) if rows else 0


def wake_pending(event, rows, ledger):
    """这一条到现在还该被回：叫得醒、账本没记、没过待回时效（issue #182）。

    与 `reply.py --list` 的 `[待回]` 同一口径（同一组 `up.*` 判定），所以
    「列表里还是待回」与「重挂的监听会不会补扫」不会分叉。
    """
    return (bool(up.wake_reason(event))
            and not up.replied_after(event.get("id"), rows, ledger)
            and not up.wake_expired(event))


def rewind_for_backlog(rows, cursor, ledger):
    """游标之前还有未回唤醒时，把起点退到最老一条之前（issue #182）。

    监听只活一轮：宿主在唤醒块回传前截断调用时，Agent 一个字都没看到，而
    游标已经被推进过这些 id——新监听从文件游标起扫，它们就永久 `[待回]`。
    退回去走原来的 fresh 分支，等于把「漏送的唤醒」重放一次；已回与过期的
    不参与（对照组见 check_listener_rehang 契约 44）。

    只在默认路径调用：`--after` / `--replay` 是调用方显式给的起点，不许被
    改写。没有 id 的坏行不参与回退——否则游标会被拉到 0，整批历史重新翻出来。
    """
    pending = [e for e in rows
               if (e.get("id") or 0) and (e.get("id") or 0) <= cursor
               and wake_pending(e, rows, ledger)]
    if not pending:
        return cursor
    target = max(0, min(e.get("id") or 0 for e in pending) - 1)
    print(f"BACKFILL: 游标 {cursor} 之前有 {len(pending)} 条未回唤醒，"
          f"回退到 {target} 重新扫描")
    return target


def report_expired(rows, start_cursor):
    """把「因过期被丢掉、且从没回过」的唤醒说出来（issue #69）。

    它们不写账本、不进 fresh（冷启动后整批陈年 @ 会变成「新唤醒」，见
    check_pending_reply 的契约），但也**不能什么都不说**：监听掉线超过
    pending_reply_ttl 后，那段时间的 @ 在输出里与「从没来过」完全一样，Agent
    没有任何线索知道自己错过了什么。计数带上 id，补不补由 Agent 决定（
    reply.py <id> --force）。

    只算 `start_cursor` 之后新到的：本轮开始时就已在文件里的历史积压，正是
    冷启动不该被翻出来的那批，保持静默。游标归零就是冷启动——那时「过期」
    只能说明文件里有陈年旧账，不能说明这次监听错过了什么。
    """
    if not start_cursor:
        return
    ledger = up.replied_ids()
    expired = [e for e in rows
               if (e.get("id") or 0) > start_cursor
               and up.wake_reason(e)
               and not up.replied_after(e.get("id"), rows, ledger)
               and up.wake_expired(e)]
    if not expired:
        return
    ids = "、".join(str(e.get("id")) for e in expired[:EXPIRED_SHOWN])
    if len(expired) > EXPIRED_SHOWN:
        ids += f" 等 {len(expired)} 条"
    print(f"EXPIRED: {len(expired)} 条未回唤醒已超过待回时效"
          f"（TTL {up.pending_reply_ttl()}s），未计入唤醒：{ids}")
    print("         要补旧话题就单独回：reply.py <id> --force")


def _unmute_line(rec):
    """解禁那一行：实测解禁时刻 + 账本原判（账本没写进时刻就不带）。

    「原判」两个字是刻意的：`mute_until` 是失败那一刻的预测，可能被管理员提前
    解禁，也可能被重新禁了一次。写成「禁言至 X」会把它读成此刻的事实。
    """
    at = rec.get("recovered_at")
    line = (f"UNMUTED: 禁言已于 {wg_clock.clock(at)} 实测解除" if at
            else "UNMUTED: 禁言已实测解除")
    try:
        until = int(rec.get("mute_until") or 0)
    except (TypeError, ValueError):
        until = 0
    if until:
        line += f"（账本原判 {wg_clock.clock(until)}）"
    return line


def poll_unmute():
    """账本上压着未定论的禁言时现查一次；确认解禁就打出唤醒块（issue #192）。

    返回是否解禁（调用方据此结束本轮等待）。四档结论里只有 `released` 出声：
    `muted` / `unknown` 静默——探测每 `PROBE_INTERVAL` 秒一次，若每次都打印一句
    「仍然禁言」，监听输出的每一屏都会被它占满，恰好盖掉真正要看的东西。

    **放在 fresh 与追问窗口之后是刻意的**：有人 @ 时先回人。那条发送成功本身就是
    「禁言不再是障碍」的最强证据（`handle()` 会写 `recovered=True`），不必先报一句
    环境变化；`UNMUTED` 只在没人叫的空窗发声，正是 issue 描述的场景。

    节流状态存在账本里（`rechecked_at`），所以监听与 `reply.py --list` 共用同一份
    事实：谁先查过，另一个不会紧接着再打一遍接口。
    """
    conclusion, _ = wg_send_failure.probe(min_interval=wg_send_failure.PROBE_INTERVAL)
    if conclusion != "released":
        return False
    print(wg_clock.now_line())
    print(_unmute_line(wg_send_failure.read() or {}))
    # 点明下一步而不是只说「解禁了」：Agent 被叫醒后要做的事有两件——先看谁在等
    # （`--list`），再把监听重挂回来（首轮的 `rewind_for_backlog` 会补扫积压）。
    print("  重挂监听会自动补扫禁言期间积压的未回唤醒（BACKFILL），也可先看 reply.py --list")
    print("WAKE")
    return True


def require_wake_identity():
    """缺 self_account 的监听必须拒绝启动，而不是假装在工作（issue #51）。

    config.json 丢失时 ``self_account()`` 读空：``wake_reason`` 的 @ 匹配
    （``str(seg.qq) == ""``）永假、关键词列表为空——监听进程活着、collector 在收
   消息、游标在推进，只有「Agent 不回应」一个症状，而 Agent 侧看到的是「没有
    唤醒」，与「群里没人说话」完全无法区分。一个永远唤不醒的监听比没有监听更糟。

    返回 0 放行；返回 1 并打印缺什么、怎么恢复（config.example.json）。
    """
    if up.self_account():
        return 0
    print("[拒绝] water-group/config.json 缺失或未设置 self_account：")
    print("       @ 判定永假、self_names 关键词为空，这个监听不可能被唤醒。")
    print("       按 water-group/config.example.json 恢复配置后重试；")
    print("       当前缺什么可以用 up.py --status 的「配置」行核对。")
    return 1


def _plugin_settings():
    """追问窗口插件的归一化设置（三态门控要它，issue #4 PD2a）。

    延迟 import commands：等唤醒这条路径不碰指令树，别让 CLI 白拖约 80ms
    进来。读不到配置时回落空设置——插件按 default_enabled 走。
    """
    try:
        import commands  # noqa: PLC0415

        return commands.load_settings()
    except Exception:  # noqa: BLE001  设置坏了不该让等唤醒这条链路崩
        return {}


def main():
    parser = argparse.ArgumentParser(description="等一条唤醒（@ 或关键词）")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"最多等多少秒（默认 {DEFAULT_TIMEOUT}，即等待预算）")
    parser.add_argument("--after", type=int, default=None, help="从这个事件 id 之后开始找")
    parser.add_argument("--replay", action="store_true", help="忽略游标，重扫所有未回唤醒")
    args = parser.parse_args()

    winconsole.ensure_utf8_stdio()

    # 先验身份再等唤醒：缺 self_account 时每一条 @ 都判不中，挂着也是白挂
    # （issue #51）。listen.py 经 run_passthrough 透传这个退出码。
    if require_wake_identity():
        return 1

    cursor = 0 if args.replay else (args.after if args.after is not None else read_cursor())
    # 本轮起点：过期统计只算这之后新到的唤醒。冷启动（0）不报，历史积压
    # 仍按 check_pending_reply 的契约静默（issue #69）。
    start_cursor = cursor
    deadline = time.time() + args.timeout
    ledger = up.replied_ids()

    # issue #164 PA：本会话只管一个群。事件流是单文件混装所有群（collector 写入点
    # 只有一个），所以过滤只能在这一侧做——改前这里一条与 conversation_id 有关的
    # 条件都没有，钉死群 111 的会话照样被群 222 的 @ 叫醒。未钉死时 rows_of 原样
    # 返回（单群机器与改前完全一致），并打一行说明隔离没生效。
    note = wg_isolation.note()
    if note:
        print(note)
    reported_empty = False
    # 重挂后的第一轮先回溯（issue #182）：游标之前若还压着未回唤醒，退回去让
    # 下面的 fresh 分支重放它们。只做一次，且只在默认路径——`--after` / `--replay`
    # 的起点是调用方显式给的，不许被改写。start_cursor 已是回退前的值，
    # report_expired 的口径不受影响。
    check_backlog = args.after is None and not args.replay
    # 禁言期间的暂停说明只打一次（issue #197）：这句是「为什么没动静」的解释，
    # 不是唤醒内容；按轮重复会把它变成噪声，恰好盖住真正要看的东西。
    mute_paused = False

    while True:
        raw_rows = up.events()
        rows = wg_isolation.rows_of(raw_rows)
        ledger = up.replied_ids()
        if check_backlog:
            check_backlog = False
            cursor = rewind_for_backlog(rows, cursor, ledger)
        # 过滤把自己的会话滤空了要出声（issue #164）：群号/self_account/状态目录
        # 配错时，「过滤生效」与「群里没人说话」在输出里长得一模一样。每轮只报一次。
        empty_note = wg_isolation.filter_report(raw_rows)
        if empty_note and not reported_empty:
            reported_empty = True
            print(empty_note)
        elif not empty_note:
            reported_empty = False
        # 过期唤醒和 --list 一样滤掉：冷启动后 .tmp 是新的、游标归零，陈年
        # 未回唤醒会整批变成「新唤醒」打印出来，Agent 据此回头回复旧话题。
        fresh = [e for e in rows
                 if (e.get("id") or 0) > cursor
                 and up.wake_reason(e)
                 and not up.replied_after(e.get("id"), rows, ledger)
                 and not up.wake_expired(e)]

        if fresh:
            # 一睁眼先给基准：下面每行是消息时间戳，不是现在（issue #95）。
            print(wg_clock.now_line())
            for e in fresh:
                stamp = wg_clock.clock_ms(e.get("timestamp"))
                images = wg_images.render(e) + wg_videos.render(e)
                quotes = wg_quotes.render_event(e)
                # 正文按正文字数裁、截断了带 --show 指引（issue #32）：
                # 80 字额度不再被 [引用 QQ 消息 <id>]@<qq> 这类段占位吃掉。
                body, _, _ = wg_text.render_body(e.get("content"), wg_text.WAKE_BODY_CHARS,
                                                  e.get("id"))
                # 人名指向与画像：档案锚 + 正文命中的外号 + 命中者的画像卡
                # （issue #30 / #163，口径唯一出处在 wg_dossiers.anchors）。
                gid = wg_aliases.group_of(e.get("conversation_id"))
                tag, hits, cards = wg_dossiers.anchors(e.get("sender"),
                                                       e.get("content"), gid)
                print(f"id={e.get('id')} {stamp} [{up.wake_reason(e)}] "
                      f"{e.get('sender')}{tag}: {body}"
                      f"{('  ' + quotes) if quotes else ''}"
                      f"{('  ' + images) if images else ''}"
                      f"{''.join('  ' + h for h in hits)}{cards}")
            write_cursor(max(latest_id(rows), max(e.get("id") or 0 for e in fresh)))
            # 群级术语每块只印一次（issue #31 的读侧）：它说的是「这个群这些词
            # 什么意思」，不是某条消息的属性，所以不随行重复。
            note = wg_dossiers.group_note_line(
                wg_aliases.group_of(fresh[0].get("conversation_id")))
            if note:
                print(note)
            print("WAKE")
            return 0

        # 没有新的 @/关键词，但追问窗口可能还开着：回复之后群里继续聊的几句
        # 全在 chat_inbox 里（mention_only 群里 collector 没把它们投递出来，#188；
        # natural 群普通消息已走 wg_events.jsonl 唤醒，不靠这路），顺带查一眼。
        # 窗口自己会过期；路上撞见了就顺手关掉，不用另起进程打扫。
        # 查窗口、判三态、收窗都在插件里（issue #4 PD2a）；宿主只负责渲染。
        # 禁言期间不扫追问窗口（issue #197）：群里闲聊没有一句发得出去，把 Agent
        # 叫醒只会白烧一轮，`listen.py` 还会把这一轮算成「有人叫」。窗口本身不动
        # ——不关、不推进 `consume()` 游标，解禁后照旧可用、过期照旧自关；`@` 唤醒
        # 也不受影响（有人明确找，`--wait-unmute` 能在等待预算内等）。
        muted = wg_send_failure.muted_now()
        if muted and not mute_paused:
            mute_paused = True
            print("# 禁言中：暂停追问窗口扫描（解禁后自动恢复，窗口过期照旧自关）")
        scan = None if muted else plugins.emit("wake_scan",
                                               {"settings": _plugin_settings()})
        if scan and scan.get("closed"):
            print(f"[追问] 窗口关闭（{scan['closed']}）")
        elif scan and scan.get("woke"):
            woke = scan["woke"]
            print(wg_clock.now_line())
            for r in woke:
                stamp = wg_clock.clock_ms(r.get("ts"))
                # 追问行与唤醒行同一份渲染口径（issue #32）。
                body, _, _ = wg_text.render_body(r.get("text"),
                                                  wg_text.WAKE_BODY_CHARS,
                                                  r.get("id"))
                # 人名指向与画像同唤醒行同款（issue #30 / #163）；追问行没有
                # conversation_id 字段时退回跨群主档别名。
                gid = wg_aliases.group_of(r.get("conversation_id"))
                tag, hits, cards = wg_dossiers.anchors(r.get("sender"),
                                                       r.get("text"), gid)
                print(f"id={r.get('id')} {stamp} [追问] {r.get('sender')}{tag}: "
                      f"{body}"
                      f"{('  ' + r['quote']) if r.get('quote') else ''}"
                      f"{('  ' + r['images']) if r.get('images') else ''}"
                      f"{''.join('  ' + h for h in hits)}{cards}")
            note = wg_dossiers.group_note_line(
                wg_aliases.group_of(woke[0].get("conversation_id")))
            if note:
                print(note)
            # 报告文案由 followup 决定：额度用完的那一批不说「还剩 0 条」
            # （issue #98）。收窗也在 consume() 里做完，不再留到下一轮。
            print(scan["report"])
            print("WAKE")
            return 0

        # Nothing to report: move past everything seen so a later call does not
        # rescan the whole file, then wait for the collector to append.
        if rows:
            moved = max(cursor, latest_id(rows))
            if moved != cursor:
                cursor = moved
                write_cursor(cursor)

        # 没人叫我们，但可能「叫得动」了（issue #192）：账本上压着未定论的禁言时
        # 现查一次解禁状态，确认解禁就立刻唤醒——不再等下一条消息，也不再指望
        # 有人想起来跑一次 `reply.py --list`。游标已推进过，所以下一轮监听不会
        # 把这次唤醒之前的事件当成新唤醒重放。
        if poll_unmute():
            return 0

        if time.time() >= deadline:
            report_expired(rows, start_cursor)
            print("TIMEOUT")
            return 2
        # 续心跳（issue #112）：监听在不在不该只活在进程表里。没有在值守的记录
        # 时是 no-op——裸跑 watch.py（门禁、Agent 手工调试）不会写账本。
        wg_listener_state.beat()
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
