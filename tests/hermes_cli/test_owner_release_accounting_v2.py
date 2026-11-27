"""V2 HOME ledger observation; unit contracts, not composed transport proof."""
import copy

import pytest

from hermes_cli import kanban_db as kb, kanban_release as release
from hermes_cli import kanban_release_prepared as prepared, kanban_release_final as final
from hermes_cli import kanban_release_certificate as cert, kanban_spend_budget as budget
from tests.hermes_cli import test_owner_release_assemble as helpers
from tests.hermes_cli.test_owner_release_prepared import snapshot
from tests.hermes_cli import test_owner_release_final as final_helpers
from tests.hermes_cli import test_owner_release_materialize as materialize_helpers

canonical = final_helpers.canonical

board = helpers.board
proof = helpers.proof
assembly = helpers.assembly


def test_v2_real_observation_once_and_whole_rollback(assembly, monkeypatch):
    a = assembly
    import time
    now = int(time.time())
    with kb.write_txn(a.conn):
        for _ in range(2):
            a.conn.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome) VALUES (?,'default','continued',?,?,'continued')",
                           (a.tid, now-budget.MIN_RUN_SECONDS, now))
    original = release._budget_plan
    observed = []
    def observe(conn, state):
        result = original(conn, state)
        observed.append(result)
        return result
    monkeypatch.setattr(release, '_budget_plan', observe)
    before = snapshot(a)
    data = prepared.prepare(a.conn, a.proof, delivery_uid='delivery-1', application_version=2)
    assert snapshot(a) == before
    assert len(observed) == 1
    assert observed[0][2] == 2
    assert data['version'] == 2
    assert data['accounting'] == {'semantics': 'core_home_ordinary_v1', 'spent_runs': 2}
    b = cert.parse(next(e['payload'] for e in data['events'] if e['kind'] == 'spend_budget_set'))
    assert b['runs'] == 2 + a.proof['intent']['runs']
    assert data['application']['result']['version'] == 1
    assert prepared.prepare(a.conn, a.proof, delivery_uid='delivery-1') == data
    assert len(observed) == 1


@pytest.mark.parametrize('governed', [False, True])
def test_v2_final_governance_union(canonical, governed):
    value = canonical
    op = value['operation']
    op['proposal'].update(version=2, accounting={'semantics': 'core_home_ordinary_v1', 'spent_runs': 0})
    allowance = value.pop('allowance')
    value.update(version=2, governance={'state': 'governed', 'allowance': allowance} if governed else {'state': 'ungoverned'})
    assert final.validate(value, op) == value


def test_v2_unknown_governance_cannot_borrow_valid_allowance(canonical):
    op = canonical['operation']
    op['proposal'].update(version=2, accounting={'semantics':'core_home_ordinary_v1','spent_runs':0})
    allowance = canonical.pop('allowance')
    canonical.update(version=2, governance={'state':'unknown','allowance':allowance})
    with pytest.raises(ValueError, match='^canonical application governance differs$'):
        final.validate(canonical, op)


@pytest.mark.parametrize('bad', [True, 2.0, '2', 0, 3])
def test_proposal_version_strict(canonical, bad):
    p = canonical['operation']['proposal']
    p.update(version=bad, accounting={'semantics': 'core_home_ordinary_v1', 'spent_runs': 0})
    with pytest.raises(ValueError):
        prepared._validate(p)


@pytest.mark.parametrize('bad', [True, 0.0, -1, 9007199254740992, None, '0'])
def test_accounting_spent_strict(canonical, bad):
    p = canonical['operation']['proposal']
    p.update(version=2, accounting={'semantics': 'core_home_ordinary_v1', 'spent_runs': bad})
    with pytest.raises(ValueError):
        prepared._validate(p)


@pytest.mark.parametrize('bad', [{}, {'state': 'unknown'}, {'state':'unknown','allowance':{}}, {'state':'ungoverned','allowance':{}}, {'state':'governed'}, {'state':'ungoverned','extra':1}])
def test_governance_exact(canonical, bad):
    p = canonical['operation']['proposal']
    p.update(version=2, accounting={'semantics': 'core_home_ordinary_v1', 'spent_runs': 0})
    canonical.pop('allowance')
    canonical.update(version=2, governance=bad)
    with pytest.raises(ValueError):
        final.validate(canonical, canonical['operation'])


