"""issue #379 gate: 入口不得吞掉被交出函数的退出码（AST 静态判定）。

背景（issue #375 / #343）：`sys.exit(main())` 依赖 `main()` 返回退出码。一旦
`main()` 在拆分或重构时漏掉带值 `return`，进程拿到 `None` → 退出 0，日志里
一路 `FAIL` 而 CI 记成通过，闸门形同不存在。`fc1b419`（#343）就犯过一次；
PR #377 只在 `check_gate_change_review.py` 自己的 `self_test()` 里钉住那一个文件，
换成别的文件重犯时没有门禁能抓。

本门禁按**形状**枚举入口，不按函数名：

- 入口 = `sys.exit(<零参调用>)` 或 `raise SystemExit(<零参调用>)`；
  别名也按导入绑定解析：`from sys import exit [as e]` 的 `e(...)`、
  `import sys as s` 的 `s.exit(...)`、`from builtins import SystemExit as SE`
  的 `raise SE(...)`。
- `<零参调用>` 指无位置参数、无关键字参数的 `Call`；因此
  `sys.exit(main())`、`raise SystemExit(main())`、`sys.exit(run_main())` 都在
  口径内；带参数的 `sys.exit(run_main(Path(...)))` 不在此列（issue #379
  证据链里的 `check_no_console_child` 就是这种形状）。
- **入口参数侧的兜底也判红（#382 复核条件 1）**：参数不是裸零参调用、而是
  `BoolOp`（`or` / `and`）或单支未知的 `IfExp` 时，同样计入入口并判红——
  `sys.exit(main() or 0)`、`sys.exit(main() if cond else 0)`、
  `raise SystemExit(main() or 0)` 与 `return f() or 0` 是同一机制：把上游
  `None` 兜成成功。两支都是非 None 常量（`sys.exit(1 if cond else 0)`）放行；
  一支是已解析且确认带值的零参调用、另一支是非零常量（`sys.exit(main() if
  cond else 1)`）也放行（非零兜底不会把失败伪装成成功）。
- **`main(); sys.exit(0)` 边界（#382 复核条件 2）**：`main()` 独立调用后再
  `sys.exit(0)` / `sys.exit()` 当前**不计入口、不判红**。理由：真实树有 46 个
  脚本是「`main()` 独立调用、失败靠异常/断言抛出」的写法，这 46 个 `main()`
  函数体实测全部没有带值 `return`，这套约定下与今天的 `main()` 等价安全。
  代价：一旦某个 `main()` 改成带值返回，显式 `sys.exit(0)` 会静默丢弃，门禁
  看不见——所以这是显式声明的边界，不是漏检。`rc = main(); sys.exit(0)` /
  `sys.exit()` 属于显式「先取码再丢弃」，**直接判红**；`rc = main(); sys.exit(rc)`
  是正确写法，但门禁不追 `rc` 来源。
- 明确不覆盖的边界（写在这里而不是靠「扫不到」侥幸放过）：
  `getattr(sys, 'exit')(...)` 等动态取属性形态；`ex = sys.exit; ex(main())`
  这类把退出函数存进变量再调用；多级属性链 `pkg.mod.main()`（一层
  `mod.main()` 会解析）；`SystemExit` 被动态赋值/遮蔽的形态；`os._exit(...)`
  （运行期 `os._exit(None)` 抛 `TypeError` 而不是干净退 0）；`.githooks/` 里
  没有 shebang、也不是 `.py` 的 Python 钩子（文件类型边界）；`atexit` 改退出码
  （静态不可见，且正常退出路径改不了退出码）。豁免守卫 `package_assert` 只验
  「包内仍有 assert」，不承诺 `main()` 真能走到那条断言。

被交出的函数按 AST 解析定义：本文件顶层 `def` / 顶层 `name = lambda: ...`、
同包 `__init__.py`（`from . import main` 入口）、`from <模块> import <函数>`、
以及一层模块限定名 `mod.main()`（`import ... as mod` / `from ... import mod`）。
判据：

- **装饰器 fail-closed（#379 复核缺口 1）**：被调函数带 `decorator_list` 时
  一律红——无法确认装饰器保留返回值。不设「装饰器白名单」，白名单本身就是
  逃逸规则；真实树被调函数实测无装饰器，新规则不产生既有命中。
- **可确认的带值 return（#379 复核缺口 3）**：函数体内每个 `return` 都必须
  是可确认的带值出口：
  - 裸 `return` / `return None` → 红（该分支仍退 0）；
  - `return <BoolOp>`（`or` / `and` 兜底）→ 红：短路结果可能落到兜底常量，
    把上游 `None` 吞成成功；
  - `return <IfExp>` → 仅当两支都能静态确认非 None（非 `None` 常量、`not`、
    比较运算、嵌套同类 `IfExp`）才算带值；`return x if cond else 0` 判红；
  - `return <Call>` / `<Name>` / `<Attribute>` 按带值处理。这是显式的能力
    边界：静态无法证明运行期是否为 `None`。实测真实树 72 个入口里 31 个含
    `Call` 出口、28 个只有常量出口、13 个含 `IfExp` 出口（均双常量分支），
    全红会把门禁变成噪音。
- 定义解析不出来 → 红（无法证明它带值返回，fail-closed）。
- 入口文件在 `EXEMPT` 显式豁免清单里 → 先解析函数、再验**理由守卫**
  （#379 复核缺口 5）：守卫不成立就红，不再整文件跳过；`EXEMPT` 里的入口
  在真实树里不再被枚举也算红（防止清单腐烂）。

**扫描失败必须判红（#379 复核缺口 4）**：`scripts/` 下只收 `.py`，`.githooks/`
下只收后缀 `.py` 或首行 shebang 含 python 的文件（其余是 shell 钩子，按文件
类型排除，不是「扫不动就放行」）。归入 Python 的文件读不出或 `ast.parse`
抛 `SyntaxError` → 计入命中并红。

**为什么不靠「函数体内有 sys.exit / 裸 assert」的逃逸规则**：那会把四条规定为安全假阳性的入口一并静默放过，以后真出现同类缺陷时同样静默；这里改成显式豁免清单 + 逐条理由守卫，改名、搬家、新增都要人看一眼。

`self_test()` 用临时包夹具做反向对照（漏 `return` 必红、补上转绿），覆盖装饰器、别名、条件表达式/`or` 兜底、语法错文件、豁免守卫、lambda、`sys.exit(0)` 显式成功与 `mod.main()` 误红面，并钉住本脚本自己的 `main()` 带值返回——否则新门禁会把自身判红（自指陷阱）。

    .venv/Scripts/python.exe scripts/check_exit_main_return.py
"""
import ast
import sys
import tempfile
from collections import namedtuple
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ('scripts', '.githooks')

