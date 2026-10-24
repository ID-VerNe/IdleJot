"""Build the plugin instances for one chat.

Separate from ``__init__`` so importing the static surface does not drag the
feature modules (and their dependencies) into every process that only needs the
scope flag names.
"""
from .base import AppPlugins


def build(chat):
    """Instantiate every app plugin for one chat. Order is registration order."""
    from .language import LanguagePlugin
    from .reactions import ChatReactions
    return AppPlugins(chat, (LanguagePlugin, ChatReactions))
