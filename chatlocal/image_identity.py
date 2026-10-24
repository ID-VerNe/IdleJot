"""Message-owned QQ image identities; no file copies or implicit history scan."""

INSERT = '''INSERT INTO message_image_ids(message_id,slot,platform,conversation_id,md5,thumbnail)
    SELECT m.id,CAST(j.key AS INTEGER),m.platform,m.conversation_id,
           lower(json_extract(j.value,'$.metadata.md5')),
           CASE WHEN json_extract(j.value,'$.metadata.thumbnail')=1 THEN 1 ELSE 0 END
    FROM messages m,json_each(m.media_json) j
    WHERE m.id=:message_id AND m.platform='qq'
      AND json_extract(j.value,'$.status')='available'
      AND json_extract(j.value,'$.kind') IN ('image','gif','sticker')
      AND coalesce(json_extract(j.value,'$.local_path'),'')!=''
      AND json_type(j.value,'$.metadata.md5')='text'
      AND length(json_extract(j.value,'$.metadata.md5'))=32
      AND lower(json_extract(j.value,'$.metadata.md5')) NOT GLOB '*[^0-9a-f]*' '''

LOOKUP = '''SELECT json_extract(m.media_json,'$['||i.slot||']') AS media_item
    FROM message_image_ids i JOIN messages m ON m.id=i.message_id
    WHERE i.platform='qq' AND i.conversation_id=? AND i.md5=?
      AND m.platform=i.platform AND m.conversation_id=i.conversation_id'''
ORDER = ' ORDER BY i.thumbnail,i.message_id DESC,i.slot LIMIT 32'


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS message_image_ids(
            message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            slot INTEGER NOT NULL,platform TEXT NOT NULL,conversation_id TEXT NOT NULL,
            md5 TEXT NOT NULL,thumbnail INTEGER NOT NULL,
            PRIMARY KEY(message_id,slot));
        CREATE INDEX IF NOT EXISTS image_identity_lookup ON message_image_ids(
            platform,conversation_id,md5,thumbnail,message_id DESC,slot);
    ''')
    insert=INSERT.replace(':message_id','NEW.id')
    db.executescript(f'''
        CREATE TRIGGER IF NOT EXISTS image_identity_insert AFTER INSERT ON messages
        BEGIN {insert}; END;
        CREATE TRIGGER IF NOT EXISTS image_identity_update
        AFTER UPDATE OF media_json,platform,conversation_id ON messages
        BEGIN DELETE FROM message_image_ids WHERE message_id=OLD.id; {insert}; END;
    ''')


def candidates(db,cid,md5,plan=None):
    sql=LOOKUP;args=[cid,md5]
    if plan is not None:
        for op,value in (('>=',plan.start),('<',plan.end)):
            if value is not None:sql+=' AND m.timestamp'+op+'?';args.append(value)
    return db.execute(sql+ORDER,args).fetchall()


def backfill_batch(store,after_id,through_id,batch_size=250):
    """Explicit rowid-keyset batch; JSON work only, never read files under lock."""
    if not 1<=batch_size<=1000 or after_id<0 or through_id<0:raise ValueError('无效索引批次参数。')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows=db.execute('SELECT id FROM messages WHERE id>? AND id<=? ORDER BY id LIMIT ?',
                        (after_id,through_id,batch_size)).fetchall()
        indexed=0
        for row in rows:
            db.execute('DELETE FROM message_image_ids WHERE message_id=?',(row['id'],))
            indexed+=db.execute(INSERT,dict(message_id=row['id'])).rowcount
        last=rows[-1]['id'] if rows else after_id
        more=db.execute('SELECT 1 FROM messages WHERE id>? AND id<=? LIMIT 1',(last,through_id)).fetchone()
    return dict(processed=len(rows),indexed=indexed,after_id=last,through_id=through_id,has_more=bool(more))