# 解析结果：被调函数节点（FunctionDef / AsyncFunctionDef / Lambda）、定义所在
# 模块的 AST（理由守卫要扫同包）、定义文件路径。
Resolved = namedtuple('Resolved', 'node tree path')

# 显式豁免（入口文件相对路径 → why + 理由守卫）。守卫是「理由仍成立」的机器
# 校验：文件后续退化（删掉 sys.exit(1) / 断言）时必须翻红，而不是继续豁免。
EXEMPT = {
    'scripts/check_qr_login/__main__.py': {
        'why': '`main()` 失败路径直接 `sys.exit(1)`，`SystemExit` 先抛出、不会正常返回 None（防 #375 复发）',
        'guard': 'sys_exit_nonzero',
    },
    'scripts/check_sixty/__main__.py': {
        'why': '`main()` 失败路径直接 `sys.exit(1)`，`SystemExit` 先抛出、不会正常返回 None（防 #375 复发）',
        'guard': 'sys_exit_nonzero',
    },
    'scripts/check_dossier_cadence/__main__.py': {
        'why': '用例里的裸 `assert` 失败抛 `AssertionError`，进程非 0，不是「打印 FAIL 却退 0」的形状（断言在同包 quiet_rounds/floor_reading 等模块，防 #375 复发）',
        'guard': 'package_assert',
    },
    'scripts/check_mcp_chat_turns/__main__.py': {
        'why': '`main()` 是 assert 驱动的用例：任何失败断言抛 `AssertionError` 让进程非 0，全部通过时返回 None→0 才是成功（#379 首次扫描新发现，人工核对安全）',
        'guard': 'package_assert',
    },
}


