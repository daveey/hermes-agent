"""Observation custody, bounds and transactional race controls."""
import copy
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_release_scope as scope
from hermes_cli import kanban_release_certificate as cert
from tests.hermes_cli import test_owner_release_foreign_scope as helpers

board = helpers.board
proof = helpers.proof
assembly = helpers.assembly
foreign = helpers.foreign


def test_capture_forbids_network_under_writer(foreign):
    a = foreign
    with kb.write_txn(a.conn), pytest.raises(ValueError, match='no writer'):
        with scope.observe(a.conn, a.hub, a.proof):
            pytest.fail('writer-held capture admitted')


@pytest.mark.parametrize('change', ['missing', 'different', 'gateway'])
def test_private_observation_authority_is_not_inferred(foreign, monkeypatch, change):
    a = foreign
    if change == 'gateway':
        a.hub.ident.gateway = 'different-home'
    else:
        saved = copy.deepcopy(a.proof) if change == 'different' else None
        if saved is not None:
            saved['intent']['note'] = 'not the winning private receipt'
        monkeypatch.setattr(scope.replica, 'verified_consumption', lambda *args: saved)
    with pytest.raises(ValueError):
        with scope.observe(a.conn, a.hub, a.proof):
            pytest.fail('uncertified observation admitted')


@pytest.mark.parametrize('change', ['proof', 'connection', 'disappear', 'bytes', 'unbound'])
def test_observation_recheck_binds_owner_and_immutable_bytes(foreign, change):
    a = foreign
    with scope.observe(a.conn, a.hub, a.proof):
        proof = copy.deepcopy(a.proof)
        with kb.write_txn(a.conn):
            original = scope._OBSERVED.get()
            def change_bytes():
                p = json.loads(a.canonical['payload'])
                p['disposition']['summary'] = 'new bytes in writer'
                a.conn.execute("UPDATE task_events SET payload=? WHERE uid='restriction-1'", (cert.encode(p),))
            actions = {
                'proof': lambda: proof['intent'].update(note='different request'),
                'connection': lambda: scope._OBSERVED.set(scope._Observation(object(), original.proof, original.rows)),
                'disappear': lambda: a.conn.execute("DELETE FROM task_events WHERE uid='restriction-1'"),
                'bytes': change_bytes,
                'unbound': lambda: scope._OBSERVED.set(None),
            }
            actions[change]()
            with pytest.raises(ValueError):
                scope.evidence(a.conn, proof)
    assert scope._OBSERVED.get() is None


@pytest.mark.parametrize('damage', ['inventory', 'bytes', 'nontext'])
def test_inventory_and_actual_decoded_bytes_are_bounded(foreign, damage):
    a = foreign
    with kb.write_txn(a.conn):
        if damage == 'inventory':
            a.conn.executemany("INSERT INTO task_events(uid,task_id,kind,payload,created_at) "
                               "VALUES (?,?,'decision_retained','{}',90)",
                               [(f'bounded-{i}', a.tid) for i in range(4097)])
        else:
            value = ' ' * (cert.MAX_BYTES + 1) if damage == 'bytes' else None
            a.conn.execute("UPDATE task_events SET payload=? WHERE uid='restriction-1'", (value,))
    with pytest.raises(ValueError):
        helpers.process(a)
    assert not a.hub.posts


@pytest.mark.parametrize('field,value', [('version', 2), ('request_uid', []), ('request_sha256', None),
                                        ('request_sha256', 'a' * 63), ('run_uid', []), ('actor_gateway', [])])
def test_typed_local_receipt_validation(foreign, field, value):
    a = foreign
    p = json.loads(a.canonical['payload'])
    p[field] = value
    a.canonical['payload'] = cert.encode(p)
    with kb.write_txn(a.conn):
        a.conn.execute("UPDATE task_events SET payload=? WHERE uid='restriction-1'", (cert.encode(p),))
    with pytest.raises(ValueError):
        helpers.process(a)
    assert not a.hub.posts


def test_completed_application_replay_needs_no_mutable_observation(foreign):
    a = foreign
    first = helpers.process(a)
    def retired(*args, **kwargs):
        pytest.fail('completed replay performed mutable scope observation')
    a.hub.row_evidence = retired
    assert helpers.process(a) == first
    assert len(a.hub.posts) == 1


