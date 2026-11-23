"""Private native captures must not publish turn lease CAS credentials."""
from copy import deepcopy
import time

from hermes_cli import fact_annotation_capture as capture
from hermes_cli.fact_annotation_views import projections, selector_for
from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_wire import encode
from hermes_cli.turn_session_activity import project_turn_activity
from tests.hermes_cli import test_fact_session_source as helpers
from tests.hermes_cli.annotation_fact_processes import request_guard

store = helpers.store


def test_native_turn_input_drops_cas_holder_but_preserves_canonical_observation(store):
    db, _, path = store
    now = time.time()
    secret = 'opaque-cas-credential-never-publish'
    db.create_session('root', 'cli')
    db._execute_write(lambda conn: conn.execute(
        'INSERT INTO session_turn_leases(conversation_id,holder,acquired_at,expires_at) VALUES (?,?,?,?)',
        ('root', secret, now, now + 60)))
    value, _ = capture.turns(path, CaptureBudget(max_seconds=2), profile='default', gateway='gateway', now=now)
    scope = Scope('turn-capture', 'default')
    key = FactKey(scope, 'AnnotationInput', 'turn')
    fact = Fact(key, value, health='fresh')
    assert secret.encode() not in encode(fact.value)
    assert secret not in repr(fact.value)
    expected = [{'id': 'root', 'profile': 'default'}]
    project_turn_activity(expected, live={'root'}, leases={'root': [{'conversation_id': 'root',
        'holder': secret, 'acquired_at': now, 'expires_at': now + 60, 'profile': 'default'}]},
        gateway='gateway', now=now)
    projection = next(p for p in projections() if p.name == 'annotations.turn')
    row = {'id': 'root', 'profile': 'default'}
    with request_guard():
        actual = projection.render(selector_for(scope, row), {key: fact}, now)
    assert {**deepcopy(row), **actual} == expected[0]
    assert secret not in repr(actual)
