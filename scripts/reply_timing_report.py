"""Summarize local timing receipts without reading chat text or credentials."""
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics

ROOT=Path(__file__).resolve().parents[1]


def describe(values):
    values=sorted(values)
    return dict(n=len(values),median=round(statistics.median(values),4),
                p90=round(values[math.ceil(len(values)*.9)-1],4),max=round(values[-1],4))


def report(path, last=10):
    rows=[];skipped=0
    for part in (path.with_suffix('.jsonl.2'),path.with_suffix('.jsonl.1'),path):
        if not part.exists():continue
        for line in part.read_text('utf-8').splitlines():
            try:rows.append(json.loads(line))
            except ValueError:skipped+=1
    rows.sort(key=lambda r:r['started_at'])
    tools=defaultdict(list);spans=defaultdict(list);last_by_session={};gaps=[];batches={};turns=[]
    for row in rows:
        tool=row.get('tool')
        if tool:tools[tool].append(row['seconds'])
        for item in row['spans']:spans[item['name']].append(item['seconds'])
        sid=row.get('links',{}).get('session_id')
        for mark in row['marks']:
            sid=sid or mark.get('session_id')
            if mark.get('batch_created_at') and mark.get('event_ids') is not None:
                batches[mark['batch_id']]=dict(at=mark['batch_created_at'],**{
                    k:row.get('result',{}).get(k) for k in ('first_received_at','last_received_at')})
        if tool and sid:
            previous=last_by_session.get(sid)
            if previous and row['started_at']>=previous['finished_at']:
                gaps.append(dict(previous_tool=previous['tool'],tool=tool,
                                 seconds=round(row['started_at']-previous['finished_at'],4)))
            if previous is None or row['finished_at']>=previous['finished_at']:last_by_session[sid]=row
        if tool in ('send_chat_reply','send_chat_message') and row.get('result',{}).get('state')=='SUCCEEDED':
            http=[s for s in row['spans'] if s['name'] in ('onebot.send_group_msg','onebot.send_private_msg') and not s.get('error_type')]
            mark=next((m for m in row['marks'] if m.get('plan_created_at')),None)
            if http and mark:
                first_end=http[0]['start']+http[0]['seconds']
                batch=batches.get(mark['batch_id'],{})
                turn=dict(trace_id=row['trace_id'],batch_to_send_call=round(row['started_at']-mark['batch_created_at'],4),
                          plan_to_send_call=round(row['started_at']-mark['plan_created_at'],4),
                          send_call_to_first_http_receipt=round(first_end-row['started_at'],4))
                if batch.get('last_received_at'):
                    turn['last_batch_receive_to_first_http_receipt']=round(first_end-batch['last_received_at'],4)
                turns.append(turn)
    return dict(records=len(rows),malformed_lines=skipped,tools={k:describe(v) for k,v in tools.items()},
                spans={k:describe(v) for k,v in spans.items()},
                polling={name:dict(count=sum(r.get('polling',{}).get(name,{}).get('count',0) for r in rows),
                                   seconds=round(sum(r.get('polling',{}).get(name,{}).get('seconds',0) for r in rows),4))
                         for name in ('wait.event_sleep','chat.validate')},
                recent_host_gaps=gaps[-last:],recent_turns=turns[-last:],
                recent_calls=[{k:r[k] for k in ('trace_id','tool','started_at','seconds','status','result') if k in r} for r in rows[-last:]],
                notes=['Nested spans overlap: do not sum their durations.',
                       'Host gaps include inference, scheduling and transport; model TTFT is not observable here.',
                       'Receive timestamps start at Tulpa, HTTP receipts do not prove recipient-visible delivery.'])


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--path',type=Path,default=ROOT/'data/diagnostics/reply-timing.jsonl')
    p.add_argument('--last',type=int,default=10)
    p.add_argument('--timeline',type=Path,help='Write an interactive timeline visualization fragment instead of JSON.')
    p.add_argument('--antigravity',nargs='?',const='latest',help='Include local Antigravity host timing in the timeline.')
    p.add_argument('--focus',help='Prefer a reply time such as 23:47:37.')
    args=p.parse_args()
    if args.timeline:
        from reply_timing_timeline import write_timeline
        host=None
        if args.antigravity:
            from antigravity_reply_timing import latest_conversation
            host=latest_conversation() if args.antigravity=='latest' else Path(args.antigravity)
        print(f'Wrote {write_timeline(args.timeline,args.path,host,args.focus)} reply timelines: {args.timeline.resolve()}')
    else:
        print(json.dumps(report(args.path,max(0,min(args.last,100))),ensure_ascii=False,indent=2))