@pytest.mark.parametrize('captured', [False, True])
def test_ordinary_scope_needs_no_foreign_observation(assembly, captured):
    from tests.hermes_cli import test_owner_release_materialize as materialize
    a = assembly
    assert scope._OBSERVED.get() is None
    if captured:
        with scope.observe(a.conn, materialize.Hub(a), a.proof):
            assert scope.evidence(a.conn, a.proof) == cert.coverage(a.proof)
    else:
        assert scope.evidence(a.conn, a.proof) == cert.coverage(a.proof)


@pytest.mark.parametrize('reply', [None, [], 'not-an-envelope'])
def test_nondict_canonical_reply_is_typed_refusal(foreign, reply):
    a = foreign
    a.hub.row_evidence = lambda *args, **kwargs: reply
    with pytest.raises(ValueError, match='authenticated canonical receipt'):
        helpers.process(a)
    assert not a.hub.posts


@pytest.mark.parametrize('coordinate', ['gateway', 'run'])
def test_matching_but_invalid_source_coordinates_refuse(foreign, coordinate):
    a = foreign
    p = json.loads(a.canonical['payload'])
    with kb.write_txn(a.conn):
        if coordinate == 'gateway':
            p['actor_gateway'] = 'invalid gateway!'
            a.canonical['origin_gateway'] = p['actor_gateway']
            a.conn.execute("UPDATE task_events SET origin_gateway=? WHERE uid='restriction-1'", (p['actor_gateway'],))
        else:
            p['run_uid'] = 'invalid run!'
            a.canonical['run_uid'] = p['run_uid']
            a.conn.execute("UPDATE task_runs SET uid=? WHERE uid='foreign-run'", (p['run_uid'],))
        a.canonical['payload'] = cert.encode(p)
        a.conn.execute("UPDATE task_events SET payload=? WHERE uid='restriction-1'", (a.canonical['payload'],))
    with pytest.raises(ValueError, match='identity differs'):
        helpers.process(a)
    assert not a.hub.posts


def test_observer_itself_refuses_unended_source_run(foreign):
    a = foreign
    with kb.write_txn(a.conn):
        a.conn.execute("UPDATE task_runs SET ended_at=NULL WHERE uid='foreign-run'")
    with pytest.raises(ValueError, match='identity differs'):
        with scope.observe(a.conn, a.hub, a.proof):
            pytest.fail('unended source run observed')


@pytest.mark.parametrize('count', [4095, 4096])
def test_valid_ignored_rows_exercise_inventory_boundary(foreign, count):
    a = foreign
    ignored = cert.encode(dict(superseded_by_safety_stop=False, disposition=dict(reason='other')))
    with kb.write_txn(a.conn):
        a.conn.executemany("INSERT INTO task_events(uid,task_id,kind,payload,created_at) "
                           "VALUES (?,?,'decision_retained',?,90)",
                           [(f'ignored-{i}', a.tid, ignored) for i in range(count)])
    if count == 4096:
        with pytest.raises(ValueError, match='inventory exceeds bound'):
            scope._rows(a.conn, a.proof)
    else:
        assert set(scope._rows(a.conn, a.proof)) == {'restriction-1'}


