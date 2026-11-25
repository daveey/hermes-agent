"""Real-SQL regressions adapted from independent review 6057624286 probes."""
from contextlib import contextmanager
import hashlib
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_home_generation as generation, kanban_home_transfer as home
from hermes_cli import kanban_shared_blockers as blockers, kanban_db_dispatch_quota as quota
from hermes_cli import kanban_sync_attachments as attachments
from hermes_cli import kanban_spend_reviewer as reviewer, kanban_spend_review_request as journal
from hermes_cli import kanban_spend_caused_review as caused, kanban_spend_decision as decision
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_sync_support as support

QUOTE = 'Finish the remaining integration and verify the acceptance tests'


@pytest.fixture
def consumer_board(tmp_path, monkeypatch):
    with contextmanager(support.synced_conn)(support.kanban_home(tmp_path, monkeypatch)) as c:
        with kb.write_txn(c):
            c.execute("UPDATE kanban_sync_state SET gateway_id='worker-a3',mode='primary'")
        yield c


def card(c, tid, status='blocked', kind=None):
    with kb.write_txn(c):
        c.execute("INSERT INTO tasks(id,title,status,block_kind,created_at,created_on,sync_version) "
                  "VALUES (?,?,?,?,1,'worker-a3',7)", (tid, tid, status, kind))
    return tid


def freeze(c, tid, phase):
    with kb.write_txn(c):
        if phase in ('pending', 'completed'):
            prior = c.execute('SELECT version FROM kanban_home_generations1 WHERE task_id=?', (tid,)).fetchone()
            generation.mirror_feed(c, tid, {'home_generation': 'fixture-generation-0001',
                'home_transfer_pending': None if phase == 'completed' else 'fixture-generation-0001'}, prior[0] + 1 if prior else 8)
        elif phase in ('prepared', 'aborted', 'malformed'):
            c.execute('DELETE FROM kanban_sync_outbox')
            record = dict(task=tid, source='worker-a3', destination='hub', nonce='fixture-transfer-0001', expected_version=7)
            home.prepare(c, tid, source=record['source'], destination=record['destination'],
                         nonce=record['nonce'], expected_version=7)
            if phase == 'aborted':
                home.abort_source(c, {**record, 'phase': 'aborted', 'rows': []})
            if phase == 'malformed':
                c.execute('DROP TRIGGER IF EXISTS home_prepared1_marker_update')
                c.execute("UPDATE kanban_home_prepared1 SET source='' WHERE task_id=?", (tid,))


def wall(c, tid, name):
    import json
    import time
    with kb.write_txn(c):
        kb._append_event(c, tid, 'blocked', {'reason': 'wall', 'blocker_id': name})
        c.execute("INSERT INTO task_runs(task_id,started_at,ended_at,status,outcome,metadata) "
                  "VALUES (?,?,?,'blocked','blocked',?)", (tid, int(time.time())-20,
                  int(time.time())-10, json.dumps({'blocker_id': name})))


@pytest.mark.parametrize('phase', ['prepared', 'pending', 'malformed', 'aborted', 'ordinary'])
def test_frozen_ready_does_not_suppress_shared_probe(consumer_board, phase):
    c = consumer_board
    card(c, 't_A', 'ready'); card(c, 't_B')
    wall(c, 't_A', 'wall'); wall(c, 't_B', 'wall')
    freeze(c, 't_A', phase)
    result = blockers.probe_stranded_blockers(c, interval=0)
    assert result == ([] if phase in ('ordinary', 'aborted') else ['t_B'])


