"""`reply.py --list` 的只读视图（issue #203 从未超红线，这句是给后来人看的）。

判定口径与唤醒行、`up.py --status` 同源：谁叫的我、回过没有、还值不值得回
（第三条是待回时效）——所以这里 import `up`，不自己再算一份。
"""
import up


def list_wakes():
    """Same wake rules and same answered-check as `up.py --status`.

    Importing up keeps one source of truth for "who woke us", "did we answer"
    and "is it still worth answering" — the third one is the reply TTL, so a
    wake older than `pending_reply.max_age_seconds` reads [过期], not [待回].

    issue #164 PA：只看本会话的群。改前遍历全量事件流，钉死群 111 的会话照样
    看到群 222 的唤醒——两个会话都以为那些 @ 是给自己的。未钉死时不过滤（与改前
    一致），但会打一行 `note()` 说清隔离没生效。
    """
    import wg_clock
    import wg_dossiers
    import wg_event_ref
    import wg_gap
    import wg_images
    import wg_isolation
    import wg_quotes
    import wg_send_failure
    import wg_text

    rows = up.events()
    # issue #164 PA：只看本会话的群。改前遍历全量事件流，钉死群 111 的会话照样
    # 看到群 222 的唤醒——两个会话都以为那些 @ 是给自己的。未钉死时不过滤（与
    # 改前一致），但下面会打 `note()` 说清隔离没生效。
    rows = wg_isolation.rows_of(rows)
    ledger = up.replied_ids()
    # 表头先给「现在」：下面每行只有消息自己的时间戳，没有基准的话，Agent
    # 分不清一条 20:50 的 @ 是刚发的还是十分钟前的（issue #95）。
    print(f"# 唤醒记录 · {wg_clock.now_line()}")
    # issue #164 PA：本会话钉死在哪一群，一眼看得见——下面每行都只来自它。
    print(f"# 本会话群 · {wg_isolation.describe()}")
    warn = wg_isolation.note()
    if warn:
        print(warn)
    # issue #114：停机窗口内漏收的 @ 本机消息在这里列出来，让「没人叫我」与
    # 「有人叫过但机器不在」可区分。抬头由 `wg_gap.header_lines()` 一次给全
    # （段标题 + 窗口起止 + 账本时刻，issue #148）：不给时刻，三小时前的早晨
    # 账本与几分钟前的新缺口读起来一样。
    gap_lines = wg_gap.at_me_lines()
    for ln in ((wg_gap.header_lines() + gap_lines) if gap_lines else []):
        print(ln)
    wg_send_failure.print_last()
    shown = 0
    for e in rows:
        reason = up.wake_reason(e)
        if not reason:
            continue
        mark = up.wake_state(e, rows, ledger)
        stamp = wg_clock.clock_ms(e.get("timestamp"))
        images = wg_images.render(e)
        quotes = wg_quotes.render_event(e)
        # 正文渲染与 watch 唤醒行同一份口径（issue #32）：段占位不占额度，
        # 截断了带 … 和 --show 指引。
        body, _, _ = wg_text.render_body(e.get("content"), wg_text.LIST_BODY_CHARS,
                                          e.get("id"))
        # 人名指向与画像与唤醒行同款（issue #30 / #163）。
        gid = wg_event_ref.group_of(e.get("conversation_id"))
        tag, hits, cards = wg_dossiers.anchors(e.get("sender"), e.get("content"), gid)
        print(f"id={e.get('id')} {stamp} [{mark}] [{reason}] {e.get('sender')}{tag}: "
              f"{body}"
              f"{('  ' + quotes) if quotes else ''}{('  ' + images) if images else ''}"
              f"{''.join('  ' + h for h in hits)}{cards}")
        shown += 1
    note = wg_dossiers.group_note_line(
        wg_event_ref.group_of(rows[-1].get("conversation_id")) if rows else "")
    if note:
        print(note)
    # 过滤把自己滤空要说出来（issue #164）：群号/self_account/状态目录配错时，
    # 「过滤生效」与「群里没人说话」在输出里完全一样，Agent 会一直等一个不会
    # 来的唤醒。正常空流不吵——`filter_report` 只在「有东西但一条都不属于我」时出声。
    empty = wg_isolation.filter_report(up.events())
    if empty:
        print(empty)
    if not shown:
        print("没有唤醒记录")
    return 0
