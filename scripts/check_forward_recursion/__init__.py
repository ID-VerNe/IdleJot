"""issue #351 用例包：合并转发递归展开。

对 `check_mcp_chat_enrichment.py` 只暴露一个 `run(...)`——夹具与 `text()` 都由
主门禁注入，所以「夹具只有一份」这条不因拆包而破。包本身不导入任何 chatlocal，
被导入时零副作用。
"""

from .cases import run

__all__ = ["run"]