@pytest.mark.parametrize('which', ['quota', 'shared', 'cleanup'])
@pytest.mark.parametrize('race', [False, True])
def test_followup_writer_race_keeps_healthy_sweep(consumer_board, monkeypatch, which, race):
    c = consumer_board
    for tid in ('t_A', 't_B'):
        card(c, tid, 'triage' if which == 'cleanup' else 'blocked', 'capability')
    if which == 'quota':
        card(c, 't_proof', 'done')
        with kb.write_txn(c):
            for tid in ('t_A', 't_B'):
                kb._append_event(c, tid, 'blocked', {'reason': 'Held in ready by the respawn guard '
                    '(blocker_auth) for 30 min without running. Last error: quota exhausted'})
            c.execute("INSERT INTO task_runs(task_id,started_at,ended_at,status,outcome) "
                      "VALUES ('t_proof',1,9999999999,'completed','completed')")
        fn = lambda: quota.release_recovered_quota_blocks(c)
    elif which == 'shared':
        for tid in ('t_A', 't_B'):
            wall(c, tid, 'wall-' + tid)
        fn = lambda: blockers.probe_stranded_blockers(c, interval=0)
    else:
        card(c, 't_parent', 'done')
        with kb.write_txn(c):
            for tid in ('t_A', 't_B'):
                c.execute("UPDATE tasks SET workspace_kind='scratch' WHERE id=?", (tid,))
                kb._append_event(c, tid, 'created', {'creator_task_id': 't_parent'})
        for tid in ('t_A', 't_B'):
            kb.add_comment(c, tid, 'worker', f'Run `hermes kanban archive {tid}`.')
        from hermes_cli.kanban_triage_sweep import sweep_stale_triage
        fn = lambda: sweep_stale_triage(c, older_than=0, now=9999999999)
    hit = []
    if race:
        if which == 'cleanup':
            original = kb.archive_task
            def archive(conn, tid, **kw):
                if tid == 't_A':
                    hit.append(True); freeze(c, tid, 'pending')
                return original(conn, tid, **kw)
            monkeypatch.setattr(kb, 'archive_task', archive)
        else:
            original = kb.unblock_task
            def unblock(conn, tid, *args, **kw):
                result = original(conn, tid, *args, **kw)
                if tid == 't_A' and result:
                    hit.append(True); freeze(c, tid, 'pending')
                return result
            monkeypatch.setattr(kb, 'unblock_task', unblock)
    fn()
    assert bool(hit) == race
    assert kb.get_task(c, 't_B').status == ('archived' if which == 'cleanup' else 'ready')
    assert kb.get_task(c, 't_A').status == ('triage' if race and which == 'cleanup' else
                                         'archived' if which == 'cleanup' else 'ready')


@pytest.mark.parametrize('phase', ['prepared', 'pending', 'completed', 'ordinary'])
@pytest.mark.parametrize('when', ['judge', 'capability'])
def test_caused_review_captures_at_writer_after_fallible_work(consumer_board, monkeypatch, phase, when):
    c = consumer_board
    tid = card(c, 't_spend', 'triage')
    with kb.write_txn(c):
        kb._append_event(c, tid, 'spend_budget_triaged', {'cause': 'scope_stopped', 'reason': QUOTE})
    ep = reviewer._current_episode(c, tid)
    with kb.write_txn(c):
        kb._append_event(c, tid, decision.NOTICE, {'episode': ep})
        kb._append_event(c, tid, reviewer.STARTED, {'episode': ep})
    sent = []
    def transition():
        freeze(c, tid, phase)
    class Hub:
        def __init__(self, *a, **kw):
            self.ident = SimpleNamespace(gateway='worker-a3')
        def decision_retain_capability(self, board):
            if when == 'capability': transition()
            return {'spend_review': 1}
        def ops(self, board, wires):
            sent.extend(wires)
            return {'results': []}
        def close(self): pass
    def judge(ctx):
        if when == 'judge': transition()
        return {'decision': 'release', 'reason': 'Remaining acceptance belongs here', 'remaining_quote': QUOTE}
    monkeypatch.setattr(reviewer, 'settings', lambda: (True, 2))
    monkeypatch.setattr(reviewer, 'judge', judge)
    monkeypatch.setattr(client, 'Hub', Hub)
    monkeypatch.setattr(client, 'load_identity', lambda: None)
    monkeypatch.setattr(client, 'push', lambda *a, **kw: None)
    reviewer.review(c, tid, ep)
    saved = journal.get(c, tid, ep)
    if phase in ('prepared', 'pending'):
        assert not saved and not sent
    else:
        assert saved and len(sent) == 1
        assert sent[0].get('home_generations') == ({tid: 'fixture-generation-0001'} if phase == 'completed' else None)


