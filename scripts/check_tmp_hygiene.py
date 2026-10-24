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

`--self-test` 用两份临时数据根做 A/B 对照（一组故意违规、一组干净），四种断言都必须
在该命中时命中、该沉默时沉默；CI 上没有私有目录，靠它证明门禁没瞎。
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HOME = Path(os.environ.get("WATER_GROUP_HOME") or (Path.home() / ".water-group"))
SCRIPT_SUFFIXES = {".py", ".md", ".ps1"}
SNAPSHOT = "wg_archive.json"


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


def verdict(report):
    """(ok, lines) — E1/E2 fail, E3 warns, E4 reports."""
    ok = True
    lines = []
    if report["stray"]:
        ok = False
        lines.append(f"E1 FAIL tmp/ 顶层有脚本：{report['stray']}"
                     "（TEMP 随时会被清，脚本请放 bin/ 或 archive/）")
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
    root = Path(tempfile.mkdtemp(prefix="wg-hygiene-selftest-"))
    try:
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

        ok_clean, lines_clean = verdict(inspect(clean))
        assert ok_clean, f"clean root must pass：\n" + "\n".join(lines_clean)
        joined_clean = "\n".join(lines_clean)
        assert "E1 ok" in joined_clean and "E2 ok" in joined_clean, joined_clean
        assert "E3 ok" in joined_clean, joined_clean
        print("✓ 干净样本：四条断言全部沉默")

        # E4：删掉归档后要看得出来
        (clean / "archive" / "scratch" / "2026-01").rename(clean / "archive" / "scratch" / "gone")
        for path in (clean / "archive" / "scratch" / "gone").glob("*"):
            path.unlink()
        shutil.rmtree(clean / "archive" / "scratch" / "gone")
        ok_after, lines_after = verdict(inspect(clean))
        assert "E4 info" in "\n".join(lines_after), "\n".join(lines_after)
        assert ok_after, "E4 只报告，不影响退出码"
        print("✓ 归档缩水：E4 报告但不拦")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("=" * 72)
    print("✓ 巡检门禁自检 3 组通过")
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
        print("\nFAIL: 私有目录巡检未通过（把脚本挪到 bin/ 或 archive/，见 bin/INDEX.md）")
        return 1
    print("\nPASS: 私有目录巡检通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
