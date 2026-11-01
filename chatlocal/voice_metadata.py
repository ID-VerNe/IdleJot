"""Metadata only: importing a voice never locates, copies or transcribes audio."""


def clean(value):
    if not isinstance(value,dict):return None
    kind=value.get('kind')
    if kind != 'qq_ptt':return None
    result={'kind':kind}
    for key in ('filename','md5','account','chat','server_id','source_db'):
        if isinstance(value.get(key),(str,int)) and not isinstance(value[key],bool):result[key]=str(value[key])[:300]
    for key in ('local_id','duration_ms','size'):
        if type(value.get(key)) is int and 0<=value[key]<2**63:result[key]=value[key]
    return result

def qq_voice(q,blob):
    meta=q._extract_qq_attachment_meta(blob)
    if not meta or meta.get('kind')!='voice':return None
    fields=q._proto_parse(blob)
    inner=next((v for t,v in fields.get(40800,[]) if t=='bytes'),blob)
    fields=q._proto_parse(inner)
    md5=next((v.hex() for t,v in fields.get(45406,[]) if t=='bytes' and len(v)==16),'')
    seconds=next((v for t,v in fields.get(45906,[]) if t=='varint'),0)
    return clean(dict(kind='qq_ptt',filename=meta.get('filename',''),md5=md5,
        duration_ms=int(seconds)*1000,size=meta.get('size',0)))
