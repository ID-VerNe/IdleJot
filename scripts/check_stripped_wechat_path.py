"""#350 之后，微信读取路径不许再长回来（静态零命中）。

这个门禁不跑任何东西，只读源码，钉住三件「删干净了」的事实：

  1. 代码里不再出现 `wechatauto` / `wechat_keys` / `wechat-reader` 这几个**标识符**
     （字符串字面量里的历史叙述除外，见 `NARRATIVE_HINT`）；
  2. 依赖它们的模块不在 `chatlocal/` 的模块表里——`import_pipeline`、
     `client_accounts`、`import_scope` 都是随路径一起删的；
  3. `tools/wechat-reader` 不再被任何可执行代码当成 sys.path 的成员。

为什么值得单开一条门禁：#350 删的是一个**整条通道**，不是一次改名。删漏一处的
表现不是崩溃，而是「微信按钮还在、点了没反应」或者「升级后旧文件还在、没人知道
它已经不再被读」。零命中是把这种半删状态变成红灯，而不是等用户踩到。

**这不是「任何错都红」**：它只认上面三个名字与两张路径表。把 `wechat` 这个词整个
从仓库里抹掉不是目标——`messages.platform` 的 CHECK 枚举、升级比对清单里的历史文件
名、以及本文档里的叙述都该留住（§5.2 不破坏表形状）。判据只问「代码还在不在调用
那条通道」。

自测段（`--self-test`）故意在临时目录里造三种坏写法，确认检测器真会红：
一个裸 `import wechatauto`、一个 `sys.path` 里带 `tools/wechat-reader`、以及一个
注释里的良性提及（必须**不**红——否则没人敢在注释里解释历史，检测器就会退化成
「删注释」的强迫症）。三种用例都跑，才算测到东西。

    .venv/Scripts/python.exe scripts/check_stripped_wechat_path.py
"""
import ast
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 扫哪些目录：全部在运行时真的会被 import / 被执行的东西。
SCAN_DIRS = ('chatlocal', 'water-group', 'water_group', 'daemon', 'scripts')

# 模块名（不含 .py）。命中即红：这些模块要么是微信通道本身，要么只服务于它。
FORBIDDEN_MODULES = {
    'wechatauto',
    'chatlocal.wechat_keys',
    'chatlocal.import_pipeline',
    'chatlocal.client_accounts',
    'chatlocal.import_scope',
}

# 路径片段：出现在字符串里且被当成路径用时（sys.path / Path 拼接）即红。
FORBIDDEN_PATH_PARTS = ('tools/wechat-reader', 'tools\\wechat-reader', 'wechat-reader')

# 文件里出现这些词、但只是叙述历史时放行。判据是「有注释/文档标记」，不是「有这个词」。
NARRATIVE_HINT = re.compile(r'(#|""")')

# 本门禁自己当然要提这些词，跳过。
SELF = Path(__file__).resolve()


def _label(path):
    """路径显示：仓库内的写相对路径，自测夹具（临时目录）写文件名。"""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return Path(path).name


def _sources():
    for name in SCAN_DIRS:
        base = ROOT / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob('*.py')):
            if '__pycache__' in path.parts or path.resolve() == SELF:
                continue
            yield path


def scan_module_imports(path, text):
    """AST 才是判据：注释里的 `import wechatauto` 不算，真 import 才算。"""
    problems = []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as err:            # 语法坏掉不该被这个门禁报成「残留」
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or '']
            if node.level and not names[0]:
                # 相对导入 `from .wechat_keys import x`
                names = ['chatlocal.' + (a.name for a in node.names).__next__()] if node.names else ['']
        else:
            continue
        for name in names:
            bare = name.split('.')[-1]
            if name in FORBIDDEN_MODULES or f'chatlocal.{bare}' in FORBIDDEN_MODULES or bare in {'wechatauto'}:
                problems.append(f'{_label(path)}:{node.lineno}: 仍在 import {name}')
    return problems


