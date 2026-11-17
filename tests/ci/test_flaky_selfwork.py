"""Real journal + courier sent-ledger proofs for declared CI intake."""
import copy
import json
import sqlite3
import subprocess

import pytest

from hermes_cli import kanban_selfwork_owner as owner
from hermes_cli import kanban_selfwork_outbound as outbound
from hermes_cli import kanban_selfwork_producer as producer
from scripts.ci import flaky_intake as intake
from scripts.ci import flaky_kanban as flaky
from scripts.ci import flaky_selfwork

REPO = 'softmax-universe/daveey-hermes'
HOME = {'gateway': 'home', 'profile': 'maintenance', 'board': 'repairs'}


@pytest.fixture
def courier(tmp_path, monkeypatch):
    for key in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_WORKER_TASK'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(owner, '_runtime', lambda: ({'kanban': {'self_work': {'home': HOME}}}, 'sender', 'default', tmp_path))
    monkeypatch.setenv('HERMES_HUB_STATE', str(tmp_path))
    db = sqlite3.connect(tmp_path / 'hub.db')
    db.executescript("CREATE TABLE meta(k TEXT,v TEXT); INSERT INTO meta VALUES('gateway','sender'); "
                     'CREATE TABLE sent(id TEXT PRIMARY KEY,body TEXT,state TEXT,result TEXT)')
    db.commit()
    sent = []

    def send(home, body, request_id):
        envelope = {'id': request_id, 'to_gw': home['gateway'], 'coll': 'tasks',
                    'key': home['profile'] + '/' + home['board'], 'action': 'selfwork-create', 'args': body}
        previous = db.execute('SELECT body FROM sent WHERE id=?', (request_id,)).fetchone()
        if previous:
            assert json.loads(previous[0]) == envelope
        with db:
            db.execute('INSERT OR IGNORE INTO sent VALUES(?,?,?,NULL)', (request_id, json.dumps(envelope), 'local'))
        sent.append(copy.deepcopy(envelope))
        return {'ok': False, 'created': False, 'handoff_complete': False, 'code': 'self_work_queued',
                'queued': True, 'home': home, 'request_id': request_id}

    monkeypatch.setattr(outbound, 'send', send)
    yield db, sent
    db.close()


class ProducerBoard:
    assignee = 'default'

    def producer(self, operation, value=None):
        if operation == 'capabilities':
            return True
        return producer.prepare(value) if operation == 'prepare' else producer.advance(value)

    def list_cards(self):
        pytest.fail('declared work must never use local fallback')


def plan(journal, number=1, repo=REPO):
    journal.record(repo, [(number, [{'test_id': f'tests/a.py::test_{number}'}])],
                   {'id': number, 'head_sha': 'a' * 40})
    journal.plan_pages(repo, journal.pending(repo))


def finish(db, sent, disposition='create', **changes):
    result = dict(ok=True, handoff_complete=True, ownership_pending=False, code='self_work_accepted',
                  request_id=sent['id'], task_id='t_owner', disposition=disposition,
                  created=disposition == 'create', idempotency_key='unrelated-primary-key', comment_id=7)
    result.update(changes)
    with db:
        db.execute("UPDATE sent SET state='done',result=? WHERE id=?", (json.dumps(result), sent['id']))
    return result


@pytest.mark.parametrize('disposition', ['create', 'append'])
def test_queue_then_authoritative_owner(courier, tmp_path, disposition):
    db, sent = courier
    journal = intake.Journal(tmp_path / 'queue.db')
    try:
        plan(journal)
        assert journal.deliver(REPO, ProducerBoard())[0].startswith('FAILED')
        assert journal.db.execute('SELECT delivered FROM occurrences').fetchone()[0] == 0
        finish(db, sent[0], disposition)
        assert journal.deliver(REPO, ProducerBoard()) == ['intake delivered home/maintenance/repairs/t_owner page=1']
        assert journal.db.execute('SELECT delivered,owner FROM occurrences').fetchone()[:] == (1, 'home/maintenance/repairs/t_owner')
        assert len(sent) == 1  # completed receipts are read, not resubmitted
    finally:
        journal.close()


