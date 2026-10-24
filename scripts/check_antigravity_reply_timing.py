"""Synthetic protobuf/SQLite timing fixtures; no live client or model calls."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parent))
from antigravity_reply_timing import fields,load_conversation,split_host_gaps


def varint(n):
    out=bytearray()
    while n>127:out.append((n&127)|128);n>>=7
    out.append(n);return bytes(out)


def scalar(k,n):return varint(k<<3)+varint(n)
def blob(k,v):return varint(k<<3|2)+varint(len(v))+v
def duration(n):return scalar(1,int(n))+scalar(2,round((n-int(n))*1e9))


def main():
    start=1_792_079_240
    secret='PRIVATE-CHAT-AND-TOKEN'
    metadata=blob(1,duration(start))+blob(8,duration(start+4))
    call=blob(2,b'call_mcp_tool')+blob(3,json.dumps(dict(ServerName='tulpa',ToolName='plan_chat_reply',
        Arguments=dict(session_id='a'*32,text=secret))).encode())
    tool=blob(1,duration(start+3.5))+blob(32,duration(start+4.01))+blob(8,duration(start+4.2))+blob(4,call)
    chat=blob(11,duration(3.5))+blob(12,duration(.5))
    generation=blob(1,chat)+blob(2,varint(1)+varint(2))
    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/'conversation.db';db=sqlite3.connect(path)
        db.executescript('CREATE TABLE steps(idx INTEGER,step_type INTEGER,metadata BLOB);CREATE TABLE gen_metadata(idx INTEGER,data BLOB);')
        db.executemany('INSERT INTO steps VALUES(?,?,?)',[(1,15,metadata),(2,132,tool),(3,132,b'\xff')])
        db.execute('INSERT INTO gen_metadata VALUES(?,?)',(1,generation));db.commit();db.close()
        before=path.read_bytes();host=load_conversation(path);assert path.read_bytes()==before
        assert secret not in json.dumps(host)
        assert host['skipped']==1
        assert [e['kind'] for e in host['events']]==['model_wait','model_stream','bridge']
        chart=[dict(label='宿主处理空档',kind='host',start=0,end=4.1),dict(label='工具',kind='tool',start=4.1,end=5)]
        out=split_host_gaps(chart,start,host)
        assert abs(sum(x['end']-x['start'] for x in out)-5)<1e-6
        assert out[-1]==chart[-1] and all(x['end']>x['start'] for x in out)
        assert abs(sum(x['end']-x['start'] for x in out if x['kind']=='model_wait')-3.5)<1e-6
        # A changed timestamp contract must not turn unknown time into inference.
        db=sqlite3.connect(path);db.execute('UPDATE steps SET metadata=? WHERE idx=1',(blob(1,duration(start))+blob(8,duration(start+9)),));db.commit();db.close()
        assert all(e['kind']=='bridge' for e in load_conversation(path)['events'])
    for bad in [b'\x00',b'\x0a\xff',b'\x0a\x05x']:
        try:fields(bad)
        except ValueError:pass
        else:raise AssertionError('malformed wire accepted')
    print('PASS: read-only metadata, no private text, verified TTFT/stream bounds, gap partition, timestamp mismatch refusal and malformed-wire handling.')


if __name__=='__main__':main()
