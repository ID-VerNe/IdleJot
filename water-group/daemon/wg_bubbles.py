"""一次回复能发几条气泡：上限与拆句的唯一出口（issue #131）。

这条上限过去是 `reply.py` 里一个裸字面量 `3`，超出部分只往 stdout 打一行 NOTE
就扔掉——群里看到的是一段戛然而止的回复，而发送层（OneBot 直发）根本没有条数
限制，是拆句在进发送之前就把第 4 句以后砍了。总结类长文因此发不全，补发又丢了
引用关系。

现在两个上限各归其位，都不再是散的裸数字：

    MAX_BUBBLES        20  OneBot 直发 / CLI 的上限，`config.json` 的
                           `reply.max_bubbles` 可下调；这条路径没有队列也没有 TTL，
                           唯一的代价是 `BUBBLE_GAP` 累积的发送时长，所以给得宽。
    TURN_MAX_BUBBLES   12  MCP 计划通道（`send_chat_reply`）的上限。这条路径每个
                           气泡之间要做 `check()`，而 `plan_ttl_seconds` 默认 90s、
                           `bubble_max_seconds` 默认 4s：20 条最坏 ≈ 80s 会顶到 TTL、
                           尾部被 `plan_expired` 取消，12 条（最坏 ≈ 48s）留足余量。

`split()` 仍然只按换行拆句，不按标点机械切——「一行一句」是 issue #80 定下的语义。
超上限时不再静默：提示必须同时说出「已发几条、丢弃几条、丢的是什么」，因为被截
的句子通常是补刀，写的人得知道自己少说了什么。
"""
import json
import math

import wg_budget  # noqa: E402  (预算的唯一出口，issue #79/#83)
from wg_paths import data_dir  # noqa: E402  (数据根的唯一出口)

MAX_BUBBLES = 20
TURN_MAX_BUBBLES = 12
# 多条气泡之间的间隔。计划通道按 bubble_min_seconds 算（默认 1.2s 起，随上一条
# 长度增加）；OneBot 直发这条路径不经过那套策略，用同一个下限保持手感一致。
BUBBLE_GAP = 1.2
# 计划通道的默认间隔区间，与 `chatlocal/mcp_chat_turns.py` 的 DEFAULTS 同一组数
# （那边不能 flat import 到 daemon，只能各自持有；真实值以数据根的 policy 文件为准）。
PLAN_GAP = (1.2, 4.)


def normalize_newlines(text):
    """把命令行里常见的字面量 \n 归一成真换行。

    reply.py 的正文是命令行参数，`"第一句\n第二句"` 在 shell 双引号里不会变成
    换行——进来的是反斜杠加 n 两个字符。splitlines() 只认真换行，于是整串被
    当成一行发成一条气泡，literal \n 明晃晃挂在群里（issue #80）。

    双反斜杠是逃生门：`\\n` 转义成字面 `\n` 留在正文里，不当分隔符——真要发
    一个反斜杠加 n 时用这个写法。tab 不特殊处理：行首行尾的缩进由 split 每行的
    strip 去掉，行中的 tab 原样保留。
    """
    text = (text or "").replace("\\\\", "\x00")
    text = text.replace("\\r\\n", "\n").replace("\\r", "\r").replace("\\n", "\n")
    return text.replace("\x00", "\\")


def split(text, limit=MAX_BUBBLES):
    """按换行拆成多条气泡；字面量 \n / \r\n 也当换行（normalize_newlines）。

    QQ 会把单条消息里的换行折叠成一个空格，所以「一行一句」只能靠多条气泡落地。
    人设卡要求「想补充就发下一条」，而 reply.py 过去只发一个气泡——于是想表达
    多层意思时只能拿斜杠硬连成一行，读起来像文档不像群聊。

    空行忽略；首尾空白去掉。超过 `limit` 时截断并打一行 WARN——**必须同时说出
    已发几条、丢弃几条、丢的是什么**（issue #131）：过去那句「只发前 3 条」混在
    `send: retcode=...` 之间，读的人根本不知道少了哪两句。
    """
    limit = max(1, int(limit))
    text = normalize_newlines(text)
    parts = [line.strip() for line in text.splitlines()]
    parts = [line for line in parts if line]
    if len(parts) > limit:
        dropped = parts[limit:]
        print(f"  WARN 气泡上限 {limit} 条：已发 {limit} 条、丢弃 {len(dropped)} 条："
              f"{dropped}（--max-bubbles 或 config reply.max_bubbles 可调）")
        parts = parts[:limit]
    return parts or [""]


def config_limit(cfg=None):
    """`config.json` 的 `reply.max_bubbles`；缺省/非法都回到 `MAX_BUBBLES`。

    非法值不静默回落：写错的人下次还得再猜一遍。上限一概夹在 1…`MAX_BUBBLES`，
    因为 `MAX_BUBBLES` 是这条路径的硬顶（issue #131 明确不做无限条）。
    """
    section = cfg.get("reply") if isinstance(cfg, dict) else None
    raw = section.get("max_bubbles") if isinstance(section, dict) else None
    if raw is None:
        return MAX_BUBBLES
    if isinstance(raw, bool) or not isinstance(raw, int):
        # 布尔是 int 的子类，`true` 会被当成 1 条——那不是配置者的本意。
        if not (isinstance(raw, str) and raw.strip().isdigit()):
            print(f"  WARN config reply.max_bubbles={raw!r} 不是正整数，按默认 "
                  f"{MAX_BUBBLES} 条处理")
            return MAX_BUBBLES
        value = int(raw.strip())
    else:
        value = raw
    if not 1 <= value <= MAX_BUBBLES:
        print(f"  WARN config reply.max_bubbles={raw!r} 超出 1–{MAX_BUBBLES}，"
              f"按 {min(max(value, 1), MAX_BUBBLES)} 条处理")
        return min(max(value, 1), MAX_BUBBLES)
    return value


