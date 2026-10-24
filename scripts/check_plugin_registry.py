"""插件注册表门禁（issue #4 PA / PB）。

七条契约 + 一条防回退，每条都对应一种真实故障：

1. **加载失败必须可见**：坏的插件留 ``load_error``，``--status`` 有一行；旧的
   ``try/except`` 置空表让群里表现为「这个指令没了」。
2. **配置里的插件名必须在注册表**：拼错不能静默忽略（PB 落地 ``plugins`` 段后生效，
   本轮有则校验）。
3. **``#帮助`` 列出的每条指令都必须真能回**：列表与门控同源，杜绝「列了却不回」。
4. **重名注册必须抛错**：插件名与指令名两处，不许静默覆盖。
5. **``plugins/*.py`` 必须静态可达**：全部出现在 ``commands`` 的 AST 闭包里。
   运行时扫目录发现插件会让 ``check_hot_reload`` 的覆盖门禁红，且 ``reload``
   无法把插件改动映射到进程。
6. **旧键与新键归一化等价**（PB）：``slash_commands.<名字>`` 与
   ``plugins.<名字>.options`` 必须产出逐字段相同的 ``settings["plugins"]``，
   否则「迁配置」会悄悄改变行为。
7. **群级覆盖生效**（PB）：``groups.<群号>.plugins.<名字>: false`` 在该群
   落空、且不出现在 ``#帮助`` 里。

issue #4 PD2a 追加（事件型插座）：

8. **事件型插件必须静态注册**：``EVENT_LOADERS`` 里的每一项都在 ``LOADERS``
   里，且 ``plugins.emit``（CLI 路）与 commands 注册表拿到同一个 PLUGIN 对象——
   两条派发路两份状态是会分叉的。
9. **legacy_section 等价**：插件声明历史上的顶层配置段（追问窗口的
   ``"followup"``），归一化结果必须与 ``plugins.<名字>`` 新键逐字段相同，
   且**不进** ``plugins_legacy``（那个列表的 --status 提示写死了
   ``slash_commands.<名字>``，顶层段不是同一种迁移）。

    .venv/Scripts/python.exe scripts/check_plugin_registry.py
"""
import json
import sys
import threading
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
    static = set(settings["commands"] or {}) | set(settings["help_keywords"])
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


