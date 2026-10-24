"""钉住唤醒内容不会被 listen.py 吞掉（issue #91）。

`listen.py` 是水群值守唯一的唤醒通道，它用 `CREATE_NO_WINDOW` 拉 `watch.py`。
父进程没有控制台时（Agent 后台拉起，正是常态），带这个 flag 的子进程 stdout
指向它自己新开的控制台，而不是父进程的管道 —— 唤醒行、追问行、`TIMEOUT`
全部落进那个没人看的控制台，Agent 只收到 `listen.py` 事后补打的一句 `WAKE`。
issue #91 里连续 4 次「只有 WAKE、没有内容」就是这么来的。

这条门禁跑一次真实的 `listen.py`，用临时 `WATER_GROUP_HOME`（不碰实机状态）：

    listen.py --cycles 1 --timeout 1 --no-digest

这一轮必然先超时一次，所以 stdout 必须同时含子进程的 `TIMEOUT` 和本进程的
`NO_WORK`，且 `TIMEOUT` 在前（顺序丢了同样说明透传是假的）。在「继承 stdout」
的旧写法上 `TIMEOUT` 是空的 —— 这是这条门禁存在的唯一理由。

issue #51 之后 `watch.py` 启动时要 `self_account`（没有就拒绝启动），而
config.json 被 gitignore、CI 的干净 checkout 里没有它：门禁因此写一份临时配置
并用 `IDLEJOT_WG_CONFIG` 指过去（`run_passthrough` 不传 `env=`，listen 的子进程
原样继承），否则会红在错误的病因上。

第二条更小：`WAKE` 只能由 `watch.py` 打。透传修好之后 `listen.py` 再补一句
`print("WAKE")` 就会出现两行，读日志的人会以为有两次唤醒（issue #91 第 5 点）。

    .venv/Scripts/python.exe scripts/check_listen_passthrough.py
"""
import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LISTEN = ROOT / "water-group" / "daemon" / "listen.py"

# 外部测试变量会把 state 又指回实机，这里必须清掉，只留临时数据根。
ISOLATING_VARS = ("IDLEJOT_WG_STATE", "TULPA_WG_STATE", "IDLEJOT_WG_DB",
                  "IDLEJOT_WG_MEMORY_DB")


def write_fixture_config(tmp):
    """issue #51 后 watch.py 启动就要 self_account，没有就拒绝启动（exit 1）。

    config.json 被 gitignore，CI 的干净 checkout 里没有它——不给 watch 一份
    最小身份的话，这条门禁会红在「监听拒绝启动」上，而它测的是透传。配置写到
    临时目录、用 `IDLEJOT_WG_CONFIG` 指过去：不碰仓库树（崩在中间不会给开发机
    留占位配置），也不读实机配置（门禁不许依赖机器状态）。
    """
    config = Path(tmp) / "config.json"
    config.write_text(json.dumps({"self_account": "10001",
                                  "onebot_token": "listen-fixture"},
                                 ensure_ascii=False), encoding="utf-8")
    return config


def run_listen(home, config):
    env = dict(os.environ)
    env["WATER_GROUP_HOME"] = str(home)
    # run_passthrough 不传 env=，listen 的 watch 子进程原样继承——覆盖对
    # 孙子的 watch.py 同样生效。
    env["IDLEJOT_WG_CONFIG"] = str(config)
    env["PYTHONUTF8"] = "1"
    for name in ISOLATING_VARS:
        env.pop(name, None)
    return subprocess.run([sys.executable, str(LISTEN), "--cycles", "1",
                           "--timeout", "1", "--no-digest"],
                          cwd=str(ROOT), env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=180)


def check_single_wake():
    """`WAKE` 只能来自 watch.py：listen.py 再补一遍就是两行，像两次唤醒。"""
    tree = ast.parse(LISTEN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "print" and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "WAKE"):
            return [f"listen.py:{node.lineno} 自己打印了 WAKE：透传后 watch.py 的块里"
                    "已经有一行，重复会让日志读起来像两次唤醒（issue #91）"]
    return []


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="wg-listen-") as home:
        config = write_fixture_config(home)
        result = run_listen(home, config)
    out = result.stdout or ""
    failures = []

    if result.returncode != 0:
        failures.append(f"listen.py 退出码 {result.returncode}（期望 0）")
    if "NO_WORK" not in out:
        failures.append("listen.py 自己没有打印 NO_WORK（--cycles 1 --no-digest 的收尾）")
    if "TIMEOUT" not in out:
        failures.append("watch.py 的 TIMEOUT 没有到达调用方：子进程输出又被吞了"
                        "（issue #91 复发，检查 listen.run 是否退回了 run_hidden）")
    elif "NO_WORK" in out and out.index("TIMEOUT") > out.index("NO_WORK"):
        failures.append("TIMEOUT 排在 NO_WORK 之后：子进程输出没有按顺序透传")
    failures.extend(check_single_wake())

    if failures:
        print("FAIL: listen.py 不透传子进程输出\n")
        for line in failures:
            print(f"  {line}")
        print("\n实际 stdout：")
        print(out.rstrip() or "  （空）")
        print("\n修法：listen.run → winconsole.run_passthrough，"
              "不要用 run_hidden 继承 stdout。")
        return 1

    print("✓ listen.py 把子进程的 TIMEOUT 原样传给了调用方")
    print("✓ listen.py 自己的 NO_WORK 仍在，顺序未乱")
    print("✓ WAKE 只由 watch.py 打，listen.py 没有重复")
    print("\nPASS: 唤醒内容不再被吞。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