@pytest.mark.parametrize('feature,expected', [(None, 1), (True, 1), (2.0, 1), ('2', 1), (1, 1), (2, 2)])
def test_capability_before_freeze_and_no_advertisement(assembly, monkeypatch, feature, expected):
    from hermes_cli import kanban_release_final_home as home, kanban_release_home as transport
    from types import SimpleNamespace
    a = assembly
    row = a.conn.execute('SELECT * FROM owner_release_home_queue').fetchone()
    monkeypatch.setattr(transport, '_identity', lambda *args: None)
    calls = []
    def capability(board):
        calls.append('capability')
        return {} if feature is None else {'owner_release_application': feature}
    def freeze(*args, **kwargs):
        calls.append('freeze')
        assert kwargs.get('application_version', 1) == expected
        raise RuntimeError('freeze observed')
    hub = SimpleNamespace(decision_retain_capability=capability)
    monkeypatch.setattr(final, 'freeze', freeze)
    with pytest.raises(RuntimeError, match='freeze observed'):
        home.process(a.conn, hub, row, 'home', 'example')
    assert calls == ['capability', 'freeze']


def test_saved_v2_incompatible_capability_never_sends(assembly, monkeypatch):
    from hermes_cli import kanban_release_final_home as home, kanban_release_home as transport
    from types import SimpleNamespace
    a = assembly
    final.freeze(a.conn, a.proof, delivery_uid='delivery-1', application_version=2)
    row = a.conn.execute('SELECT * FROM owner_release_home_queue').fetchone()
    monkeypatch.setattr(transport, '_identity', lambda *args: None)
    hub = materialize_helpers.Hub(a)
    hub.decision_retain_capability = lambda board: {}
    with pytest.raises(ValueError, match='application v2'):
        home.process(a.conn, hub, row, 'home', 'example')
    assert final.saved(a.conn, 'intent-1')['proposal']['version'] == 2


@pytest.mark.parametrize('writer,unlimited,reached', [('agent', True, False), ('agent', True, True), ('human', False, False), ('human', False, True), ('unknown', False, False)])
def test_actual_usage_receipt_cache_and_provenance_parity(assembly, writer, unlimited, reached):
    from types import SimpleNamespace
    import time
    a = assembly
    now = int(time.time())
    with kb.write_txn(a.conn):
        rid = a.conn.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome) VALUES (?,'default','continued',?,?,'continued')",
                             (a.tid, now-1, now)).lastrowid
        kb._append_event(a.conn, a.tid, 'worker_result', {'usage': {'cache_read_tokens': 100}}, run_id=rid)
        prior = {'runs': 0 if unlimited else 10, 'cache_read_tokens': 50 if reached else 1000,
                 'writer_kind': writer, 'reason':'prior', 'by':'prior'}
        if writer == 'unknown':
            prior.pop('writer_kind')
        # Standing budget predates the selected stop, not an unacknowledged
        # competing decision made after it.
        a.conn.execute("INSERT INTO task_events(uid,task_id,kind,payload,created_at,origin_gateway) VALUES ('prior-budget',?,'spend_budget_set',?,1,'home')",
                       (a.tid, cert.encode(prior)))
    spent = budget.spend(a.conn, a.tid)
    assert spent.runs == 1 and spent.reported == 1 and spent.tokens['cache_read_tokens'] == 100
    state = SimpleNamespace(tid=a.tid, writer=('agent', 'chat-1', 'ordinary'), runs=a.proof['intent']['runs'])
    before = snapshot(a)
    if writer == 'human' and reached:
        with pytest.raises(release._Refusal, match='cache-read limit'):
            release._budget_plan(a.conn, state)
        with pytest.raises(release._Refusal, match='cache-read limit'):
            prepared.prepare(a.conn, a.proof, delivery_uid='delivery-1', application_version=2)
    else:
        ordinary = release._budget_plan(a.conn, state)
        data = prepared.prepare(a.conn, a.proof, delivery_uid='delivery-1', application_version=2)
        assert data['accounting']['spent_runs'] == ordinary[2] == 1
        budgets = [cert.parse(e['payload']) for e in data['events'] if e['kind']=='spend_budget_set']
        if ordinary[0] is None:
            assert budgets == [] and data['comments'] == []
        else:
            assert budgets[0]['runs'] == ordinary[0]
            assert budgets[0]['cache_read_tokens'] == ordinary[1]
            if writer in ('human', 'unknown'):
                assert budgets[0]['cache_read_writer_kind'] == writer
    assert snapshot(a) == before


