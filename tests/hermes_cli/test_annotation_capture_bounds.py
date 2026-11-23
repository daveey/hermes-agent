"""Review membership admission cannot poison existing native captures."""
import time

import pytest

from hermes_cli import fact_annotation_capture as capture
from hermes_cli.fact_annotation_sources import NativeAnnotationOwner, NativeAnnotationSource
from hermes_cli.fact_annotation_views import projections, selector_for
from hermes_cli.fact_capture import CaptureBudget, CaptureBudgetExceeded
from hermes_cli.fact_contract import Scope, SourceId
from hermes_cli.fact_materializers import view_reducer
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_store import FactStore
from tests.hermes_cli import test_fact_session_source as helpers

store = helpers.store


def test_review_demand_budget_and_failed_mailbox_admission_preserve_membership():
    scope = Scope('review-demand-bounds', 'default')
    engine = ReducerEngine(FactStore(), scope)
    engine.register(view_reducer(projections()))
    source = NativeAnnotationSource(engine, SourceId(scope, 'annotations.review', 'local'), [],
        lambda budget, wall, ids: ({'messages': {}, 'reviews': []}, wall + 30))
    owner = NativeAnnotationOwner({scope: engine}, [], operational=[source],
                                  budget=CaptureBudget(max_keys=1, max_seconds=2))
    try:
        owner.demand(scope, 'annotations.review', selector_for(scope, {'id': 's'}))
        assert source._ids == {'s'}
        with pytest.raises(CaptureBudgetExceeded):
            owner.demand(scope, 'annotations.review', selector_for(scope, {'id': 'other'}))
        assert source._ids == {'s'}
        with pytest.raises(ValueError):
            owner.demand(scope, 'annotations.review', {})
        assert source._ids == {'s'}
        # Mailbox validation refuses a new key before source membership changes.
        source.max_keys = 2
        owner._demands[scope].close()
        with pytest.raises(RuntimeError):
            owner.demand(scope, 'annotations.review', selector_for(scope, {'id': 'other'}))
        assert source._ids == {'s'}
    finally:
        owner.close()


def test_review_raw_assistant_text_total_byte_budget_precedes_provider_io(store, monkeypatch):
    from hermes_cli import review_session_activity as review
    db, _, path = store
    now = time.time()
    for sid in ('a', 'b'):
        db.create_session(sid, 'cli')
        db._execute_write(lambda conn, sid=sid: conn.execute(
            'INSERT INTO messages(session_id,role,content,timestamp) VALUES (?,\'assistant\',?,?)',
            (sid, 'x' * 3000, now)))
    monkeypatch.setattr(review, '_pr_info', lambda key: pytest.fail('provider lookup before byte validation'))
    with pytest.raises(CaptureBudgetExceeded):
        capture.reviews(path, CaptureBudget(max_bytes=5000, max_seconds=2), ids={'a', 'b'}, now=now)
