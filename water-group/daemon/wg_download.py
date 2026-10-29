"""daemon 侧的唯一安全下载口（issue #200 P2）。

为什么要有它：群文件视频的下载地址来自 QQ 多媒体 CDN（``multimedia.nt.qq.com.cn``），
而 daemon 侧 ``mcp_client.py`` 只取文本与图片块，拿不到服务端那份下载器
（``chatlocal/mcp_media_transport.py:108-157``）。所以这里按**同一套判据**重写一份，
而不是随手 ``requests.get``——地址虽然来自 OneBot 事件而不是模型，重定向链的每一跳
仍可能把请求带进内网。

判据逐条对齐服务端，改这里必须同步改那边（``scripts/check_video_visibility.py``
交叉比对两边的常量）：

1. **DNS pin 每 hop**：自己解析、自己 ``connect`` 到解析出的 IP，不在别处二次解析
   （防 DNS rebinding）；
2. **拒绝非公网**：``ipaddress.is_global`` 为假的地址（内网 / 环回 / 链路本地）一律拒；
3. **最多 5 次重定向**，第 6 次直接拒；
4. **字节上限**：``Content-Length`` 与流式读取双闸，两个都要防——前者可撒谎，后者才是真闸；
5. **不走代理**：直连 socket，宿主代理不参与（与 ``wg_up_wake._onebot_call`` 同理）；
6. **不执行、不解压**：拿到的就是原始字节，调用方自己验魔数。

只接受 ``http`` / ``https``。``file://``、``ftp://`` 之类在这里就拒，不留到下游。
"""
import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit

MAX_REDIRECTS = 5          # 与服务端同值
CONNECT_TIMEOUT = 5.0      # 单跳建连超时
DEFAULT_TIMEOUT = 25.0     # 整个下载的墙钟预算，与服务端 ``deadline`` 同值


class DownloadError(ValueError):
    """下载失败：消息本身已经是能给 Agent 看的一句原因。"""


def _public_addresses(host, port):
    """解析 host，全部必须公网；任一私网即整条拒（同服务端 ``public_addresses``）。"""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise DownloadError(f"解析不出 {host} 的地址") from None
    addresses = []
    for info in infos:
        address = info[4][0]
        # 解析与判定必须分开：``DownloadError`` 自己是 ``ValueError`` 的子类，
        # 写在一个 try 里会让「非公网」被下面的 except 改写成「形态无法判定」,
        # 报出与事实不符的原因（判据没错，话错了也一样是缺陷）。
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            raise DownloadError(f"{host} 的地址形态无法判定，已拒绝下载") from None
        if not parsed.is_global:
            raise DownloadError(f"{host} 指向非公网地址，已拒绝下载")
        addresses.append((info[0], info[1], info[2], info[3] if len(info) > 3 else "", info[4]))
    if not addresses:
        raise DownloadError(f"解析不出 {host} 的地址")
    # IPv4 优先，与服务端同序（``addresses.sort(key=lambda a: ':' in a)``）。
    addresses.sort(key=lambda item: ":" in item[4][0])
    return addresses


def _target(url):
    """只要 http/https；其余协议在这里就拒。"""
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https"):
        raise DownloadError(f"不支持的下载协议 {parts.scheme or '(空)'}")
    if not parts.hostname:
        raise DownloadError("下载地址缺少主机名")
    return parts


def download(url, *, max_bytes, timeout=DEFAULT_TIMEOUT, cancel=None):
    """下载一个 URL，返回原始字节；任何不合规都抛 ``DownloadError``。

    ``cancel`` 是可选的 ``threading.Event``，被 set 时尽快中止（长下载不该拖死调用方）。
    """
    deadline = time.monotonic() + float(timeout)
    seen = 0
    while True:
        if seen > MAX_REDIRECTS:
            raise DownloadError(f"重定向超过 {MAX_REDIRECTS} 次")
        if cancel is not None and cancel.is_set():
            raise DownloadError("下载已取消")
        if time.monotonic() >= deadline:
            raise DownloadError(f"下载超过 {int(timeout)} 秒预算")
        parts = _target(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        family, socktype, proto, _, sockaddr = _public_addresses(parts.hostname, port)[0]
        raw = None
        try:
            raw = _fetch_once(parts, family, socktype, proto, sockaddr, max_bytes,
                              deadline, cancel)
        except _Redirect as hop:
            url = urljoin(url, hop.location or "")
            seen += 1
            continue
        return raw


class _Redirect(Exception):
    """内部信号：这一跳是重定向，外层换 URL 重来。"""

    def __init__(self, location):
        super().__init__(location or "")
        self.location = location


def _fetch_once(parts, family, socktype, proto, sockaddr, max_bytes, deadline, cancel):
    port = parts.port or (443 if parts.scheme == "https" else 80)
    sock = socket.socket(family, socktype, proto)
    conn = http.client.HTTPConnection(parts.hostname, port, timeout=CONNECT_TIMEOUT)
    try:
        sock.settimeout(min(CONNECT_TIMEOUT, max(0.1, deadline - time.monotonic())))
        sock.connect(sockaddr)
        if parts.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
        conn.sock = sock
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn.request("GET", path, headers={"User-Agent": "IdleJot/1.0", "Accept": "*/*",
                                           "Accept-Encoding": "identity"})
        response = conn.getresponse()
        if response.status in (301, 302, 303, 307, 308):
            raise _Redirect(response.getheader("Location"))
        if response.status != 200:
            raise DownloadError(_status_reason(response.status))
        declared = _declared_length(response)
        if declared > max_bytes:
            raise DownloadError(f"文件 {declared} 字节，超过本次上限 {max_bytes} 字节")
        body = bytearray()
        while True:
            if cancel is not None and cancel.is_set():
                raise DownloadError("下载已取消")
            if time.monotonic() >= deadline:
                raise DownloadError("下载超时")
            chunk = response.read(min(65536, max_bytes + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > max_bytes:
                raise DownloadError(f"文件超过本次上限 {max_bytes} 字节，已中止")
        if not body:
            raise DownloadError("下载回来是空文件")
        return bytes(body)
    except DownloadError:
        raise
    except (OSError, http.client.HTTPException):
        raise DownloadError("下载失败：连接中断或地址已失效") from None
    finally:
        try:
            conn.close()
        finally:
            sock.close()


def _declared_length(response):
    try:
        return max(0, int(response.getheader("Content-Length") or 0))
    except (TypeError, ValueError):
        return 0


def _status_reason(status):
    """把 CDN 的失败说成人话；``-5503`` 是 issue #200 实测到的过期码。"""
    if status in (403, 400):
        return f"下载地址已过期或被拒（HTTP {status}），需要重新取得链接"
    if status == 404:
        return "文件已从服务器删除（HTTP 404）"
    return f"下载失败（HTTP {status}）"