def test_timeout_midnight_growth_restart_retains_exact_payload(courier, tmp_path, monkeypatch):
    db, sent = courier
    original = outbound.send

    def timeout(*args):
        original(*args)
        raise subprocess.TimeoutExpired('courier', 30)

    monkeypatch.setattr(outbound, 'send', timeout)
    monkeypatch.setattr(intake, 'utc_day', lambda: '2026-01-01')
    path = tmp_path / 'queue.db'
    journal = intake.Journal(path)
    plan(journal)
    assert journal.deliver(REPO, ProducerBoard())[0].startswith('FAILED')
    snapshot = journal.db.execute('SELECT producer_request FROM pages').fetchone()[0]
    journal.close()
    monkeypatch.setattr(outbound, 'send', original)
    monkeypatch.setattr(intake, 'utc_day', lambda: '2026-01-02')
    monkeypatch.setattr(owner, '_runtime', lambda: pytest.fail('pending retry must not prepare again'))
    journal = intake.Journal(path)
    try:
        plan(journal, 2)
        journal.recheck_unattempted(REPO)
        journal.deliver(REPO, ProducerBoard(), page_ids={1})
        assert sent[0] == sent[1]
        assert journal.db.execute('SELECT producer_request FROM pages WHERE id=1').fetchone()[0] == snapshot
        assert 'test_2' not in sent[1]['args']['create']['body']
        finish(db, sent[1], 'append')
        journal.deliver(REPO, ProducerBoard(), page_ids={1})
        assert journal.db.execute('SELECT count(*) FROM occurrences WHERE delivered=1').fetchone()[0] == 1
        assert len(journal.pending(REPO)) == 1
    finally:
        journal.close()


@pytest.mark.parametrize('change', [
    {'ok': False}, {'handoff_complete': False}, {'ownership_pending': True}, {'code': 'self_work_queued'},
    {'request_id': 'another'}, {'task_id': None}, {'task_id': '../other'}, {'disposition': 'rejected'},
    {'created': False}, {'disposition': 'append', 'created': True},
    {'disposition': 'append', 'created': False, 'comment_id': 0},
    {'disposition': 'append', 'created': False, 'comment_id': True},
])
def test_malformed_receipt_stays_pending(courier, tmp_path, change):
    db, sent = courier
    journal = intake.Journal(tmp_path / 'queue.db')
    try:
        plan(journal)
        journal.deliver(REPO, ProducerBoard())
        finish(db, sent[0], **change)
        assert journal.deliver(REPO, ProducerBoard())[0].startswith('FAILED')
        assert journal.db.execute('SELECT delivered FROM occurrences').fetchone()[0] == 0
    finally:
        journal.close()


@pytest.mark.parametrize('state', ['local', 'waiting', 'rejected', 'failed'])
def test_nonterminal_courier_state_not_delivery(courier, tmp_path, state):
    db, sent = courier
    journal = intake.Journal(tmp_path / 'queue.db')
    try:
        plan(journal)
        journal.deliver(REPO, ProducerBoard())
        finish(db, sent[0])
        with db:
            db.execute('UPDATE sent SET state=?', (state,))
        assert journal.deliver(REPO, ProducerBoard())[0].startswith('FAILED')
    finally:
        journal.close()


@pytest.mark.parametrize('field,value', [('key', 'default/default'), ('to_gw', 'foreign'), ('args', {}), ('id', 'other')])
def test_foreign_envelope_rejected(courier, tmp_path, field, value):
    db, sent = courier
    journal = intake.Journal(tmp_path / 'queue.db')
    try:
        plan(journal)
        journal.deliver(REPO, ProducerBoard())
        finish(db, sent[0])
        with db:
            db.execute('UPDATE sent SET body=?', (json.dumps({**sent[0], field: value}),))
        assert journal.deliver(REPO, ProducerBoard())[0].startswith('FAILED')
        assert len(sent) == 1
    finally:
        journal.close()