@pytest.mark.parametrize('accounting', [None, {}, {'semantics':'other','spent_runs':0},
    {'semantics':'core_home_ordinary_v1','spent_runs':0,'window_start':0}])
def test_accounting_exact_keys(canonical, accounting):
    p = canonical['operation']['proposal']
    p.update(version=2, accounting=accounting)
    with pytest.raises(ValueError, match='accounting'):
        prepared._validate(p)


@pytest.mark.parametrize('version', [1, True, 2.0, '2'])
def test_certificate_version_must_match_proposal_strictly(canonical, version):
    p = canonical['operation']['proposal']
    p.update(version=2, accounting={'semantics':'core_home_ordinary_v1','spent_runs':0})
    canonical.pop('allowance')
    canonical.update(version=version, governance={'state':'ungoverned'})
    with pytest.raises(ValueError, match='operation'):
        final.validate(canonical, canonical['operation'])


def test_v1_does_not_accept_accounting(canonical):
    canonical['operation']['proposal']['accounting'] = {'semantics':'core_home_ordinary_v1','spent_runs':0}
    with pytest.raises(ValueError, match='shape'):
        final._wire(canonical['operation'])


def test_saved_v1_stays_v1_with_feature2(assembly, monkeypatch):
    from hermes_cli import kanban_release_final_home as home, kanban_release_home as transport
    from tests.hermes_cli.test_owner_release_materialize import Hub
    a = assembly
    expected = final.freeze(a.conn, a.proof, delivery_uid='delivery-1')
    hub = Hub(a)
    hub.decision_retain_capability = lambda board: {'owner_release_application': 2}
    monkeypatch.setattr(transport, '_identity', lambda *args: None)
    row = a.conn.execute('SELECT * FROM owner_release_home_queue').fetchone()
    home.process(a.conn, hub, row, 'home', 'example')
    assert hub.posts == [[expected]]
    assert final.saved(a.conn, 'intent-1') == expected


class _V2Hub(materialize_helpers.Hub):
    """Actual materialize-test Hub adapter stamped as v2."""
    def __init__(self, a, capability, loss=False):
        super().__init__(a, loss)
        self.cap_calls = 0
        self._cap = capability

    def decision_retain_capability(self, board):
        self.cap_calls += 1
        if isinstance(self._cap, Exception):
            raise self._cap
        return self._cap

    def ops(self, board, ops):
        try:
            return super().ops(board, ops)
        finally:
            _as_v2(self.value)


def _as_v2(value):
    value.pop('allowance', None)
    value.update(version=2, governance={'state': 'ungoverned'})
    return value


def _setup(a, monkeypatch):
    from hermes_cli import kanban_release_home as transport, kanban_operator_call as operator
    monkeypatch.setattr(operator, '_board_slug', lambda _conn: 'example')
    return a.conn.execute('SELECT * FROM owner_release_home_queue').fetchone()


@pytest.mark.parametrize('later', [{}, {'owner_release_application': 1}, OSError('capability down')])
def test_completed_local_v2_precedes_mutable_capability(assembly, monkeypatch, later):
    from hermes_cli import kanban_release_home as home
    a = assembly
    row = _setup(a, monkeypatch)
    hub = _V2Hub(a, {'owner_release_application': 2})
    got = home._process(a.conn, hub, row, 'home', 'example')
    assert len(hub.posts) == 1 and hub.posts[0][0]['proposal']['version'] == 2
    hub._cap, hub.cap_calls, hub.value = later, 0, None
    assert home._process(a.conn, hub, row, 'home', 'example') == got
    assert hub.cap_calls == 0 and len(hub.posts) == 1


@pytest.mark.parametrize('later', [{}, OSError('capability down')])
def test_remote_v2_history_recovers_without_capability_or_post(assembly, monkeypatch, later):
    from hermes_cli import kanban_release_home as home
    a = assembly
    row = _setup(a, monkeypatch)
    wire = final.freeze(a.conn, a.proof, delivery_uid='delivery-1', application_version=2)
    hub = _V2Hub(a, later)
    hub.value = _as_v2(materialize_helpers.adapter(wire))
    got = home._process(a.conn, hub, row, 'home', 'example')
    assert got['result']['outcome'] == 'released'
    assert hub.posts == [] and hub.cap_calls == 0


