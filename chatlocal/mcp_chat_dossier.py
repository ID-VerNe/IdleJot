"""MCP-facing member dossier / group notes adapters. No model calls.

Pure pass-through from MCP tool args to ``MemoryEngine`` kwargs, with the
session_id -> group_id fallback as the only real logic. Extracted from
``MCPChat`` (issue #169 PF-1) along the same seam as ``ChatDetails`` /
``ChatTurns``: the four dossier tools are a distinct responsibility from
chat session lifecycle, so they sit on their own collaborator held by
``MCPChat`` rather than on the chat class itself.
"""


class ChatDossiers:
    def __init__(self, chat):
        self.chat = chat
        self.access = chat.access

    def _group_id(self, grant, args):
        """Resolve the group id from args, falling back to the session's group.

        All four dossier tools accept ``group_id`` directly; when omitted but a
        ``session_id`` is given, read the session row and take the tail of its
        ``conversation_id`` (``account:group:GID``). Auth already checked by the
        caller; a missing/ambiguous session just yields no group.
        """
        gid = args.get('group_id')
        if not gid and args.get('session_id'):
            try:
                row = self.chat.row(args['session_id'], grant['id'])
                gid = row['conversation_id'].split(':')[-1]
            except Exception:
                pass
        return str(gid) if gid else None

    def query_member_dossier(self, grant, args):
        self.chat.permission(grant)
        from water_group.memory_engine import get_memory_engine
        engine = get_memory_engine()
        gid = self._group_id(grant, args)
        return engine.query_member_dossier(
            str(args.get('identifier', '')), group_id=gid)

    def update_member_dossier(self, grant, args):
        self.chat.permission(grant)
        from water_group.memory_engine import get_memory_engine
        engine = get_memory_engine()
        gid = self._group_id(grant, args)
        return engine.update_member_dossier(
            identifier=str(args.get('identifier', '')),
            key_memories=args.get('key_memories'),
            traits=args.get('traits'),
            aliases=args.get('aliases'),
            interests=args.get('interests'),
            tone_style=args.get('tone_style'),
            role=args.get('role'),
            primary_name=args.get('primary_name'),
            overwrite=bool(args.get('overwrite', False)),
            group_id=gid,
        )

    def consolidate_chat_memory(self, grant, args):
        self.chat.permission(grant)
        from water_group.memory_engine import get_memory_engine
        engine = get_memory_engine()
        gid = self._group_id(grant, args)
        return engine.consolidate_chat_memory(
            group_id=gid,
            session_id=args.get('session_id'),
            records=args.get('records'),
            events=args.get('events'),
            messages=args.get('messages'),
            summary=args.get('summary'),
            notes=args.get('notes'),
        )

    def query_group_notes(self, grant, args):
        self.chat.permission(grant)
        from water_group.memory_engine import get_memory_engine
        engine = get_memory_engine()
        gid = self._group_id(grant, args)
        notes = engine.query_group_notes(
            gid or '', query=args.get('query'), limit=args.get('limit', 20))
        return {"group_id": gid, "notes": notes}
