"""issue #379 gate: 入口不得吞掉被交出函数的退出码（AST 静态判定）。

背景（issue #375 / #343）：`sys.exit(main())` 依赖 `main()` 返回退出码。一旦
`main()` 在拆分或重构时漏掉带值 `return`，进程拿到 `None` → 退出 0，日志里
一路 `FAIL` 而 CI 记成通过，闸门形同不存在。`fc1b419`（#343）就犯过一次；
PR #377 只在 `check_gate_change_review.py` 自己的 `self_test()` 里钉住那一个文件，
换成别的文件重犯时没有门禁能抓。

本门禁按**形状**枚举入口，不按函数名：

- 入口 = `sys.exit(<零参调用>)` 或 `raise SystemExit(<零参调用>)`；
- `<零参调用>` 指无位置参数、无关键字参数的 `Call`；
- 因此 `sys.exit(main())`、`raise SystemExit(main())`、`sys.exit(run_main())`
  都在口径内；带参数的 `sys.exit(run_main(Path(...)))` 不在此列（issue #379
  证据链里的 `check_no_console_child` 就是这种形状）。

被交出的函数按 AST 解析定义：本文件顶层 `def`、同包 `__init__.py`（`from . import main`
入口）、以及 `from <模块> import <函数>`。判据：

- 函数体内**没有任何带值 `return`**（`ast.Return` 且 `value is not None`，按函数
  作用域统计，跳过嵌套 `def` / `class` / `lambda`）→ 红；
- 定义解析不出来 → 红（无法证明它带值返回，fail-closed）；
- 入口文件在 `EXEMPT` 显式豁免清单里 → 放行，且理由必须写在清单里。

**为什么不靠「函数体内有 sys.exit / 裸 assert」的逃逸规则**：已知的安全假阳性
（`check_qr_login` / `check_sixty` / `check_dossier_cadence`，以及本门禁首次扫描
新发现的 `check_mcp_chat_turns`）会因此被静默放过，以后真出现同类缺陷时同样静默。
这里改成显式豁免清单，改名、搬家、新增都要人看一眼。

`self_test()` 用临时包夹具做反向对照（漏 `return` 必红、补上转绿），并钉住本脚本
自己的 `main()` 带值返回——否则新门禁会把自身判红（自指陷阱）。

    .venv/Scripts/python.exe scripts/check_exit_main_return.py
"""
import ast
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ('scripts', '.githooks')

# 显式豁免（入口文件相对路径 → 为什么安全）。四条都是人工核对过的安全假阳性；
# 理由写在这里，不靠正则侥幸不命中（防 #375 复发）。
EXEMPT = {
    'scripts/check_qr_login/__main__.py':
        '`main()` 失败路径直接 `sys.exit(1)`，`SystemExit` 先抛出、不会正常返回 None（防 #375 复发）',
    'scripts/check_sixty/__main__.py':
        '`main()` 失败路径直接 `sys.exit(1)`，`SystemExit` 先抛出、不会正常返回 None（防 #375 复发）',
    'scripts/check_dossier_cadence/__main__.py':
        '用例里的裸 `assert` 失败抛 `AssertionError`，进程非 0，不是「打印 FAIL 却退 0」的形状（防 #375 复发）',
    'scripts/check_mcp_chat_turns/__main__.py':
        '`main()` 是 assert 驱动的用例：任何失败断言抛 `AssertionError` 让进程非 0，全部通过时返回 None→0 才是成功（#379 首次扫描新发现，人工核对安全）',
}


def exit_argument(node):
    """`sys.exit(...)` / `raise SystemExit(...)` 的第一个参数节点；其它节点返回 None。"""
    if isinstance(node, ast.Call):
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == 'exit'
            and isinstance(func.value, ast.Name)
            and func.value.id == 'sys'
        ):
            return node.args[0] if node.args else None
        return None
    if isinstance(node, ast.Raise):
        exc = node.exc
        if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name) and exc.func.id == 'SystemExit':
            return exc.args[0] if exc.args else None
    return None


def callee_name(call):
    """被交出的调用名：`main()` → `main`；`obj.method()` → `method`；其它返回 None。"""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def own_nodes(body):
    """遍历函数体但不进入嵌套函数 / 类：它们的 `return` 不属于这个函数的出口。"""
    stack = list(body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def has_value_return(func):
    """函数内是否存在带值 `return`（`return` 裸返回与完全没有 return 都不算）。"""
    return any(
        isinstance(node, ast.Return) and node.value is not None
        for node in own_nodes(func.body)
    )


def escape_routes(func):
    """函数体内可让进程非 0 的逃生路径（只用于诊断文案，不作为放行依据）。"""
    routes = []
    for node in own_nodes(func.body):
        if isinstance(node, ast.Assert):
            routes.append('裸 assert')
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) \
                and isinstance(node.exc.func, ast.Name) and node.exc.func.id == 'SystemExit':
            routes.append('raise SystemExit')
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == 'exit' and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == 'sys':
            routes.append('sys.exit')
    return routes