def own_nodes(body):
    """遍历函数体但不进入嵌套函数 / 类：它们的 `return` 不属于这个函数的出口。"""
    stack = list(body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def return_values(func_node):
    """被调函数的所有带值出口：普通函数取 `ast.Return`，lambda 取表达式体。"""
    if isinstance(func_node, ast.Lambda):
        return [func_node.body]
    return [
        node.value
        for node in own_nodes(func_node.body)
        if isinstance(node, ast.Return) and node.value is not None
    ]


def has_bare_return(func_node):
    """函数体内是否存在裸 `return`（该分支退 0）。lambda 没有裸 return。"""
    if isinstance(func_node, ast.Lambda):
        return False
    return any(
        isinstance(node, ast.Return) and node.value is None
        for node in own_nodes(func_node.body)
    )


def constant_int(expr):
    """整型常量（含正负号）的取值；不是整型常量返回 None。"""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, int) and not isinstance(expr.value, bool):
        return expr.value
    if (
        isinstance(expr, ast.UnaryOp)
        and isinstance(expr.op, (ast.USub, ast.UAdd))
        and isinstance(expr.operand, ast.Constant)
        and isinstance(expr.operand.value, int)
        and not isinstance(expr.operand.value, bool)
    ):
        return -expr.operand.value if isinstance(expr.op, ast.USub) else expr.operand.value
    return None


def static_non_none(expr):
    """能否静态确认表达式不是 `None`（常量 / bool 运算 / 容器 / 同类条件表达式）。"""
    if isinstance(expr, ast.Constant):
        return expr.value is not None
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, (ast.Not, ast.USub, ast.UAdd)):
        return isinstance(expr.op, ast.Not) or constant_int(expr) is not None
    if isinstance(expr, (ast.Compare, ast.JoinedStr, ast.List, ast.Tuple, ast.Set, ast.Dict)):
        return True
    if isinstance(expr, ast.IfExp):
        return static_non_none(expr.body) and static_non_none(expr.orelse)
    return False


def return_problem(func_node):
    """返回 None 表示所有出口都可确认带值；否则返回一句判红理由。"""
    if has_bare_return(func_node):
        return '函数里存在裸 `return`，该分支的退出码会恒为 None→0'
    values = return_values(func_node)
    if not values:
        return '函数体没有带值 return'
    for value in values:
        if isinstance(value, ast.Constant) and value.value is None:
            return '出现 `return None`，失败路径同样退 0'
        if isinstance(value, ast.BoolOp):
            return f'`return {ast.unparse(value)}` 用布尔短路兜底，上游 None 会被吞成成功'
        if isinstance(value, ast.IfExp) and not (
            static_non_none(value.body) and static_non_none(value.orelse)
        ):
            return f'`return {ast.unparse(value)}` 有一支无法静态确认非 None'
    return None


def decorator_problem(record):
    """被调函数带装饰器 → fail-closed 判红（无法确认装饰器保留返回值）。"""
    node = record.node
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list:
        names = '、'.join(ast.unparse(item) for item in node.decorator_list)
        return f'被调函数被装饰器包装（{names}），无法确认装饰器保留返回值'
    return None


def import_aliases(tree):
    """收集 `sys.exit` / `SystemExit` 的导入别名（#379 复核缺口 2）。"""
    sys_names = {'sys'}
    exit_names = set()
    system_exit_names = {'SystemExit'}
    builtins_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == 'sys':
                    sys_names.add(alias.asname or 'sys')
                elif alias.name == 'builtins':
                    builtins_names.add(alias.asname or 'builtins')
        elif isinstance(node, ast.ImportFrom) and not node.level:
            if node.module == 'sys':
                for alias in node.names:
                    if alias.name == 'exit':
                        exit_names.add(alias.asname or 'exit')
            elif node.module == 'builtins':
                for alias in node.names:
                    if alias.name == 'SystemExit':
                        system_exit_names.add(alias.asname or 'SystemExit')
    return sys_names, exit_names, system_exit_names, builtins_names


