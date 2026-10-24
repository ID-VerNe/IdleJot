"""Module size gate: no file or class may grow past the split line (issue #11).

Root cause this gate guards against: the two DRY/SRP audit rounds
(``doc/DRY_SRP_AUDIT_2026-10-19.md`` / ``-20.md``) fixed duplicated fragments but
never capped single-module growth, so by issue #11 the memory engine had become
one 1500-line class mixing six responsibilities, ``reload.py`` mixed AST import
closure analysis with process/port restart execution, and ``mcp_chat.py`` mixed a
hand-written schema factory with the chat class. Every later change pays review
and regression cost multiplied by that size.

The gate asserts two walls:

1. No source file exceeds ``MAX_FILE_LINES`` physical lines.
2. No class spans more than ``MAX_CLASS_SPAN`` lines.

Files in the 700+ band are reported as ADVISORY (visible, not fatal) so they do
not silently drift toward the wall. Current violators live in ``EXEMPTIONS``,
each mapped to the issue #11 section that removes it; an empty exemption list
means issue #11's split work is done.

    .venv/Scripts/python.exe scripts/check_module_size.py
    .venv/Scripts/python.exe scripts/check_module_size.py --self-test
"""
import argparse
import ast
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = ('chatlocal', 'water-group', 'water_group')
MAX_FILE_LINES = 1000
MAX_CLASS_SPAN = 800
ADVISORY_FILE_LINES = 700

# Violators pinned at gate creation, each removed by one issue #11 section.
# Keep this list honest: a stale entry hides real growth.
EXEMPTIONS = {
    'water-group/memory_engine.py': 'PA: split storage/use-case layers',
    'water-group/daemon/reload.py': 'PD: split plan analysis from restart execution',
    'chatlocal/mcp_chat.py': 'PC: move the schema factory out',
}


def iter_sources(root):
    """Every Python source the gate owns, in a stable order."""
    for folder in SCAN_DIRS:
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob('*.py')):
            if '__pycache__' in path.parts:
                continue
            yield path
    for path in sorted(root.glob('*.py')):
        yield path


def class_spans(path):
    """[(lineno, end_lineno, name)] for every class, nested ones included."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            end = node.end_lineno or node.lineno
            spans.append((node.lineno, end, node.name))
    return sorted(spans)


def rel(path, root):
    return path.relative_to(root).as_posix()


def scan(root):
    """Return (violations, advisories, errors) as printable line lists."""
    violations, advisories, errors = [], [], []
    for path in iter_sources(root):
        name = rel(path, root)
        if name in EXEMPTIONS:
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f'{name}: unreadable ({exc})')
            continue
        lines = len(text.splitlines())
        if lines > MAX_FILE_LINES:
            violations.append(
                f'{name}: {lines} lines > {MAX_FILE_LINES} '
                f'-- split it (issue #11)')
        elif lines >= ADVISORY_FILE_LINES:
            # a violating file is already reported above; do not double-list it
            advisories.append(f'{name}: {lines} lines (advisory band)')
        try:
            spans = class_spans(path)
        except SyntaxError as exc:
            errors.append(f'{name}: does not parse ({exc})')
            continue
        for lineno, end, cls in spans:
            span = end - lineno + 1
            if span > MAX_CLASS_SPAN:
                violations.append(
                    f'{name}:{lineno}-{end} class {cls} spans {span} lines '
                    f'> {MAX_CLASS_SPAN} -- split it (issue #11)')
    for name in sorted(set(EXEMPTIONS) - {rel(p, root) for p in iter_sources(root)}):
        errors.append(f'{name}: exemption entry points at a missing file '
                      f'-- drop it from EXEMPTIONS')
    return violations, advisories, errors


def run(root):
    violations, advisories, errors = scan(root)
    for line in advisories:
        print(f'ADVISORY {line}')
    for line in violations:
        print(f'FAIL {line}')
    for line in errors:
        print(f'ERROR {line}')
    if not (violations or errors):
        print(f'OK module size within walls '
              f'(file<={MAX_FILE_LINES}, class<={MAX_CLASS_SPAN}, '
              f'{len(EXEMPTIONS)} exemptions pending issue #11 sections)')
        return 0
    return 1


def _make_file(path, lines):
    path.write_text('\n'.join(f'x = {i}' for i in range(lines)) + '\n',
                    encoding='utf-8')


def _make_class(path, span):
    body = '\n'.join(f'    def m{i}(self): pass' for i in range(span - 2))
    path.write_text(f'class Big:\n{body}\n', encoding='utf-8')


def self_test():
    """A/B fixtures in a temp root: over-limit must fail, exemption must save."""
    global EXEMPTIONS
    original = EXEMPTIONS
    EXEMPTIONS = {}  # temp trees have none of the repo's real files
    failures = []
    try:
        failures = _self_test_body()
    finally:
        EXEMPTIONS = original
    if failures:
        for line in failures:
            print(f'SELF-TEST FAIL {line}')
        return 1
    print('SELF-TEST OK module size gate A/B fixtures behaved as designed')
    return 0


def _self_test_body():
    global EXEMPTIONS
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'chatlocal').mkdir()
        big = root / 'chatlocal' / 'big_file.py'
        _make_file(big, MAX_FILE_LINES + 1)
        cls = root / 'chatlocal' / 'big_class.py'
        _make_class(cls, MAX_CLASS_SPAN + 3)
        clean = root / 'chatlocal' / 'clean.py'
        _make_file(clean, 50)

        code = run(root)
        if code != 1:
            failures.append('over-limit tree must fail')
        # both violators must be named, the clean file must not appear
        out = _capture(lambda: run(root))
        if 'big_file.py' not in out or 'big_class.py' not in out:
            failures.append('violations must be named in output')
        if 'clean.py' in out:
            failures.append('clean file must not be reported')
        if 'FAIL' not in out:
            failures.append('violations must be prefixed FAIL')

        EXEMPTIONS = {'chatlocal/big_file.py': 'self-test',
                      'chatlocal/big_class.py': 'self-test'}
        try:
            code = run(root)
            if code != 0:
                failures.append('exempted tree must pass')
        finally:
            EXEMPTIONS = {}

        (root / 'chatlocal' / 'big_file.py').unlink()
        (root / 'chatlocal' / 'big_class.py').unlink()
        code = run(root)
        if code != 0:
            failures.append('clean tree must pass')

        # a stale exemption entry must be an error, not silence
        EXEMPTIONS = {'chatlocal/ghost.py': 'self-test'}
        try:
            code = run(root)
            if code != 1:
                failures.append('stale exemption must fail')
            out = _capture(lambda: run(root))
            if 'ghost.py' not in out:
                failures.append('stale exemption must be named')
        finally:
            EXEMPTIONS = {}
    return failures


def _capture(fn):
    from io import StringIO
    import contextlib
    buf = StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', default=str(ROOT),
                        help='tree to scan (default: repo root)')
    parser.add_argument('--self-test', action='store_true',
                        help='run A/B fixtures in a temporary tree')
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    return run(Path(args.root))


if __name__ == '__main__':
    sys.exit(main())
