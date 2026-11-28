"""Reserved explicit revisions suppress repeat asks, never authorize a release."""
from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import kanban_card_answer as answers
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_release
from hermes_cli import kanban_release_offer as offers
from hermes_cli import kanban_stranded_park as stranded
from tests.hermes_cli import test_kanban_release_offer as offer_tests
from tests.hermes_cli import test_kanban_stranded_park as park

kanban_home = offer_tests.kanban_home
own_gateway = offer_tests.own_gateway
board_allowance = offer_tests.board_allowance


def _dump(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def source(home, monkeypatch, task, ask):
    """Real ActionStore tables, including immutable rev1 projection and rev2 approval."""
    from hermes_cli import install_identity
    monkeypatch.setattr(install_identity, 'get_install_id', lambda: 'fixture-install')
    from hermes_cli import kanban_card_answer_runtime as runtime
    monkeypatch.setattr(runtime, '_profile', lambda: ('default', home))
    path = home / 'user-steps/actions.sqlite3'
    path.parent.mkdir(exist_ok=True)
    owner = dict(gatewayId='install:fixture-install', profile='default', storedSessionId='owner-chat')
    ns = json.dumps(['install:fixture-install', 'default'], separators=(',', ':'))
    old = dict(requestId='request-one', revision=1, owner=owner, kind='choice')
    frozen = dict(requestId='request-one', revision=2, owner=owner, kind='approval', state='open',
                  scope={'complete': True, 'markdown': 'Continue within both caps', 'sourceRefs': []},
                  limits=[dict(key='additional_runs', value='20', permissionRequired=True),
                          dict(key='additional_spend_usd_max', value='5', permissionRequired=True)],
                  choices=[dict(id='release', effect='approve', label='Release')])
    scope_digest = answers.digest({k: v for k, v in frozen.items() if k != 'state'})
    decision = dict(requestId='request-one', expectedRevision=2, choiceId='release', idempotencyKey='decision-one')
    op = dict(operationId='op-one', requestId='request-one', revision=2, owner=owner,
              recordedAt='2026-01-01T00:00:00+00:00', dispatchState='delivered', decision=decision,
              idempotencyKey='decision-one', binding=dict(scopeDigest=scope_digest),
              intent=dict(request=frozen, effect='approve', executionAuthorized=True,
                          permissionGrants=[dict(key='additional_runs', value='20'),
                                            dict(key='additional_spend_usd_max', value='5')]))
    resolution = dict(operationId='op-one', choiceId='release', revision=2, recordedAt=op['recordedAt'])
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE operations(namespace,session,id,body);
            CREATE TABLE kanban_projections(namespace,session,request_id,board,task,snapshot,request);
            CREATE TABLE revisions(namespace,id,revision,body);
            CREATE TABLE requests(namespace,session,id,revision,state,resolution);
            CREATE TABLE owner_receipts(namespace,operation,phase,fingerprint,body);
        ''')
        db.execute('INSERT INTO operations VALUES(?,?,?,?)', (ns, 'owner-chat', 'op-one', _dump(op)))
        db.execute('INSERT INTO kanban_projections VALUES(?,?,?,?,?,?,?)',
                   (ns, 'owner-chat', 'request-one', 'default', task,
                    _dump(dict(task=task, session='owner-chat', generation=1, ask=ask)), _dump(old)))
        for revision in (old, frozen):
            db.execute('INSERT INTO revisions VALUES(?,?,?,?)',
                       (ns, 'request-one', revision['revision'], _dump(revision)))
        db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?)',
                   (ns, 'owner-chat', 'request-one', 2, 'resolved', _dump(resolution)))
        for phase in ('acknowledged', 'reserved'):
            receipt = dict(operationId='op-one', requestId='request-one', revision=2, scopeDigest=scope_digest,
                           phase=phase, evidence=[])
            if phase == 'reserved':
                receipt['applicationKey'] = answers.digest([ns, 'op-one', 2, scope_digest])
            fingerprint = answers.digest(receipt)
            receipt.update(fingerprint=fingerprint, consumer=owner, recordedAt=op['recordedAt'])
            db.execute('INSERT INTO owner_receipts VALUES(?,?,?,?,?)',
                       (ns, 'op-one', phase, fingerprint, _dump(receipt)))
    return path


def setup(conn, home, monkeypatch, allowance):
    tid = offer_tests._stopped_with_live_chat(conn, allowance)
    offers.offer_release(conn, tid)
    ask = offer_tests._ask(conn, tid)
    path = source(home, monkeypatch, tid, ask)
    return tid, ask, path


def test_reserved_revision_sweep_closes_ask_without_releasing(kanban_home, own_gateway, board_allowance, monkeypatch):
    with kbc.connect_closing() as conn:
        tid, ask, path = setup(conn, kanban_home, monkeypatch, board_allowance)
        before = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        original = path.read_bytes()
        assert park._sweep(conn, now=park._later(3), cfg={}) == []
        after = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        assert after['human_request'] is None
        assert {k: v for k, v in before.items() if k != 'human_request'} == {
            k: v for k, v in after.items() if k != 'human_request'}
        assert answers.snapshot(conn, tid) is None  # closeout has no human ask to repeat
        assert kanban_release.offer(conn, tid) == ask['release_target']
        assert not board_allowance['renewed'] and offer_tests._released(conn, tid) == 0
        assert path.read_bytes() == original
        for hour in (4, 5):
            stranded._LAST_RUN.clear()
            assert park._sweep(conn, now=park._later(hour), cfg={}) == []
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='release_application_pending'",
                            (tid,)).fetchone()[0] == 1
        with pytest.raises(offers.OfferRefused, match='reserved'):
            offers.offer_release(conn, tid)


@pytest.mark.parametrize('mutation', [
    "UPDATE kanban_projections SET snapshot=json_set(snapshot,'$.ask.release_target.episode','other-stop')",
    "UPDATE operations SET namespace='foreign'",
    "UPDATE requests SET revision=3",
    "DELETE FROM owner_receipts WHERE phase='reserved'",
    "UPDATE owner_receipts SET phase='applied' WHERE phase='reserved'",
    "UPDATE revisions SET body='{}' WHERE revision=1",
    "UPDATE operations SET body=json_set(body,'$.dispatchState','cancelled')",
    "UPDATE operations SET body=json_set(body,'$.retired',json('true'))",
    "UPDATE operations SET body=json_set(body,'$.owner.profile','foreign')",
    "UPDATE operations SET body=json_set(body,'$.binding.scopeDigest','wrong')",
    "UPDATE operations SET body=json_set(body,'$.decision.expectedRevision',3)",
    "UPDATE operations SET body=json_set(body,'$.intent.executionAuthorized',json('false'))",
    "UPDATE operations SET body=json_set(body,'$.intent.effect','answer')",
    "UPDATE requests SET state='withdrawn'",
    "UPDATE requests SET resolution='{}'",
    "UPDATE owner_receipts SET body=json_set(body,'$.applicationKey','wrong') WHERE phase='reserved'",
    "UPDATE owner_receipts SET fingerprint='wrong'",
    "UPDATE kanban_projections SET snapshot=json_set(snapshot,'$.session','foreign')",
    "UPDATE kanban_projections SET task='other-task'",
    "UPDATE kanban_projections SET board='other-board'",
])
def test_invalid_or_old_custody_cannot_suppress(kanban_home, own_gateway, board_allowance, monkeypatch, mutation):
    with kbc.connect_closing() as conn:
        tid, ask, path = setup(conn, kanban_home, monkeypatch, board_allowance)
        with sqlite3.connect(path) as db:
            db.execute(mutation)
        park._sweep(conn, now=park._later(3), cfg={})
        assert offer_tests._ask(conn, tid) == ask
        assert offers.offer_release(conn, tid)['outcome'] == 'already_offered'


def test_unrelated_replaced_ask_is_not_cleared(kanban_home, own_gateway, board_allowance, monkeypatch):
    with kbc.connect_closing() as conn:
        tid, ask, _ = setup(conn, kanban_home, monkeypatch, board_allowance)
        replacement = {**ask, 'action': 'A different physical action needs attention'}
        with conn:
            conn.execute('UPDATE tasks SET human_request=? WHERE id=?', (_dump(replacement), tid))
        park._sweep(conn, now=park._later(3), cfg={})
        assert offer_tests._ask(conn, tid) == replacement


@pytest.mark.parametrize('schema', ['core-only', 'unrelated-projection'])
def test_optional_source_schema_does_not_disable_offers(kanban_home, own_gateway, board_allowance, monkeypatch, schema):
    from hermes_cli import kanban_card_answer_runtime as runtime
    from hermes_cli import install_identity
    monkeypatch.setattr(runtime, '_profile', lambda: ('default', kanban_home))
    monkeypatch.setattr(install_identity, 'get_install_id', lambda: 'fixture-install')
    path = kanban_home / 'user-steps/actions.sqlite3'
    path.parent.mkdir(exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE operations(namespace,session,id,body)')
        if schema == 'unrelated-projection':
            db.execute('CREATE TABLE kanban_projections(namespace,board,task)')
            db.execute("INSERT INTO kanban_projections VALUES('other','default','other-task')")
    with kbc.connect_closing() as conn:
        tid = offer_tests._stopped_with_live_chat(conn, board_allowance)
        assert offers.offer_release(conn, tid)['outcome'] == 'offered'


@pytest.mark.parametrize('stamped', [False, True])
def test_different_filed_ask_keeps_normal_handling(kanban_home, own_gateway, board_allowance, monkeypatch, stamped):
    from hermes_cli import kanban_release_reserved as reserved
    with kbc.connect_closing() as conn:
        tid, ask, _ = setup(conn, kanban_home, monkeypatch, board_allowance)
        replacement = {**ask, 'action': 'A distinct approved scope question'}
        if not stamped:
            replacement.pop('release_target')
        from hermes_cli import kanban_db as kb
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET human_request=? WHERE id=?', (_dump(replacement), tid))
            kb._append_event(conn, tid, stranded.EVENT, stranded._record(replacement))
        assert reserved.reconcile(conn, tid) is False
        assert offer_tests._ask(conn, tid) == replacement
        assert offers._refusal(conn, tid).code == 'application_pending'
        park._sweep(conn, now=park._later(3), cfg={})
        assert offer_tests._ask(conn, tid) == replacement


def test_corrupt_candidate_source_defers_without_mutation(kanban_home, own_gateway, board_allowance, monkeypatch):
    with kbc.connect_closing() as conn:
        tid, ask, path = setup(conn, kanban_home, monkeypatch, board_allowance)
        with sqlite3.connect(path) as db:
            db.execute("UPDATE operations SET body='broken-json'")
        assert park._sweep(conn, now=park._later(3), cfg={}) == []
        assert offer_tests._ask(conn, tid) == ask
        assert offers._refusal(conn, tid).code == 'application_unreadable'
        assert not board_allowance['renewed']


def test_new_stop_with_same_projection_generation_is_not_suppressed(kanban_home, own_gateway, board_allowance, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_release_reserved as reserved
    from hermes_cli import kanban_spend_decision as decision
    with kbc.connect_closing() as conn:
        tid, ask, _ = setup(conn, kanban_home, monkeypatch, board_allowance)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'promoted_manual', {'actor': 'fixture', 'reason': 'another round'})
            kb._append_event(conn, tid, decision.EVENT, {'reason': 'Spend budget: successor stop'})
        assert kanban_release.offer(conn, tid) != ask['release_target']
        assert reserved.reconcile(conn, tid) is False
        assert offers.offer_release(conn, tid)['outcome'] == 'offered'
        assert offer_tests._ask(conn, tid)['release_target'] != ask['release_target']


@pytest.mark.parametrize('entrypoint', ['escalate', 'refile'])
def test_direct_filing_paths_recheck_reservation(kanban_home, own_gateway, board_allowance, monkeypatch, entrypoint):
    with kbc.connect_closing() as conn:
        tid, _, _ = setup(conn, kanban_home, monkeypatch, board_allowance)
        row = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        entry = stranded.park_entry(conn, tid)
        kwargs = dict(now=park._later(3), age=10800)
        if entrypoint == 'escalate':
            kwargs.update(defer=0, desk=None, contained=False)
        method = {'escalate': stranded._escalate, 'refile': stranded._refile}[entrypoint]
        assert method(conn, row, entry, **kwargs) is False
        assert answers.snapshot(conn, tid) is None