@pytest.mark.parametrize('later', [{}, {'owner_release_application': 1}])
def test_missing_remote_v2_with_refused_capability_never_sends(assembly, monkeypatch, later):
    from hermes_cli import kanban_release_home as home
    a = assembly
    row = _setup(a, monkeypatch)
    wire = final.freeze(a.conn, a.proof, delivery_uid='delivery-1', application_version=2)
    hub = _V2Hub(a, later)
    with pytest.raises(ValueError, match='application v2'):
        home._process(a.conn, hub, row, 'home', 'example')
    assert hub.posts == [] and final.saved(a.conn, 'intent-1') == wire


@pytest.mark.parametrize('version', [True, False, 1.0, 2.0, '1', '2', None, 0, 3])
@pytest.mark.parametrize('entrypoint', ['prepare', 'freeze'])
def test_new_version_api_rejects_non_integer_selection(assembly, version, entrypoint):
    a = assembly
    final.install(a.conn)
    before = snapshot(a)
    call = prepared.prepare if entrypoint == 'prepare' else final.freeze
    with pytest.raises(ValueError, match='^unsupported application version$'):
        call(a.conn, a.proof, delivery_uid='delivery-1', application_version=version)
    assert snapshot(a) == before
    assert prepared.verified_prepared(a.conn, 'intent-1') is None
    assert final.saved(a.conn, 'intent-1') is None


@pytest.mark.parametrize('explicit', [False, True])
def test_v1_freeze_preserves_legacy_prepare_call_seam(assembly, monkeypatch, explicit):
    a = assembly
    original = prepared.prepare
    calls = []
    def legacy(conn, proof, *, delivery_uid):
        calls.append(delivery_uid)
        return original(conn, proof, delivery_uid=delivery_uid)
    monkeypatch.setattr(prepared, 'prepare', legacy)
    kwargs = {'application_version': 1} if explicit else {}
    wire = final.freeze(a.conn, a.proof, delivery_uid='delivery-1', **kwargs)
    assert wire is not None
    assert calls == ['delivery-1']
    assert wire['proposal']['version'] == 1
    assert 'accounting' not in wire['proposal']
    assert final.freeze(a.conn, a.proof, delivery_uid='delivery-1', application_version=2) == wire


@pytest.mark.parametrize('count', [0, 2])
def test_v2_requires_exactly_one_observation_and_restores_context(assembly, monkeypatch, count):
    from hermes_cli import kanban_release_assemble as assemble
    a = assembly
    original = assemble._budget
    def altered(conn, body, seen):
        result = original(conn, body, seen)
        capture = assemble._BUDGET_OBSERVATION.get()
        assert capture is not None
        capture[:] = capture * count
        return result
    monkeypatch.setattr(assemble, '_budget', altered)
    outside = [99]
    token = assemble._BUDGET_OBSERVATION.set(outside)
    before = snapshot(a)
    try:
        with pytest.raises(ValueError, match='^proposal requires exactly one real budget observation$'):
            prepared.prepare(a.conn, a.proof, delivery_uid='delivery-1', application_version=2)
        assert assemble._BUDGET_OBSERVATION.get() is outside
        assert outside == [99]
        assert snapshot(a) == before
        assert prepared.verified_prepared(a.conn, 'intent-1') is None
    finally:
        assemble._BUDGET_OBSERVATION.reset(token)


@pytest.mark.parametrize('field,bad', [('runs', True), ('used', 0.0), ('remaining', 0),
    ('remaining', 9007199254740992), ('window_uid', ''), ('episode_uid', 'other')])
def test_v2_governed_still_validates_allowance(canonical, field, bad):
    op = canonical['operation']
    op['proposal'].update(version=2, accounting={'semantics':'core_home_ordinary_v1','spent_runs':0})
    allowance = canonical.pop('allowance')
    allowance[field] = bad
    canonical.update(version=2, governance={'state':'governed','allowance':allowance})
    with pytest.raises(ValueError, match='^canonical application protected allowance differs$'):
        final.validate(canonical, op)