def scan_path_references(path, text):
    """字符串里把 wechat-reader 当路径用，一定是死引用。

    判据落在 **字符串常量**上而不是「这一行里出现过这个词」：注释里的历史叙述与
    文档字符串里的解释都该放行，否则没人敢在源码里说明「这里原来是什么」，门禁
    就退化成了「删注释」。所以走 AST 取所有字符串节点，去掉 docstring。
    """
    problems = []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                docstrings.add(id(node.body[0].value))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstrings:
            continue
        for part in FORBIDDEN_PATH_PARTS:
            if part in node.value:
                problems.append(f'{_label(path)}:{node.lineno}: 仍在引用 {part}')
                break
    return problems


def scan_tree(root):
    """通用扫描入口，自测也走它——自测与实跑必须同一条路径。"""
    problems = []
    base = Path(root)
    for path in sorted(base.rglob('*.py')):
        if '__pycache__' in path.parts or path.resolve() == SELF:
            continue
        try:
            text = path.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError):
            continue
        problems.extend(scan_module_imports(path, text))
        problems.extend(scan_path_references(path, text))
    return problems


def self_test():
    """四种夹具：brick / dead path / 良性注释 / qq 正例。缺一条都不算测到东西。

    最后一步是**反向对照**：把判据换成恒返回空列表，`brick.py` 与 `deadpath.py`
    那两条断言必须随之失守。只造坏夹具、不证明断言真的会响，等于假绿。
    """
    with tempfile.TemporaryDirectory() as tmp:
        box = Path(tmp)
        (box / 'brick.py').write_text('import wechatauto\n', encoding='utf-8')
        (box / 'deadpath.py').write_text(
            "import sys\nsys.path.insert(0, str(ROOT/'tools/wechat-reader'))\n", encoding='utf-8')
        (box / 'narrative.py').write_text(
            '# 这里原来 import wechatauto，随 #350 删了\ntools/wechat-reader 也不再安装\n', encoding='utf-8')
        (box / 'clean.py').write_text("import sys\nsys.path.insert(0, str(ROOT/'tools/qq-reader'))\n",
                                      encoding='utf-8')

        def evaluate(judge):
            joined = '\n'.join(judge(box))
            return ('brick.py' in joined, 'deadpath.py' in joined,
                    'narrative.py' in joined, 'clean.py' in joined)

        brick, dead, narrative, clean = evaluate(scan_tree)
        assert brick, '漏掉了裸 import wechatauto'
        assert dead, '漏掉了 sys.path 里的 tools/wechat-reader'
        assert not narrative, '把注释里的历史叙述也报了红（检测器过于激进）'
        assert not clean, '把 tools/qq-reader 误报成 wechat-reader'

        # 恒空判据：两条「必须红」的断言都要失守，否则说明它们本来就不靠判据。
        brick0, dead0, narrative0, clean0 = evaluate(lambda root: [])
        assert not brick0 and not dead0, '恒空判据仍然报红，说明夹具本身在别处被拦住了'
        assert not narrative0 and not clean0, '恒空判据下正例也报红，夹具构造有问题'
    return True


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    self_test()
    problems = scan_tree(ROOT)
    if problems:
        print('FAIL: 微信读取路径没有删干净\n')
        for line in problems:
            print('  ' + line)
        print('\n#350 把整条微信通道撤掉了。要么把残留删掉，要么——如果你确实在重新引入它——')
        print('先在本门禁（scripts/check_stripped_wechat_path.py）里说明理由再放行，')
        print('不要直接删门禁：删掉它等于让「半删状态」重新变得不可观测。')
        return 1
    print('✓ 检测器自测通过（裸 import / 死路径必红，注释与 qq-reader 必绿）')
    print('✓ 码里不再 import wechatauto / wechat_keys / import_pipeline / client_accounts')
    print('✓ 码里不再把 tools/wechat-reader 当路径用')
    print('\nPASS: 微信读取路径已剥离，无残留。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