def test_plugin_config():
    print("[旧键 ↔ 新键 ↔ 群级覆盖]")
    old = commands.settings_from({
        "slash_commands": {"enabled": True,
                           "miyoushe": {"ck": ["a=b"], "uid": "1", "admin_qq": ["2"],
                                        "qr_login": {"daily_limit": 3}},
                           "sixty": {"enabled": True, "base_url": "http://x",
                                     "allow": ["天气"]}}})
    new = commands.settings_from({
        "slash_commands": {"enabled": True},
        "plugins": {
            "miyoushe": {"enabled": True, "options": {
                "ck": ["a=b"], "uid": "1", "admin_qq": ["2"],
                "qr_login": {"daily_limit": 3}}},
            "sixty": {"enabled": True, "options": {
                "base_url": "http://x", "allow": ["天气"]}}}})
    check("旧键与新键归一化逐字段相等", old["plugins"] == new["plugins"],
          f"{old['plugins']} != {new['plugins']}")
    check("旧键记为 legacy（--status 提示可迁）",
          old["plugins_legacy"] == ["miyoushe", "sixty"], str(old["plugins_legacy"]))
    check("新键不算 legacy", new["plugins_legacy"] == [], str(new["plugins_legacy"]))
    line = commands.PLUGINS.status_line(old)
    check("--status 标注旧键", "旧键 slash_commands.miyoushe" in line, line)

    # 群级覆盖：总开关开着，某个群单独关掉
    scoped = commands.settings_from({
        "slash_commands": {"enabled": True,
                           "sixty": {"enabled": True, "base_url": "http://x"}},
        "groups": {"1": {"enabled": True, "plugins": {"sixty": False}},
                   "2": {"enabled": True}}})
    check("群级关闭：该群 #天气 落空",
          not commands.PLUGINS.available("天气", scoped, "1"))
    check("群级关闭：别的群 #天气 可用",
          commands.PLUGINS.available("天气", scoped, "2"))
    check("群级关闭：#帮助 不列", "天气" not in commands.help_text(scoped, "1"))
    check("群级关闭：#帮助 照列别的群", "天气" in commands.help_text(scoped, "2"))
    check("群级关闭不影响别的插件",
          commands.PLUGINS.available("体力", scoped, "1"))

    # 群白名单插件在 --status 里不能被显示成停用（group_id="" 只判总开关）
    only = commands.settings_from({"plugins": {"sixty": {
        "enabled": True, "groups": ["2"]}}})
    check("--status 不把群白名单插件当停用",
          "sixty 已启用" in commands.PLUGINS.status_line(only),
          commands.PLUGINS.status_line(only))

    # 第三种旧写法（PD2a）：顶层 followup 段逐字段等效，且不进 plugins_legacy
    legacy_followup = commands.settings_from({"followup": {
        "enabled": True, "max_messages": 5, "idle_seconds": 60}})
    new_followup = commands.settings_from({"plugins": {"followup": {
        "enabled": True, "options": {"max_messages": 5, "idle_seconds": 60}}}})
    check("legacy_section 顶层段与新键归一化逐字段相等",
          legacy_followup["plugins"]["followup"] == new_followup["plugins"]["followup"],
          f"{legacy_followup['plugins']['followup']} != "
          f"{new_followup['plugins']['followup']}")
    check("顶层段不进 plugins_legacy", legacy_followup["plugins_legacy"] == [],
          str(legacy_followup["plugins_legacy"]))
    check("顶层段停用也认",
          commands.settings_from({"followup": {"enabled": False}})
          ["plugins"]["followup"]["enabled"] is False)


def test_event_plugins():
    """事件型插座（issue #4 PD2a）：没有 ``#`` 指令的能力怎么注册与派发。"""
    print("[事件型插件]")
    import plugins

    follow = commands.PLUGINS.get("followup")
    check("followup 已注册", follow is not None)
    check("followup 挂 on_event", follow is not None and follow.hooks.on_event is not None)
    check("CLI 路与注册表是同一个 PLUGIN", plugins._load_followup() is follow,
          "两条派发路两份状态是会分叉的")
    check("EVENT_LOADERS 非空", bool(plugins.EVENT_LOADERS))
    for name, loader in plugins.EVENT_LOADERS:
        check(f"EVENT_LOADERS[{name}] 也在 LOADERS", (name, loader) in plugins.LOADERS)
    # 无指令插件不许显示成「停用(0 指令)」——那读起来像坏了。
    # 只钉 followup 段：sixty 那种「有指令但全被门控关掉」显示 0 指令是合理的。
    line = commands.PLUGINS.status_line(commands.settings_from({}))
    segment = next(s for s in line.split(" / ") if s.startswith("followup"))
    check("--status 无指令插件不显示指令计数", "指令" not in segment, segment)
    check("--status 有 followup 开关一行", "followup 已启用" in line, line)
    off = commands.settings_from({"plugins": {"followup": {"enabled": False}}})
    check("--status 停用态一致", "followup 停用" in commands.PLUGINS.status_line(off),
          commands.PLUGINS.status_line(off))


