"""App-side plugin protocol: one socket for MCP tools and live-event features.

The daemon has its own protocol in ``water-group/daemon/plugins`` for ``#``
commands. This is the other half: MCP tools, per-connection scopes and live
OneBot events. The two stay separate on purpose - different hosts, different
lifetimes, different authorization models. Merging them would force one to
pretend it has the other's guarantees.

Three rules keep this honest:

1. **A hook exists only when something uses it this round.** No reserved
   methods for a feature that has not migrated yet; an unused hook is a
   promise nobody keeps (see the ``Host.store()`` debt on issue #4).
2. **Plugin methods forward.** Business logic stays in the feature module.
   ``ChatReactions.policy/guard/execute/...`` are untouched so the end-to-end
   gates keep asserting the same behaviour.
3. **The host never branches on a plugin name.** Anything name-shaped outside
   this package is a bug: it means the socket is missing a hook.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence


class AppPlugin:
    """One app-side capability. Subclasses fill the class attributes.

    Instances are built once per ``MCPChat`` and live as long as it does, so
    ``__init__`` must not open sockets or start threads - the same rule the
    daemon protocol learned in issue #4 PC.
    """

    name: str = ''
    # Class-level so import-time constants (``CHAT_TOOLS``) can read them
    # without an instance.
    tool_names: frozenset[str] = frozenset()
    scope_flag: str | None = None
    expression: str | None = None
    # Event name this plugin's wake signal reports to the wait loop. Defaults
    # to the plugin name; a feature whose wire event differs declares it here
    # (the host must not know the literal).
    wake_event: str | None = None

    def __init__(self, chat: Any):
        self.chat = chat
        if not self.wake_event:
            self.wake_event = self.name

    # -- switch ---------------------------------------------------------
    def enabled(self) -> bool:
        """False means the whole feature is off: no tool, no event, no wake."""
        return True

    def scope(self, scope: Mapping, row: Mapping) -> Mapping:
        """Effective scope for this batch. A plugin may clear its own flag."""
        return scope

    # -- tools ----------------------------------------------------------
    def schemas(self, tool: Any, scope: Mapping) -> Sequence[dict]:
        return ()

    def dispatch(self, grant: Mapping, name: str, args: Mapping, cancel: Any) -> dict:
        """Execute one tool of this plugin. ``args`` already has the key.

        Called before ``permission()`` when the plugin has no ``expression``:
        a plan-independent tool authorizes itself (its own binding/scope checks),
        exactly like a scopeless one. Expression tools instead flow through the
        reply plan, where the bubble dispatch supplies the name again.
        """
        raise NotImplementedError(self.name)

    def rejected(self, args: Mapping, code: str, note: str) -> dict:
        return dict(state='REJECTED', session_id=args.get('session_id'),
                    error_code=code, note=note)

    def deny(self, grant: Mapping, target: Any, row: Mapping) -> tuple[str, str] | None:
        """Pre-flight refusal for this plugin's expression, or None."""
        return None

    def exempt_cancel(self) -> bool:
        """True: a cancelled call must not discard this plugin's durable receipt."""
        return False

    def owns_receipt(self) -> bool:
        """True: the plugin decides its own revision semantics."""
        return False

    # -- prompt and batch data ------------------------------------------
    def hint(self, scope: Mapping) -> str | None:
        return None

    def batch(self, grant: Mapping, row: Mapping) -> dict:
        """This batch's own data. Computed before the packet exists."""
        return {}

    def wake_hint(self, data: Mapping) -> str | None:
        """Text for this batch when the plugin is the reason it was delivered."""
        return None

    def brief(self) -> str | None:
        """One-line reminder for the compact wait payload."""
        return None

    def decorate(self, packet: dict, data: Mapping) -> None:
        """Write this plugin's batch fields. The plugin owns the field names."""

    def stamp(self) -> str | None:
        """Connection epoch written into each inbox payload, or None."""
        return None

    # -- events ---------------------------------------------------------
    def receive(self, event: Mapping) -> bool:
        """True means the event is consumed and must not reach other handlers."""
        return False

    def wake(self, row: Mapping) -> list:
        """Pending items that should wake a waiting call. Bounded, no rebuild."""
        return []

    def reset(self) -> None:
        """The live connection changed. Drop anything derived from it."""

    # -- lifecycle and stored messages ------------------------------------
    def resume(self) -> None:
        """The live service resumed; restart anything this plugin paused."""

    def close(self) -> None:
        """The service is suspending; stop threads, keep durable data."""

    def suspend(self) -> None:
        """Drop connection-derived state, inside the host lock, once down.

        The in-lock counterpart of ``close()``: the host sets its own
        unavailable flag first, then fans this out. Unlike ``reset()`` a
        suspend has no successor connection, so a plugin whose epoch marks
        live-connection identity keeps it -- the epoch rotates only when a
        new connection takes over (``reset``), never here.
        """

    def on_stored(self, row: Mapping, payload: Mapping) -> None:
        """A new message row was stored for this session."""

    def stopped(self, sid: str) -> None:
        """One chat session stopped; drop everything derived from it."""

    # -- descriptors -------------------------------------------------------
    # Descriptor hooks return the feature's own wire contract as data; the
    # host keeps the control flow and never learns the field names or texts.
    def wake_signal(self, grant: Mapping, row: Mapping) -> dict | None:
        """A wait-loop wake owned by this plugin, or None.

        Keys: ``kind`` (the wire event name), ``ready``, ``payload`` (the
        result field's value), ``next_call`` and ``note``.
        """
        return None

    def plan_context(self, p: Mapping) -> dict | None:
        """Plan-context descriptor for one READY plan, or None.

        Keys: ``field`` (result field name), ``context`` (its value) plus the
        optional overrides ``phase`` / ``next`` / ``next_call`` (a full
        replacement for the host's own follow-up call).
        """
        return None

    def select(self, grant: Mapping, p: Mapping, expression_ids: Sequence[str]) -> tuple[str, str] | None:
        """Apply an inline expression selection. ``(code, note)`` on refusal.

        A plugin that does not own this plan's context must not refuse;
        success is None. Only one plugin answers a given plan today.
        """
        return None

    def handoff(self, grant: Mapping, data: Mapping, name: str, args: Mapping, cancel: Any) -> dict | None:
        """Post-dispatch chat handoff for this call, or None to leave data.

        ``name`` lets one plugin answer for several of its tools; returning a
        dict replaces the tool result (the host then revalidates the session).
        """
        return None

    def durable(self, data: Mapping, name: str) -> dict | None:
        """A stage result that outlives a cancelled or failed handoff.

        Keys: ``receipt`` (the durable result as it survives a failed handoff
        -- follow-up fields already dropped), ``cancel_note`` and
        ``failure_note`` -- what the host echoes when either happens.
        """
        return None


