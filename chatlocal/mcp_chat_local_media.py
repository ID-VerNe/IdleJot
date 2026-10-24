"""Event-scoped local QQ media, including verified cached history pixels."""
import base64
import hashlib
import json
from pathlib import Path
import re
import time
import warnings
from urllib.parse import unquote, urlsplit

from . import reply_timing as timing

MAX_BYTES=6*1024*1024


def image_md5(ref):
    match=re.fullmatch(r'\{?([A-Fa-f0-9]{32})\}?(?:\.[A-Za-z0-9]{1,8})?',ref.get('file',''))
    return match[1].lower() if match else None


@timing.stage('image.history_lookup')
def history_image(store,cid,ref,cancel,source,*,prefer_original=False,plan=None):
    """Look up message-owned MD5 identities, then verify actual local bytes.

    Native source_id and OneBot message_id are not interchangeable. A matching
    time alone is never sufficient, and absent hashes do not authorize a hit.
    """
    md5=image_md5(ref);stamp=ref.get('timestamp')
    if not md5:return None
    from .image_identity import candidates
    def batches():
        with store.connect() as db:
            indexed=candidates(db,cid,md5,plan)
        yield [json.loads(row['media_item']) for row in indexed]
        # Invalid indexed files must not hide valid nearby, unindexed history.
        if type(stamp) in (int,float) and stamp>0:
            lo=max(stamp-1000,plan.start) if plan is not None and plan.start is not None else stamp-1000
            hi=min(stamp+1000,plan.end) if plan is not None and plan.end is not None else stamp+1000
            with store.connect() as db:
                rows=db.execute('''SELECT media_json FROM messages
                    WHERE platform='qq' AND conversation_id=? AND timestamp>=? AND timestamp<?
                    ORDER BY timestamp,id LIMIT 32''',(cid,lo,hi)).fetchall()
            yield [item for row in rows for item in json.loads(row['media_json'])]
    from .media import media_path
    reads=0
    for items in batches():
        for item in sorted(items,key=lambda item:bool(item.get('metadata',{}).get('thumbnail'))):
            if cancel.is_set():return None
            metadata=item.get('metadata',{});thumb=bool(metadata.get('thumbnail'))
            if prefer_original and thumb:continue
            expected=metadata.get('md5')
            expected=expected.lower() if isinstance(expected,str) else ''
            if expected and expected!=md5:continue
            if thumb and (expected!=md5 or not item.get('sha256')):continue
            try:path=media_path(item)
            except (ValueError,OSError):continue
            if path is None:continue
            reads+=1
            if reads>8:return None
            raw=read_path(str(path),[path.parent.parent])
            if raw is None:continue
            if item.get('sha256') and hashlib.sha256(raw).hexdigest()!=item['sha256']:continue
            if not thumb and hashlib.md5(raw).hexdigest()!=md5:continue
            source.update(source='history_cache',variant='thumb' if thumb else 'ori')
            timing.note(image_source='history_cache',image_local_variant=source['variant'])
            return raw
    return None


def roots(directory,account,kind='local_roots'):
    try:result=[Path.home()/'Documents'/'Tencent Files'/account/'nt_qq'/'nt_data'] if kind=='local_roots' else []
    except RuntimeError:result=[]
    # Additional roots are explicit local operator configuration, not MCP input.
    policy=Path(directory)/'mcp-chat-media-policy.json'
    try:
        cfg=json.loads(policy.read_text('utf-8-sig'))
    except FileNotFoundError:cfg={}
    except (ValueError,OSError):
        warnings.warn('本地图片配置不可读，使用默认目录。',RuntimeWarning)
        cfg={}
    entries=cfg.get(kind,{}) if isinstance(cfg,dict) else None
    paths=entries.get(account,[]) if isinstance(entries,dict) else None
    if not isinstance(paths,list) or any(not isinstance(p,str) or '\0' in p or not Path(p).is_absolute() for p in paths):
        warnings.warn('本地图片配置结构错误，使用默认目录。',RuntimeWarning)
    else:result.extend(Path(p) for p in paths[:8])
    allowed=[]
    for path in result:
        try:allowed.append(path.resolve())
        except OSError:continue
    return allowed


def read_path(value,allowed):
    if not isinstance(value,str) or len(value)>2048:return None
    if value.startswith('file:'):
        parsed=urlsplit(value)
        if parsed.netloc not in ('','localhost'):return None
        value=unquote(parsed.path)
        if re.match(r'^/[A-Za-z]:',value):value=value[1:]
    try:
        path=Path(value)
        if not path.is_absolute():return None
        path=path.resolve(strict=True)
        if not any(path.is_relative_to(root) for root in allowed):return None
        if not path.is_file() or path.stat().st_size>MAX_BYTES:return None
        with path.open('rb') as f:raw=f.read(MAX_BYTES+1)
        return raw if 0<len(raw)<=MAX_BYTES else None
    except (ValueError,OSError):return None


def reference_bytes(ref,allowed):
    encoded=ref.get('base64')
    file=ref.get('file','')
    if not encoded and isinstance(file,str) and file.startswith('base64://'):encoded=file[9:]
    if isinstance(encoded,str) and len(encoded)<=(MAX_BYTES+2)//3*4:
        try:
            raw=base64.b64decode(encoded,validate=True)
            if 0<len(raw)<=MAX_BYTES:return raw
        except ValueError:pass
    for key in ('local_file','file','path'):
        raw=read_path(ref.get(key),allowed)
        if raw is not None:return raw
    return None


@timing.stage('image.local_lookup')
def find_local(ref,directory,account,cancel,source=None,*,prefer_original=False):
    allowed=roots(directory,account)
    raw=reference_bytes(ref,allowed)
    if raw is not None:
        if source is not None:source.update(source='local_reference',variant='provided')
        timing.note(image_source='local_reference')
        return raw
    filename=ref.get('file','')
    match=re.fullmatch(r'\{?([A-Fa-f0-9]{32})\}?(?:\.[A-Za-z0-9]{1,8})?',filename)
    if not match:return None
    md5=match[1].lower()
    stamp=ref.get('timestamp',time.time()*1000)
    try:month=time.strftime('%Y-%m',time.localtime(stamp/1000))
    except (ValueError,TypeError,OverflowError,OSError):return None
    # Bounded deterministic directories, never a recursive QQ cache walk.
    months=list(dict.fromkeys((month,time.strftime('%Y-%m'))))
    hash_roots=roots(directory,account,'hash_cache_roots')
    for variant in ('Ori','Thumb'):
        if prefer_original and variant=='Thumb':continue
        for root in dict.fromkeys(allowed+hash_roots):
            for m in months:
                folder=root/'Pic'/m/variant
                for prefix in (md5,md5.upper()):
                    for i,path in enumerate(folder.glob(prefix+'*')):
                        if i>=16 or cancel.is_set():break
                        raw=read_path(str(path),allowed+hash_roots)
                        if raw is None:continue
                        if variant=='Ori' and hashlib.md5(raw).hexdigest()!=md5:continue
                        if variant=='Thumb' and not re.fullmatch(md5+r'_[0-9]+\.[A-Za-z0-9]{1,8}',path.name,re.I):continue
                        if source is not None:source.update(source='qq_local_cache',variant=variant.lower())
                        timing.note(image_source='qq_local_cache',image_local_variant=variant.lower())
                        return raw
    return None
