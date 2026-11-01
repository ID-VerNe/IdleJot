"""Image transport: addressing, DNS pinning, download, byte bounds (issue #11 PC).

Split out of ``mcp_chat_media.py``: everything that turns a QQ image URL (or an
inline base64 payload) into bounded raw bytes, with the SSRF guards that make
that safe. Pixel decoding lives in ``mcp_media_pixels``; the MCP-facing class
stays in the ``mcp_chat_media`` facade.
"""
import base64
import binascii
import http.client
import ipaddress
import re
import socket
import ssl
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import dns.exception
import dns.message
import dns.query
import dns.resolver

from . import reply_timing as timing

MAX_BYTES = 6 * 1024 * 1024
# 下载判据的公开常量（issue #200）：daemon 侧的 ``wg_download`` 复述同一套闸门，
# ``scripts/check_video_visibility.py`` 断言两边同值。数字以前直接写在 ``download()``
# 里（``range(5)`` / ``+25``），门禁无从比对，漂移也没有任何东西会喊。
MAX_REDIRECTS = 5
DOWNLOAD_TIMEOUT_SECONDS = 25
MAX_BASE64_LENGTH = 4 * ((MAX_BYTES + 2) // 3)


def image_ref(data):
    """Private bounded adapter references; never returned directly through MCP."""
    result = {}
    if isinstance(data.get('url'), str) and len(data['url']) <= 4096:
        result['url'] = data['url']
    file = data.get('file')
    if isinstance(file,str) and file.startswith(('https://','http://')) and len(file)<=4096:
        result.setdefault('url',file)
    if isinstance(file, str) and re.fullmatch(r'[A-Za-z0-9_{}.-]{1,180}', file) and '..' not in file:
        result['file'] = file
    elif isinstance(file,str) and len(file)<=2048 and (Path(file).is_absolute() or file.startswith('file:')):
        result['local_file']=file
    encoded=data.get('base64')
    if isinstance(file,str) and file.startswith('base64://'):encoded=file[9:]
    if isinstance(encoded,str) and len(encoded)<=(MAX_BYTES+2)//3*4:result['base64']=encoded
    for key in ('path','local_file'):
        if isinstance(data.get(key),str) and len(data[key])<=2048:result['local_file']=data[key]
    if type(data.get('timestamp')) in (int,float):result['timestamp']=data['timestamp']
    return result


def remote_target(url, *, trusted_file=False):
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    try:public_ip=trusted_file and ipaddress.ip_address(host).is_global
    except ValueError:public_ip=False
    domains=('qq.com','qq.com.cn','qpic.cn','gtimg.com','gtimg.cn')+ (('qcloud.com','myqcloud.com') if trusted_file else ())
    if (parts.scheme not in ('http', 'https') or parts.username or parts.password or
        (parts.port not in (None, 80, 443) and not (trusted_file and 0<parts.port<65536)) or parts.fragment or
        not (public_ip or any(host == d or host.endswith('.'+d) for d in domains))):
        raise ValueError('图片来源不是允许的 QQ 图片服务。')
    return parts


@timing.stage('image.dns')
def public_addresses(host, port, *, deadline=None):
    deadline=min(deadline or float('inf'),time.monotonic()+2)
    try:
        literal=ipaddress.ip_address(host)
    except ValueError:literal=None
    addresses=[str(literal)] if literal else []
    if not literal:
        try:
            # dnspython reads configured DNS servers and bounds the wire query;
            # socket.getaddrinfo cannot enforce a per-call timeout.
            with timing.span('image.dns.system_lookup'):
                resolver=dns.resolver.Resolver()
                answers=resolver.resolve_name(host,lifetime=min(.7,max(0,deadline-time.monotonic())),search=False)
                addresses=list(answers.addresses())
        except (dns.exception.DNSException,OSError):pass
    fake=ipaddress.ip_network('198.18.0.0/15')
    fake_detected=bool(addresses) and all(ipaddress.ip_address(a) in fake for a in addresses)
    timing.note(dns_system_record_count=len(addresses),dns_fake_ip_detected=fake_detected)
    if not addresses or fake_detected:
        try:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise ValueError('DNS budget exhausted')
            # Public resolver bootstrap, not a CDN address. Numeric transport
            # avoids recursively resolving the fallback hostname; TLS still
            # verifies dns.alidns.com. dnspython enforces one absolute expiry.
            with timing.span('image.dns.tls_query'):
                response=dns.query.tls(dns.message.make_query(host,'A'),'223.5.5.5',
                    server_hostname='dns.alidns.com',timeout=remaining,verify=True)
            answer=dns.resolver.Answer(dns.name.from_text(host),dns.rdatatype.A,dns.rdataclass.IN,response)
            if answer.rrset is None:raise ValueError('No DNS addresses')
            addresses=[a.address for a in answer]
            timing.note(dns_tls_record_count=len(addresses))
        except (dns.exception.DNSException,OSError,ValueError):
            raise ValueError('图片 DNS 解析未在短预算内完成，或安全备用解析不可用。') from None
    if time.monotonic()>deadline:raise ValueError('图片 DNS 解析预算已耗尽。')
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ValueError('图片地址指向非公网，已拒绝。')
    addresses.sort(key=lambda a:':' in a)
    return [(socket.AF_INET6 if ':' in a else socket.AF_INET,socket.SOCK_STREAM,6,'',
             (a,port,0,0) if ':' in a else (a,port)) for a in addresses]


@timing.stage('image.download')
def download(url, cancel, *, max_bytes=MAX_BYTES, trusted_file=False):
    """DNS pinned per hop, no proxy, no local networks even after redirects."""
    deadline = time.monotonic()+DOWNLOAD_TIMEOUT_SECONDS
    try:
        for _ in range(MAX_REDIRECTS):
            parts = remote_target(url,trusted_file=trusted_file)
            port = parts.port or (443 if parts.scheme == 'https' else 80)
            records = public_addresses(parts.hostname, port, deadline=deadline)
            if cancel.is_set() or time.monotonic() >= deadline:
                raise ValueError('图片读取已取消或超时。')
            family, socktype, proto, _, address = records[0]
            sock = socket.socket(family, socktype, proto)
            conn = http.client.HTTPConnection(parts.hostname, port, timeout=5)
            try:
                sock.settimeout(min(5, max(.1, deadline-time.monotonic())))
                with timing.span('image.tcp_connect'):
                    sock.connect(address)
                if parts.scheme == 'https':
                    with timing.span('image.tls'):
                        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
                conn.sock = sock
                path = parts.path or '/'
                if parts.query:path += '?'+parts.query
                with timing.span('image.response_headers'):
                    conn.request('GET', path, headers={'User-Agent':'Tulpa/0.5', 'Accept':'*/*', 'Accept-Encoding':'identity'})
                    response = conn.getresponse()
                if response.status in (301,302,303,307,308):
                    url = urljoin(url, response.getheader('Location') or '')
                    continue
                if response.status != 200:raise ValueError('QQ 图片暂不可用或地址已过期。')
                if int(response.getheader('Content-Length') or 0) > max_bytes:
                    timing.note(image_error='byte_limit')
                    raise ValueError('媒体超过本次读取的大小上限。')
                raw = bytearray()
                with timing.span('image.download_body'):
                    while True:
                        if cancel.is_set() or time.monotonic() >= deadline:
                            raise ValueError('图片读取已取消或超时。')
                        chunk = response.read(min(65536, max_bytes+1-len(raw)))
                        if not chunk:break
                        raw.extend(chunk)
                        if len(raw)>max_bytes:
                            timing.note(image_error='byte_limit')
                            raise ValueError('媒体超过本次读取的大小上限。')
                return bytes(raw)
            finally:
                conn.close();sock.close()
        raise ValueError('图片重定向次数过多。')
    except (OSError, http.client.HTTPException):
        raise ValueError('QQ 图片下载失败；可稍后重试，消息接收不受影响。') from None


def uploaded_bytes(encoded):
    """Decode a bounded inline image, never a filesystem or remote reference."""
    if not isinstance(encoded, str) or not 4 <= len(encoded) <= MAX_BASE64_LENGTH:
        raise ValueError('图片为空或超过单张 6 MiB 上限。')
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('data_base64 必须是图片原始字节的纯 Base64，不接受路径、URL 或 data: 前缀。') from None
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError('图片为空或超过单张 6 MiB 上限。')
    return raw
