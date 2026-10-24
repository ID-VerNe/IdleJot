"""Explicit, resumable metadata-only image indexing. Does not contact OneBot."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from chatlocal.store import Store
from chatlocal.image_identity import backfill_batch


def main():
    parser=argparse.ArgumentParser(description='分批建立历史图片身份索引，不读取/复制图片或访问网络。')
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--batch-size',type=int,default=250)
    parser.add_argument('--max-batches',type=int,default=1)
    parser.add_argument('--after-id',type=int,default=0)
    parser.add_argument('--through-id',type=int)
    args=parser.parse_args()
    if not args.database.is_file():parser.error('数据库文件不存在。')
    if not 1<=args.batch_size<=1000 or args.max_batches<1 or args.after_id<0:parser.error('批次参数无效。')
    if args.through_id is not None and args.through_id<0:parser.error('through-id 无效。')
    store=Store(args.database)
    with store.connect() as db:
        high=db.execute('SELECT coalesce(max(id),0) FROM messages').fetchone()[0]
    through=min(args.through_id,high) if args.through_id is not None else high
    after=args.after_id;processed=indexed=0
    for _ in range(args.max_batches):
        result=backfill_batch(store,after,through,args.batch_size)
        processed+=result['processed'];indexed+=result['indexed'];after=result['after_id']
        if not result['has_more']:break
    print(json.dumps(dict(result,processed=processed,indexed=indexed),ensure_ascii=False))


if __name__=='__main__':main()
