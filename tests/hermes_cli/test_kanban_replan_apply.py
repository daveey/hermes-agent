"""Portable replan publication preserves original acceptance and transactional custody."""
from contextlib import contextmanager
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_auto_replan as replan
from hermes_cli import kanban_replan_apply as publication
from tests.hermes_cli import test_kanban_auto_replan as helpers

kanban_home = helpers.kanban_home


def test_publication_is_append_only_and_idempotent(kanban_home):
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn)
        original = kb.get_task(conn, tid).body
        parent = replan.hold(conn, tid)
        assert kb.complete_task(conn, parent, result='Measure the failing boundary, then implement and verify the smallest fix.')
        kb.recompute_ready(conn)
        assert replan.claim_refused(conn, tid)
        assert replan.hold(conn, tid) is None
        task = kb.get_task(conn, tid)
        assert task.body.startswith(original + '\n\n')
        assert 'Measure the failing boundary' in task.body
        assert len(helpers.helpers._events(conn, tid, publication.APPLIED)) == 1
        assert not replan.claim_refused(conn, tid)
        assert replan.hold(conn, tid) is None
        assert kb.get_task(conn, tid).body == task.body


def test_partial_live_file_reads_done_handoff_from_replica(kanban_home, monkeypatch):
    from hermes_cli import kanban_foreign_card as foreign
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn)
        parent = replan.hold(conn, tid)
        assert kb.complete_task(conn, parent, result='Portable plan from worker completion')
        replica = sqlite3.connect(':memory:')
        replica.row_factory = sqlite3.Row
        conn.backup(replica)
        assert kb.delete_task(conn, parent)
        @contextmanager
        def open_replica(*a):
            yield replica
        monkeypatch.setattr(foreign, 'open_replica', open_replica)
        try:
            kb.recompute_ready(conn)
            assert replan.hold(conn, tid) is None
            assert 'Portable plan from worker completion' in kb.get_task(conn, tid).body
            assert not replan.claim_refused(conn, tid)
        finally:
            replica.close()


def test_publication_failure_rolls_back_body_comment_and_reset(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn)
        parent = replan.hold(conn, tid)
        assert kb.complete_task(conn, parent, result='Plan preserved across retry')
        kb.recompute_ready(conn)
        original = kb.get_task(conn, tid).body
        comments = len(kb.list_comments(conn, tid))
        append = kb._append_event
        def failed(conn, tid, kind, *a, **kw):
            if kind == publication.APPLIED:
                raise ValueError('injected after body write')
            return append(conn, tid, kind, *a, **kw)
        monkeypatch.setattr(kb, '_append_event', failed)
        with pytest.raises(ValueError, match='injected after body write'):
            replan.hold(conn, tid)
        assert kb.get_task(conn, tid).body == original
        assert len(kb.list_comments(conn, tid)) == comments
        assert not publication.published(conn, tid, parent)
        assert replan.claim_refused(conn, tid)
