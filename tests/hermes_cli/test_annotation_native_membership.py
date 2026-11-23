"""Native goal/cron capture to actual decorated requests, without fixture facts."""
import json
import threading

import pytest

from hermes_cli import kanban_annotation_facts as annotations
from hermes_cli.fact_annotation_membership import AnnotationMembershipSource
from hermes_cli.fact_annotation_views import projections, selector_for
from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_content_owner import ContentOwner
from hermes_cli.fact_contract import Scope, SourceId
from hermes_cli.fact_inprocess import InProcessFacts, IncrementalLifecycle
from hermes_cli.fact_materializers import view_reducer
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_store import FactStore, view_key
from hermes_cli.goal_session_activity import annotate_goal_activity, project_goal_activity
from hermes_cli.cron_session_activity import annotate_cron_activity, project_cron_activity
from tests.hermes_cli import test_fact_session_source as sessions
from tests.waiting import wait_until

store = sessions.store


@pytest.mark.platforms('linux')
def test_native_capture_to_real_consumer_mutation_and_lineage(store, tmp_path, monkeypatch):
    db, _, path = store
    db._execute_write(lambda conn: conn.execute(
        'INSERT INTO state_meta(key,value) VALUES (?,?)',
        ('goal:root', json.dumps({'status': 'active', 'goal': 'test'}))))
    jobs = tmp_path / 'jobs.json'
    jobs.write_text(json.dumps([{'owner_session_id': 'root', 'next_run_at': 'tomorrow'}]))
    scope = Scope('native-annotations', 'default')
    facts = FactStore()
    engine = ReducerEngine(facts, scope)
    engine.register(view_reducer(projections()))
    sources = [AnnotationMembershipSource(engine, SourceId(scope, 'annotations.' + family, 'local'), p)
               for family, p in [('goal', path), ('cron', jobs)]]
    threads = set()
    for source in sources:
        original = source.capture
        def capture(*args, original=original):
            threads.add(threading.get_ident())
            return original(*args)
        monkeypatch.setattr(source, 'capture', capture)
    owner = ContentOwner({scope: engine}, [], operational=sources, budget=CaptureBudget(max_seconds=2))
    local = InProcessFacts(facts, {scope}, demand=owner.demand)
    life = IncrementalLifecycle({(scope, 'annotations')})
    handle = local.acquire(scope, lifecycle=life)
    consumer = annotations.AnnotationFacts(handle, scope, stop_legacy=lambda: None,
                                          start_legacy=lambda: None, view_facts=True)
    life.report_ready('annotations', scope)
    life.transition('annotations', scope, 'facts', 1)
    rows = [{'id': 's', '_lineage_ids': ['root']}, {'id': 's', '_lineage_ids': ['other']}]
    try:
        for family in ('goal', 'cron'):
            for row in rows:
                owner.demand(scope, 'annotations.' + family, selector_for(scope, row))
        def ready():
            snapshot = facts.snapshot(scope)
            return all((f := snapshot.get(view_key(scope, 'annotations.' + family, selector_for(scope, row))))
                       is not None and f.health == 'fresh' for family in ('goal', 'cron') for row in rows)
        assert wait_until(ready, timeout=10)
        # Real decorated consumer calls; their legacy collectors must never run.
        def forbidden():
            pytest.fail('request performed legacy capture')
        monkeypatch.setattr('hermes_cli.goal_session_activity.active_goal_sessions', forbidden)
        monkeypatch.setattr('hermes_cli.cron_session_activity.cron_owner_sessions', forbidden)
        expected = [dict(row) for row in rows]
        project_goal_activity(expected, {'root'})
        project_cron_activity(expected, {'root'})
        with annotations.use(consumer):
            actual = annotate_cron_activity(annotate_goal_activity([dict(row) for row in rows]))
        assert actual == expected
        assert threads == {owner.feed._thread.ident}
        before = facts.snapshot(scope)
        db._execute_write(lambda conn: conn.execute("DELETE FROM state_meta WHERE key='goal:root'"))
        goal_key = view_key(scope, 'annotations.goal', selector_for(scope, rows[0]))
        assert wait_until(lambda: facts.snapshot(scope).get(goal_key).value.get('payload') == {}, timeout=10)
        assert before.get(goal_key).value['payload'] == {'goal_active': True}
        # Malformed inventory is unavailable, never certified zero. Repair from
        # a real native file hint heals the already-accepted view demand.
        jobs.write_text('{broken')
        cron_key = view_key(scope, 'annotations.cron', selector_for(scope, rows[0]))
        assert wait_until(lambda: facts.snapshot(scope).get(cron_key).health == 'stale', timeout=10)
        with annotations.use(consumer), pytest.raises(annotations.AnnotationUnavailable):
            annotate_cron_activity([dict(rows[0])])
        jobs.write_text('[]')
        assert wait_until(lambda: facts.snapshot(scope).get(cron_key).health == 'fresh'
                          and facts.snapshot(scope).get(cron_key).value['payload'] == {}, timeout=10)
    finally:
        life.transition('annotations', scope, 'legacy', 2)
        consumer.close()
        owner.close()
        handle.close()
        local.close()


def test_successful_recapture_not_demand_renews_evidence(tmp_path, monkeypatch):
    from hermes_cli.fact_annotation_membership import MembershipCapture
    scope = Scope('renewal', 'default')
    facts = FactStore()
    engine = ReducerEngine(facts, scope)
    source = AnnotationMembershipSource(engine, SourceId(scope, 'annotations.cron', 'local'), tmp_path / 'jobs')
    source.commit(MembershipCapture({'owners': ()}, 'one', 100, 30))
    rev = facts.snapshot(scope).rev
    source.commit(MembershipCapture({'owners': ()}, 'one', 110, 40))
    assert facts.snapshot(scope).rev == rev
    source.commit(MembershipCapture({'owners': ()}, 'one', 131, 61))
    assert facts.snapshot(scope).get(source.key).observed_at == 131
    assert facts.snapshot(scope).get(source.key).valid_until >= 211
    source.failure()
    assert facts.snapshot(scope).get(source.key).health == 'stale'
    assert facts.snapshot(scope).get(source.key).observed_at == 131


def test_cron_absence_is_empty_but_bad_json_is_unavailable(tmp_path):
    from hermes_cli.fact_annotation_membership import _cron
    path = tmp_path / 'not-created' / 'jobs.json'
    assert _cron(path, CaptureBudget()) == {'owners': ()}
    path.parent.mkdir()
    path.write_text('')
    assert _cron(path, CaptureBudget()) == {'owners': ()}
    path.write_text('{broken')
    with pytest.raises(ValueError):
        _cron(path, CaptureBudget())