def import_targets(path, node, name):
    """`ImportFrom` 可能指向的模块文件（相对导入 / `scripts.*` / `scripts/<模块>`）。"""
    targets = []
    if node.level:
        base = path.parent
        for _ in range(node.level - 1):
            base = base.parent
        if node.module:
            parts = node.module.split('.')
            target = base.joinpath(*parts)
            targets += [target.with_suffix('.py'), target / '__init__.py']
        else:
            # `from . import main`：既可能是包级函数（__init__.py），也可能是子模块 main.py。
            targets += [base / '__init__.py', base / f'{name}.py']
    elif node.module:
        parts = node.module.split('.')
        for root in (ROOT, ROOT / 'scripts'):
            target = root.joinpath(*parts)
            targets += [target.with_suffix('.py'), target / '__init__.py']
    return targets


def find_top_level_def(path, name):
    """在 `path` 的顶层找 `def <name>`；文件不可读 / 语法错时返回 None。"""
    try:
        tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'), filename=str(path))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def resolve_function(path, tree, name):
    """解析被交出的函数定义：本文件顶层 def → `from ... import` 指向的模块。"""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        if not any(alias.name == name for alias in node.names):
            continue
        for target in import_targets(path, node, name):
            if not target.is_file():
                continue
            found = find_top_level_def(target, name)
            if found is not None:
                return found
    return None


def collect_sources(root):
    """扫描范围：`scripts/` 下的 .py 与 `.githooks/` 的全部文件（钩子是 shell 时跳过）。"""
    sources = []
    for base_name in SCAN_DIRS:
        base = root / base_name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob('*')):
            if not path.is_file():
                continue
            if base_name == 'scripts' and path.suffix != '.py':
                continue
            sources.append((path.relative_to(root).as_posix(), path))
    return sources


def audit(sources):
    """扫描一组 `(相对路径, 文件路径)` → (入口, 豁免, 命中)。纯函数，供 main 与 self_test 共用。"""
    entries, exempted, hits = [], [], []
    for rel, path in sources:
        try:
            tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'), filename=str(path))
        except (OSError, SyntaxError):
            continue  # .githooks 下是 shell 钩子，不贡献入口
        for node in ast.walk(tree):
            argument = exit_argument(node)
            if argument is None:
                continue
            kind = 'sys.exit' if isinstance(node, ast.Call) else 'raise SystemExit'
            if not isinstance(argument, ast.Call) or argument.args or argument.keywords:
                continue  # 非零参调用不在口径内（判据按形状，不按函数名）
            name = callee_name(argument)
            if name is None:
                hits.append((rel, node.lineno, f'{kind}(<非简单调用>) 无法解析被交出的函数'))
                continue
            entries.append((rel, node.lineno, kind, name))
            if rel in EXEMPT:
                exempted.append((rel, node.lineno, kind, name))
                continue
            func = resolve_function(path, tree, name)
            if func is None:
                hits.append(
                    (rel, node.lineno,
                     f'{kind}({name}()) 的被调函数定义未解析，无法确认它带值 return')
                )
                continue
            if not has_value_return(func):
                routes = '、'.join(sorted(set(escape_routes(func)))) or '无'
                hits.append(
                    (rel, node.lineno,
                     f'{kind}({name}()) 的函数体没有带值 return（逃生路径：{routes}），'
                     '退出码会恒为 None→0')
                )
    return entries, exempted, hits


