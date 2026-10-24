"""Build an interactive timeline from local timing receipts or older call logs."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

ROOT=Path(__file__).resolve().parents[1]
LABELS={'get_chat_session':'读取当前会话','wait_chat_messages':'等待／读取消息',
        'plan_chat_reply':'提交回复计划','select_chat_expressions':'选择表达参考',
        'read_chat_image':'读取图片','send_chat_reply':'发送回复','send_chat_message':'发送回复'}
DETAILS={'send.check_target':'账号与会话校验','send.check_reply_target':'引用／提醒对象校验',
         'send.dispatch_guard':'发送前授权与新鲜度检查','onebot.client_create':'HTTP 客户端创建',
         'onebot.http_roundtrip':'HTTP 请求往返','onebot.send_group_msg':'OneBot 发送接口',
         'send.bubble_interval':'气泡间主动间隔','operation.prepare':'发送准备',
         'image.download':'图片下载','image.dns':'图片 DNS 解析','image.decode_encode':'图片解码与编码'}


def local_time(at):
    return datetime.fromtimestamp(at,timezone(timedelta(hours=8))).strftime('%H:%M:%S')


def stages(calls,start,end):
    result=[];cursor=start
    for call in sorted(calls,key=lambda x:x['at']):
        a=max(start,call['at']);b=min(end,call['at']+call['seconds'])
        if b<=a:continue
        if a>cursor+.01:
            result.append(dict(label='宿主处理空档',kind='host',start=cursor-start,end=a-start))
        result.append(dict(label=LABELS.get(call['tool'],call['tool'])+('（被拒绝）' if call.get('status')=='rejected' else ''),
                           kind='send' if call['tool'].startswith('send_chat') else 'tool',start=a-start,end=b-start))
        cursor=max(cursor,b)
    if cursor<end-.01:result.append(dict(label='宿主处理空档',kind='host',start=cursor-start,end=end-start))
    return result


def load_traces(path):
    rows=[]
    for part in (path.with_suffix('.jsonl.2'),path.with_suffix('.jsonl.1'),path):
        if not part.exists():continue
        for line in part.read_text('utf-8').splitlines():
            try:rows.append(json.loads(line))
            except ValueError:pass
    return rows


def timeline_data(trace_path, host_path=None, focus=None):
    rows=load_traces(trace_path);output=[]
    for row in rows:
        if row.get('tool') not in ('send_chat_reply','send_chat_message') or row.get('result',{}).get('state')!='SUCCEEDED':continue
        mark=next((m for m in row['marks'] if m.get('batch_created_at') and m.get('plan_id')),None)
        http=[s for s in row['spans'] if s['name']=='onebot.send_group_msg' and not s.get('error_type')]
        if not mark or not http:continue
        sid=mark['session_id'];start=mark['batch_created_at'];end=row['finished_at']
        related=[r for r in rows if r.get('tool') and (r.get('links',{}).get('session_id')==sid or
                  any(m.get('session_id')==sid for m in r['marks'])) and start<=r['started_at']<end]
        calls=[dict(tool=r['tool'],at=r['started_at'],seconds=r['seconds'],status=r.get('result',{}).get('state','').lower()) for r in related]
        chart=stages(calls,start,end)
        # Nested waits occupy their true position inside the parent send span.
        for s in row['spans']:
            if s['name']=='send.bubble_interval':
                chart.append(dict(label='气泡间主动等待（发送内）',kind='wait',start=s['start']-start,end=s['start']+s['seconds']-start))
        detail=[]
        for name,label in DETAILS.items():
            seconds=sum(s['seconds'] for r in related for s in r['spans'] if s['name']==name)
            if seconds:detail.append(dict(label=label,seconds=seconds))
        output.append(dict(label=local_time(row['started_at'])+' · 详细计时',at=row['started_at'],source='新增分段日志',
                           origin=start,session_id=sid,total=end-start,first=http[0]['start']+http[0]['seconds']-start,stages=chart,details=detail))
    db=sqlite3.connect((ROOT/'data/mcp-access.sqlite3').as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        calls=[dict(r) for r in db.execute("SELECT tool,grant_id,at,seconds,status FROM calls WHERE tool!='chat_expression' ORDER BY at")]
        for row in db.execute("SELECT * FROM chat_turn_plans WHERE state='SUCCEEDED' ORDER BY created DESC LIMIT 30"):
            payload=json.loads(row['payload']);queue=json.loads(row['queue'])
            receipts=[q['receipt'] for q in queue if q.get('receipt',{}).get('state')=='SUCCEEDED']
            if not receipts:continue
            if any(abs(o['at']-receipts[0]['created'])<3 for o in output):continue
            start=payload['read_at'];end=receipts[-1]['updated']
            selected=[c for c in calls if c['grant_id']==payload['grant_id'] and start<=c['at']<end]
            # Clip a read call which created the batch just before returning.
            prior=[c for c in calls if c['grant_id']==payload['grant_id'] and c['at']<start<c['at']+c['seconds']]
            if prior:selected.insert(0,prior[-1])
            label=local_time(receipts[0]['created'])+' · '+str(len(receipts))+' 条气泡'
            image=any(c['tool']=='read_chat_image' for c in selected)
            if image:label+=' · 含读图'
            output.append(dict(label=label,at=receipts[0]['created'],source='历史调用记录重建',total=end-start,
                               origin=start,session_id=row['session_id'],first=receipts[0]['updated']-start,stages=stages(selected,start,end),details=[]))
    finally:db.close()
    output.sort(key=lambda o:o['at'],reverse=True)
    # Prefer the most recent rich trace; otherwise show a real image-reading turn.
    preferred=next((o for o in output if o['source']=='新增分段日志'),None) or next((o for o in output if '含读图' in o['label']),None)
    if preferred:output.remove(preferred);output.insert(0,preferred)
    if host_path is not None:
        from antigravity_reply_timing import load_conversation, split_host_gaps
        host=load_conversation(host_path)
        for item in output:
            matching=any(e.get('tool') in ('send_chat_reply','send_chat_message') and
                         e.get('links',{}).get('session_id')==item['session_id'] and
                         e['start']<=item['at']<=e['end'] for e in host['events'])
            if not matching:continue
            start=item['origin']
            item['stages']=split_host_gaps(item['stages'],start,host)
            item['source']+=' + Antigravity 请求／工具计时'
    chosen=output[:20]
    if focus:
        pinned=next((o for o in output if o['label'].startswith(focus)),None)
        if pinned and pinned not in chosen:chosen[-1:]=[pinned]
        if pinned:
            chosen.remove(pinned);chosen.insert(0,pinned)
    return chosen


def write_timeline(output,trace_path,host_path=None,focus=None):
    data=timeline_data(trace_path,host_path,focus)
    if not data:raise ValueError('尚无可绘制的成功回复记录。')
    template=(ROOT/'scripts/reply_timeline.html').read_text('utf-8')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(template.replace('@@DATA@@',json.dumps(data,ensure_ascii=False).replace('<','\\u003c')),encoding='utf-8')
    return len(data)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=ROOT/'.tmp/reply-timeline.html')
    p.add_argument('--path',type=Path,default=ROOT/'data/diagnostics/reply-timing.jsonl')
    p.add_argument('--antigravity',nargs='?',const='latest',help='Read local host timing from latest Tulpa chat or an explicit conversation DB.')
    p.add_argument('--focus',help='Prefer a reply time such as 23:47:37.')
    a=p.parse_args();host=None
    if a.antigravity:
        from antigravity_reply_timing import latest_conversation
        host=latest_conversation() if a.antigravity=='latest' else Path(a.antigravity)
    print(f'Wrote {write_timeline(a.output,a.path,host,a.focus)} reply timelines: {a.output.resolve()}')
