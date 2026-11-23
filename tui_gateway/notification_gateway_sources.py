"""Native wake hints for f2; the loaded-f owner supplies the sole census clock.

No payloads, claims or cursors live here. Existing serialized executors read and
ACK their durable stores. The 30s census repairs missed hints and source roster
changes; it says nothing about upstream freshness.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextvars import copy_context
from pathlib import Path
import threading

log = logging.getLogger(__name__)


def enabled():
    # Dedicated task-mandated emergency switch, not general configuration.
    return os.environ.get('HERMES_FACTS_GATEWAY', '1').strip().lower() not in {'0', 'false', 'no', 'off'}  # health: allow HX002 -- explicit per-consumer emergency rollback


def inventory():
    from hermes_cli import kanban_db
    from tui_gateway.kanban_unloaded_gates import _profile_paths
    from tui_gateway.kanban_remote_notices import hub_db_path, ledger_path

    paths = {hub_db_path(), ledger_path(), *[Path(p) for p in _profile_paths()]}
    with kanban_db.pin_first_board_resolution():
        for board in kanban_db.list_boards(include_archived=False):
            paths.add(kanban_db.kanban_db_path(board.get('slug') or kanban_db.DEFAULT_BOARD))
    # Do not subscribe to every child of the home: logging/cache writes would
    # self-wake the executors. DB parent watches already cover replacement and
    # sidecars. Other host-pass inputs keep the shared 30s maintenance census.
    return tuple(sorted(paths)), ()


class Action:
    def __init__(self, owner, wake):
        self.owner, self.wake = owner, wake
        self.pending = False
        self.closed = False

    def due(self, *, legacy_due):
        with self.owner.lock:
            pending = self.pending
            self.pending = False
            return pending and not self.closed

    def close(self):
        with self.owner.lock:
            self.closed = True
            self.owner.actions.discard(self)
        self.owner.event.set()  # source owner, not the caller, withdraws the final native watch


class Sources:
    def __init__(self, event):
        self.event = event
        self.context = copy_context()
        self.lock = threading.Lock()
        self.actions = set()
        self.watch = None
        self.signature = None
        self.dirty = True
        self.retry_at = 0.0
        self.ready = False

    def add(self, wake):
        action = Action(self, wake)
        with self.lock:
            self.actions.add(action)
            self.dirty = True
        self.event.set()
        return action

    def changed(self):
        with self.lock:
            self.dirty = True
        self.event.set()

    def step(self, now, *, reconcile):
        with self.lock:
            if not self.actions:
                old, self.watch = self.watch, None
                self.signature = None
                self.ready = False
                self.retry_at = 0.0
            else:
                old = None
            dirty = self.dirty
            self.dirty = False
            active = bool(self.actions)
        if old is not None:
            old.close()
        if not active:
            return
        refresh = reconcile or now >= self.retry_at
        if refresh or (dirty and self.watch is not None):
            try:
                from hermes_cli.fact_source_watch import NativeSourceWatch
                signature = self.context.run(inventory) if refresh else self.signature
                if signature != self.signature:
                    # Arm successor before withdrawing predecessor; no watch gap.
                    watch = NativeSourceWatch(signature[0], self.changed, directories=signature[1])
                    old, self.watch = self.watch, watch
                    self.signature = signature
                    if old is not None:
                        old.close()
                if self.watch is not None:
                    self.watch.check()
                if refresh:
                    if not self.ready:
                        log.info('gateway notification native sources ready (%d paths; shared 30s census)',
                                 len(signature[0]))
                    self.ready = True
                    self.retry_at = float('inf')
            except NotImplementedError:
                self.ready = False
                self.retry_at = float('inf')  # unsupported platform: census only, not a 1s retry loop
                log.warning('gateway native notification hints unsupported; shared 30s census only')
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                self.ready = False
                self.retry_at = now + 1.0
                log.warning('gateway notification source readiness unavailable: %s', exc)
            dirty = True
        if dirty or reconcile:
            with self.lock:
                actions = tuple(self.actions)
                for action in actions:
                    action.pending = True
            for action in actions:
                try:
                    action.wake()  # wake only, never execute under the source lock
                except RuntimeError:
                    # A watcher may have closed its asyncio loop during fanout.
                    # Retire only that membership; other durable executors still wake.
                    action.close()

    def close(self):
        if self.watch is not None:
            self.watch.close()
        with self.lock:
            for action in self.actions:
                action.closed = True
            self.actions.clear()
