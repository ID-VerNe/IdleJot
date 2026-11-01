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
  取不到就返回空串，由调用方退回类型标签——不把「解析不出」写成「没有内容」；
* :func:`card_link` 取卡片指向的跳转地址（issue #311），:func:`link_fields` 把它
  变成段上的 ``url`` 字段，:func:`card_text` 是 content 占位的唯一构造点——
  链接以前只躺在 ``raw`` 原文里，Agent 看得到标题却不知道它指向哪个视频。

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
LINK_LIMIT = 400         # 卡片跳转地址：够装下带一串参数的分享链接，也不许撑爆 payload

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

# 会带跳转地址的段（issue #311）。视频/文件段不在此列：它们的 `url` 是带 rkey 的
# CDN 地址，约 8 小时就过期（#200），该由 `wg_videos` 的可下载命令交出去，
# 塞进 content 只是把一条快过期的地址写进上下文和记忆。
CARD_KINDS = ('json', 'xml', 'share', 'music', 'markdown')

# 链接字段的键名优先级。QQ 小程序的 `meta.detail_1` 同时有 `qqdocurl`（分享短链，
# 就是群友点开的那条）与 `url`（`m.q.qq.com/a/s/…` 的小程序内部路径），前者才是
# 「这条卡片指向哪里」的答案；`jumpUrl` 是资讯类卡片的同一件事换了个键名。
_LINK_KEYS = ('qqdocurl', 'jumpurl', 'jump_url', 'url', 'link', 'shareurl',
              'share_url', 'sourceurl', 'source_url')
_LINK_RE = re.compile(r'https?://\S+$', re.I)
# 链接的递归深度：`{"data": "<一层 JSON 字符串>"}` 这种双重编码本身就吃掉两层，
# 再往下 `meta.detail_1.qqdocurl` 还要三层。上限给到 8 是「够深 + 仍是有限」，
# 卡片是外部输入，不给自己开一条无界递归。
_LINK_DEPTH = 8
_XML_URL_TAG_RE = r'<url>(.*?)</url>'
_XML_URL_ATTR_RE = r'url\s*=\s*["\']([^"\']*)["\']'
# 链接里出现这些字符就没法安全嵌进 `[卡片：…]` 占位（`]` 会让唤醒行的占位剥离
# 正则停在半路，把后半截 URL 留在正文里占额度），一律当解析失败。
_LINK_BAD_CHARS = '[]<>\n\t '


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
    segment.update(link_fields(raw_kind, raw_data))
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


def card_link(raw_data):
    """卡片类段指向的跳转地址（issue #311）：取第一个 http(s) 链接，取不到返回空串。

    入参与 :func:`card_summary` 同款——原始 ``data`` 或落盘后的 ``raw`` 文本都能读，
    否则「入库时有链接、补读时又没有」等于把同一个缺陷换个入口复现。

    只认带 scheme 的 http/https。``m.q.qq.com/a/s/…`` 这种没有 scheme 的值一律
    丢弃，不替它补 ``https://``：猜错的地址比没有地址更坏——Agent 会照着去读一个
    打不开的链接，然后把「读不到」当成「内容不存在」。原文仍在 ``raw`` 里，
    真要补读还有路。
    """
    return _link_from(raw_data, 0)


def link_fields(raw_kind, raw_data):
    """段上的链接字段（issue #311）：``{'url': …[, 'url_truncated': True]}`` 或 ``{}``。

    ``unsupported()`` 与历史行回填共用这一处，限额与截断标记因此只有一份——
    否则两条渲染路径迟早给出两个长度。
    """
    if kind_of(raw_kind) not in CARD_KINDS:
        return {}
    link = card_link(raw_data)
    if not link:
        return {}
    kept = clip(link, LINK_LIMIT)
    fields = {'url': kept.text}
    if kept.truncated:
        fields['url_truncated'] = True
    return fields


def card_text(raw_kind, raw_data):
    """content 里那段占位：``[卡片：标题 → https://…]``（issue #311）。

    入库侧（``mcp_chat.receive``）与引用/转发节点渲染（``mcp_chat_details.render``）
    都调这里，标题、链接、方括号的写法只有一份——#165 的教训是两条路径各写一份
    「拼 content」，改一处漏一处。

    链接放在标题后面而不是另起一段：content 是给人扫的一行，读者要能连着读成
    「这条卡片是什么、指向哪里」。唤醒行里整段仍被 `strip_placeholders` 剥成
    ``[卡片]``，链接不占正文额度。
    """
    text = label(raw_kind)
    body = card_summary(raw_data)
    if body:
        text += '：' + body
    fields = link_fields(raw_kind, raw_data)
    if fields:
        text += ' → ' + fields['url'] + ('…' if fields.get('url_truncated') else '')
    return '[' + text + ']'


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


def _link_from(value, depth):
    """在 dict / JSON 文本 / XML 文本里找第一个跳转地址（issue #311）。

    与 :func:`_summary_from` 的差别只有一处：摘要按键名找**人看的字**，链接要往下
    钻到 ``meta.detail_1`` 这种卡片自己起的键名里，所以这里递归**所有** dict 值而
    不是固定的几层。深度仍有上限（``_LINK_DEPTH``）——卡片是外部输入，
    不给自己开一条无界递归。
    """
    if depth > _LINK_DEPTH or value is None:
        return ''
    if isinstance(value, dict):
        # 键名大小写混用（`jumpUrl` / `qqdocurl` / `URL`），统一小写后再按优先级比；
        # 本层有链接就先给本层，没有再往下钻——同一张卡片里顶层 `url` 与
        # `detail_1.qqdocurl` 撞车时，先说卡片自己声明的那个。
        lowered = {}
        for key, item in value.items():
            if isinstance(key, str):
                lowered.setdefault(key.strip().lower(), item)
        for key in _LINK_KEYS:
            if key in lowered:
                found = _clean_link(lowered[key])
                if found:
                    return found
        for item in value.values():
            found = _link_from(item, depth + 1)
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
            return _link_from(json.loads(text), depth + 1)
        except ValueError:
            return ''
    if text.startswith('<'):
        return _xml_link(text)
    return ''


def _xml_link(text):
    """XML 卡片：先认 ``<url>…</url>``，再认根节点上的 ``url="…"`` 属性。"""
    for pattern in (_XML_URL_TAG_RE, _XML_URL_ATTR_RE):
        for match in re.finditer(pattern, text, re.S | re.I):
            found = _clean_link(match.group(1))
            if found:
                return found
    return ''


def _clean_link(raw):
    """反转义 + 校验，产出一个能直接交给 Agent 的地址；不合格返回空串。

    JSON 里的 ``\\/`` 与 ``\\u0026``、XML 里的 ``&amp;`` 都是同一条链接的不同转义
    形态，不还原的话 Agent 拿到的地址是打不开的（``https:\\/\\/b23.tv\\/abc``）。
    还原之后才判 scheme：``javascript:`` / ``file:`` / ``data:`` 这类一律丢弃——
    这条字段会进 Agent 的上下文，没理由把非网页协议也递过去。
    """
    text = html.unescape(str(raw or '')).replace('\\/', '/')
    text = text.replace('\\u0026', '&').replace('\\u003d', '=').replace('\\u003f', '?')
    # 链接里不该有空白：真出现了就是「地址 + 后面粘着的文案」，取第一段。
    text = text.split()[0] if text.split() else ''
    if not text or any(char in text for char in _LINK_BAD_CHARS):
        return ''
    return text if _LINK_RE.match(text) else ''
