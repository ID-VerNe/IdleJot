"""Agent 读消息时的正文渲染：段占位不占额度，截断必须看得出来（issue #32）。

issue #32 的翻车形态：唤醒行 `(content or '')[:80]` 硬切 80 字，而 `content` 里
`[引用 QQ 消息 <id>]@<qq>` 这类段占位（入库段，见 `chatlocal/mcp_chat.py`）也占
额度——一个引用占位就 24 字符，正文实际只剩一半。更糟的是没有省略号：Agent 拿到
半句话，看不出被切过，只能自己直连 sqlite 反查全文。

同族口径在四个脚本里各写一份（watch 80 / --list 50 / --context 120 / --status
40），数字互不知情，改一处漏三处。本模块把「怎么显示一条消息的正文」收成一份：

* :func:`render_body` 先按段占位表剥离（保留可读替代），再用
  `chatlocal.text_limits.clip()` 截断——「怎么切」的唯一归属仍然是 text_limits，
  这里只决定「切之前先剔什么」和「切完怎么标记」；
* 截断后追加 `…` 与 `--show <id>` 指引，让「被切过」可检出（#90 的契约：要么
  完整，要么明说）；
* :func:`show_event` 按 id 读全文，取代「Agent 自己直连 sqlite 猜列名」：
  wg_events 命中 → 印 content 全文 + referenced_messages 展开；未命中 → 退回
  chat_inbox 只读库；两条都没有 → 明说状态，不靠空输出表达失败。

额度是调用点的参数，不写死在这里：唤醒行、列表行、上下文行、status 行的信息
密度本就不同，但都必须经过同一个 render_body。

延迟 import `chatlocal.text_limits`：daemon 脚本以
`python water-group/daemon/xxx.py` 直接跑，`sys.path[0]` 是 daemon 目录自身，
仓库根不在路径上——与 `dossier.py:event_ceiling()` 同一范式（issue #90）。
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

from wg_paths import data_dir, state_dir

STATE = state_dir()
EVENTS = STATE / "wg_events.jsonl"
DB = Path(os.environ.get("IDLEJOT_WG_DB")
          or os.environ.get("TULPA_WG_DB", data_dir() / "mcp-access.sqlite3"))

# 四个调用点的显示额度。曾经是 80 / 50 / 120 / 40 四个散落字面量；收口到这里
# 之后改一处即四处一致，且每个数字的名字说明了它服务哪一行。
WAKE_BODY_CHARS = 80     # watch.py 唤醒行 / 追问行
LIST_BODY_CHARS = 50     # reply.py --list
CONTEXT_BODY_CHARS = 120  # reply.py --context
STATUS_BODY_CHARS = 40   # up.py --status 最近唤醒

# 入库段在 content 里的占位形态（chatlocal/mcp_chat.py 的 parts.append 清单）。
# 引用占位最长：一个 `[引用 QQ 消息 123456789]` 就是 24 字符——唤醒行 80 字额度
# 里它能吃掉三成，正文反而先被切。#32 报的「…是在」断点就是这么来的。
# 剥离时保留一个短替代：读者仍要知道这里有过一个引用/图片，只是不值得占额度，
# 原文走 render_event（wg_quotes）与 --show。
#
# `@\d{5,}` 曾经也在这张表里（剥成裸 `@`），issue #138 把它撤了：@ 是**指向**
# 信息，不是占位。剥成裸 `@` 之后追问行只剩「@ 」，读者只能理解成「他在 @ 我」，
# 于是答出「你 @ 我，是想让我……」这种认错人的话。带显示名的 @ 本来就在 content
# 里（`mcp_chat.receive` 拼 `@<显示名>`），缺名字时保留 `@<qq>` 是唯一能说清
# 「@ 的是谁」的形态。自指防护（`wg_aliases.AT_MENTION_RE`）自带正则，不依赖这里。
PLACEHOLDERS = (
    (re.compile(r"\[引用 QQ 消息 \d+\]"), "[引用]"),
    (re.compile(r"\[合并转发\]"), "[转发]"),
    (re.compile(r"\[图片\]"), "[图]"),
    (re.compile(r"\[语音\]|\[视频\]|\[文件\]"), "[附件]"),
    (re.compile(r"\[表情\]|\[非文本消息\]"), "[非文本]"),
    # issue #165：未知段现在带类型（`[不支持:json]` / `[卡片：标题]`）。唤醒行
    # 是给人扫的一行，卡片标题那种几十字的东西不该占掉正文额度——剥成短替代，
    # 原始体与类型仍走 `--show`（`unsupported_lines`）。`[卡片…]` 在表里必须
    # 排在 `[不支持…]` 之后，两条互不重叠，顺序只为读起来顺。
    (re.compile(r"\[卡片[^\]]*\]|\[分享[^\]]*\]|\[位置[^\]]*\]|\[名片[^\]]*\]|\[音乐[^\]]*\]"), "[卡片]"),
    (re.compile(r"\[不支持:[^\]]*\]"), "[非文本]"),
)


def clip():
    """text_limits 的 clip，按 dossier.py:event_ceiling() 的延迟导入范式取。

    每次调用现取而不是模块级缓存：text_limits 是纯函数模块，缓存只会让测试的
    monkeypatch 失效；延迟导入的成本是一次 sys.path 命中加字典查找。
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from chatlocal.text_limits import clip as _clip
    return _clip


