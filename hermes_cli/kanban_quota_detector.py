"""Durable local contradiction counter. No board access from managed workers.

Workers/recovery call :func:`record_contradiction` (local SQLite only, fail-soft).
The dispatcher tick calls :func:`drain_owner`, which is a no-op in any worker or
fenced descendant context, and writes through ``kanban_db.create_task`` /
``add_comment`` (the supported canonical path).

Schema (``<kanban_home>/quota_contradictions.db``):
  observations(scope, at, accounts)  accounts = JSON [{"id": pool row id, "cooldown_until": epoch|null}]
  reports(scope PK, at, payload, receipt)  receipt NULL = pending owner delivery
Only pool row IDs and cooldown ends are stored: never tokens, emails or labels.
The report key identifies host/provider/model.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
import time
from contextlib import closing

try:
    import fcntl as _fcntl
except ImportError:  # Windows: drain_owner skips (see there)
    _fcntl = None

log = logging.getLogger(__name__)
_NO_FLOCK_LOGGED = False
WINDOW_SECONDS = 1800
THRESHOLD = 3
_ID = re.compile(r'[A-Za-z0-9_.:-]{1,64}')


def clean_accounts(accounts):
    """Keep only (row id, cooldown end); anything else (tokens, emails) is dropped."""
    out = []
    for a in accounts or ():
        if isinstance(a, str):
            a = {'id': a}
        aid = a.get('id') if isinstance(a, dict) else None
        if not isinstance(aid, str) or not _ID.fullmatch(aid):
            continue
        cd = a.get('cooldown_until')
        ok = isinstance(cd, (int, float)) and not isinstance(cd, bool)
        out.append({'id': aid, 'cooldown_until': float(cd) if ok else None})
    return out


class Detector:
    def __init__(self, path, *, timeout=0.25):
        self.path = path
        self.timeout = timeout

    def _connect(self):
        # quota_contradictions.db: local evidence/outbox, never a board; board writes
        # go through the dispatcher owner's supported create_task/add_comment calls.
        conn = sqlite3.connect(self.path, timeout=self.timeout)  # noqa: kanban-lockguard (not a board)
        conn.execute('CREATE TABLE IF NOT EXISTS observations (scope TEXT, at REAL, accounts TEXT)')
        if 'accounts' not in [r[1] for r in conn.execute('PRAGMA table_info(observations)')]:
            conn.execute('ALTER TABLE observations ADD COLUMN accounts TEXT')
        conn.execute('CREATE TABLE IF NOT EXISTS reports '
                     '(scope TEXT PRIMARY KEY, at REAL, payload TEXT, receipt TEXT)')
        conn.commit()
        return conn

    def record(self, host, provider, model, *, now, accounts=()):
        """Record a proven contradiction (including one successfully healed).

        One pending report or delivered report per rolling window per scope.
        BEGIN IMMEDIATE serializes threshold checks across worker processes.
        """
        scope = 'quota-contradiction:' + hashlib.sha256(
            json.dumps([host, provider, model], separators=(',', ':')).encode()).hexdigest()
        with closing(self._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM observations WHERE at < ?', (now - WINDOW_SECONDS,))
            conn.execute('INSERT INTO observations VALUES (?, ?, ?)',
                         (scope, now, json.dumps(clean_accounts(accounts))))
            count = conn.execute('SELECT COUNT(*) FROM observations WHERE scope=? AND at<=?',
                                 (scope, now)).fetchone()[0]
            old = conn.execute('SELECT at, receipt FROM reports WHERE scope=?', (scope,)).fetchone()
            if count < THRESHOLD or (old and (old[1] is None or now - old[0] < WINDOW_SECONDS)):
                return None
            merged = {}
            for (raw,) in conn.execute('SELECT accounts FROM observations WHERE scope=? AND at<=?',
                                       (scope, now)):
                for a in json.loads(raw or '[]'):
                    known = [v for v in (a['cooldown_until'], merged.get(a['id'])) if v is not None]
                    merged[a['id']] = max(known) if known else None
            report = dict(key=scope, host=host, provider=provider, model=model, count=count,
                          window_seconds=WINDOW_SECONDS, observed_at=now,
                          accounts=[{'id': k, 'cooldown_until': v} for k, v in sorted(merged.items())])
            conn.execute('INSERT OR REPLACE INTO reports VALUES (?, ?, ?, NULL)',
                         (scope, now, json.dumps(report)))
            return report

    def pending(self):
        with closing(self._connect()) as conn:
            return [json.loads(row[0]) for row in conn.execute(
                'SELECT payload FROM reports WHERE receipt IS NULL ORDER BY at')]

    def deliver(self, create_or_comment):
        """Owner-only drain; callback must dedup by key, including uncertain replies.

        Failed/unknown submissions remain pending. The owner callback, not this
        local outbox, decides whether an existing card is still open.
        """
        delivered = 0
        for report in self.pending():
            try:
                receipt = create_or_comment(report)
                if not isinstance(receipt, str) or not receipt:
                    continue
                with closing(self._connect()) as conn, conn:
                    changed = conn.execute('UPDATE reports SET receipt=? WHERE scope=? AND payload=? '
                                           'AND receipt IS NULL',
                                           (receipt, report['key'], json.dumps(report))).rowcount
                delivered += changed
            except Exception:  # health: allow BLE001 -- owner reporting is best effort; retain pending evidence
                log.warning('quota contradiction owner delivery deferred')
        return delivered


def default_path():
    """Profile-local evidence store, shared with this home's dispatcher."""
    from hermes_cli.kanban_db import kanban_home
    return str(kanban_home() / 'quota_contradictions.db')


