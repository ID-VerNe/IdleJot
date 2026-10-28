"""私有数据目录的巡检门禁（issue #6 分节 PD）。

`~/.water-group/tmp/` 是 `TEMP` 的重定向目标（`chatlocal/config.py:22-23`），语义就是
「随时可以整目录删掉」。把脚本放进去，等于把归档放在垃圾桶的隔层里：既没有版本，
也没有保护，还会越攒越多。四条断言把它钉住：

  E1  `tmp/` 顶层不得出现 `.py` / `.md` / `.ps1`           硬失败
  E2  `bin/` 与 `archive/` 的每个 `.py` 在对应 INDEX 里有登记  硬失败
  E3  `drafts/` 非空                                      警告
  E4  `archive/` 条目数不少于上次快照                       信息

E1 是核心：只允许机器产物进 `tmp/`。E4 只报告不拦——「归档被人顺手清了」这件事
用计数能看出来，用退出码表达不出来。

红的成因是「放错家」，所以红的出口必须同屏给出（issue #147）：`.md` 草稿的家是
`drafts/`（未发，发完即删）或 `archive/drafts-settled-<YYYY-MM>/`（已发），脚本的家
是 `bin/`（复用）或 `archive/scratch/<YYYY-MM>/`（一次性）——**搬完必须在对应
INDEX.md 补一行**，否则 E2 立刻接棒。只写「挪到 bin/ 或 archive/」时，`.md` 无处
可去、登记格式没人说，红就一直红着（issue #147 的现场：35 项攒了三周）。

`--self-test` 用两份临时数据根做 A/B 对照（一组故意违规、一组干净），四种断言都必须
在该命中时命中、该沉默时沉默；销账段同理——只有红的时候才出现。CI 上没有私有目录，
靠它证明门禁没瞎。
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from check_fixture_support import temp_dir  # noqa: E402

HOME = Path(os.environ.get("WATER_GROUP_HOME") or (Path.home() / ".water-group"))
# E1 的判据按「放错家」分两类：草稿的家是 drafts/ 或 archive/drafts-*/，脚本的家是
# bin/ 或 archive/scratch/*。合起来就是不许出现在 tmp/ 顶层的那三种后缀。
DOC_SUFFIXES = {".md"}
CODE_SUFFIXES = {".py", ".ps1"}
SCRIPT_SUFFIXES = DOC_SUFFIXES | CODE_SUFFIXES
SNAPSHOT = "wg_archive.json"
# 销账登记行：`bin/INDEX.md` 与 `archive/INDEX.md` 的表格列不同（后者是四列
# 「归档日期 / 原名 / 用途 / 结局」），这里给的是 archive 那份——bin 那份多一列
# 「调用示例」，照着既有行补即可，不必在此复述两套表头。
INDEX_ROW = "| <日期> | `<文件名>` | <用途一行> | <结局> |"


def read_index(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def python_scripts(folder):
    if not folder.exists():
        return []
    return sorted(p for p in folder.rglob("*.py") if p.is_file())


def unchecked_scripts(home):
    """Scripts that exist under bin/ or archive/ without an INDEX line (E2)."""
    missing = []
    for folder, index in ((home / "bin", home / "bin" / "INDEX.md"),
                          (home / "archive", home / "archive" / "INDEX.md")):
        text = read_index(index)
        for script in python_scripts(folder):
            if script.name not in text:
                missing.append(str(script))
    return missing


def stray_scripts(home):
    """E1: anything script-shaped sitting at the top of tmp/."""
    tmp = home / "tmp"
    if not tmp.exists():
        return []
    return sorted(p.name for p in tmp.iterdir()
                  if p.is_file() and p.suffix.lower() in SCRIPT_SUFFIXES)


def drafts(home):
    folder = home / "drafts"
    if not folder.exists():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_file())


def archive_entries(home):
    """Every archived file, recursively — the point is that nothing disappears."""
    folder = home / "archive"
    if not folder.exists():
        return []
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())


def inspect(home):
    """Return a report dict; never writes except the E4 snapshot."""
    report = {
        "home": str(home),
        "stray": stray_scripts(home),
        "unchecked": unchecked_scripts(home),
        "drafts": drafts(home),
        "archive_before": None,
        "archive_now": len(archive_entries(home)),
    }
    state = home / "state"
    snapshot = state / SNAPSHOT
    try:
        report["archive_before"] = int(json.loads(snapshot.read_text(encoding="utf-8"))["count"])
    except (OSError, ValueError, KeyError, TypeError):
        report["archive_before"] = None
    if state.exists() or report["archive_now"]:
        state.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(json.dumps({"count": report["archive_now"]}, ensure_ascii=False),
                            encoding="utf-8")
    return report


def settle_lines(report):
    """销账指引：红的出口（issue #147）。只在真的红时出现。

    文案由现场推导（月份取当下、条数取实际、按后缀分组），不是写死的模板——
    门禁数得出来几个 `.md`、几个脚本，就没理由让人自己去数。

    命令是**可整段粘贴**的（issue #147 修复候选 c 的原话），所以走通配整批搬，
    不用逐文件占位符。代价是「每个文件去哪」这个判断被压成一行注释：草稿里
    **还没发**的要先挑进 `drafts/`，剩下的整批归档；脚本默认一次性进 archive，
    要复用的自己改投 `bin/`。两种落点都是「只增不删」，搬错方向不丢数据。

    数据根写成 `${WATER_GROUP_HOME:-~/.water-group}`：既跟着真环境走，又不在
    输出里印本机绝对路径（那些路径会被贴进 issue/PR，撞 §5.3）。
    """
    stray = report["stray"]
    by_suffix = {}
    for name in stray:
        by_suffix.setdefault(Path(name).suffix.lower(), []).append(name)
    docs = sum(len(by_suffix.get(suffix, [])) for suffix in DOC_SUFFIXES)
    code = sum(len(by_suffix.get(suffix, [])) for suffix in CODE_SUFFIXES)
    code_globs = [f'"$W"/tmp/*{suffix}' for suffix in sorted(CODE_SUFFIXES)
                  if by_suffix.get(suffix)]
    month = datetime.now().strftime("%Y-%m")
    drafts_dir = f"archive/drafts-settled-{month}"
    scratch_dir = f"archive/scratch/{month}"
    lines = ["", "怎么销账（改完再跑一次本门禁；私有根不进仓库）："]
    if docs:
        lines.append(f"  .md 草稿 {docs} 个：还没发的先进 drafts/（发完即删），"
                     f"已经发过的整批进 {drafts_dir}/")
    if code:
        lines.append(f"  脚本 {code} 个：默认按一次性进 {scratch_dir}/；"
                     "要复用的改成进 bin/（bin 也要登记）")
    if docs or code:
        lines.append('      W="${WATER_GROUP_HOME:-~/.water-group}"')
        if docs:
            lines.append(f'      ls "$W"/tmp/*.md   # 逐份看，还没发的先 mv 进 "$W"/drafts/')
            lines.append(f'      mkdir -p "$W/{drafts_dir}" && '
                         f'mv "$W"/tmp/*.md "$W/{drafts_dir}/"')
        if code:
            lines.append(f'      mkdir -p "$W/{scratch_dir}" && '
                         f'mv {" ".join(code_globs)} "$W/{scratch_dir}/"')
    lines.append("  搬完在对应 INDEX.md 补一行（每个文件一行；archive 四列，bin 多一列「调用示例」）：")
    lines.append(f"      {INDEX_ROW}")
    if report["unchecked"]:
        lines.append("  E2 那几条是「已归档但漏登记」：只补 INDEX 行，文件不用再动。")
    return lines


def verdict(report):
    """(ok, lines) — E1/E2 fail, E3 warns, E4 reports."""
    ok = True
    lines = []
    if report["stray"]:
        ok = False
        lines.append(f"E1 FAIL tmp/ 顶层有脚本：{report['stray']}"
                     "（TEMP 随时会被清，去向见下）")
    else:
        lines.append("E1 ok   tmp/ 顶层只有机器产物")
    if report["unchecked"]:
        ok = False
        lines.append(f"E2 FAIL 未登记（对应 INDEX.md 无记录）：{report['unchecked']}")
    else:
        lines.append("E2 ok   bin/ 与 archive/ 的脚本都在索引里")
    if report["drafts"]:
        lines.append(f"E3 warn drafts/ 非空（发完该删）：{report['drafts']}")
    else:
        lines.append("E3 ok   drafts/ 已清空")
    before = report["archive_before"]
    if before is not None and report["archive_now"] < before:
        lines.append(f"E4 info archive/ 条目从 {before} 降到 {report['archive_now']}，"
                     "有人删过归档？")
    else:
        lines.append(f"E4 ok   archive/ 条目 {report['archive_now']}"
                     + (f"（上次 {before}）" if before is not None else "（首次快照）"))
    if not ok:
        lines.extend(settle_lines(report))
    return ok, lines


def seed(root, *, dirty):
    """Build a fake data root. dirty=True plants one violation per assertion."""
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    (root / "drafts").mkdir(parents=True, exist_ok=True)
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "archive" / "scratch" / "2026-01").mkdir(parents=True, exist_ok=True)
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "tmp" / "gradio").mkdir(exist_ok=True)          # 机器产物，允许
    (root / "tmp" / "blob.bin").write_bytes(b"\x00\x01")    # 机器产物，允许
    if dirty:
        (root / "tmp" / "leftover.py").write_text("print(1)\n", encoding="utf-8")
        (root / "tmp" / "issue-lost.md").write_text("## 现象\n", encoding="utf-8")
        (root / "bin" / "unlisted.py").write_text("print(1)\n", encoding="utf-8")
        (root / "drafts" / "issue-forgot.md").write_text("TITLE: x\n", encoding="utf-8")
        (root / "bin" / "INDEX.md").write_text("| 文件 |\n|---|\n", encoding="utf-8")
        (root / "archive" / "INDEX.md").write_text("| 文件 |\n|---|\n", encoding="utf-8")
    else:
        (root / "bin" / "listed.py").write_text("print(1)\n", encoding="utf-8")
        (root / "bin" / "INDEX.md").write_text("| `listed.py` |\n", encoding="utf-8")
        (root / "archive" / "scratch" / "2026-01" / "old.py").write_text("print(1)\n", encoding="utf-8")
        (root / "archive" / "INDEX.md").write_text("| `old.py` |\n", encoding="utf-8")


def self_test():
    """A/B controls: every assertion must fire when violated and stay quiet otherwise."""
    with temp_dir(prefix="wg-hygiene-selftest-") as root:
        dirty = root / "dirty"
        clean = root / "clean"
        seed(dirty, dirty=True)
        seed(clean, dirty=False)

        ok_dirty, lines_dirty = verdict(inspect(dirty))
        assert not ok_dirty, "dirty root must fail"
        joined = "\n".join(lines_dirty)
        assert "E1 FAIL" in joined and "leftover.py" in joined, joined
        assert "E2 FAIL" in joined and "unlisted.py" in joined, joined
        assert "E3 warn" in joined and "issue-forgot.md" in joined, joined
        print("✓ 违规样本：E1/E2 硬失败，E3 警告")

        # 销账指引（issue #147）：红的时候必须同屏给出「去哪、怎么搬、登记成什么样」。
        # 这三条是出口的全部内容，退化成一句「挪到 bin/ 或 archive/」就红。
        assert "怎么销账" in joined, joined
        assert "drafts/" in joined and "archive/scratch/" in joined, joined
        assert "| <日期> | `<文件名>` |" in joined and "<结局>" in joined, joined
        print("✓ 违规样本：销账指引含草稿去处、脚本去处与 INDEX 行格式")

        ok_clean, lines_clean = verdict(inspect(clean))
        assert ok_clean, f"clean root must pass：\n" + "\n".join(lines_clean)
        joined_clean = "\n".join(lines_clean)
        assert "E1 ok" in joined_clean and "E2 ok" in joined_clean, joined_clean
        assert "E3 ok" in joined_clean, joined_clean
        # 绿的时候不许留销账段：那不是「结论」，是噪音（每跑一次都要读一遍）。
        assert "怎么销账" not in joined_clean, joined_clean
        print("✓ 干净样本：四条断言全部沉默，销账指引不出现")

        # E4：删掉归档后要看得出来
        (clean / "archive" / "scratch" / "2026-01").rename(clean / "archive" / "scratch" / "gone")
        for path in (clean / "archive" / "scratch" / "gone").glob("*"):
            path.unlink()
        shutil.rmtree(clean / "archive" / "scratch" / "gone")
        ok_after, lines_after = verdict(inspect(clean))
        assert "E4 info" in "\n".join(lines_after), "\n".join(lines_after)
        assert ok_after, "E4 只报告，不影响退出码"
        print("✓ 归档缩水：E4 报告但不拦")
    print("=" * 72)
    print("✓ 巡检门禁自检 4 组通过")
    print("=" * 72)


def main():
    parser = argparse.ArgumentParser(description="私有数据目录巡检（issue #6 PD）")
    parser.add_argument("--home", help="数据根，缺省 ~/.water-group 或 WATER_GROUP_HOME")
    parser.add_argument("--self-test", action="store_true", help="A/B 对照自检（CI 跑这个）")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    home = Path(args.home) if args.home else HOME
    if not home.exists():
        # CI 与全新机器上没有私有数据根：至少把断言本身跑一遍，别让这条门禁
        # 变成永远绿的空壳。
        print(f"note: {home} 不存在，只跑自检（A/B 对照）")
        self_test()
        return 0
    report = inspect(home)
    ok, lines = verdict(report)
    for line in lines:
        print(line)
    if not ok:
        print("\nFAIL: 私有目录巡检未通过（照上面的销账段搬，搬完再跑一次）")
        return 1
    print("\nPASS: 私有目录巡检通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