def self_test():
    """离线自检：反向对照（漏 return 必红、补上转绿）+ 形状对照 + 自指陷阱。"""
    failures = []

    def expect(name, got, want):
        if got != want:
            failures.append(f'{name}：期望 {want!r}，实得 {got!r}')

    def write_package(root, package, main_body):
        package_dir = Path(root) / package
        package_dir.mkdir(parents=True, exist_ok=True)
        (package_dir / '__init__.py').write_text(main_body, encoding='utf-8')
        (package_dir / '__main__.py').write_text(
            'import sys\nfrom . import main\n\n'
            "if __name__ == '__main__':\n    sys.exit(main())\n",
            encoding='utf-8',
        )
        return package_dir

    bad_main = 'def main():\n    print("FAIL")\n'
    good_main = 'def main():\n    print("FAIL")\n    return 0\n'

    with tempfile.TemporaryDirectory() as tmp:
        # 坏形态：包入口把没有带值 return 的 main() 交给 sys.exit → 必须红。
        package = write_package(tmp, 'pkg', bad_main)
        sources = [('scripts/pkg/__main__.py', package / '__main__.py')]
        entries, exempted, hits = audit(sources)
        expect('#379 反向对照：漏 return 被扫出', len(hits), 1)
        expect('#379 反向对照：命中签名带文件名', hits and hits[0][0], 'scripts/pkg/__main__.py')
        expect('#379 反向对照：入口被枚举', len(entries), 1)

        # 同形好形态：补上带值 return → 转绿（证明不是「怎么扫都红」）。
        package = write_package(tmp, 'pkg', good_main)
        entries, exempted, hits = audit([('scripts/pkg/__main__.py', package / '__main__.py')])
        expect('#379 同形对照：带值 return 放行', len(hits), 0)
        expect('#379 同形对照：入口仍被枚举', len(entries), 1)

        # 形状对照：`raise SystemExit(main())` 与非 main 命名同样命中。
        (package / '__init__.py').write_text(bad_main, encoding='utf-8')
        bad_raise = package / '__main__.py'
        bad_raise.write_text(
            'from . import main\n\n'
            "if __name__ == '__main__':\n    raise SystemExit(main())\n",
            encoding='utf-8',
        )
        entries, exempted, hits = audit([('scripts/pkg/__main__.py', bad_raise)])
        expect('#379 raise SystemExit 形态同样命中', len(hits), 1)

        alias_dir = Path(tmp) / 'alias'
        alias_dir.mkdir()
        (alias_dir / '__init__.py').write_text('def run_main():\n    print("FAIL")\n', encoding='utf-8')
        (alias_dir / '__main__.py').write_text(
            'import sys\nfrom . import run_main\n\n'
            "if __name__ == '__main__':\n    sys.exit(run_main())\n",
            encoding='utf-8',
        )
        entries, exempted, hits = audit([('scripts/alias/__main__.py', alias_dir / '__main__.py')])
        expect('#379 非 main 命名的零参调用同样命中', len(hits), 1)
        expect('#379 非 main 命名的入口名被识别', entries and entries[0][3], 'run_main')

        # 非零参调用不在口径内：与 check_no_console_child 的真实形状同形。
        nonzero = Path(tmp) / 'nonzero'
        nonzero.mkdir()
        (nonzero / '__init__.py').write_text('def run_main(root):\n    return 0\n', encoding='utf-8')
        (nonzero / '__main__.py').write_text(
            'import sys\nfrom pathlib import Path\nfrom . import run_main\n\n'
            "if __name__ == '__main__':\n    sys.exit(run_main(Path('x')))\n",
            encoding='utf-8',
        )
        entries, exempted, hits = audit([('scripts/nonzero/__main__.py', nonzero / '__main__.py')])
        expect('#379 非零参调用不误判', (len(entries), len(hits)), (0, 0))

        # 豁免清单：三条安全假阳性按入口路径放行，且必须计入「豁免」而不是「通过」。
        exempt_dir = Path(tmp) / 'check_dossier_cadence'
        exempt_dir.mkdir()
        (exempt_dir / '__init__.py').write_text('def main():\n    assert True\n', encoding='utf-8')
        (exempt_dir / '__main__.py').write_text(
            'import sys\nfrom . import main\n\n'
            "if __name__ == '__main__':\n    sys.exit(main())\n",
            encoding='utf-8',
        )
        entries, exempted, hits = audit(
            [('scripts/check_dossier_cadence/__main__.py', exempt_dir / '__main__.py')]
        )
        expect('#379 豁免清单按入口路径放行', len(hits), 0)
        expect('#379 豁免会计入豁免数而非通过数', len(exempted), 1)

    # 自指陷阱：本脚本自己的 main() 必须带值 return，否则新门禁会把自己判红。
    own_tree = ast.parse(Path(__file__).read_text(encoding='utf-8'), filename=__file__)
    own_main = next(
        (node for node in own_tree.body
         if isinstance(node, ast.FunctionDef) and node.name == 'main'),
        None,
    )
    expect('#379 自指：本脚本有 main() 定义', own_main is not None, True)
    expect('#379 自指：本脚本 main() 带值 return',
           has_value_return(own_main) if own_main else False, True)
    own_rel = Path(__file__).resolve().relative_to(ROOT).as_posix()
    entries, exempted, hits = audit([(own_rel, Path(__file__).resolve())])
    expect('#379 自指：本脚本扫描零命中', len(hits), 0)

    if failures:
        for line in failures:
            print(f'SELF-TEST FAIL {line}')
        raise AssertionError(f'self_test 失败 {len(failures)} 项（issue #379 夹具）')
    print('SELF-TEST OK 入口枚举、零参形状、包式定义解析、显式豁免与自指陷阱的回归夹具全部符合预期')


def main():
    """入口：先自检，再扫真实树；有命中返回 1（本函数自身必须带值 return）。"""
    self_test()
    entries, exempted, hits = audit(collect_sources(ROOT))
    print(f'  info 入口 {len(entries)} 个（sys.exit / raise SystemExit），豁免 {len(exempted)} 个')
    for rel, line, kind, name in exempted:
        print(f'  ok  豁免：{rel}:{line} {kind}({name}()) — {EXEMPT[rel]}')
    if hits:
        for rel, line, message in hits:
            print(f'  FAIL {rel}:{line} {message}')
        print(f'FAIL: {len(hits)} 个入口会把真实退出码吞成 0（issue #379）。')
        return 1
    print('PASS: 所有入口的被调函数都有带值 return（或在显式豁免清单里）。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