def exit_call(node, aliases):
    """`sys.exit(...)` / `raise SystemExit(...)` 对应的调用节点；其它节点返回 None。"""
    sys_names, exit_names, system_exit_names, builtins_names = aliases
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in exit_names:
            return node
        if (
            isinstance(func, ast.Attribute)
            and func.attr == 'exit'
            and isinstance(func.value, ast.Name)
            and func.value.id in sys_names
        ):
            return node
        return None
    if isinstance(node, ast.Raise):
        exc = node.exc
        if not isinstance(exc, ast.Call):
            return None
        func = exc.func
        if isinstance(func, ast.Name) and func.id in system_exit_names:
            return exc
        if (
            isinstance(func, ast.Attribute)
            and func.attr == 'SystemExit'
            and isinstance(func.value, ast.Name)
            and func.value.id in builtins_names
        ):
            return exc
    return None


def exit_argument(node, aliases):
    """退出调用的第一个参数节点；无参数或非退出调用返回 None。"""
    call = exit_call(node, aliases)
    return call.args[0] if call is not None and call.args else None


def zero_arg_call(expr):
    """无位置参数、无关键字参数的 `Call`；否则 None。"""
    if isinstance(expr, ast.Call) and not expr.args and not expr.keywords:
        return expr
    return None


def first_zero_arg_call(expr):
    """兜底表达式里被交出的那个零参调用（`main() or 0` / `main() if c else 0`）。"""
    for node in ast.walk(expr):
        call = zero_arg_call(node)
        if call is not None:
            return call
    return None


def entry_argument_problem(argument, inner, record, wrapper):
    """入口参数侧的兜底判据（#382 复核条件 1）；None 表示可放行。

    `wrapper=False` 是裸零参调用，没有兜底问题；`BoolOp` 一律红（短路结果可能
    落到兜底常量）；`IfExp` 两支都非 None 常量时放行，一支是已确认带值的零参
    调用、另一支是非零整型常量时也放行（非零兜底不会把失败伪装成成功），其余红。
    """
    if not wrapper:
        return None
    if isinstance(argument, ast.BoolOp):
        return f'参数 `{ast.unparse(argument)}` 用布尔短路兜底，上游 None 会被吞成成功'
    if isinstance(argument, ast.IfExp):
        branches = (argument.body, argument.orelse)
        if all(isinstance(item, ast.Constant) and item.value is not None for item in branches):
            return None
        other = argument.orelse if argument.body is inner else argument.body
        fallback = constant_int(other)
        safe_call = (
            record is not None
            and decorator_problem(record) is None
            and return_problem(record.node) is None
        )
        if fallback is not None and fallback != 0 and safe_call:
            return None
        return f'参数 `{ast.unparse(argument)}` 有一支无法静态确认非 None（不能靠 0/None 兜底）'
    return None


def callee_ref(call):
    """被交出的调用：(限定名, 函数名)。`main()` → (None, 'main')；
    `mod.main()` → ('mod', 'main')；更深的属性链 → ('?', None)。"""
    func = call.func
    if isinstance(func, ast.Name):
        return None, func.id
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name):
            return func.value.id, func.attr
        return '?', None
    return None, None


def relative_base(path, level):
    """相对导入的基准目录（`from . import x` 的 level=1 就是当前包）。"""
    base = path.parent
    for _ in range(level - 1):
        base = base.parent
    return base


def import_targets(path, node, name):
    """`ImportFrom` 可能指向的模块文件（相对导入 / `scripts.*` / `scripts/<模块>`）。"""
    targets = []
    if node.level:
        base = relative_base(path, node.level)
        if node.module:
            target = base.joinpath(*node.module.split('.'))
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


def module_targets(path, tree, qualifier):
    """`mod.main()` 里 `mod` 指向的模块文件（一层模块限定名）。"""
    targets = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.asname or alias.name) != qualifier:
                    continue
                parts = alias.name.split('.')
                for root in (ROOT, ROOT / 'scripts'):
                    target = root.joinpath(*parts)
                    targets += [target.with_suffix('.py'), target / '__init__.py']
        elif isinstance(node, ast.ImportFrom):
            if not any((alias.asname or alias.name) == qualifier for alias in node.names):
                continue
            if node.module:
                base = relative_base(path, node.level) if node.level else None
                roots = (base,) if base else (ROOT, ROOT / 'scripts')
                for root in roots:
                    target = root.joinpath(*node.module.split('.'), qualifier)
                    targets += [target.with_suffix('.py'), target / '__init__.py']
            else:
                base = relative_base(path, node.level) if node.level else path.parent
                targets += [base / f'{qualifier}.py', base / qualifier / '__init__.py']
    return targets


