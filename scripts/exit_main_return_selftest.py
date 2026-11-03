"""`check_exit_main_return.py` 的离线回归夹具（#379 / #382）。

拆到独立模块，避免主门禁因夹具继续增长越过 `check_module_size.py` 的 advisory
行数上限；本文件不是门禁（不以 `check_` 开头），不登记进 `gates.json`。
`run_self_test(gate)` 由主门禁在 `self_test()` 里传入当前模块对象，保证
`EXEMPT` 补丁作用在真正运行的那份模块上。
"""
import ast
import tempfile
from pathlib import Path


def run_self_test(gate):
    """每条口径都配注入必红 / 修复必绿的对照，并钉住自指陷阱。"""
    failures = []

    def expect(name, got, want):
        if got != want:
            failures.append(f'{name}：期望 {want!r}，实得 {got!r}')

    def write_package(root, package, init_body, entry_body=None):
        package_dir = Path(root) / package
        package_dir.mkdir(parents=True, exist_ok=True)
        (package_dir / '__init__.py').write_text(init_body, encoding='utf-8')
        (package_dir / '__main__.py').write_text(
            entry_body if entry_body is not None else
            'import sys\nfrom . import main\n\n'
            "if __name__ == '__main__':\n    sys.exit(main())\n",
            encoding='utf-8',
        )
        return package_dir

    def audit_package(package_dir):
        return gate.audit([(f'scripts/{package_dir.name}/__main__.py', package_dir / '__main__.py')])

    bad_main = 'def main():\n    print("FAIL")\n'
    good_main = 'def main():\n    print("FAIL")\n    return 0\n'

    with tempfile.TemporaryDirectory() as tmp:
        # 1) 原有反向对照：漏 return 必红，同形补上转绿。
        package = write_package(tmp, 'pkg', bad_main)
        entries, exempted, hits = audit_package(package)
        expect('#379 反向对照：漏 return 被扫出', len(hits), 1)
        expect('#379 反向对照：命中签名带文件名', hits and hits[0][0], 'scripts/pkg/__main__.py')
        expect('#379 反向对照：入口被枚举', len(entries), 1)

        package = write_package(tmp, 'pkg', good_main)
        entries, exempted, hits = audit_package(package)
        expect('#379 同形对照：带值 return 放行', len(hits), 0)
        expect('#379 同形对照：入口仍被枚举', len(entries), 1)

        # 2) raise SystemExit 形态与非 main 命名同样命中。
        (package / '__init__.py').write_text(bad_main, encoding='utf-8')
        bad_raise = package / '__main__.py'
        bad_raise.write_text(
            'from . import main\n\n'
            "if __name__ == '__main__':\n    raise SystemExit(main())\n",
            encoding='utf-8',
        )
        entries, exempted, hits = gate.audit([('scripts/pkg/__main__.py', bad_raise)])
        expect('#379 raise SystemExit 形态同样命中', len(hits), 1)

        alias_dir = Path(tmp) / 'alias'
        alias_dir.mkdir()
        (alias_dir / '__init__.py').write_text('def run_main():\n    print("FAIL")\n', encoding='utf-8')
        (alias_dir / '__main__.py').write_text(
            'import sys\nfrom . import run_main\n\n'
            "if __name__ == '__main__':\n    sys.exit(run_main())\n",
            encoding='utf-8',
        )
        entries, exempted, hits = gate.audit([('scripts/alias/__main__.py', alias_dir / '__main__.py')])
        expect('#379 非 main 命名的零参调用同样命中', len(hits), 1)
        expect('#379 非 main 命名的入口名被识别', entries and entries[0][3], 'run_main')

        # 3) 非零参调用不在口径内：与 check_no_console_child 的真实形状同形。
        nonzero = Path(tmp) / 'nonzero'
        nonzero.mkdir()
        (nonzero / '__init__.py').write_text('def run_main(root):\n    return 0\n', encoding='utf-8')
        (nonzero / '__main__.py').write_text(
            'import sys\nfrom pathlib import Path\nfrom . import run_main\n\n'
            "if __name__ == '__main__':\n    sys.exit(run_main(Path('x')))\n",
            encoding='utf-8',
        )
        entries, exempted, hits = gate.audit([('scripts/nonzero/__main__.py', nonzero / '__main__.py')])
        expect('#379 非零参调用不误判', (len(entries), len(hits)), (0, 0))

        # 3b) 入口参数侧兜底（#382 复核条件 1）：与 `return f() or 0` 同一机制。
        wrapper_cases = {
            'entry_or': ('sys.exit(main() or 0)', 1, 1),
            'entry_raise_or': ('raise SystemExit(main() or 0)', 1, 1),
            'entry_if_zero': ('sys.exit(main() if cond else 0)', 1, 1),
            'entry_if_one': ('sys.exit(main() if cond else 1)', 1, 0),
            'entry_if_consts': ('sys.exit(1 if cond else 0)', 0, 0),
        }
        for package_name, (statement, want_entries, want_hits) in wrapper_cases.items():
            package = write_package(
                tmp, package_name, good_main,
                'import sys\nfrom . import main\n\n'
                "if __name__ == '__main__':\n    " + statement + '\n',
            )
            entries, exempted, hits = audit_package(package)
            expect(
                f'#382 入口兜底：{package_name}',
                (len(entries), len(hits)),
                (want_entries, want_hits),
            )

        # 3c) `main(); sys.exit(0)` 边界与 `rc = main(); ...` 显式丢弃（条件 2）。
        boundary_cases = {
            'entry_seq_zero': ('main()\n    sys.exit(0)', 0, 0),
            'entry_seq_noarg': ('main()\n    sys.exit()', 0, 0),
            'entry_rc_zero': ('rc = main()\n    sys.exit(0)', 0, 1),
            'entry_rc_noarg': ('rc = main()\n    sys.exit()', 0, 1),
            'entry_rc_pass': ('rc = main()\n    sys.exit(rc)', 0, 0),
            'entry_ex_assign': ('ex = sys.exit\n    ex(main())', 0, 0),
        }
        for package_name, (statement, want_entries, want_hits) in boundary_cases.items():
            package = write_package(
                tmp, package_name, good_main,
                'import sys\nfrom . import main\n\n'
                "if __name__ == '__main__':\n    " + statement + '\n',
            )
            entries, exempted, hits = audit_package(package)
            expect(
                f'#382 边界：{package_name}',
                (len(entries), len(hits)),
                (want_entries, want_hits),
            )

        # 4) 装饰器 fail-closed：即使函数本身带值 return，装饰器也让判据红。
        package = write_package(
            tmp, 'decorated',
            'def deco(fn):\n    return fn\n\n\n@deco\ndef main():\n    return 0\n',
        )
        entries, exempted, hits = audit_package(package)
        expect('#379 装饰器 fail-closed：带 return 也红', len(hits), 1)
        expect('#379 装饰器 fail-closed：文案点明装饰器', '装饰器' in (hits[0][2] if hits else ''), True)
        package = write_package(tmp, 'undecorated', good_main)
        entries, exempted, hits = audit_package(package)
        expect('#379 装饰器对照：去掉装饰器转绿', len(hits), 0)

        # 5) sys.exit / SystemExit 别名解析（from sys import exit as e 等）。
        package = write_package(
            tmp, 'alias_exit', bad_main,
            'from sys import exit as e\nfrom . import main\n\n'
            "if __name__ == '__main__':\n    e(main())\n",
        )
        entries, exempted, hits = audit_package(package)
        expect('#379 别名：from sys import exit as e 命中', len(hits), 1)

        package = write_package(
            tmp, 'alias_sys', bad_main,
            'import sys as s\nfrom . import main\n\n'
            "if __name__ == '__main__':\n    s.exit(main())\n",
        )
        entries, exempted, hits = audit_package(package)
        expect('#379 别名：import sys as s 命中', len(hits), 1)

        package = write_package(
            tmp, 'alias_se', bad_main,
            'from builtins import SystemExit as SE\nfrom . import main\n\n'
            "if __name__ == '__main__':\n    raise SE(main())\n",
        )
        entries, exempted, hits = audit_package(package)
        expect('#379 别名：SystemExit 别名命中', len(hits), 1)

        package = write_package(
            tmp, 'alias_getattr', bad_main,
            'import sys\nfrom . import main\n\n'
            "if __name__ == '__main__':\n    getattr(sys, 'exit')(main())\n",
        )
        entries, exempted, hits = audit_package(package)
        expect('#379 别名边界：getattr 形态不在口径', (len(entries), len(hits)), (0, 0))

        # 6) 条件表达式 / or 兜底口径。
        cases = {
            'fallback_or': ('def main():\n    return run() or 0\n', 1),
            'fallback_if': ('def main():\n    return 1 if bad else 0\n', 0),
            'fallback_if_negative': ('def main():\n    return 0 if ok else -1\n', 0),
            'fallback_if_name': ('def main():\n    return x if cond else 0\n', 1),
            'return_none': ('def main():\n    return None\n', 1),
            'return_bare': ('def main():\n    if cond:\n        return 1\n    return\n', 1),
            'return_const': ('def main():\n    return 0\n', 0),
        }
        for package_name, (body, want) in cases.items():
            package = write_package(tmp, package_name, body)
            entries, exempted, hits = audit_package(package)
            expect(f'#379 兜底口径：{package_name}', len(hits), want)

        # 7) SyntaxError fail-closed；shell 钩子按文件类型排除。
        broken = Path(tmp) / 'broken.py'
        broken.write_text('def main(:\n', encoding='utf-8')
        entries, exempted, hits = gate.audit([('scripts/broken.py', broken)])
        expect('#379 语法错文件 fail-closed', len(hits), 1)
        expect('#379 语法错命中文案带 fail-closed', 'fail-closed' in (hits[0][2] if hits else ''), True)

        hook_tree = Path(tmp) / 'hooks_tree'
        (hook_tree / 'scripts').mkdir(parents=True)
        (hook_tree / '.githooks').mkdir()
        (hook_tree / 'scripts' / 'ok.py').write_text(good_main, encoding='utf-8')
        (hook_tree / '.githooks' / 'pre-push').write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        sources = gate.collect_sources(hook_tree)
        expect('#379 shell 钩子不进 AST 扫描', [rel for rel, _p in sources], ['scripts/ok.py'])
        expect(
            '#379 shell 钩子被判为非 Python',
            gate.is_python_source(hook_tree / '.githooks' / 'pre-push'),
            False,
        )

        # 8) 豁免守卫：理由仍在 → 豁免；守卫退化 → 红。
        exempt_dir = Path(tmp) / 'check_dossier_cadence'
        exempt_dir.mkdir()
        (exempt_dir / '__init__.py').write_text('def main():\n    assert True\n', encoding='utf-8')
        (exempt_dir / '__main__.py').write_text(
            'import sys\nfrom . import main\n\n'
            "if __name__ == '__main__':\n    sys.exit(main())\n",
            encoding='utf-8',
        )
        entries, exempted, hits = gate.audit(
            [('scripts/check_dossier_cadence/__main__.py', exempt_dir / '__main__.py')]
        )
        expect('#379 豁免守卫：理由成立时放行', len(hits), 0)
        expect('#379 豁免会计入豁免数而非通过数', len(exempted), 1)

        (exempt_dir / '__init__.py').write_text('def main():\n    print("FAIL")\n', encoding='utf-8')
        entries, exempted, hits = gate.audit(
            [('scripts/check_dossier_cadence/__main__.py', exempt_dir / '__main__.py')]
        )
        expect('#379 豁免守卫：断言被删后翻红', len(hits), 1)
        expect('#379 豁免守卫：文案点明理由不成立', '理由已不成立' in (hits[0][2] if hits else ''), True)

        exit_dir = Path(tmp) / 'check_qr_login'
        exit_dir.mkdir()
        (exit_dir / '__init__.py').write_text('import sys\n\n\ndef main():\n    sys.exit(1)\n',
                                              encoding='utf-8')
        (exit_dir / '__main__.py').write_text(
            'import sys\nfrom . import main\n\n'
            "if __name__ == '__main__':\n    sys.exit(main())\n",
            encoding='utf-8',
        )
        entries, exempted, hits = gate.audit(
            [('scripts/check_qr_login/__main__.py', exit_dir / '__main__.py')]
        )
        expect('#379 豁免守卫：sys.exit(1) 成立时放行', (len(hits), len(exempted)), (0, 1))
        (exit_dir / '__init__.py').write_text('def main():\n    print("FAIL")\n', encoding='utf-8')
        entries, exempted, hits = gate.audit(
            [('scripts/check_qr_login/__main__.py', exit_dir / '__main__.py')]
        )
        expect('#379 豁免守卫：sys.exit(1) 被删后翻红', len(hits), 1)

        ghost = 'scripts/ghost/__main__.py'
        saved = dict(gate.EXEMPT)
        gate.EXEMPT.clear()
        gate.EXEMPT[ghost] = {'why': '自检夹具', 'guard': 'package_assert'}
        try:
            entries, exempted, hits = gate.audit([], check_stale=True)
            expect('#379 失效豁免：真实树里不存在的豁免判红', len(hits), 1)
            expect('#379 失效豁免：文案点明不再被枚举', '不再被枚举' in (hits[0][2] if hits else ''), True)
        finally:
            gate.EXEMPT.clear()
            gate.EXEMPT.update(saved)

        # 9) 复核提到的误红面：sys.exit(0)、lambda、mod.main()。
        ok = Path(tmp) / 'explicit_zero'
        ok.mkdir()
        (ok / '__init__.py').write_text(good_main, encoding='utf-8')
        (ok / '__main__.py').write_text(
            'import sys\n\nif __name__ == \'__main__\':\n    sys.exit(0)\n', encoding='utf-8')
        entries, exempted, hits = gate.audit(
            [('scripts/explicit_zero/__main__.py', ok / '__main__.py')])
        expect('#379 显式成功：sys.exit(0) 不计入口不误红', (len(entries), len(hits)), (0, 0))

        lam = Path(tmp) / 'lambda_main'
        lam.mkdir()
        (lam / '__init__.py').write_text('main = lambda: 0\n', encoding='utf-8')
        (lam / '__main__.py').write_text(
            'import sys\nfrom . import main\n\n'
            "if __name__ == '__main__':\n    sys.exit(main())\n",
            encoding='utf-8',
        )
        entries, exempted, hits = gate.audit(
            [('scripts/lambda_main/__main__.py', lam / '__main__.py')])
        expect('#379 lambda：赋值为带值 lambda 时放行', (len(entries), len(hits)), (1, 0))
        (lam / '__init__.py').write_text('main = lambda: None\n', encoding='utf-8')
        entries, exempted, hits = gate.audit(
            [('scripts/lambda_main/__main__.py', lam / '__main__.py')])
        expect('#379 lambda：lambda 体为 None 判红', len(hits), 1)

        lam_iife = Path(tmp) / 'lambda_iife'
        lam_iife.mkdir()
        (lam_iife / '__main__.py').write_text(
            'import sys\n\nif __name__ == \'__main__\':\n    sys.exit((lambda: 0)())\n',
            encoding='utf-8',
        )
        entries, exempted, hits = gate.audit(
            [('scripts/lambda_iife/__main__.py', lam_iife / '__main__.py')])
        expect('#379 lambda：立即调用带值不误红', (len(entries), len(hits)), (1, 0))

        mod_dir = Path(tmp) / 'mod_main'
        mod_dir.mkdir()
        (mod_dir / 'mod.py').write_text('def main():\n    return 0\n', encoding='utf-8')
        (mod_dir / '__main__.py').write_text(
            'import sys\nfrom . import mod\n\n'
            "if __name__ == '__main__':\n    sys.exit(mod.main())\n",
            encoding='utf-8',
        )
        entries, exempted, hits = gate.audit(
            [('scripts/mod_main/__main__.py', mod_dir / '__main__.py')])
        expect('#379 mod.main()：一层限定名解析后放行', (len(entries), len(hits)), (1, 0))
        (mod_dir / 'mod.py').write_text('def main():\n    print("FAIL")\n', encoding='utf-8')
        entries, exempted, hits = gate.audit(
            [('scripts/mod_main/__main__.py', mod_dir / '__main__.py')])
        expect('#379 mod.main()：漏 return 判红', len(hits), 1)

    # 10) 自指陷阱：主门禁自己的 main() 必须带值 return，且真实树扫描零命中。
    gate_path = Path(gate.__file__).resolve()
    own_tree = gate.parse_module(gate_path)
    expect('#379 自指：本脚本可解析', own_tree is not None, True)
    own_main = next(
        (node for node in (own_tree.body if own_tree else [])
         if isinstance(node, ast.FunctionDef) and node.name == 'main'),
        None,
    )
    expect('#379 自指：本脚本有 main() 定义', own_main is not None, True)
    expect('#379 自指：本脚本 main() 可确认带值 return',
           gate.return_problem(own_main) if own_main else 'missing', None)
    own_rel = gate_path.relative_to(gate.ROOT).as_posix()
    entries, exempted, hits = gate.audit([(own_rel, gate_path)])
    expect('#379 自指：本脚本扫描零命中', len(hits), 0)

    if failures:
        for line in failures:
            print(f'SELF-TEST FAIL {line}')
        raise AssertionError(f'self_test 失败 {len(failures)} 项（issue #379 夹具）')
    print('SELF-TEST OK 入口枚举、别名、装饰器 fail-closed、兜底口径、语法错 fail-closed、'
          '豁免守卫与自指陷阱的回归夹具全部符合预期')
