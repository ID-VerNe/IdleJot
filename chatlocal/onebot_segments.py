"""OneBot 11 message segments shared by ingest and detail rendering.

Both the live ingest path and the quoted-message renderer rebuild segments
from raw OneBot payloads. Keeping the field names and truncation limits here
stops the two paths from drifting apart.

issue #165: 白名单之外的段（卡片 / 小程序 / 分享 / 位置 / 戳一戳 …）以前只留一句
``[非文本消息]``，原始 ``kind`` 与整段 ``data`` 一起消失。更糟的是降级写法
``dict(type=label)`` 把**中文标签**写进了 ``segments[].type``——下游拿到的不是
「类型未知」而是「类型就叫非文本消息」，连「这是卡片还是戳一戳」都无从判断，
也没有任何字段可供补读。所以保真形态收在这里，两条路径共用一份：

* :func:`unsupported` 产出 ``{type:'unsupported', kind:…, raw:…}``，``type`` 恒为
  英文标识，``kind`` 是归一后的原始段名，``raw`` 是受限长度的原始体（裁过就带
  ``raw_truncated``）；
* :func:`label` 只决定 **content 里给人看的那几个字**，绝不回流到 ``type``；
* :func:`card_summary` 从 ``json`` / ``xml`` / ``share`` 段里取标题或摘要（PB），
  取不到就返回空串，由调用方退回类型标签——不把「解析不出」写成「没有内容」。

原始体用 ``raw`` 而不是 ``data``：``mcp_chat_details`` 里有一条历史兼容分支，
把 ``{type, data}`` 当成旧版 OneBot 段展开（``dict(type=seg['type'], **seg['data'])``），
键名一旦撞上，字符串会当关键字参数展开而抛 TypeError。
"""
import html
import json
import re

from .text_limits import READ_PREVIEW_CHARS, clip

ID_LIMIT = 100
QQ_LIMIT = 30
NAME_LIMIT = 100
KIND_LIMIT = 32          # 段名是短标识；超长的是坏数据，不是新类型
CARD_SUMMARY_CHARS = 40  # content 里的卡片摘要：够看出是什么，不把唤醒行占满

# 单条消息里所有降级段原始体的合计上限。入库 payload 有 20000 字符的整体封顶
# （mcp_chat.receive 末尾），逐段 2000 而不合计的话，十条卡片就能把整条消息的
# segments 挤成空列表——修一个丢字段的 bug 换一个丢整条消息的 bug。
UNSUPPORTED_TOTAL_CHARS = 4000
UNSUPPORTED_RAW_CHARS = READ_PREVIEW_CHARS  # 单段：与「交给 Agent 的一段正文」同额

# content 里给人看的标签。**不进** segments[].type（issue #165 的病根）。
LABELS = {
    'image': '图片', 'record': '语音', 'video': '视频', 'file': '文件',
    'face': '表情', 'forward': '合并转发', 'reply': '引用', 'at': '@',
    'json': '卡片', 'xml': '卡片', 'share': '分享', 'location': '位置',
    'contact': '名片', 'music': '音乐', 'markdown': 'Markdown',
    'poke': '戳一戳', 'dice': '骰子', 'rps': '猜拳',
}

# OneBot 段名形态：小写字母开头，字母数字下划线。旧行里存的中文标签不是段名，
# 归一成 unknown——把「非文本消息」当成 kind 传下去等于换了个地方说假话。
KIND_RE = re.compile(r'[a-z][a-z0-9_]{0,31}')
_XML_TAG_RE = r'<{tag}>(.*?)</{tag}>'
_XML_TAGS = ('title', 'summary', 'des', 'prompt')
_JSON_KEYS = ('prompt', 'summary', 'title', 'desc')
_JSON_NESTS = ('meta', 'news', 'detail', 'card', 'data')


def message_id(raw):
    """Normalise an OneBot message id for use inside a segment."""
    return str(raw or '')[:ID_LIMIT]


def reply(raw_id):
    return dict(type='reply', onebot_message_id=message_id(raw_id))


def qq(raw_qq):
    """Normalise an @-target QQ number."""
    return str(raw_qq or '')[:QQ_LIMIT]


def name(raw_name):
    """Normalise the display name carried by an @ segment (issue #52).

    OneBot's raw at segment has ``data.name`` (the @-ed member's display
    name). Dropping it here is what made every wake line render a bare
    ``@``; the ingest and detail-render paths share this normaliser so
    they cannot drift apart on the field name or the limit. Whitespace-only
    names count as absent -- ``@`` followed by blanks is the original defect
    wearing a disguise.
    """
    return str(raw_name or '').strip()[:NAME_LIMIT]


def at(raw_qq, raw_name=None):
    segment = dict(type='at', qq=qq(raw_qq))
    display = name(raw_name)
    if display:
        segment['name'] = display
    return segment