def record_contradiction(provider, model, accounts=(), *, host=None, now=None, path=None):
    """Worker-safe entry point: local SQLite only; never raises. Returns a new report or None."""
    try:
        return Detector(path or default_path()).record(
            host or socket.gethostname(), provider, model,
            now=time.time() if now is None else now, accounts=accounts)
    except Exception:  # health: allow BLE001 -- evidence is best effort, never break the worker
        log.warning('quota contradiction record failed', exc_info=True)
        return None


def _body(report):
    lines = [f"Quota contradiction x{report['count']} within {report['window_seconds']}s on "
             f"host={report['host']} provider={report['provider']} model={report['model']}.",
             'Accounts (pool row id: cooldown end epoch):']
    lines += [f"- {a['id']}: {a['cooldown_until']}" for a in report['accounts']] or ['- (none reported)']
    return '\n'.join(lines) + f"\nreport: {report['key']}@{int(report['observed_at'])}"


def create_or_comment(conn, report):
    """Owner-side canonical writer: comment on the open card for this scope, else create one.

    The idempotency key embeds the report time so a finished card never swallows a later report.
    """
    from hermes_cli import kanban_db as kb
    marker = f"{report['key']}@{int(report['observed_at'])}"
    row = conn.execute("SELECT id FROM tasks WHERE idempotency_key LIKE ? AND status NOT IN "
                       "('done','archived') ORDER BY created_at DESC LIMIT 1",
                       (report['key'] + '@%',)).fetchone()
    if row:
        tid = row[0]
        if not any(marker in (c.body or '') for c in kb.list_comments(conn, tid)):
            kb.add_comment(conn, tid, 'quota-detector', _body(report))
        return tid
    return kb.create_task(
        conn, title=f"Quota contradiction: {report['provider']}/{report['model']} on {report['host']}",
        body=_body(report), created_by='quota-detector', triage=True, idempotency_key=marker)


def drain_owner(conn, *, detector=None, writer=None):
    """Owner-side drain, called from the dispatcher tick. Fail-soft; 0 in worker/fenced contexts."""
    try:
        if os.environ.get('HERMES_KANBAN_TASK'):
            return 0
        from agent.delegation_context import is_delegated_child_process_context
        if is_delegated_child_process_context():
            return 0
        det = detector or Detector(default_path())
        if not det.pending():
            return 0
        if _fcntl is None:
            # No flock (Windows): one owner-side drainer cannot be guaranteed, and create_task's
            # idempotency check is not atomic. Keep the reports pending rather than warn every tick.
            global _NO_FLOCK_LOGGED
            if not _NO_FLOCK_LOGGED:
                _NO_FLOCK_LOGGED = True
                log.debug('quota contradiction drain skipped: no fcntl.flock on this platform')
            return 0
        # one drainer at a time: create_task's idempotency check is not atomic
        with open(str(det.path) + '.drain.lock', 'ab') as lock:
            try:
                _fcntl.flock(lock, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            except OSError:
                return 0
            return det.deliver(lambda r: (writer or create_or_comment)(conn, r))
    except Exception:  # health: allow BLE001 -- maintenance must not break dispatch
        log.warning('quota contradiction drain failed', exc_info=True)
        return 0
