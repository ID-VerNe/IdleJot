"""``reply.py --list`` 的渲染：唤醒块、缺口段、发送失败注脚（issue #164 顺带收口）。

从 `reply.py` 搬出来的原因有两个，都不是「代码不好看」：

1. **体量**：`reply.py` 已经贴着 `check_module_size` 的 1000 行墙（issue #11）。issue
   #164 要给 `--list` 加群过滤与「本会话群」抬头，只加不减就撞墙；而这个函数的全部
   依赖（事件流、账本、渲染口径）都是只读的，搬走不牵动任何发送路径。
2. **口径**：`--list` 与 `watch.py` 的唤醒块本来是两份渲染，`id=` / 时间戳 / 正文
   截断 / 别名指向逐行重复。搬出来之后这里是**唯一**一份「唤醒行长什么样」，
   `watch.py` 的差异只在「要不要打 now line 与 WAKE」——那部分留在 watch 里。

`reply.list_wakes` 仍是同名入口（门禁与调用点读的一直是它），只是变成一行委托。
"""
from __future__ import annotations

import wg_aliases
import wg_clock
import wg_gap
import wg_images
import wg_isolation
import wg_quotes
import wg_send_failure
import wg_text


def context_row(row, wakes, around=None):
    """`reply.py --context` 的一行：时间、说话人、正文、引用、图、别名指向、标记。

    与唤醒行同一套渲染口径（issue #30/#32/#94），区别只在「谁说的」与行尾标记
    （撤回 / 唤醒 / 回这条）。搬进来是为了让 `reply.py` 留在 1000 行墙内（#11）。
    """
    stamp = wg_clock.clock_ms(row["ts"])
    who = "我" if row["self"] else (row["sender"] or "?")
    # 上下文行同一份渲染口径（issue #32）：段占位不占额度，截断了说被切过。
    text, _, _ = wg_text.render_body(row["text"], wg_text.CONTEXT_BODY_CHARS, row["id"])
    # 人名指向：闲聊里叫外号也要知道是谁（issue #30）。
    gid = wg_aliases.group_of(row.get("conversation_id"))
    tag = "" if row["self"] else wg_aliases.sender_tag(row["sender"], gid)
    hits = [] if row["self"] else wg_aliases.body_hits(row["text"], gid,
                                                       sender=row["sender"])
    marks = []
    if row["recalled"]:
        marks.append("撤回")
    if row["id"] in wakes:
        marks.append("唤醒")
    if around and row["id"] == int(around):
        marks.append("回这条")
    tail = f"  <- {'、'.join(marks)}" if marks else ""
    quote = ("  " + row["quote"]) if row.get("quote") else ""
    alias_tail = "".join("  " + h for h in hits)
    print(f"{row['id']} {stamp} {who}{tag}: {text}{quote}"
          f"{('  ' + row['images']) if row.get('images') else ''}{alias_tail}{tail}")


def render(rows, ledger, wake_reason, wake_state):
    """按本会话的群渲染唤醒块，返回行数。

    参数而不是 import：`up` 是门面，`reply.py` 与门禁都靠**替换 `up.*`** 打桩
    （`check_pending_reply` 的 `CONSUMED_THROUGH_FACADE`）。把 `up` 传进来，
    桩打在哪儿都跟着走，本模块不需要知道 `up` 存在。
    """
    print(f"# 唤醒记录 · {wg_clock.now_line()}")
    # issue #164 PA：本会话钉死在哪一群，一眼看得见——下面每行都只来自它。
    print(f"# 本会话群 · {wg_isolation.describe()}")
    note = wg_isolation.note()
    if note:
        print(note)
    empty_note = wg_isolation.filter_report(rows)
    if empty_note:
        print(empty_note)
    # issue #114：停机窗口内漏收的 @ 本机消息（NapCat 不在的那段时间，永久不进
    # chat_inbox）。这里列出来，让「没人叫我」与「有人叫过但机器不在」可区分——
    # 列在唤醒块之前：Agent 先看到漏的、再看到当前待回的。行首 `id=-`（占位，
    # 不可 reply.py <id>），`msg_id=` 是 OneBot 原始 id，Agent 真要回手动发送用。
    # issue #148：抬头由 `wg_gap.header_lines()` 一次给全（段标题 + 窗口起止 +
    # 账本时刻），不给时刻的话三小时前的早晨账本与几分钟前的新缺口读起来一样。
    gap_lines = wg_gap.at_me_lines()
    for ln in ((wg_gap.header_lines() + gap_lines) if gap_lines else []):
        print(ln)
    wg_send_failure.print_last()
    shown = 0
    for e in wg_isolation.rows_of(rows):
        reason = wake_reason(e)
        if not reason:
            continue
        mark = wake_state(e, rows, ledger)
        stamp = wg_clock.clock_ms(e.get("timestamp"))
        images = wg_images.render(e)
        quotes = wg_quotes.render_event(e)
        # 正文渲染与 watch 唤醒行同一份口径（issue #32）：段占位不占额度，
        # 截断了带 … 和 --show 指引。
        body, _, _ = wg_text.render_body(e.get("content"), wg_text.LIST_BODY_CHARS,
                                         e.get("id"))
        # 人名指向与唤醒行同款（issue #30）。
        gid = wg_aliases.group_of(e.get("conversation_id"))
        tag = wg_aliases.sender_tag(e.get("sender"), gid)
        hits = wg_aliases.body_hits(e.get("content"), gid, sender=e.get("sender"))
        print(f"id={e.get('id')} {stamp} [{mark}] [{reason}] {e.get('sender')}{tag}: "
              f"{body}"
              f"{('  ' + quotes) if quotes else ''}{('  ' + images) if images else ''}"
              f"{''.join('  ' + h for h in hits)}")
        shown += 1
    if not shown:
        print("没有唤醒记录")
    return shown
