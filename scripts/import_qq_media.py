"""Read a local file into an authorized Tulpa asset; never sends to QQ."""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from import_chat_image import connection, UploadError
from chatlocal.mcp_qq_media import MAX_BYTES, inspect, filename
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def media_bytes(path, kind):
    try:
        fd=os.open(Path(path).expanduser(),os.O_RDONLY|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as f:
            info=os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=MAX_BYTES:raise UploadError('请选择非空普通文件，最多32 MiB。')
            raw=f.read(MAX_BYTES+1)
    except OSError:raise UploadError('无法读取附件文件。') from None
    try:inspect(raw,kind)
    except ValueError as exc:raise UploadError(str(exc)) from None
    return raw


async def upload(config, path, kind, server=None, name=None):
    url,auth=connection(config,server);raw=media_bytes(path,kind)
    name=filename(name or Path(path).name)
    async with httpx.AsyncClient(headers={'Authorization':auth},timeout=120,trust_env=False,follow_redirects=False) as http:
        async with streamable_http_client(url,http_client=http) as (read,write,_):
            async with ClientSession(read,write) as client:
                info=await client.initialize()
                if info.serverInfo.name!='tulpa':raise UploadError('目标 MCP 服务不是 Tulpa，未上传。')
                if 'import_qq_media' not in {t.name for t in (await client.list_tools()).tools}:raise UploadError('此连接未开放附件导入，请检查扩展权限。')
                result=await client.call_tool('import_qq_media',dict(kind=kind,filename=name,data_base64=base64.b64encode(raw).decode()))
    data=result.structuredContent
    if not isinstance(data,dict):raise UploadError('Tulpa 未返回附件导入结果。')
    if result.isError:return {k:data[k] for k in ('error','error_code') if k in data},False
    return {k:data[k] for k in ('asset_id','kind','filename','sha256','bytes','mime_type','note') if k in data},True


def main():
    p=argparse.ArgumentParser(description='通过 Tulpa MCP 导入附件；仅导入，不向 QQ 发送。')
    p.add_argument('file');p.add_argument('--config',required=True)
    p.add_argument('--kind',required=True,choices=['voice','video','file','image'])
    p.add_argument('--server');p.add_argument('--name')
    args=p.parse_args()
    try:data,ok=asyncio.run(upload(args.config,args.file,args.kind,args.server,args.name))
    except UploadError as exc:print(str(exc),file=sys.stderr);return 1
    except Exception:print('附件导入未完成；请核对服务、权限和网络。',file=sys.stderr);return 1
    print(json.dumps(data,ensure_ascii=False,indent=2));return 0 if ok else 1


if __name__=='__main__':raise SystemExit(main())