def parse_module(path):
    """解析一个 Python 文件；读不出 / 语法错返回 None（调用方按 fail-closed 处理）。"""
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
        return ast.parse(text, filename=str(path))
    except (OSError, SyntaxError):
        return None


def find_top_level_def(path, name):
    """在 `path` 的顶层找 `def <name>` 或 `name = lambda: ...`；解析失败返回 None。"""
    tree = parse_module(path)
    if tree is None:
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return Resolved(node, tree, path)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == name
                    and isinstance(node.value, ast.Lambda)
                ):
                    return Resolved(node.value, tree, path)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and isinstance(node.value, ast.Lambda)
        ):
            return Resolved(node.value, tree, path)
    return None


def resolve_function(path, tree, qualifier, name):
    """解析被交出的函数定义；解析不出返回 None。"""
    if qualifier is None:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return Resolved(node, tree, path)
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Name)
                        and target.id == name
                        and isinstance(node.value, ast.Lambda)
                    ):
                        return Resolved(node.value, tree, path)
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == name
                and isinstance(node.value, ast.Lambda)
            ):
                return Resolved(node.value, tree, path)
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
    if qualifier == '?':
        return None
    for target in module_targets(path, tree, qualifier):
        if not target.is_file():
            continue
        found = find_top_level_def(target, name)
        if found is not None:
            return found
    return None


def guard_sys_exit_nonzero(record):
    """守卫：被调函数体内仍有非零常量 `sys.exit(...)`。"""
    for node in own_nodes(record.node.body):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == 'exit'
            and isinstance(func.value, ast.Name)
            and func.value.id == 'sys'
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, int)
            and node.args[0].value != 0
        ):
            return True
    return False


def guard_package_assert(record):
    """守卫：定义模块所在包内仍有断言驱动用例（断言可能拆在同包模块里）。"""
    base = record.path.parent
    for path in sorted(base.glob('*.py')):
        tree = parse_module(path)
        if tree is None:
            return False  # 扫不动就无法确认理由仍成立 → 守卫失败
        if any(isinstance(node, ast.Assert) for node in ast.walk(tree)):
            return True
    return False


GUARDS = {
    'sys_exit_nonzero': guard_sys_exit_nonzero,
    'package_assert': guard_package_assert,
}


def is_python_source(path):
    """`.py` 或 shebang 含 python 的钩子才进 AST 扫描；读不出按 Python 处理（fail-closed）。"""
    if path.suffix == '.py':
        return True
    try:
        first = path.read_text(encoding='utf-8', errors='replace').splitlines()[0]
    except (OSError, IndexError):
        return True
    return first.startswith('#!') and 'python' in first.lower()


def collect_sources(root):
    """扫描范围：`scripts/` 下的 .py 与 `.githooks/` 下的 Python 钩子。"""
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
            if base_name != 'scripts' and not is_python_source(path):
                continue
            sources.append((path.relative_to(root).as_posix(), path))
    return sources


def discarded_exit_hits(tree, aliases):
    """`rc = main(); sys.exit(0)` / `sys.exit()` 的显式丢弃（#382 复核条件 2）。

    只认同一语句块里**紧邻**的「先取码、再显式退 0/空参」；`main(); sys.exit(0)`
    是已声明边界，`rc = main(); sys.exit(rc)` 是正确写法，都不判红。
    """
    hits = []
    for node in ast.walk(tree):
        for field in ('body', 'orelse', 'finalbody'):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for prev, nxt in zip(block, block[1:]):
                if not isinstance(nxt, ast.Expr):
                    continue
                call = exit_call(nxt.value, aliases)
                if call is None:
                    continue
                success = not call.args or constant_int(call.args[0]) == 0
                if not success:
                    continue
                variable = None
                if (
                    isinstance(prev, ast.Assign)
                    and len(prev.targets) == 1
                    and isinstance(prev.targets[0], ast.Name)
                    and zero_arg_call(prev.value) is not None
                ):
                    variable = prev.targets[0].id
                elif (
                    isinstance(prev, ast.AnnAssign)
                    and isinstance(prev.target, ast.Name)
                    and zero_arg_call(prev.value) is not None
                ):
                    variable = prev.target.id
                if variable is not None:
                    hits.append(
                        (nxt.lineno,
                         f'`{variable} = {ast.unparse(prev.value)}` 后 `{ast.unparse(nxt.value)}`：'
                         '先取退出码再显式丢弃，返回值没有传给退出码')
                    )
    return hits


