import os
import time
import requests

# bootstrap_tulpa.py 是 bootstrap.py 的私有拆分（issue #277 R1）：Tulpa MCP
# HTTP JSON-RPC 健康探针与就绪屏障。依赖 is_port_open + get_python_exe +
# start_process_detached + ROOT_DIR。这些工具留在 bootstrap.py，本模块函数体内经
# `_shared()` 延迟取——避免模块顶层 `from water_group import bootstrap` 造成循环
# 导入与脚本态双装载。ROOT_DIR 是常量，从 DAG 叶子 bootstrap_win32 取（无循环风险）。
from water_group.bootstrap_win32 import ROOT_DIR


def _shared():
    """延迟取 bootstrap.py 模块对象；调用时已完全装载。"""
    from water_group import bootstrap
    return bootstrap


def check_tulpa_mcp_health(port: int = 48777, host: str = "127.0.0.1", token: str = None, timeout: float = 1.5) -> tuple[bool, str]:
    """Probes the Tulpa MCP HTTP endpoint with a JSON-RPC POST request.

    Verifies that the ASGI application on port 48777 is not only listening on TCP
    but is actively processing JSON-RPC MCP requests (returns 200 on authorized ping
    or 401 on protocol endpoint verification). Bypasses system proxies for clean loopback probing.
    """
    url = f"http://{host}:{port}/mcp"
    headers = {
        "Host": f"{host}:{port}",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {
        "jsonrpc": "2.0",
        "id": "healthcheck",
        "method": "ping",
    }
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=timeout, proxies={"http": None, "https": None})
        # 200 means full JSON-RPC response; 401 means MCP endpoint is active and guarded
        if resp.status_code in (200, 401):
            return True, f"HTTP POST 握手成功 (状态码: {resp.status_code})"
        return False, f"HTTP 返回异常状态码: {resp.status_code}"
    except Exception as e:
        return False, f"HTTP POST 握手失败: {e}"

def wait_tulpa_mcp_ready(port: int = 48777, host: str = "127.0.0.1", token: str = None, timeout_seconds: float = 15.0) -> tuple[bool, str]:
    """Wait barrier ensuring Tulpa MCP HTTP POST JSON-RPC endpoint is fully ready.

    Prevents race conditions where an Agent invokes MCP right after TCP socket opens
    but before ASGI middleware finishes initialization, causing one-way client deadlock.
    """
    start_time = time.time()
    last_err = ""
    while time.time() - start_time < timeout_seconds:
        ok, msg = check_tulpa_mcp_health(port, host=host, token=token)
        if ok:
            return True, msg
        last_err = msg
        time.sleep(0.5)
    return False, f"Tulpa MCP 就绪等待超时 ({timeout_seconds}s): {last_err}"

def ensure_tulpa():
    b = _shared()
    if b.is_port_open(48777) and b.is_port_open(47860):
        ready, msg = check_tulpa_mcp_health(48777)
        if ready:
            return True, f"在线且 MCP 握手就绪 ({msg})"

    print("[启动] 正在后台启动 Tulpa 主服务 (Web: 47860, MCP: 48777)...")
    server_script = os.environ.get("TULPA_WG_SERVER", "server_wg.py")
    b.start_process_detached([b.get_python_exe(), server_script], cwd=str(ROOT_DIR), log_name="tulpa.log")

    ports_ok = False
    for _ in range(12):
        time.sleep(1)
        if b.is_port_open(48777) and b.is_port_open(47860):
            ports_ok = True
            break

    if not ports_ok:
        return False, "端口监听超时，请查看 Tulpa 运行日志 (~/.water-group/logs/tulpa.log)"

    ready, msg = wait_tulpa_mcp_ready(48777, timeout_seconds=12.0)
    if ready:
        return True, f"启动成功且 MCP 握手就绪 ({msg})"
    return False, f"Tulpa 启动就绪屏障未通过: {msg}"
