"""Read-only Antigravity timing adapter; never export prompts or tool content.

Wire field numbers were verified against the installed language_server.exe's
cortex.proto descriptors (Antigravity 2.23.1). Generation durations are checked
against step timestamps; unsupported/inconsistent records remain unclassified.
"""
import json
from contextlib import closing
from pathlib import Path
import sqlite3


def varint(data, offset):
    value=0
    for shift in range(0,70,7):
        if offset>=len(data):raise ValueError('truncated varint')
        byte=data[offset];offset+=1;value|=(byte&127)<<shift
        if byte<128:return value,offset
    raise ValueError('oversize varint')


def fields(data):
    if not isinstance(data,bytes) or len(data)>32*1024*1024:raise ValueError('invalid protobuf size')
    offset=0;result=[]
    while offset<len(data):
        tag,offset=varint(data,offset);number,wire=tag>>3,tag&7
        if not number:raise ValueError('invalid field')
        if wire==0:value,offset=varint(data,offset)
        elif wire in (1,2,5):
            if wire==2:size,offset=varint(data,offset)
            else:size=8 if wire==1 else 4
            if offset+size>len(data):raise ValueError('truncated field')
            value=data[offset:offset+size];offset+=size
        else:raise ValueError('unsupported wire type')
        result.append((number,wire,value))
    return result


def field(data, number, default=None):
    return next((v for n,w,v in fields(data) if n==number),default)


def seconds(data):
    if data is None:return None
    return field(data,1,0)+field(data,2,0)/1e9


def open_readonly(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    return db


def latest_conversation(base=None):
    base=base or Path.home()/'.gemini/antigravity'
    with closing(open_readonly(base/'conversation_summaries.db')) as db:
        rows=db.execute('SELECT conversation_id FROM conversation_summaries ORDER BY last_modified_time DESC LIMIT 8').fetchall()
    # UUID identifiers only; never accept a database-provided path.
    import uuid
    for row in rows:
        cid=str(uuid.UUID(row[0]));path=base/'conversations'/f'{cid}.db'
        if not path.exists():continue
        with closing(open_readonly(path)) as db:
            found=db.execute('SELECT 1 FROM steps WHERE instr(metadata,?)>0 AND instr(metadata,?)>0 LIMIT 1',
                             (b'send_chat_reply',b'tulpa')).fetchone()
        if found:return path
    raise ValueError('no recent Tulpa chat conversation')


def load_conversation(path,server_names=('tulpa',)):
    steps={};events=[];skipped=0
    with closing(open_readonly(path)) as db:
        for row in db.execute('SELECT idx,step_type,metadata FROM steps'):
            try:
                data=row['metadata'];created=seconds(field(data,1));started=seconds(field(data,32));completed=seconds(field(data,8))
                if not created or not 1_500_000_000<created<3_000_000_000:continue
                item=dict(index=row['idx'],created=created,started=started,completed=completed)
                steps[row['idx']]=item
                call=field(data,4)
                if not call or not started or not completed or completed<started:continue
                name=field(call,2,b'').decode('utf-8');arguments=json.loads(field(call,3,b'{}'))
                if not isinstance(arguments,dict):continue
                if name=='call_mcp_tool' and arguments.get('ServerName') in server_names:
                    tool=arguments.get('ToolName','')
                    if not isinstance(tool,str) or not tool.replace('_','').isalnum():continue
                    args=arguments.get('Arguments',{})
                    args=args if isinstance(args,dict) else {}
                    links={k:v for k,v in args.items() if k in ('session_id','plan_id','batch_id') and isinstance(v,str) and len(v)==32}
                    item.update(tool=tool,links=links)
                    events.append(dict(start=started,end=completed,label='MCP 往返 · '+tool,kind='bridge',tool=tool,links=links,index=row['idx']))
                elif name=='view_file':
                    # Classify generated tool-output files by shape, without
                    # reading their content or persisting their absolute path.
                    parts=Path(arguments.get('AbsolutePath','')).parts
                    output_file=len(parts)>=4 and parts[-4:-2]==('.system_generated','steps') and parts[-1]=='output.txt'
                    label='读取工具结果文件' if output_file else '宿主读取文件'
                    events.append(dict(start=started,end=completed,label=label,kind='bridge',index=row['idx']))
            except (ValueError,TypeError,UnicodeError):skipped+=1
        for row in db.execute('SELECT idx,data FROM gen_metadata'):
            try:
                data=row['data'];chat=field(data,1)
                if not chat:continue
                indices=[]
                for n,w,value in fields(data):
                    if n!=2:continue
                    if w==0:indices.append(value)
                    elif w==2:
                        offset=0
                        while offset<len(value):number,offset=varint(value,offset);indices.append(number)
                covered=[steps[i] for i in indices if i in steps]
                ttft=seconds(field(chat,11));stream=seconds(field(chat,12))
                if not covered or ttft is None or stream is None or min(ttft,stream)<0:continue
                start=min(x['created'] for x in covered);first=start+ttft;end=first+stream
                # finished_generating_at belongs to the model generation,
                # completed_at for a tool includes actual tool execution.
                generation_step=next((x for x in covered if not x.get('tool') and x['completed'] and abs(x['completed']-end)<.25),None)
                if not generation_step:skipped+=1;continue
                events.append(dict(start=start,end=first,label='模型请求 · 等待首 token',kind='model_wait',generation=row['idx']))
                if stream:events.append(dict(start=first,end=end,label='模型输出流（含工具参数）',kind='model_stream',generation=row['idx']))
            except (ValueError,TypeError,UnicodeError):skipped+=1
    return dict(events=sorted(events,key=lambda x:x['start']),skipped=skipped,
                source='Antigravity local cortex metadata, timestamp-validated',steps=steps)


def split_host_gaps(chart, start, host):
    """Partition gaps only. Keep existing measured Tulpa spans unchanged."""
    result=[]
    for stage in chart:
        if stage['kind']!='host':result.append(stage);continue
        a=start+stage['start'];b=start+stage['end'];cursor=a
        for event in host['events']:
            lo=max(a,event['start'],cursor);hi=min(b,event['end'])
            if hi<=lo:continue
            if lo>cursor+.0001:result.append(dict(label='宿主调度／未归类',kind='host',start=cursor-start,end=lo-start))
            label='MCP 派发／结果返回' if event.get('tool') else event['label']
            result.append(dict(label=label,kind=event['kind'],start=lo-start,end=hi-start))
            cursor=hi
        if cursor<b-.0001:result.append(dict(label='宿主调度／未归类',kind='host',start=cursor-start,end=b-start))
    return result
