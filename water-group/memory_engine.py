"""Group member dynamic memory & personalized targeted reply system.

Two-Tier Storage:
- Tier 1 (L1 Context): Compact synthesized profiles (aliases, tone style, XP preferences, 3-5 key memories)
  automatically injected into Agent's context when members speak (~80 tokens per relevant member).
- Tier 2 (L2 DB Storage): Persistent in SQLite (data/group_memories.sqlite3) with timeline event logs
  (timestamp, quote, context) and structured slot cards (interests, jargons, relationships). Exposed via
  MCP tool `query_member_dossier`.

Boundary Rules:
- Cross-group shared personality/XP profiles (same QQ account shares profile across groups).
- Group-scoped private event timelines and gossip (events recorded in group A are filtered by group_id).

issue #11 (section PA) split the engine into four layers; this module is the
facade that keeps every existing import path working:

    water_group.memory_engine.MemoryEngine        # composed class, same API
    water_group.memory_engine.get_memory_engine   # singleton, same semantics
    water_group.memory_engine.MEMORY_SLOTS        # re-exported constants

Layers (see each module's docstring):
    memory_store.py    connection, schema, data-root constants
    memory_profile.py  profiles, seeding, matching, sync, #5 read-side union
    memory_observe.py  events, banter commands, idle digestion
    memory_dossier.py  L1 cards, L2 dossier reads/writes, consolidation
"""
import threading

from .memory_dossier import MemoryDossierMixin
from .memory_observe import MemoryObserveMixin
from .memory_profile import MemoryProfileMixin
from .memory_store import (
    DEFAULT_DB_PATH,
    GENERIC_ALIASES,
    INTEREST_SLOTS,
    MEMORY_SLOTS,
    WG_HOME,
    MemoryStoreMixin,
)

__all__ = [
    "MemoryEngine",
    "get_memory_engine",
    "MemoryStoreMixin",
    "MemoryProfileMixin",
    "MemoryObserveMixin",
    "MemoryDossierMixin",
    "WG_HOME",
    "DEFAULT_DB_PATH",
    "MEMORY_SLOTS",
    "INTEREST_SLOTS",
    "GENERIC_ALIASES",
]


class MemoryEngine(MemoryStoreMixin, MemoryProfileMixin, MemoryObserveMixin, MemoryDossierMixin):
    """Group member dynamic memory engine (facade over the four layers)."""


# Global engine singleton
_ENGINE_INSTANCE = None
_ENGINE_LOCK = threading.Lock()


def get_memory_engine(db_path=None):
    global _ENGINE_INSTANCE
    with _ENGINE_LOCK:
        if _ENGINE_INSTANCE is None:
            _ENGINE_INSTANCE = MemoryEngine(db_path)
            # Auto seed on first load
            _ENGINE_INSTANCE.seed_from_persona()
        elif db_path and str(_ENGINE_INSTANCE.db_path) != str(db_path):
            _ENGINE_INSTANCE = MemoryEngine(db_path)
            _ENGINE_INSTANCE.seed_from_persona()
        return _ENGINE_INSTANCE