def audit(sources, check_stale=False):
    """扫描一组 `(相对路径, 文件路径)` → (入口, 豁免, 命中)。纯函数，供 main 与 self_test 共用。"""
    entries, exempted, hits = [], [], []
    for rel, path in sources:
        tree = parse_module(path)
        if tree is None:
            hits.append((rel, 0, '无法解析为 Python（读取失败或 SyntaxError），按 fail-closed 判红'))
            continue
        aliases = import_aliases(tree)
        for node in ast.walk(tree):
            argument = exit_argument(node, aliases)
            if argument is None:
                continue
            kind = 'sys.exit' if isinstance(node, ast.Call) else 'raise SystemExit'
            if zero_arg_call(argument) is not None:
                inner, wrapper = argument, False
            elif isinstance(argument, (ast.BoolOp, ast.IfExp)):
                inner = first_zero_arg_call(argument)
                if inner is None:
                    continue  # 纯常量 switch / 常量兜底，没有可解析的被交出函数
                wrapper = True
            else:
                continue  # 非零参调用不在口径内（判据按形状，不按函数名）
            if isinstance(inner.func, ast.Lambda):
                name = '<lambda>'
                record = Resolved(inner.func, tree, path)
            else:
                qualifier, name = callee_ref(inner)
                if name is None:
                    hits.append(
                        (rel, node.lineno,
                         f'{kind}({ast.unparse(argument)}) 无法解析被交出的函数')
                    )
                    continue
                record = resolve_function(path, tree, qualifier, name)
                if record is None:
                    hits.append(
                        (rel, node.lineno,
                         f'{kind}({ast.unparse(argument)}) 的被调函数定义未解析，'
                         '无法确认它带值 return')
                    )
                    continue
            entries.append((rel, node.lineno, kind, name))
            wrapper_problem = entry_argument_problem(argument, inner, record, wrapper)
            if wrapper_problem is not None:
                hits.append(
                    (rel, node.lineno, f'{kind}({ast.unparse(argument)}) 的{wrapper_problem}')
                )
                continue
            if rel in EXEMPT:
                guard = GUARDS[EXEMPT[rel]['guard']]
                if guard(record):
                    exempted.append((rel, node.lineno, kind, name))
                else:
                    hits.append(
                        (rel, node.lineno,
                         f'{kind}({name}()) 的豁免理由已不成立（守卫 {EXEMPT[rel]["guard"]} 未命中），'
                         '请修复或删除豁免')
                    )
                continue
            problem = decorator_problem(record) or return_problem(record.node)
            if problem is not None:
                hits.append((rel, node.lineno, f'{kind}({name}()) 的{problem}'))
        for line, message in discarded_exit_hits(tree, aliases):
            hits.append((rel, line, message))
    if check_stale:
        seen = {rel for rel, _line, _kind, _name in entries}
        for rel in EXEMPT:
            if rel not in seen:
                hits.append((rel, 0, '豁免项已不再被枚举（入口消失或改写），请复核理由或删除豁免'))
    return entries, exempted, hits


def self_test():
    """离线自检：委托给同目录夹具模块，避免主门禁越过 advisory 行数上限。"""
    script_dir = Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    from exit_main_return_selftest import run_self_test
    run_self_test(sys.modules[__name__])


def main():
    """入口：先自检，再扫真实树；有命中返回 1（本函数自身必须带值 return）。"""
    self_test()
    entries, exempted, hits = audit(collect_sources(ROOT), check_stale=True)
    print(f'  info 入口 {len(entries)} 个（sys.exit / raise SystemExit），豁免 {len(exempted)} 个')
    for rel, line, kind, name in exempted:
        print(f'  ok  豁免：{rel}:{line} {kind}({name}()) — {EXEMPT[rel]["why"]}')
    if hits:
        for rel, line, message in hits:
            print(f'  FAIL {rel}:{line} {message}')
        print(f'FAIL: {len(hits)} 个入口会把真实退出码吞成 0（issue #379）。')
        return 1
    print('PASS: 所有入口的被调函数都有可确认的带值 return（或在带守卫的显式豁免清单里）。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
