"""App-side plugin surface: protocol, static names, and the loader.

Importing this package must stay cheap and acyclic: ``mcp_access`` reads the
scope flag names from here, and the hot-reload closure walker follows every
import - including the ones inside function bodies. The feature modules are
therefore imported by ``loader``, not here.
"""
from .base import AppPlugin, AppPlugins
from .spec import EXPRESSIONS, OPEN_WORLD, SCOPE_BY_TOOL, SCOPE_FLAGS, TOOL_NAMES

__all__ = ['AppPlugin', 'AppPlugins',
           'EXPRESSIONS', 'OPEN_WORLD', 'SCOPE_BY_TOOL', 'SCOPE_FLAGS', 'TOOL_NAMES']
