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
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

import wg_event_ref  # noqa: E402  (按 id 取行与来源顺序的唯一入口，issue #216)

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


def connect_ro(path, *, timeout=5.0):
    """chatlocal.sqlite_util.connect_ro 的延迟导入门面（issue #169 PC）。

    daemon 读侧（dossier / followup / wg_context / wg_gap / wg_text / mint_grant）
    原本各写一份 `sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True,
    timeout=5)`——Windows 反斜杠直接拼 `?mode=ro` 会被 sqlite 当文件名的一部分，
    `wg_gap.py:63` 把这个坑写成了注释却没进助手。合流到 `chatlocal.sqlite_util.
    connect_ro`，本门面只做延迟导入（同 `clip()` 范式：`chatlocal/config.py` 在
    import 期建数据目录，daemon 不能模块级 `import chatlocal`）。timeout 默认 5.0
    与原读侧一致；`investigation_sql` 那种跑不可信 SQL 的紧预算自己传 `timeout=0.5`。
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from chatlocal.sqlite_util import connect_ro as _connect_ro
    return _connect_ro(path, timeout=timeout)


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


def render_body(text, limit, event_id=None, name=None):
    """一行里能显示的正文：段占位不占额度，截断了就说被切过。

    返回 (text, truncated, original_length)，与 text_limits.Clipped 同构——
    唯一差别是截断时尾部有 `…` 和读全文的路径。调用方直接把 text 拼进行里，
    不需要再判断 truncated；参数化 event_id 只是为了生成那条路径（拿得到才给）。

    ``name`` 是那条路径上要打的记号（issue #203）。事件流的行仍打裸数字
    ``--show 7``；会话库的行（追问行 / 上下文行）必须打 ``--show q7``——同一个
    数字在两套编号里都可能有，提示里不给命名空间就等于教 Agent 抄错。省略时
    退回裸 ``event_id``，与改前一致。
    """
    if limit <= 0:
        raise ValueError("render_body() needs a positive limit")
    stripped, original_length = strip_placeholders(text)
    result = clip()(stripped, limit)
    if not result.truncated:
        return result.text, False, original_length
    hint = f"（原 {original_length} 字）"
    ref = name if name is not None else event_id
    if ref:
        hint = f"（原 {original_length} 字，--show {ref} 读全文）"
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


def _print_event(event_id, payload, source, name=None):
    """一条消息的全文。引用/转发展开字段在就原样附在后面，不截断。

    ``name`` 是首行要打给 Agent 抄的记号（issue #203）：会话库的行打 ``q<n>``，
    事件流的行打裸数字。省略时按裸 ``event_id`` 打，与改前一致。
    """
    import wg_clock
    import wg_images
    import wg_quotes

    ref = name if name is not None else event_id
    stamp = wg_clock.stamp((payload.get("timestamp") or 0) / 1000)
    print(f"id={ref} {stamp} {payload.get('sender') or '?'}（{source}）")
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


def show_event(event_id, namespace=None):
    """按 id 读一条消息的全文：wg_events → chat_inbox → 明说没有。

    wg_events.jsonl 只收 @ 和关键词（mention_only），追问要读的闲聊不在里面，
    所以第二条路必须有；chat_inbox 每会话只留最近 1000 条
    （`chatlocal/mcp_chat.py` 的 cutoff），滚出去了就说滚出去了，不编。

    issue #203：两套编号会撞号，``--show 7`` 与 ``--show q7`` 必须读到不同的两条。
    ``namespace`` 显式给出时只查那一处（``wg_event_ref.EVENT`` / ``.INBOX``）；
    省略时保留改前的回退链（事件流优先，未命中回库）——直接调本函数的旧调用方
    不受影响，``reply.py --show`` 则由 ``wg_event_ref.parse`` 给出命名空间。

    issue #216：来源顺序与「按 id 取行」都归 `wg_event_ref`——这里以前自己写了一份
    「先扫事件流、再查库」的扫描，与 `reply.event_target` 各认一套字段名，于是同一个
    id 两个入口给出两种结论（这里读得到、那边回不了）。命名空间决定问哪一处，
    回退仍然只发生在裸数字上。
    """
    ref = wg_event_ref.resolve_id(event_id, namespace=namespace)
    if ref is not None:
        name = wg_event_ref.format_ref(ref.namespace, ref.event_id)
        return _print_event(event_id, ref.payload, ref.source, name=name)
    # 读不到时记号按**字面**写法打：裸数字没说要看哪套，就用裸数字问一遍。
    name = (wg_event_ref.format_ref(namespace, event_id) if namespace
            else str(event_id))
    return _missing(name, src_name(namespace))


def src_name(namespace):
    """命名空间 -> ``_missing`` 认的来源名（``wg_events`` / ``chat_inbox`` / ``""``）。"""
    if namespace is None:
        return ""
    return wg_event_ref.SRC_INBOX if namespace == wg_event_ref.INBOX else wg_event_ref.SRC_EVENTS


def _missing(ref, source):
    """读不到那条时说清是哪套编号、读的是哪一处，以及可能的原因。"""
    where = {"wg_events": "事件流 wg_events.jsonl",
             "chat_inbox": "会话库 chat_inbox"}.get(source, "本地")
    print(f"{where}里没有 id={ref} 的全文。")
    if source == "chat_inbox":
        print("它可能超过 1000 条被 chat_inbox 挤掉（每会话只留最近 1000 条）、")
        print("不在当前数据根的库里、或这个 id 从来不存在。")
    elif source == "wg_events":
        print("事件流只收 @ 与关键词命中的消息，闲聊不在里面；")
        print("要看群里的闲聊请用追问行/上下文行的 q<n>（--show q<n>）。")
    else:
        print("可能的原因：它超过 1000 条被 chat_inbox 挤掉（每会话只留最近 1000 条）、")
        print("不在当前数据根的库里、或这个 id 从来不存在。")
    return 1
