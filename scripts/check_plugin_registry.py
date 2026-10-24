"""插件注册表门禁（issue #4 PA）。

四条契约 + 一条防回退，每条都对应一种真实故障：

1. **加载失败必须可见**：坏的插件留 ``load_error``，``--status`` 有一行；旧的
   ``try/except`` 置空表让群里表现为「这个指令没了」。
2. **配置里的插件名必须在注册表**：拼错不能静默忽略（PB 落地 ``plugins`` 段后生效，
   本轮有则校验）。
3. **``#帮助`` 列出的每条指令都必须真能回**：列表与门控同源，杜绝「列了却不回」。
4. **重名注册必须抛错**：插件名与指令名两处，不许静默覆盖。
5. **``plugins/*.py`` 必须静态可达**：全部出现在 ``commands`` 的 AST 闭包里。
   运行时扫目录发现插件会让 ``check_hot_reload`` 的覆盖门禁红，且 ``reload``
   无法把插件改动映射到进程。

    .venv/Scripts/python.exe scripts/check_plugin_registry.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

import commands  # noqa: E402
import reload as wg_reload  # noqa: E402
import wg_config  # noqa: E402
from plugins.base import Command, Plugin, PluginError  # noqa: E402
from plugins.registry import Registry  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def _fake(name="fake", keys=("echo",)):
    return Plugin(
        name=name,
        commands=tuple(Command(key, f"{key} 描述", lambda args, ctx: "ok") for key in keys),
    )


def test_load_errors():
    print("[加载失败可见]")
    registry = commands.PLUGINS
    check("内置插件全部加载成功", registry.load_errors == {}, str(registry.load_errors))
    check("注册表至少有两个插件", len(registry.plugins()) >= 2,
          str([p.name for p in registry.plugins()]))
    for plugin in registry.plugins():
        check(f"{plugin.name} 有指令或钩子", bool(plugin.commands) or bool(plugin.hooks),
              plugin.name)
    # 失败要带原因，且 status_line 里有一行
    probe = Registry()
    probe.note_error("broken", ImportError("no module named broken"))
    line = probe.status_line({})
    check("load_error 带类型与原因", "ImportError: no module named broken" in line, line)
    check("status_line 标出加载失败", "broken 加载失败" in line, line)


def test_config_names():
    print("[配置 ↔ 注册表]")
    known = {plugin.name for plugin in commands.PLUGINS.plugins()}
    paths = [wg_config.CONFIG, ROOT / "water-group" / "config.example.json"]
    for path in paths:
        if not path.is_file():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        section = raw.get("plugins") or {}
        unknown = sorted(set(section) - known)
        check(f"{path.name} 里的插件名都在注册表", not unknown, str(unknown))
        for name, cfg in section.items():
            if name not in known:
                continue
            options = (cfg or {}).get("options")
            check(f"{path.name} 的 {name}.options 是对象",
                  options is None or isinstance(options, dict), str(type(options)))


def test_help_is_answerable():
    print("[#帮助 与门控同源]")
    cfg = json.loads((ROOT / "water-group" / "config.example.json").read_text(encoding="utf-8"))
    settings = commands.settings_from(cfg)
    group_id = next(iter(settings["groups"] or [""]), "")
    text = commands.help_text(settings, group_id)
    listed = []
    for line in text.splitlines():
        if not line.startswith(settings["prefix"]):
            continue
        key = line[len(settings["prefix"]):].split(" ", 1)[0].split("—", 1)[0].strip()
        if key:
            listed.append(key)
    check("#帮助 有内容", bool(listed), text[:120])
    static = set(settings["commands"] or {}) | set(commands.LOGIN_DESCS) | set(settings["help_keywords"])
    for key in listed:
        if key in static:
            continue
        if key not in commands.HANDLERS:
            check(f"#{key} 有出处", False, "帮助列了但既非静态、也非 handler")
            continue
        check(f"#{key} 门控为开", commands.PLUGINS.available(key, settings, group_id))
    # 反向：被门控关掉的指令不得出现在帮助里
    closed = [key for key in commands.HANDLERS
              if not commands.PLUGINS.available(key, settings, group_id)]
    check("门控关掉的指令不在帮助里", not (set(closed) & set(listed)), str(set(closed) & set(listed)))


def test_duplicate():
    print("[重名 fail-fast]")
    registry = Registry()
    registry.register(_fake("alpha", ("echo",)))
    try:
        registry.register(_fake("alpha", ("other",)))
    except PluginError as exc:
        check("插件重名抛 PluginError", "插件重名" in str(exc), str(exc))
    else:
        check("插件重名抛 PluginError", False, "没有抛错")
    try:
        registry.register(_fake("beta", ("echo",)))
    except PluginError as exc:
        check("指令重名抛 PluginError", "指令重名" in str(exc), str(exc))
    else:
        check("指令重名抛 PluginError", False, "没有抛错")
    # 半注册检查：冲突失败后 beta 不得留在表里
    check("冲突失败不留半注册", registry.get("beta") is None)
    try:
        registry.register(Plugin(name="gamma", api=999))
    except PluginError as exc:
        check("api 版本不匹配抛错", "api=999" in str(exc), str(exc))
    else:
        check("api 版本不匹配抛错", False, "没有抛错")
    # 能力缺失：插件声明宿主没有的能力
    try:
        registry.register(Plugin(name="delta", requires=frozenset({"tools"})),
                          capabilities={"commands"})
    except PluginError as exc:
        check("能力缺失抛错", "delta" in str(exc) or "能力" in str(exc), str(exc))
    else:
        check("能力缺失抛错", False, "没有抛错")


def test_static_reachable():
    print("[静态可达]")
    closure, unknown = wg_reload.Planner().closure("commands")
    check("commands 闭包无动态 import", unknown is False, str(unknown))
    plugin_dir = ROOT / "water-group" / "daemon" / "plugins"
    files = sorted(plugin_dir.glob("*.py"))
    check("plugins 目录有文件", len(files) >= 5, str(len(files)))
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        check(f"{rel} 在 commands 闭包里", rel in closure, "运行时扫目录会让 check_hot_reload 红")


def main():
    test_load_errors()
    test_config_names()
    test_help_is_answerable()
    test_duplicate()
    test_static_reachable()
    print()
    if FAILED:
        print(f"{len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
