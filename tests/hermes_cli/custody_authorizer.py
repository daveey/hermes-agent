"""Test-only SQLite custody-write guard; no SQL/source text matching. [daveey]

The authorizer observes SQLite's resolved table/action, including dynamic SQL and triggers.
Statement caching is disabled so a different caller cannot reuse an earlier preparation.
This is runtime coverage, not proof that unexecuted paths have been exercised.
"""
from __future__ import annotations

import sqlite3
import sys

from hermes_cli import kanban_sync_capture_sites as inventory

TABLES = frozenset({"kanban_sync_outbox", "kanban_sync_parent_envelopes"})
ACTIONS = {sqlite3.SQLITE_INSERT: "INSERT", sqlite3.SQLITE_UPDATE: "UPDATE", sqlite3.SQLITE_DELETE: "DELETE"}


def caller():
    frame = sys._getframe(1)
    while frame:
        module = frame.f_globals.get("__name__", "")
        if module != __name__:
            return module, frame.f_code.co_qualname
        frame = frame.f_back
    return "<unknown>", "<unknown>"


class _RememberConnection:
    def execute(self, *args, **kwargs):
        try:
            return super().execute(*args, **kwargs)
        finally:
            after = getattr(self, "_custody_after", None)
            if after:
                after()

    def executemany(self, *args, **kwargs):
        try:
            return super().executemany(*args, **kwargs)
        finally:
            after = getattr(self, "_custody_after", None)
            if after:
                after()

    def executescript(self, *args, **kwargs):
        try:
            return super().executescript(*args, **kwargs)
        finally:
            after = getattr(self, "_custody_after", None)
            if after:
                after()


class CustodyGuard:
    def __init__(self):
        self.observed = []
        self.violations = []
        self.triggers = {}
        self.trigger_definitions = {}
        self.pending = set()
        self.allowed = {
            ("hermes_cli." + file[:-3], function, verb, table)
            for (file, function, verb, table) in inventory.SITES
            if file.startswith("kanban_")
        }
        # _insert_trigger returns SQL; install_triggers submits that audited generated trigger.
        self.allowed.add(("hermes_cli.kanban_db_sync", "install_triggers", "INSERT", "kanban_sync_outbox"))
        for (file, function, verb, _), (_, reaches) in inventory.DYNAMIC_SITES.items():
            if reaches:
                self.allowed.update(("hermes_cli." + file[:-3], function, verb, table) for table in TABLES)

    def install(self, connection, database):
        external = None
        filename = connection.execute("PRAGMA database_list").fetchone()[2]
        database_key = filename if filename else object()
        # A copied/restored fixture can retain provenance only for a byte-identical trigger
        # definition actually created under this guard, never by trigger name alone.
        for schema in ("main", "temp"):
            for name, sql in connection.execute(f"SELECT name, sql FROM {schema}.sqlite_master WHERE type='trigger'"):
                identity = self.trigger_definitions.get(sql)
                if identity is not None:
                    self.triggers[database_key, schema, name] = identity

        def remember():
            if database_key not in self.pending:
                return
            self.pending.discard(database_key)
            for schema in ("main", "temp"):
                for name, sql in sqlite3.Connection.execute(
                        connection, f"SELECT name, sql FROM {schema}.sqlite_master WHERE type='trigger'"):
                    identity = self.triggers.get((database_key, schema, name))
                    if identity is not None:
                        self.trigger_definitions[sql] = identity

        connection._custody_after = remember

        def authorize(action, table, column, schema, trigger):
            identity = caller()
            key = (database_key, schema, table)
            if action in (sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_TRIGGER):
                self.triggers[key] = identity
                self.pending.add(database_key)
            if action in ACTIONS and table in TABLES:
                if trigger is not None:
                    creator = self.triggers.get((database_key, schema, trigger), ("<unknown-trigger>", trigger))
                    # Fixture-created triggers cannot lend test exemption to a production caller.
                    if not creator[0].startswith("tests."):
                        identity = creator
                entry = (*identity, ACTIONS[action], table)
                self.observed.append(entry)
                # Test fixture setup is not a production writer. Production frames are never skipped
                # to find an allowed ancestor: an unlisted helper stays unlisted.
                if not identity[0].startswith("tests.") and entry not in self.allowed:
                    self.violations.append(entry)
                    return sqlite3.SQLITE_DENY
            return external(action, table, column, schema, trigger) if external else sqlite3.SQLITE_OK

        def replace(authorizer_callback):
            nonlocal external
            external = authorizer_callback
            sqlite3.Connection.set_authorizer(connection, authorize)

        connection.set_authorizer = replace
        sqlite3.Connection.set_authorizer(connection, authorize)

    def factory(self, real_connect):
        guard = self

        class Guarded(_RememberConnection, sqlite3.Connection):
            pass

        def connect(database, *args, **kwargs):
            # Positional cached_statements/factory are deliberately unsupported rather than silently
            # leave a connection unguarded. The Board opens connections with keyword options.
            if len(args) > 4:
                raise TypeError("custody guard requires keyword factory/cached_statements")
            base = kwargs.pop("factory", sqlite3.Connection)
            cls = base if issubclass(base, _RememberConnection) else (
                Guarded if base is sqlite3.Connection else type("CustodyGuarded", (_RememberConnection, base), {}))
            kwargs["factory"] = cls
            kwargs["cached_statements"] = 0
            connection = real_connect(database, *args, **kwargs)
            guard.install(connection, database)
            return connection

        return connect
