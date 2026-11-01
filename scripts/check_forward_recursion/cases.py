"""issue #351：合并转发里的合并转发要能展开，且展开得过的那三层真的看得见。

这组用例按**维度**铺，不是按数量：默认参数、深度上限、并列多条互不挤占、
两个共享预算各自耗尽、渲染侧兜底、入库接线各一条。少写任何一条，都可能留下
「绿着但不工作」的实现——默认配置一条就能打穿「所有阈值都被覆盖成 0」那种
假绿（#162 的教训）。

拆成独立包是为了 `check_module_size.py` 的 1000 行硬上限（原文件 1033 行）。
`main()` 里的夹具（`add` / `packet` / `message`）通过参数注入，所以这里不碰
任何环境搭建——夹具仍旧只有一份。
"""


def run(chat,row,add,packet,message,text,nodes_limit):
    """`text` 与 `nodes_limit` 由调用方注入，避免与主门禁的模块级同名定义分叉。"""
    from chatlocal.mcp_chat_details import MAX_FORWARD_DEPTH, MAX_TEXT, sanitize_nodes
    MAX_NODES = nodes_limit

    def leaf(body='叶子正文'):
        return [dict(sender=dict(nickname='L0'), message=[text(body)])]

    def tree(level):
        # 第 level 层节点里再套一层转发，一直套到 0。depth 只数**转发**的层数。
        if level == 0:
            return leaf()
        return [dict(sender=dict(nickname='L%d' % level),
                     message=[text('第%d层' % level),
                              dict(type='forward', data=dict(messages=tree(level - 1)))])]

    def walk(value):
        # 只能 yield dict：`body()` / `reasons()` 直接读字段，list 进来就炸。
        if isinstance(value, list):
            for child in value:
                yield from walk(child)
        elif isinstance(value, dict):
            yield value
            for key in ('nodes', 'segments'):
                for child in value.get(key, []) or []:
                    yield from walk(child)

    def body(value):
        return ''.join(v.get('text', '') for v in walk(value) if v.get('type') == 'text')

    def reasons(value):
        return [v.get('limit_reason') for v in walk(value) if v.get('limit_reason')]

    # 1. 默认参数：外层引用算第 1 层，里面再展开 MAX_FORWARD_DEPTH-1 层，
    #    MAX_FORWARD_DEPTH 层是叶子。**不改任何预算**，红在这里就是默认配置不工作。
    opened = sanitize_nodes(tree(MAX_FORWARD_DEPTH - 1))
    assert '叶子正文' in body(opened), body(opened)
    assert not reasons(opened), reasons(opened)

    # 2. 再多一层：进第 MAX_FORWARD_DEPTH+1 层的那个转发被抓，正文拿不到，理由说得清。
    over = sanitize_nodes(tree(MAX_FORWARD_DEPTH))
    assert '叶子正文' not in body(over), body(over)
    assert reasons(over) == ['depth'], reasons(over)

    # 2b. 上面两条都用**相对**层数，常量被悄悄放大时它们会跟着放宽、一起变绿
    #    （实测把 MAX_FORWARD_DEPTH 改成 5，1/2/6 三条全绿）。所以这里再钉一次
    #    **绝对**层数：三层转发必须能展，四层必须停在 'depth'。这条断言不随常量
    #    移动——要改它就得同时改这里，改不动就是真的放宽了。
    assert MAX_FORWARD_DEPTH >= 3, '嵌套转发至少展开 3 层，否则「转发里的转发」仍看不见'
    assert '叶子正文' in body(sanitize_nodes(tree(2))), '三层转发展开不全'
    four = sanitize_nodes(tree(3))
    assert 'depth' in reasons(four), reasons(four)

    # 3. 深度是硬上限，不是「前几条先吃预算」：六条并列嵌套转发，每一条都要展到位。
    #    这条钉的是 `budget['text']`（这一层的额度）与 `nested_text`（总账）必须分开记
    #    ——混成一笔时第一条就把总账吃光，并列的第二条起连一层都展不出来。
    many = [dict(sender=dict(nickname='S%d' % i),
                 message=[text('外层%d' % i),
                          dict(type='forward', data=dict(messages=tree(MAX_FORWARD_DEPTH - 2)))])
            for i in range(6)]
    joined = sanitize_nodes(many)
    for i in range(6):
        assert '外层%d' % i in body(joined) and '叶子正文' in body(joined), (i, body(joined))
    assert not reasons(joined), reasons(joined)

    # 3b. `item` 那一维：同一个节点里「正文」与「内层转发」必须分账，不按先后到者得。
    #    上面的并列用例里每条正文只有四五个字，层额度根本用不完，所以「正文直接吃
    #    共享的层额度、把内层转发饿到 share=0」那种实现照样绿。
    #    形状必须是**三层**：`item` 只在嵌套里被收窄到 NESTED_TEXT_CHARS，一层嵌套
    #    时它还是整份层额度，正文再长也饿不死转发（实测一层嵌套下两种实现同绿）。
    #    「引一段长文 + 转发原帖」是最常见的形状，正文独占额度等于这条路径永远
    #    看不见转发内容。
    fat = [dict(sender=dict(nickname='O'), message=[
        text('说明'),
        dict(type='forward', data=dict(messages=[dict(sender=dict(nickname='A'), message=[
            text('长文' * 200),
            dict(type='forward', data=dict(messages=[dict(sender=dict(nickname='B'),
                                                          message=[text('深叶子')])]))])]))])]
    fed = sanitize_nodes(fat, budget=dict(nodes=MAX_NODES, text=MAX_TEXT, nested_text=MAX_TEXT))
    assert '深叶子' in body(fed), ('外层长正文把内层转发的额度吃光了', body(fed))
    # 反向也要钉：正文确实该被收窄（不是「两边都给满」那种假分账）。
    assert '长文' * 200 not in body(fed), '正文没有收窄，等于没分账'

    # 4. 节点预算跨层共享，且耗尽时点名是「条数」而不是别的。
    #    必须配对照组：`nodes=3` 在这个形状里正好够（外层 1 + L1 1 + 叶子 1），
    #    不少一条；只有 `nodes=2` 才真的截断。少了对照组，「把预算写死成大值」
    #    的实现也会看起来是绿的。
    shape=[dict(message=[text('a'), dict(type='forward', data=dict(messages=tree(1)))])]
    exact=dict(nodes=3, text=MAX_TEXT)
    assert not reasons(sanitize_nodes(shape, budget=exact)), '按维度算好的节点预算被多扣了'
    tight=dict(nodes=2, text=MAX_TEXT)
    budgeted=sanitize_nodes(shape, budget=tight)
    assert 'node_limit' in reasons(budgeted), reasons(budgeted)

    # 5. 字符预算跨层共享：给一个只够写几个字的总账，被砍的那一层必须点名
    #    content_limit。只断言「正文变短了」不够——正文变短本来就是这条预算的
    #    正常表现，会绿着的坏实现是「砍了但不吭声」。
    starved = dict(nodes=MAX_NODES, text=MAX_TEXT, nested_text=1)
    trimmed = sanitize_nodes([dict(message=[text('前言'),
                                           dict(type='forward', data=dict(messages=tree(MAX_FORWARD_DEPTH - 1)))])],
                             budget=starved)
    assert 'content_limit' in reasons(trimmed), reasons(trimmed)
    # 对照组：总账够用时不许出现 content_limit，否则上面那条被恒真函数蒙混过去。
    roomy = dict(nodes=MAX_NODES, text=MAX_TEXT, nested_text=MAX_TEXT)
    assert 'content_limit' not in reasons(sanitize_nodes(
        [dict(message=[text('前言'), dict(type='forward', data=dict(messages=tree(MAX_FORWARD_DEPTH - 1)))])],
        budget=roomy)), '宽裕的字符预算被误报成 content_limit'

    # 5b. 总账（nested_text）必须是**独立的一笔账**：层额度（budget['text']）、
    #    单节点额度（item）、条数预算全给足，只有总账卡在半路时，被砍的那一条要
    #    点名 content_limit。上面 5 的 `nested_text=1` 挡不住「总账根本没递减」的
    #    实现——那时 `item` 与层额度自己就能把字砍到 1，理由照样是 content_limit。
    #    这里两条并列转发各带 80 字正文，总账只给 80：第一条吃完、第二条必须吭声。
    def payable(tag):
        return [dict(message=[dict(type='forward', data=dict(messages=[dict(message=[text(tag * 80)])]))])
                for _ in range(2)]

    exact_ledger = dict(nodes=MAX_NODES, text=MAX_TEXT, nested_text=80)
    ledger = sanitize_nodes(payable('甲'), budget=exact_ledger)
    assert 'content_limit' in reasons(ledger), ('总账只够一条时第二条没有被砍', reasons(ledger))
    # 光有理由不够：「标了 content_limit 却一个字都没少」的实现也满足上面那条
    # （实测把总账递减删掉，正文照样是 160 字、理由照样有）。总账 80 就是硬上限。
    assert len(body(ledger)) == 80, ('总账写的 80 字没兑现', len(body(ledger)))
    # 对照组：总账给到 160（两条正好装下），同形状一条都不许砍——否则「恒报
    # content_limit」的实现也绿。
    roomy_ledger = sanitize_nodes(payable('甲'), budget=dict(exact_ledger, nested_text=160))
    assert 'content_limit' not in reasons(roomy_ledger), '两条正好装下的总账被误报'
    assert len(body(roomy_ledger)) == 160, len(body(roomy_ledger))

    # 6. 渲染侧兜底：历史行与 adapter 直喂的节点树没经过 sanitize，`render_forward`
    #    自己也得在 MAX_FORWARD_DEPTH 处停下，否则递归没有终点。
    #    `raw_tree(n)` 的最外层是 `层n`，渲染一遍正好让这个数递减 1：第 1 层看着
    #    顶层转发、第 2 层看着它的第一个子转发……所以「层 n-MAX_FORWARD_DEPTH+1」
    #    是最后一层看得见的，再往里就没进来。
    def raw_tree(level):
        if level == 0:
            return [dict(sender='W', segments=[dict(type='text', text='底部')])]
        return [dict(sender='X%d' % level,
                     segments=[dict(type='text', text='层%d' % level),
                               dict(type='forward', nodes=raw_tree(level - 1))])]

    top = MAX_FORWARD_DEPTH + 2
    rendered = chat.details.render_forward(dict(state='available', nodes=raw_tree(top)), [], 1, True, None)
    # `limit_reason` 落在**耗尽的那一层**（内层转发段），不是顶层总账——顶层是
    # 'available' 很正常（它的节点都在，是子节点被砍了）。在整棵树里找。
    assert 'depth' in reasons(rendered), rendered
    assert '层%d' % (top - MAX_FORWARD_DEPTH + 1) in body(rendered), body(rendered)
    assert '层%d' % (top - MAX_FORWARD_DEPTH) not in body(rendered), body(rendered)

    # 7. 入库接线：content 里必须能看见嵌套正文，占位仍是一对配平的方括号。
    #    只断言 segments 不算数——Agent 读的是 content。
    eid,_=add([dict(type='forward', data=dict(messages=tree(MAX_FORWARD_DEPTH - 1)))])
    entry=message(packet(),eid)
    assert '叶子正文' in entry['content'], entry['content']
    assert entry['content'].count('[')==entry['content'].count(']'), entry['content']
    # 同一棵树的 structuredContent 也要在，否则「content 看得见、Agent 拿不到节点」。
    detail=entry['referenced_messages'][0]
    assert '叶子正文' in body(detail), detail
    print('PASS: forward-in-forward expands to MAX_FORWARD_DEPTH by default, hard depth cap, '
          'parallel siblings do not starve each other, shared node/text budgets attribute the cut, '
          'render-side guard, ingest wiring')
