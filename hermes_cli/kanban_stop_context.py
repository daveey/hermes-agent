"""Live owner token for the stop-only ``kanban_stop_ended`` control. [daveey]

Plain session ContextVars and a non-None turn event are inherited by copied contexts, threads
and subprocess environments, so they are not proof that the caller is the owning chat's live
turn. This token is: the interactive turn runner binds one around its actual tool execution
(:func:`bound`), every copied context shares the SAME mutable object, and closing it at turn
end, replacement or cancellation invalidates every copy at once. Nothing exports it to the
environment and no tool argument can mint one.

Lock order is fixed: this token's lock (bounded), then the board's ``BEGIN IMMEDIATE``.
Closing takes the same token lock, so a cancellation either lands before the commit (refused)
or waits for it to finish; never the inverse order.
"""
from __future__ import annotations

import itertools
import os
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar

from hermes_cli.kanban_stop_ended import StopRefused

INTERACTIVE_SOURCES = frozenset({'desktop', 'cli', 'tui', 'acp', 'gui'})
_ROUTING = ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_WORKER_TASK', 'HERMES_DELEGATED_CHILD_CONTEXT', 'HERMES_CRON_SESSION')
LOCK_TIMEOUT_S = 5.0
_GENERATION = itertools.count(1)
_CURRENT: ContextVar['OwnerToken | None'] = ContextVar('hermes_stop_only_owner_token', default=None)


def _session_source(session_id: str):
    from hermes_constants import get_hermes_home
    from hermes_cli.kanban_session_provenance import _read_sources
    sources, readable = _read_sources(str(get_hermes_home() / 'state.db'), [session_id])
    return sources.get(session_id) if readable else None


def _routing_context() -> bool:
    from gateway.session_context import get_session_env
    return any(os.environ.get(k) or get_session_env(k, '') for k in _ROUTING)


class OwnerToken:
    """One interactive turn's stop-only authority. Mutable, shared by copies, closes permanently."""

    def __init__(self, session_id: str, gateway: str, profile: str, *, source_reader=_session_source):
        self.session_id, self.gateway, self.profile = session_id, gateway, profile or 'default'
        self.generation = next(_GENERATION)
        self._lock = threading.Lock()
        self._open = True
        self._source_reader = source_reader

    @property
    def open(self) -> bool:
        return self._open

    def close(self) -> None:
        with self._lock:  # waits for an in-flight commit; afterwards every copy refuses
            self._open = False

    def identity(self) -> dict:
        return {'gateway': self.gateway, 'profile': self.profile, 'session': self.session_id,
                'generation': self.generation}

    @contextmanager
    def held(self):
        if not self._lock.acquire(timeout=LOCK_TIMEOUT_S):
            raise StopRefused('stop_ended_owner_busy', 'owner token busy; retry the same request')
        try:
            if not self._open or _CURRENT.get() is not self:
                raise StopRefused('stop_ended_owner_required', 'owner token closed or not this execution\'s token')
            if _routing_context():
                raise StopRefused('stop_ended_owner_required', 'machine, cron, Kanban or delegated context')
            # Fresh, positive provenance at commit time, not a cached classification.
            if self._source_reader(self.session_id) not in INTERACTIVE_SOURCES:
                raise StopRefused('stop_ended_owner_required', 'session is not a local interactive chat')
            yield {k: v for k, v in self.identity().items() if k != 'generation'}
        finally:
            self._lock.release()

    def verify_link(self, conn, task_id: str, who: dict) -> None:
        """Inside the write transaction: exactly this chat holds the card's unreleased ``created`` link."""
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
            raise StopRefused('stop_ended_owner_required', 'board has no chat ownership links')
        owners = {(r['chat_gateway'], r['chat_profile'] or 'default', r['chat_session']) for r in conn.execute(
            "SELECT chat_gateway,chat_profile,chat_session FROM task_chat_links "
            "WHERE task_id=? AND kind='created' AND released=0", (task_id,))}
        if owners != {(who['gateway'], who['profile'], who['session'])}:
            raise StopRefused('stop_ended_owner_required',
                              'caller is not the single unreleased owning chat of this card')


def current() -> OwnerToken:
    token = _CURRENT.get()
    if token is None or not token.open:
        raise StopRefused('stop_ended_owner_required', 'no live owning-chat turn is bound to this execution')
    return token


@contextmanager
def bound(session_id: str, *, gateway: str, profile: str, source: str, source_reader=_session_source):
    """Bind around one interactive turn's tool execution; closes permanently on exit (any exit)."""
    token = None
    if (source in INTERACTIVE_SOURCES and isinstance(session_id, str)
            and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', session_id) and gateway):
        token = OwnerToken(session_id, gateway, profile, source_reader=source_reader)
    reset = _CURRENT.set(token)
    try:
        yield token
    finally:
        if token is not None:
            token.close()
        _CURRENT.reset(reset)