@pytest.mark.parametrize('opted,help_text,expected', [(False, 'selfwork-producer', False), (True, 'create show', False),
                                                     (True, 'selfwork-producer', True)])
def test_capability_before_any_submission(monkeypatch, opted, help_text, expected):
    board = flaky.CliBoard(self_work_ci=opted)
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return help_text if args == ('--help',) else json.dumps(producer.CAPABILITY)

    monkeypatch.setattr(board, '_run', run)
    assert board.producer('capabilities') is expected
    assert all('prepare' not in c and 'advance' not in c and 'create' not in c for c in calls)


@pytest.mark.parametrize('repo,attempted', [('product/repo', 0), (REPO, 1)])
def test_other_repo_and_escaped_legacy_untouched(tmp_path, repo, attempted):
    journal = intake.Journal(tmp_path / 'queue.db')
    try:
        plan(journal, repo=repo)
        with journal.db:
            journal.db.execute('UPDATE pages SET attempted=?', (attempted,))
        page = journal.db.execute('SELECT * FROM pages').fetchone()
        assert flaky_selfwork.handle(journal, ProducerBoard(), repo, page) is None
    finally:
        journal.close()


@pytest.mark.parametrize('change', [
    {'disposition': []}, {'disposition': {}}, {'version': True}, {'status': 'pending'},
    {'request_id': 'wrong'}, {'owner': None}, {'owner': {'gateway': 'other', 'profile': 'maintenance',
                                                     'board': 'repairs', 'task_id': 't_owner'}},
])
def test_adapter_malformed_completed_reply_remains_pending(courier, tmp_path, change):
    journal = intake.Journal(tmp_path / 'queue.db')
    board = ProducerBoard()
    try:
        plan(journal)
        journal.deliver(REPO, board)
        frozen = json.loads(journal.db.execute('SELECT producer_request FROM pages').fetchone()[0])
        board.producer = lambda *a: {'version': 1, 'status': 'completed', 'request_id': frozen['request_id'],
                                    'disposition': 'create', 'owner': {**HOME, 'task_id': 't_owner'}, **change}
        assert journal.deliver(REPO, board)[0].startswith('FAILED')
        assert journal.db.execute('SELECT delivered FROM occurrences').fetchone()[0] == 0
    finally:
        journal.close()


@pytest.mark.parametrize('version,expected', [(0, False), (2, False), (1, True)])
def test_capability_version_fallback_before_submission(monkeypatch, version, expected):
    board = flaky.CliBoard(self_work_ci=True)
    monkeypatch.setattr(board, '_run', lambda *a: 'selfwork-producer' if a == ('--help',)
                        else json.dumps({'self_work_producer': version}))
    assert board.producer('capabilities') is expected


@pytest.mark.parametrize('action,worker', [('create', 't_worker'), ('selfwork-producer', None)])
def test_only_producer_strips_worker_alias(monkeypatch, action, worker):
    from types import SimpleNamespace
    monkeypatch.setenv('HERMES_KANBAN_WORKER_TASK', 't_worker')
    def run(*a, **kw):
        assert kw['env'].get('HERMES_KANBAN_WORKER_TASK') == worker
        return SimpleNamespace(returncode=0, stdout='{}')
    monkeypatch.setattr(subprocess, 'run', run)
    assert flaky.CliBoard()._run(action) == '{}'


def test_pending_request_survives_removed_adapter(courier, tmp_path):
    journal = intake.Journal(tmp_path / 'queue.db')
    try:
        plan(journal)
        journal.deliver(REPO, ProducerBoard())
        assert journal.deliver(REPO, object())[0].startswith('FAILED')
        assert journal.db.execute('SELECT delivered FROM occurrences').fetchone()[0] == 0
    finally:
        journal.close()