def estimate_seconds(count, gap=BUBBLE_GAP):
    """发 `count` 条气泡最少要花多久（只有间隔，不含单条发送耗时）。"""
    return max(0, int(count) - 1) * gap


def plan_gap_range(policy=None):
    """计划通道的间隔区间 `(下界, 上界)`，读数据根下的 `mcp-chat-policy.json`。

    **不能 `import chatlocal`**（同 `wg_up_wake.app_plugins_line` 的理由：那是
    app 侧，daemon 只按文件读），所以这里按同一份文件复述取值。文件缺省或字段
    非法时回落 `PLAN_GAP`——报账只需要一个量级正确的区间，不是校验器；真正的
    校验在 `ChatTurns.policy()`（那里非法会 `chat_policy_invalid` 拒绝发送）。
    """
    low, high = PLAN_GAP
    if policy is None:
        try:
            raw = json.loads((data_dir() / "mcp-chat-policy.json").read_text("utf-8-sig"))
        except (OSError, ValueError):
            raw = None
        policy = raw if isinstance(raw, dict) else {}
    try:
        low = float(policy.get("bubble_min_seconds", low))
        high = float(policy.get("bubble_max_seconds", high))
    except (TypeError, ValueError):
        return PLAN_GAP
    if not (math.isfinite(low) and math.isfinite(high)) or low < 0 or high < low:
        return PLAN_GAP
    return low, high


def cost_note(count, wait=None, gap=BUBBLE_GAP, gap_max=None):
    """发送前报出预计耗时与「这条路径上还能等多久」；一条气泡时返回空串。

    `--wait` 的额度是「整次调用共享的总额」（issue #88），发送占掉的时间会从等唤醒
    的额度里扣。20 条 ≈ 23s 不是小数目，发之前说出来，别让长回复悄悄把预算吃光
    （issue #131）。

    `wait` 是调用方这一次实际要等的秒数（`--no-wait` 就是 0），缺省按预算上限算。
    不传实际值会写出一句假话：`--no-wait` 时说「还能等 85s」、`--wait 30` 时也说
    85s——输出里的假话比没有输出更坏（#88 的教训）。

    `gap_max` 给出时按**区间**报（issue #136）：计划通道的真实间隔是
    `bubble_min_seconds…bubble_max_seconds`（默认 1.2–4s，随上一条长度增加），
    只报 `BUBBLE_GAP` 会把预算占用说小。区间形态下句子用「至少 Xs（最坏 Ys）」，
    剩余额度按**最坏**扣——报账宁可保守。
    """
    count = int(count)
    if count < 2:
        return ""
    wait = wg_budget.WAIT_BUDGET if wait is None else max(0, int(wait))
    wait = min(wait, wg_budget.WAIT_BUDGET)
    low = estimate_seconds(count, gap)
    if gap_max is None:
        span = f"间隔 {gap:g}s × {count - 1} ≈ {low:.1f}s"
        spend = low
    else:
        high = estimate_seconds(count, gap_max)
        span = f"间隔 {gap:g}–{gap_max:g}s × {count - 1} 至少 {low:.1f}s（最坏 {high:.1f}s）"
        spend = high
    if not wait:
        tail = "本次不续挂监听 --no-wait，不占等待预算"
    else:
        tail = f"本次最多等 {wait}s，发送后还能等 {max(0, wait - int(math.ceil(spend)))}s"
    return f"  cost: {count} 条气泡，{span}（{tail}）"


def cost_note_for(count, wait=None, plan_path=False, policy=None):
    """按发送路径选间隔形态（issue #136）。

    计划通道的真实间隔是 policy 的 `bubble_min…max` 区间，只报 `BUBBLE_GAP` 会把
    预算占用说小；OneBot 直发不经过那套策略，用固定节奏就是实话。两条路径分开报，
    才不会让「cost」在一条路上是账、在另一条路上是猜。

    `policy` 只为门禁可注入：运行时一律读数据根下的 policy 文件。
    """
    low, high = plan_gap_range(policy) if plan_path else (BUBBLE_GAP, None)
    return cost_note(count, wait, gap=low, gap_max=high)


def plan_overflow_message(count):
    """计划通道装不下的条数：返回该说的话，装得下返回 None。

    计划通道（不带 OneBot 编号时才会被选中）卡在 `TURN_MAX_BUBBLES`。**在发送
    之前**就要拒绝：发到第 13 条才发现装不下，群里留下的是半截回复，而首条引用
    已经用掉了（issue #131）。
    """
    if int(count) <= TURN_MAX_BUBBLES:
        return None
    return (f"计划通道（send_chat_reply）一次最多 {TURN_MAX_BUBBLES} 条气泡，"
            f"这次拆出 {count} 条：一条都没发。把正文压到 {TURN_MAX_BUBBLES} 行以内，"
            f"或去掉 --no-quote 让首条挂引用走 OneBot 直发（上限 {MAX_BUBBLES} 条）")