def strip_placeholders(text):
    """把段占位换成短替代，返回 (替代后的文本, 剥离前的原长)。

    原长按**剥离前**的整条 content 算：`--show` 指引说的是「原 N 字」，
    读者要能对着库里的行核对，占位符也是那行的一部分。这也让「原 N 字」
    在任何渲染路径上都指向同一个数——库里那行有多长。
    """
    value = "" if text is None else str(text)
    original_length = len(value)
    for pattern, short in PLACEHOLDERS:
        value = pattern.sub(short, value)
    return value, original_length


def render_fitted(value, limit):
    """短显示截断：折叠空白、裁到 limit、超限尾部只加 `…`，不带任何提示。

    给引用原文、转发摘要、发送者名这类「一行后缀」用（wg_quotes._fit）：
    它们出现在别人的正文后面，再挂一句「--show 读全文」会把唤醒行撑散。
    """
    if limit <= 0:
        raise ValueError("render_fitted() needs a positive limit")
    text = " ".join(str(value or "").split())
    result = clip()(text, limit)
    return result.text if not result.truncated else f"{result.text}…"


def render_body(text, limit, event_id=None):
    """一行里能显示的正文：段占位不占额度，截断了就说被切过。

    返回 (text, truncated, original_length)，与 text_limits.Clipped 同构——
    唯一差别是截断时尾部有 `…` 和读全文的路径。调用方直接把 text 拼进行里，
    不需要再判断 truncated；参数化 event_id 只是为了生成那条路径（拿得到才给）。
    """
    if limit <= 0:
        raise ValueError("render_body() needs a positive limit")
    stripped, original_length = strip_placeholders(text)
    result = clip()(stripped, limit)
    if not result.truncated:
        return result.text, False, original_length
    hint = f"（原 {original_length} 字）"
    if event_id:
        hint = f"（原 {original_length} 字，--show {event_id} 读全文）"
    return f"{result.text}…{hint}", True, original_length


def render_handler_reply(text, limit, event_id=None):
    """资讯 handler 的回复正文截断（issue #169 PB）：唯一归宿，三份 `_clip` 合流到此。

    `render_body` 是「Agent 读消息」的渲染口径（唤醒行/列表/上下文），它剥段占位、
    带 `--show <id>` 读全文指引。资讯 handler 的输出不是某条群消息的正文——是简报、
    热搜、班次这类**生成的**多行文本——没有段占位可剥，也没有「按 id 读全文」的库
    行可指，只求「超长就截、截了就标」。所以这里走 `clip()` 的硬切（不剥占位、不回落
    句末），尾部只挂 `…（已截断）`，不挂 `--show` 指引。

    三份旧实现（`sixty._clip` / `miyoushe._clip` / `busboss._clip`）的写法是
    `text[:limit] + " …（已截断）"`（先切到上限再加 7 字尾巴，净超 7）与
    `text[:limit - 1] + "…"`。两者都让输出超过自己声明的 `REPLY_CLIP`——正是
    #169 复现 B。这里必须守住 `limit`：先给尾巴让出 7 字（`…（已截断）` 的长度），
    再用 `clip()` 切剩下的额度，最后拼尾巴——净输出恰好 `limit`，不再超上限。

    `event_id` 保留参数位与 `render_body` 同构，但资讯回复不挂 `--show`——
    留它是为了将来若要给资讯结果也加「展开全文」，调用点不用再改签名。
    """
    if limit <= 0:
        raise ValueError("render_handler_reply() needs a positive limit")
    marker = "…（已截断）"
    # 尾巴的长度先从额度里扣掉，clip() 再切剩下的，净输出 = limit。
    # limit 装不下「正文 + 完整尾巴」时（极短上限），把尾巴截到能装下的长度——
    # 这种上限本身不该用于资讯回复（REPLY_CLIP 都是几百字），但守住「不超 limit」
    # 比守「尾巴完整」更重要：超上限就是 #169 复现 B 的原症状。
    if limit <= len(marker):
        return marker[:limit]
    result = clip()(text, limit - len(marker))
    if not result.truncated:
        return result.text
    return f"{result.text}{marker}"


