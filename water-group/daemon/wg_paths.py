"""本地数据根目录解析——全仓单一事实来源。

运行时数据全部住在 `~/.water-group/`，仓库里只留代码与 gitignore 的配置：

    ~/.water-group/
      state/    守护状态（wg_*.json、游标、锁、心跳、mcp token、status.json、
                监听存亡账本 wg_listeners/<pid>.json，issue #112/#119；
                旧版单行 wg_listener.json 仍只读兼容）
      logs/     一切日志（supervisor、collector、commands、napcat、launcher…）
      data/     chatlocal SQLite 库与媒体缓存
      tmp/      临时目录（Gradio TEMP、agent patch、voice 抽取、terminal 锁）
      users/    每用户扫码绑定凭证（未来 #登录，见 .plan/user-ck-qr-login.md）
      rules/    私有化 Agent 规则（water_group.md：群号/QQ/花名/地域/查数据源速查）
      personas/ 私有化人格卡（部署者自建，不入库；仓库 personas/ 只留公开模板）

覆盖方式：
  WATER_GROUP_HOME     整个数据根挪走（测试/备份用）
  IDLEJOT_WG_STATE     仅 state 子目录（历史变量，测试隔离与未来多群继续有效）
  TULPA_WG_STATE       同上，旧名

为什么各 daemon 脚本不 import 一个包级模块：它们都以
`python water-group/daemon/xxx.py` 直接跑，sys.path[0] 是 daemon 目录自身，
所以同目录 flat import（`from wg_paths import state_dir`）即可，不需要包结构。
"""
import os
from pathlib import Path


def wg_home() -> Path:
    """数据根。默认 ~/.water-group，WATER_GROUP_HOME 可整个覆盖。"""
    return Path(os.environ.get("WATER_GROUP_HOME") or (Path.home() / ".water-group"))


def state_dir() -> Path:
    """守护状态目录。IDLEJOT_WG_STATE / TULPA_WG_STATE 优先（测试与多群隔离）。"""
    override = os.environ.get("IDLEJOT_WG_STATE") or os.environ.get("TULPA_WG_STATE")
    return Path(override) if override else wg_home() / "state"


def logs_dir() -> Path:
    return wg_home() / "logs"


def data_dir() -> Path:
    return wg_home() / "data"


def tmp_dir() -> Path:
    return wg_home() / "tmp"


def rules_dir() -> Path:
    """私有化 Agent 规则（不随仓库发布）。"""
    return wg_home() / "rules"


def personas_dir() -> Path:
    """私有化人格卡（不随仓库发布）。"""
    return wg_home() / "personas"


def users_dir() -> Path:
    """每用户扫码凭证（.plan/user-ck-qr-login.md）。"""
    return wg_home() / "users"
