"""Bridge package shim for water-group.

Enables standard Python import conventions:
    import water_group
    import water_group.memory_engine
    from water_group import bootstrap
    from water_group.memory_engine import get_memory_engine
    import water_group.sync_groups as sg

Resolves the Python limitation where hyphenated directory names (water-group)
cannot be imported directly as valid identifiers.

This module is the only water-group file that knows the hyphenated directory
exists: it extends its own __path__ so every water-group file is reachable as
``water_group.<name>`` exactly once. The directory itself is deliberately NOT
added to sys.path — a file reachable under two names would load twice and
split its state, which is how the memory engine singleton used to break.
Modules that double as scripts (bootstrap, sync_groups) add the repo root to
sys.path only when run as __main__.
"""
import importlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_WG_DIR = _ROOT / "water-group"
_wg_str = str(_WG_DIR)

if _wg_str not in __path__:
    __path__.append(_wg_str)

_SUBMODULES = frozenset({
    "memory_engine", "memory_commands", "memory_format",
    "memory_store", "memory_profile", "memory_observe", "memory_dossier",
    "memory_pollution", "bootstrap", "sync_groups",
})
__all__ = sorted(_SUBMODULES)


def __getattr__(name: str):
    """Resolve a water-group submodule lazily (PEP 562).

    Only names in _SUBMODULES resolve, and always under the ``water_group.``
    prefix, so a file is never loaded twice under two names. Lazy resolution
    also keeps optional dependencies (bootstrap needs requests) out of the
    import path of the memory engine.
    """
    if name not in _SUBMODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f"{__name__}.{name}")
    globals()[name] = module
    return module


def __dir__():
    return sorted(set(globals()) | _SUBMODULES)