@pytest.mark.parametrize('phase', ['prepared', 'pending', 'aborted', 'ordinary'])
@pytest.mark.parametrize('late', [False, True])
def test_attachment_custody_skips_io_and_late_bookkeeping(consumer_board, monkeypatch, phase, late):
    c = consumer_board
    data = b'fixture bytes'
    key = 'sha256:' + hashlib.sha256(data).hexdigest()
    for tid in ('t_A', 't_B'):
        card(c, tid, 'todo')
        with kb.write_txn(c):
            c.execute("INSERT INTO task_attachments(task_id,uid,filename,stored_path,size,created_at,blob_key) "
                      "VALUES (?,?,?,'',?,1,?)", (tid, 'attachment_' + tid, 'probe.txt', len(data), key))
    if not late: freeze(c, 't_A', phase)
    calls = []
    class Hub:
        def put_attachment(self, *a): raise AssertionError('no upload')
        def get_attachment_blob(self, board, uid):
            calls.append(uid)
            if late and uid == 'attachment_t_A': freeze(c, 't_A', phase)
            return data
    attachments.transfer_pending(c, Hub(), 'default', limit=2)
    rows = {r['task_id']: r['stored_path'] for r in c.execute('SELECT * FROM task_attachments')}
    blocked = phase in ('prepared', 'pending')
    assert bool(rows['t_A']) == (not blocked)
    assert rows['t_B']
    assert ('attachment_t_A' in calls) == (late or not blocked)
    if blocked:
        assert c.execute("SELECT 1 FROM kanban_attachment_retry WHERE uid='attachment_t_A'").fetchone() is None


def seed_request(c, tid='t_A'):
    card(c, tid, 'triage')
    with kb.write_txn(c):
        kb._append_event(c, tid, 'spend_budget_triaged', {'cause': 'scope_stopped', 'reason': QUOTE})
    ep = reviewer._current_episode(c, tid)
    with kb.write_txn(c):
        kb._append_event(c, tid, reviewer.STARTED, {'episode': ep})
    wire = dict(task=tid, episode=ep, event_uid='fixture-review-request', verdict='release')
    return ep, wire


@pytest.mark.parametrize('action', ['freeze', 'close', 'refuse', 'escalate'])
@pytest.mark.parametrize('phase', ['prepared', 'pending', 'malformed', 'aborted'])
def test_review_journal_writer_entry_checks_task_column(consumer_board, monkeypatch, action, phase):
    c = consumer_board
    ep, wire = seed_request(c)
    if action != 'freeze':
        journal.freeze(c, 'board', 'worker-a3', wire)
    original = kb.write_txn
    hit = []
    @contextmanager
    def writer(conn, *a, **kw):
        if not hit:
            hit.append(True)
            freeze(c, 't_A', phase)
        with original(conn, *a, **kw):
            yield
    monkeypatch.setattr(kb, 'write_txn', writer)
    before = journal.get(c, 't_A', ep)
    if action == 'freeze':
        if phase != 'aborted':
            with pytest.raises(ValueError, match='home transfer'):
                journal.freeze(c, 'board', 'worker-a3', wire)
        else:
            journal.freeze(c, 'board', 'worker-a3', wire)
    elif action == 'close':
        journal.close(c, [('t_A', ep)])
    elif action == 'refuse':
        journal.refuse(c, 't_A', ep, 'refused')
    else:
        journal.escalate_unconfirmed(c, 't_A', ep)
    assert hit
    if phase != 'aborted':
        assert journal.get(c, 't_A', ep) == before
        assert reviewer.state(c, 't_A', ep) == reviewer.STARTED
        if before:
            assert ('t_A', ep) in journal.open_keys(c)


def test_existing_review_wire_is_not_enriched_or_replaced(consumer_board):
    c = consumer_board
    _ep, wire = seed_request(c)
    first = journal.freeze(c, 'board', 'worker-a3', wire)
    assert 'home_generations' not in first['wire']
    freeze(c, 't_A', 'completed')
    assert journal.freeze(c, 'other', 'other', {**wire, 'verdict': 'archive'}) == first
    # Replayed legacy bytes do not silently acquire today's epoch, even while pending.
    freeze(c, 't_A', 'pending')
    assert journal.freeze(c, 'board', 'worker-a3', wire) == first