class AppPlugins:
    """Built instances, keyed by name. The only door the host uses."""

    def __init__(self, chat: Any, classes: Iterable[type[AppPlugin]] = ()):
        self.chat = chat
        self.instances: dict[str, AppPlugin] = {}
        for cls in classes:
            self.instances[cls.name] = cls(chat)

    def get(self, name: str) -> AppPlugin | None:
        return self.instances.get(name)

    def all(self) -> tuple[AppPlugin, ...]:
        return tuple(self.instances.values())

    def tool_owner(self, name: str) -> AppPlugin | None:
        for plugin in self.instances.values():
            if name in plugin.tool_names:
                return plugin
        return None

    def expression_owner(self, kind: str) -> AppPlugin | None:
        for plugin in self.instances.values():
            if plugin.expression == kind:
                return plugin
        return None

    def receive(self, event: Mapping) -> bool:
        for plugin in self.instances.values():
            if plugin.receive(event):
                return True
        return False

    def wake(self, row: Mapping) -> tuple[str | None, list]:
        for plugin in self.instances.values():
            items = plugin.wake(row)
            if items:
                return plugin.name, items
        return None, []

    def scope(self, scope: Mapping, row: Mapping) -> Mapping:
        for plugin in self.instances.values():
            scope = plugin.scope(scope, row)
        return scope

    def batch(self, grant: Mapping, row: Mapping) -> dict[str, dict]:
        return {plugin.name: data for plugin in self.instances.values()
                if (data := plugin.batch(grant, row))}

    def decorate(self, packet: dict, data: Mapping[str, dict]) -> None:
        for plugin in self.instances.values():
            if plugin.name in data:
                plugin.decorate(packet, data[plugin.name])

    def hints(self, scope: Mapping) -> list[str]:
        return [text for plugin in self.instances.values()
                if (text := plugin.hint(scope))]

    def wake_hint(self, data: Mapping[str, dict]) -> str | None:
        for plugin in self.instances.values():
            if plugin.name in data and (text := plugin.wake_hint(data[plugin.name])):
                return text
        return None

    def brief(self, event: str) -> str | None:
        plugin = self.instances.get(event)
        return plugin.brief() if plugin else None

    def stamps(self) -> dict[str, str]:
        return {plugin.name: value for plugin in self.instances.values()
                if (value := plugin.stamp())}

    def reset(self) -> None:
        for plugin in self.instances.values():
            plugin.reset()

    def resume(self) -> None:
        for plugin in self.instances.values():
            plugin.resume()

    def close(self) -> None:
        for plugin in self.instances.values():
            plugin.close()

    def suspend(self) -> None:
        for plugin in self.instances.values():
            plugin.suspend()

    def on_stored(self, row: Mapping, payload: Mapping) -> None:
        for plugin in self.instances.values():
            plugin.on_stored(row, payload)

    def stopped(self, sid: str) -> None:
        for plugin in self.instances.values():
            plugin.stopped(sid)

    def wake_signal(self, grant: Mapping, row: Mapping) -> dict | None:
        for plugin in self.instances.values():
            signal = plugin.wake_signal(grant, row)
            if signal is not None:
                return signal
        return None

    def plan_context(self, p: Mapping) -> dict | None:
        for plugin in self.instances.values():
            patch = plugin.plan_context(p)
            if patch is not None:
                return patch
        return None

    def select(self, grant: Mapping, p: Mapping, expression_ids: Sequence[str]) -> tuple[str, str] | None:
        for plugin in self.instances.values():
            refusal = plugin.select(grant, p, expression_ids)
            if refusal is not None:
                return refusal
        return None
