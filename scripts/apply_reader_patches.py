"""Apply `scripts/reader_patches.py` to the vendored third-party trees under `tools/`.

issue #337: `patch_wechat_media` existed for a whole release and nothing ever
called it. The console-window gate scans `VENDORED_SOURCES` when the files are
present, so on a machine that has `tools/` the gate stayed red and the report
looked exactly like "our own runtime code is wrong" -- while the only correct fix
was to patch the third-party copy. The patch and the installer (`install_readers.py`,
which was never committed to this repository) had drifted apart.

This driver is that missing link. It is deliberately dumb:

* it does not download anything, does not decide which upstream version to pin,
  and does not create `tools/` -- a vendored tree is installed by whoever installs
  it, this only adapts what is already there;
* it reads the patch table from `reader_patches.VENDORED_PATCHES`, so a patch and
  its target file cannot drift apart again;
* every patch in that table is idempotent by contract, so running this twice is
  safe and reports `已是补丁态` the second time;
* a drifted anchor raises `ValueError` and the file is left byte-for-byte
  untouched -- guessing at a shifted anchor is how an adaptation turns into
  corruption;
* before writing, the original is copied to `<file>.tulpa-bak` (only if that
  backup does not already exist, so a re-run never overwrites the pristine copy)
  and the result is `ast.parse`d; a syntactically broken result rolls back.

Absent files are *not* failures: `tools/` is gitignored, so CI, `check_pr_parity.py`
and any fresh clone have none of it. Same three-state thinking as the gate
(`doc/CONSOLE_WINDOW_GATE.md` §3): present-and-patched, present-and-unpatched,
absent-because-not-installed -- and the tool says which one it is looking at.

    uv run python scripts/apply_reader_patches.py            # apply, if tools/ is here
    uv run python scripts/apply_reader_patches.py --check    # report only, non-zero if any needs a patch
"""
import argparse
import ast
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from reader_patches import VENDORED_PATCHES  # noqa: E402

BACKUP_SUFFIX = '.tulpa-bak'


def _describe(rel, reason):
    return f'{rel}  —— {reason}'


def inspect(path, patch):
    """('absent'|'patched'|'needs-patch', bytes or None) for one table row."""
    if not path.is_file():
        return 'absent', None
    data = path.read_bytes()
    try:
        if patch(data) == data:
            return 'patched', data
    except ValueError:
        # A raised patch means the anchor drifted. The file is still patch-able in
        # the sense that it is not in the patched state, and the exact reason comes
        # from the patch itself when we actually try to apply it.
        return 'needs-patch', data
    return 'needs-patch', data


def apply_one(path, patch, rel):
    """Write the patched bytes. Returns a short human status line."""
    original = path.read_bytes()
    try:
        patched = patch(original)
    except ValueError as exc:
        raise ValueError(f'{rel}: 补丁锚点漂移，文件保持原样未改（{exc}）；'
                         '请对照上游 diff 人工复核后再更新补丁') from None
    if patched == original:
        return '已是补丁态'
    try:
        ast.parse(patched.decode('utf-8'))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise ValueError(f'{rel}: 打完补丁后无法解析（{exc}），已放弃写入，文件保持原样') from None
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_bytes(patched)
    return f'已打补丁（原文件备份到 {backup.name}）'


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='把 scripts/reader_patches.py 的补丁应用到 tools/ 下的 vendored 副本（issue #337）')
    parser.add_argument('--check', action='store_true',
                        help='只报告不写入；有文件需要补丁时退出码为 1')
    parser.add_argument('--root', default=str(ROOT),
                        help='仓库根（默认本仓；测试用临时目录时传入）')
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    needs = []
    problems = []
    applied = []
    for row in VENDORED_PATCHES:
        rel = row['path']
        path = root / rel
        state, _ = inspect(path, row['patch'])
        if state == 'absent':
            print(f'· 跳过：{_describe(rel, "本机没有这份第三方源码（tools/ 受 .gitignore 保护），不是失败")}')
            continue
        if args.check:
            print(f'{"✓" if state == "patched" else "✗"} {_describe(rel, row["reason"])}'
                  f' —— {"已是补丁态" if state == "patched" else "需要补丁"}')
            if state != 'patched':
                needs.append(rel)
            continue
        try:
            status = apply_one(path, row['patch'], rel)
        except ValueError as exc:
            problems.append(str(exc))
            print(f'✗ {rel}  —— {exc}')
            continue
        print(f'✓ {_describe(rel, row["reason"])} —— {status}')
        applied.append(rel)

    if problems:
        print(f'\nFAIL: {len(problems)} 个文件补丁失败')
        return 1
    if args.check and needs:
        print(f'\n需要补丁：{len(needs)} 个文件；跑 `python scripts/apply_reader_patches.py` 应用（不要把它们移出门禁扫描范围）')
        return 1
    if args.check:
        print('\nPASS: tools/ 下的 vendored 副本都已是补丁态（或本机没有安装）。')
        return 0
    if applied:
        print(f'\n已应用 {len(applied)} 个补丁。')
    else:
        print('\n无事可做：本机没有需要补丁的 vendored 副本。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