@pytest.mark.parametrize('verdict', ['release', 'wait_on', 'escalate', 'refused'])
@pytest.mark.parametrize('phase', ['prepared', 'pending', 'ordinary'])
def test_caused_review_late_settlement_preserves_custody(consumer_board, monkeypatch, verdict, phase):
    c = consumer_board
    ep, wire = seed_request(c)
    parent = card(c, 't_wait_parent', 'ready')
    with kb.write_txn(c):
        c.execute("UPDATE tasks SET assignee='default' WHERE id IN (?,?)", ('t_A', parent))
    journal.freeze(c, 'board', 'worker-a3', wire)
    before = [tuple(r) for r in c.execute('SELECT * FROM task_events WHERE task_id=?', ('t_A',))]
    def canonical(*args, **kwargs):
        freeze(c, 't_A', phase)
        if verdict == 'refused':
            raise caused.Refused('canonical refusal')
        return dict(verdict=verdict, reason='confirmed', request_uid=wire['event_uid'], target=parent)
    monkeypatch.setattr(caused, 'canonical', canonical)
    result = caused.review(c, 't_A', ep, None, None)
    if phase != 'ordinary':
        assert result == 'pending'
        assert [tuple(r) for r in c.execute('SELECT * FROM task_events WHERE task_id=?', ('t_A',))] == before
        assert reviewer.state(c, 't_A', ep) == reviewer.STARTED
    else:
        assert result in ('settled', 'escalated')
        if verdict == 'wait_on':
            import json
            row = c.execute('SELECT payload,created_at FROM task_events WHERE task_id=? AND kind=?',
                            ('t_A', reviewer.SETTLED)).fetchone()
            data = json.loads(row[0])
            assert (data['status'], data['parent']) == ('todo', parent)
            assert data['actionability_intent']['deadline_at'] == row[1] + 86400
            assert data['actionability_intent']['execution_authority'] is False
            assert data['actionability_parent_ids'] == [parent]


@pytest.mark.parametrize('seam', ['metadata', 'retry'])
def test_attachment_writer_entry_custody_and_single_slot_peer(consumer_board, monkeypatch, seam):
    c = consumer_board
    data = b'fixture bytes'
    key = 'sha256:' + hashlib.sha256(data).hexdigest()
    for tid in ('t_A', 't_B'):
        card(c, tid, 'todo')
        with kb.write_txn(c):
            c.execute("INSERT INTO task_attachments(task_id,uid,filename,stored_path,size,created_at,blob_key) "
                      "VALUES (?,?,?,'',?,1,?)", (tid, 'attachment_' + tid, 'probe.txt', len(data), key))
    calls = []
    class Hub:
        def put_attachment(self, *a): raise AssertionError('no upload')
        def get_attachment_blob(self, board, uid):
            calls.append(uid)
            return data
    original = attachments.write_txn
    entries = []
    @contextmanager
    def writer(conn, *a, **kw):
        entries.append(True)
        # setup is done explicitly below; metadata is first, retry is third (after peer).
        if len(entries) == (1 if seam == 'metadata' else 3):
            freeze(c, 't_A', 'pending')
        with original(conn, *a, **kw):
            yield
    attachments._setup(c)
    monkeypatch.setattr(attachments, 'write_txn', writer)
    attachments.transfer_pending(c, Hub(), 'default', limit=2)
    assert home.frozen(c, 't_A')
    assert c.execute("SELECT stored_path FROM task_attachments WHERE task_id='t_B'").fetchone()[0]
    assert c.execute("SELECT 1 FROM kanban_attachment_retry WHERE uid='attachment_t_A'").fetchone() is None
    if seam == 'metadata':
        assert not c.execute("SELECT stored_path FROM task_attachments WHERE task_id='t_A'").fetchone()[0]
    # A frozen first row must not consume the only next-round I/O slot.
    with kb.write_txn(c):
        c.execute("DELETE FROM kanban_attachment_retry WHERE uid='attachment_t_B'")
    calls.clear()
    assert attachments.transfer_pending(c, Hub(), 'default', limit=1) == 1