def test_aggregate_bytes_bound_not_shadowed_by_single_row_parser(foreign):
    a = foreign
    ignored = cert.encode(dict(superseded_by_safety_stop=False,
                               disposition=dict(reason='other', summary='x' * (cert.MAX_BYTES // 2))))
    assert len(ignored.encode('utf-8')) < cert.MAX_BYTES
    assert cert.parse(ignored)['disposition']['reason'] == 'other'
    with kb.write_txn(a.conn):
        a.conn.executemany("INSERT INTO task_events(uid,task_id,kind,payload,created_at) "
                           "VALUES (?,?,'decision_retained',?,90)",
                           [(f'large-{i}', a.tid, ignored) for i in range(2)])
    with pytest.raises(ValueError, match='bytes exceed bound'):
        scope._rows(a.conn, a.proof)


def test_non_scope_disposition_is_ignored_not_granted(foreign):
    a = foreign
    p = json.loads(a.canonical['payload'])
    p['event_uid'] = 'irrelevant-retain'
    p['disposition']['reason'] = 'decision_pending'
    with kb.write_txn(a.conn):
        a.conn.execute("INSERT INTO task_events(uid,task_id,kind,payload,created_at,origin_gateway,run_id) "
                       "SELECT ?,task_id,kind,?,created_at,origin_gateway,run_id FROM task_events "
                       "WHERE uid='restriction-1'", (p['event_uid'], cert.encode(p)))
    # Only the scoped observation projection is under test; this does not
    # establish permission to clear an independent decision-pending hold.
    assert set(scope._rows(a.conn, a.proof)) == {'restriction-1'}
    assert not a.hub.posts


@pytest.mark.parametrize('uid', ['invalid uid!', 'invalid/uid'])
def test_matching_invalid_event_identity_is_not_a_scope_row(foreign, uid):
    a = foreign
    p = json.loads(a.canonical['payload'])
    p['event_uid'] = uid
    with kb.write_txn(a.conn):
        a.conn.execute("UPDATE task_events SET uid=?,payload=? WHERE uid='restriction-1'", (uid, cert.encode(p)))
    with pytest.raises(ValueError, match='identity differs'):
        scope._rows(a.conn, a.proof)


def test_duplicate_projection_identity_refuses_even_without_storage_uniqueness(foreign):
    """Defensive cursor contract, not a reachable duplicate in a healthy unique-index schema."""
    a = foreign
    row = dict(a.conn.execute("SELECT e.*,r.uid AS run_uid,r.task_id AS run_task,r.ended_at AS run_end,"
                             "r.outcome AS run_outcome FROM task_events e JOIN task_runs r ON r.id=e.run_id "
                             "WHERE e.uid='restriction-1'").fetchone())
    class DuplicateCursor:
        def execute(self, *args):
            return self
        def fetchall(self):
            return [row, row]
    with pytest.raises(ValueError, match='identity differs'):
        scope._rows(DuplicateCursor(), a.proof)


@pytest.mark.parametrize('mixed', [False, True])
def test_confirmed_local_projection_is_not_recaptured_as_foreign(foreign, monkeypatch, mixed):
    """Composition contract with injected ordinary evidence; real custody is covered by renewal receipt suites."""
    a = foreign
    ordinary = scope.renewal.scope_evidence(a.conn, a.tid, 'stop-1')
    local = dict(a.conn.execute("SELECT * FROM task_events WHERE uid='restriction-1'").fetchone())
    if mixed:
        local['uid'] = 'local-restriction'
        p = json.loads(a.canonical['payload'])
        p['event_uid'] = local['uid']
        with kb.write_txn(a.conn):
            a.conn.execute("INSERT INTO task_events(uid,task_id,kind,payload,created_at,origin_gateway,run_id) "
                           "SELECT ?,task_id,kind,?,created_at,origin_gateway,run_id FROM task_events "
                           "WHERE uid='restriction-1'", (local['uid'], cert.encode(p)))
        payload = json.loads(a.proof['renewal']['payload'])
        payload['covered_restriction_uids'].append(local['uid'])
        a.proof['renewal']['payload'] = cert.encode(payload)
    ordinary.append(local)
    monkeypatch.setattr(scope.renewal, 'scope_evidence', lambda *args: ordinary)
    monkeypatch.setattr(scope.replica, 'verified_consumption', lambda *args: a.proof)
    if not mixed:
        def no_network(*args, **kwargs):
            pytest.fail('locally confirmed scope was recaptured')
        a.hub.row_evidence = no_network
    with scope.observe(a.conn, a.hub, a.proof):
        assert scope.evidence(a.conn, a.proof) == cert.coverage(a.proof)


def test_foreign_becoming_ordinary_does_not_discard_captured_bytes(foreign, monkeypatch):
    """A changing custody classifier must not bypass the captured-row comparison."""
    a = foreign
    ordinary = scope.renewal.scope_evidence(a.conn, a.tid, 'stop-1')
    local = dict(a.conn.execute("SELECT * FROM task_events WHERE uid='restriction-1'").fetchone())
    with scope.observe(a.conn, a.hub, a.proof):
        monkeypatch.setattr(scope.renewal, 'scope_evidence', lambda *args: ordinary + [local])
        with pytest.raises(ValueError, match='receipt disappeared'):
            scope.evidence(a.conn, a.proof)


def test_empty_unvalidated_projection_is_defensively_refused(assembly, monkeypatch):
    """Private seam defense: real validated certificates always have nonempty coverage."""
    a = assembly
    damaged = copy.deepcopy(a.proof)
    payload = json.loads(damaged['renewal']['payload'])
    payload['covered_restriction_uids'] = []
    damaged['renewal']['payload'] = cert.encode(payload)
    monkeypatch.setattr(scope.renewal, 'scope_evidence', lambda *args: [])
    with pytest.raises(ValueError, match='current scope evidence differs'):
        scope.evidence(a.conn, damaged)