def kind_of(raw_kind):
    """归一原始段名；不是 OneBot 段名形态的一律记 ``unknown``。"""
    value = str(raw_kind or '').strip().lower()
    return value if KIND_RE.fullmatch(value) else 'unknown'


def label(raw_kind):
    """content 里那个方括号标签：认识的给中文名，不认识的点名原始 kind。"""
    kind = kind_of(raw_kind)
    if kind in LABELS:
        return LABELS[kind]
    return '非文本消息' if kind == 'unknown' else '不支持:' + kind


def unsupported(raw_kind, raw_data=None, budget=None):
    """保真降级：原始段名与受限长度的原始体都留下来（issue #165）。

    ``budget`` 是**一条消息**共享的 ``{'raw': 剩余字符数}``；不传就按单段上限算。
    预算耗尽时只留 ``raw_truncated``，不假装原始体为空——「没保留」与「本来就是
    空的」是两件事，读者要能分开。
    """
    segment = dict(type='unsupported', kind=kind_of(raw_kind))
    body = _raw_body(raw_data)
    if not body:
        return segment
    allowance = _allowance(budget)
    kept = clip(body, allowance) if allowance > 0 else None
    if kept is not None and kept.text:
        segment['raw'] = kept.text
        if kept.truncated:
            segment['raw_truncated'] = True
        if isinstance(budget, dict):
            budget['raw'] = max(0, int(budget.get('raw', UNSUPPORTED_TOTAL_CHARS)) - len(kept.text))
    else:
        segment['raw_truncated'] = True
        if isinstance(budget, dict):
            budget['raw'] = 0
    return segment


def card_summary(raw_data):
    """卡片类段的可读摘要（issue #165 PB）：标题 / prompt / 摘要，取不到返回空串。

    ``json`` 段的 ``data.data`` 是一层 JSON 字符串（NapCat 原样透传），``xml`` 段
    是一段 XML 文本，``share`` 段则直接给 ``title``。两条都不上 XML 解析器：
    QQ 卡片是外部输入，实体展开这类攻击面不值得为一个标题引入；正则只取前几个
    已知标签，取不到就当没有。

    入参既可以是原始 ``data``，也可以是 ``unsupported`` 段落下来的 ``raw`` 文本
    （``--show`` 走的是后者），两种形态都要能读出标题——否则同一张卡片在入库时
    有摘要、补读时又变成一句「不支持」，等于把修好的缺陷换个入口复现。

    方括号在摘要里一律换成空格：摘要要嵌进 ``[卡片：…]`` 这个占位里，而 QQ 卡片
    的 prompt 十有八九自带 ``[QQ小程序]`` 前缀。嵌套方括号会让唤醒行的占位剥离
    正则（`\\[卡片[^\\]]*\\]`）停在第一个 ``]``，把后半截标题留在正文里——那就是
    「卡片标题不占额度」这条承诺的反例。
    """
    found = _summary_from(raw_data, 0)
    if not found:
        return ''
    cleaned = found.replace('[', ' ').replace(']', ' ')
    return clip(' '.join(cleaned.split()), CARD_SUMMARY_CHARS).text


def _allowance(budget):
    """这一段落最多能留多少原始体，并把预算初始化/扣减留给调用处。"""
    if not isinstance(budget, dict):
        return UNSUPPORTED_RAW_CHARS
    if 'raw' not in budget:
        budget['raw'] = UNSUPPORTED_TOTAL_CHARS
    return max(0, min(UNSUPPORTED_RAW_CHARS, int(budget['raw'])))


def _raw_body(raw_data):
    """原始 data 的稳定文本形态：键序固定，免得同一段两次落库字节不同。"""
    if raw_data is None:
        return ''
    if isinstance(raw_data, str):
        return raw_data.strip()
    try:
        return json.dumps(raw_data, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(raw_data)


def _summary_from(value, depth):
    """在一段可能是 dict / JSON 文本 / XML 文本 / 普通字符串的数据里找摘要。"""
    if depth > 4 or value is None:
        return ''
    if isinstance(value, dict):
        for key in _JSON_KEYS:
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
        for key in _JSON_NESTS:
            found = _summary_from(value.get(key), depth + 1)
            if found:
                return found
        return ''
    if not isinstance(value, str):
        return ''
    text = value.strip()
    if not text:
        return ''
    if text.startswith('{'):
        try:
            return _summary_from(json.loads(text), depth + 1)
        except ValueError:
            return ''
    if text.startswith('<'):
        return _xml_summary(text)
    return ''


def _xml_summary(text):
    for tag in _XML_TAGS:
        match = re.search(_XML_TAG_RE.format(tag=tag), text, re.S | re.I)
        if match:
            value = html.unescape(match.group(1)).strip()
            if value:
                return value
    return ''