def bounded_handler(fn, *, limit, need_ck=True):
    """资讯 handler 的注册装饰器（issue #169 PB）：截断 + 凭证标记的唯一归宿。

    `sixty._bounded` 与 `miyoushe._bounded` 原是同一段包装器复制两次，唯一差别是
    `need_ck` 默认值（60s 全 False、米游社默认 True）与 `limit`（各自 `REPLY_CLIP`）。
    合流后共享逻辑（包一层截断 + 落 `need_ck` 属性）只在此处；各模块的 `_bounded`
    退成一行闭包，绑定自己的 `limit` 与默认 `need_ck`——那是模块业务上限，不是重复实现。

    `commands.run_handler` 与插件层（`sixty_plugin` / `miyoushe_plugin`）都按
    `getattr(fn, "need_ck", ...)` 读这个属性，所以装饰器必须真把属性贴在 wrapped 上。
    """
    def wrapped(args, ctx):
        return render_handler_reply(fn(args, ctx), limit)
    wrapped.need_ck = need_ck
    return wrapped


def _read_events():
    rows = []
    try:
        for line in EVENTS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def _inbox_row(event_id):
    """chat_inbox 里按全局 rowid 取一行；拿不到或库不可读都返回 None。

    只读连接，与 followup.lookup / reply.recent_chatter 同一套姿势：
    `?mode=ro` 保证这条路径永远不改库。
    """
    try:
        db = sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        row = db.execute("SELECT payload FROM chat_inbox WHERE id=?",
                         (int(event_id),)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    if not row:
        return None
    try:
        payload = json.loads(row[0])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _print_event(event_id, payload, source):
    """一条消息的全文。引用/转发展开字段在就原样附在后面，不截断。"""
    import wg_clock
    import wg_images
    import wg_quotes

    stamp = wg_clock.stamp((payload.get("timestamp") or 0) / 1000)
    print(f"id={event_id} {stamp} {payload.get('sender') or '?'}（{source}）")
    print(payload.get("content") or "")
    if payload.get("truncated"):
        # 入库时就超了 12000 字上限：字段说的是实话，这里必须转述。
        print("（入库时已超单条上限，正文不完整）")
    for line in unsupported_lines(payload.get("segments")):
        # issue #165 PC：content 里只有一句「不支持:json」，读者补读时要知道
        # 原始体在不在、被裁过没有。说得出就说，说不出的写「未保留」。
        print(line)
    suffix = wg_quotes.render_event(payload)
    images = wg_images.render(payload)
    if suffix:
        print(suffix)
    if images:
        print(images)
    return 0


def unsupported_lines(segments):
    """`--show` 里为降级段补的说明行（issue #165 PC）。

    content 那句 `[卡片：标题]` 只说得出「是什么」；原始体留没留、留了多少，
    只有段上知道。这里逐段报「类型 + 原始体状态」，让「这条我收到的是空壳」
    变成可判断的结论——`raw_truncated` 是实话，不许改写成「无原始体」。
    """
    lines = []
    for segment in segments or []:
        if not isinstance(segment, dict) or segment.get("type") != "unsupported":
            continue
        kind = str(segment.get("kind") or "unknown")
        raw = segment.get("raw")
        if raw:
            state = "原始体已截断" if segment.get("raw_truncated") else "原始体完整"
            lines.append(f"（段 {kind}：{state}，{len(raw)} 字）")
        else:
            reason = "原始体超预算未保留" if segment.get("raw_truncated") else "无原始体"
            lines.append(f"（段 {kind}：{reason}）")
    return lines


def show_event(event_id):
    """按 id 读一条消息的全文：wg_events → chat_inbox → 明说没有。

    wg_events.jsonl 只收 @ 和关键词（mention_only），追问要读的闲聊不在里面，
    所以第二条路必须有；chat_inbox 每会话只留最近 1000 条
    （`chatlocal/mcp_chat.py` 的 cutoff），滚出去了就说滚出去了，不编。
    """
    for row in _read_events():
        if row.get("id") == event_id:
            return _print_event(event_id, row, "wg_events")

    payload = _inbox_row(event_id)
    if payload is not None:
        return _print_event(event_id, payload, "chat_inbox")

    print(f"本地没有 id={event_id} 的全文。")
    print("可能的原因：它超过 1000 条被 chat_inbox 挤掉（每会话只留最近 1000 条）、")
    print("不在当前数据根的库里、或这个 id 从来不存在。")
    return 1