def test_lifecycle():
    print("[生命周期 / 登录指令归属]")
    owner = commands.PLUGINS.owner("登录")
    check("登录指令归属 miyoushe 插件",
          owner is not None and owner.name == "miyoushe", getattr(owner, "name", "None"))
    for key in ("登录", "登录状态", "绑定", "解绑"):
        check(f"#{key} 在 HANDLERS", key in commands.HANDLERS)
    text = commands.help_text(commands.settings_from({}))
    check("#帮助 里四条登录指令都在",
          all(f"#{key}" in text for key in ("登录", "登录状态", "绑定", "解绑")), text[:200])

    # start_all / stop_all 对称：轮询线程起得来、收得回去、重入不重复起
    def pollers():
        return [t for t in threading.enumerate() if t.name == "qr-login-poller"]

    commands.SETTINGS_BOX["settings"] = commands.settings_from({})
    commands.PLUGINS.start_all()
    check("start_all 起轮询线程", len(pollers()) == 1, str(len(pollers())))
    commands.PLUGINS.start_all()
    check("start_all 幂等（不重复起线程）", len(pollers()) == 1, str(len(pollers())))
    commands.PLUGINS.stop_all()
    check("stop_all 收回线程", not pollers(), str(len(pollers())))
    check("--status 有登录轮询一行",
          "登录轮询" in commands.PLUGINS.status_line(commands.SETTINGS_BOX["settings"]))


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


def test_cross_protocol():
    """两套协议拼在一起时不打架（issue #4 PE）。

    daemon 的 ``#`` 指令插件与 app 的工具/事件插件各有宿主，刻意不合并；但
    它们在同一个配置目录、同一个进程树里跑，名字撞了就是灾难：一个 ``#抽卡``
    和同名工具会让模型与群友指同一件事却执行不同代码。
    """
    print("[两套协议]")
    # 门禁自己装路径：CI 直接 `python scripts/xxx.py`，sys.path[0] 是 scripts/，
    # 仓库根不在里面（本地靠 PYTHONPATH 才绿过一轮，是假绿）。
    # append 而非 insert：daemon 目录保持高优先级，仓库根不会影子掉导入。
    if str(ROOT) not in sys.path:
        sys.path.append(str(ROOT))
    from chatlocal import app_plugins  # 叶子包：只 import base + spec，无副作用

    daemon_plugins = commands.PLUGINS.plugins()
    daemon_names = {plugin.name for plugin in daemon_plugins}
    daemon_keys = {command.name for plugin in daemon_plugins for command in plugin.commands}
    app_names = {item["name"] for item in app_plugins.spec.PLUGINS}
    app_tools = set(app_plugins.TOOL_NAMES)
    app_scopes = set(app_plugins.SCOPE_FLAGS)

    check("两套注册表都非空", bool(daemon_names) and bool(app_names),
          f"daemon={sorted(daemon_names)} app={sorted(app_names)}")
    check("插件名不重叠", not (daemon_names & app_names), str(sorted(daemon_names & app_names)))
    check("指令名与工具名不重叠", not (daemon_keys & app_tools), str(sorted(daemon_keys & app_tools)))
    check("app 授权位不占用配置顶层键", not (app_scopes & set(commands.DEFAULTS)),
          str(sorted(app_scopes & set(commands.DEFAULTS))))

    closure, unknown = wg_reload.Planner().closure("app")
    check("app 闭包无动态 import", unknown is False, str(unknown))
    app_dir = ROOT / "chatlocal" / "app_plugins"
    files = sorted(app_dir.glob("*.py"))
    check("app_plugins 目录有文件", len(files) >= 5, str(len(files)))
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        check(f"{rel} 在 app 闭包里", rel in closure, "漂出闭包后改插件不会热重载")

    print(f"  daemon 插件: {' / '.join(sorted(daemon_names))}（{len(daemon_keys)} 条指令）")
    print(f"  app 插件   : {' / '.join(sorted(app_names))}（{len(app_tools)} 个工具）")


def main():
    test_load_errors()
    test_config_names()
    test_help_is_answerable()
    test_plugin_config()
    test_event_plugins()
    test_lifecycle()
    test_duplicate()
    test_static_reachable()
    test_cross_protocol()
    print()
    if FAILED:
        print(f"{len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
