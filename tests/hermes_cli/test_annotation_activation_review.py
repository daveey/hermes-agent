"""Review repros: optional absence and exact remote replica parity. [daveey]"""
import time

import pytest

from hermes_cli import kanban_session_activity as kanban
from hermes_cli import review_session_activity as review
from hermes_cli.fact_annotation_runtime import AnnotationRuntime
from tests.hermes_cli import test_kanban_session_activity as helpers

kanban_root = helpers.kanban_root


@pytest.mark.parametrize('case', ['healthy', 'unused-profile', 'no-pr-info', 'failed-pr-info'])
def test_optional_absence_does_not_prevent_startup(kanban_root, tmp_path, monkeypatch, case):
    from hermes_state import SessionDB
    from hermes_cli.fact_contract import FactKey
    from hermes_cli.fact_annotation_views import selector_for
    from hermes_cli.fact_store import view_key
    from tests.waiting import wait_until
    home = tmp_path / 'default'
    home.mkdir()
    db = SessionDB(db_path=home / 'state.db')
    db.create_session('seed', 'cli')
    homes = {'default': home}
    monkeypatch.setattr(kanban, '_remote_cards', lambda *args: [])
    if case == 'unused-profile':
        unused = tmp_path / 'profiles/unused'
        unused.mkdir(parents=True)
        (unused / 'config.yaml').write_text('{}')
        homes['unused'] = unused
    if case in ('no-pr-info', 'failed-pr-info'):
        db._execute_write(lambda conn: conn.execute(
            "INSERT INTO messages(session_id,role,content,timestamp) VALUES ('seed','assistant',?,1)",
            ('https://github.com/review-fixture/private/pull/7',)))
        def fetch(key):
            if case == 'failed-pr-info':
                raise RuntimeError('provider unavailable')
        monkeypatch.setattr(review, '_fetch_override', fetch)
        monkeypatch.setattr(review, '_sync_fetch', True)
        monkeypatch.setattr(review, '_pr_cache', {})
        monkeypatch.setattr(review, '_inflight', set())
    runtime = AnnotationRuntime(homes, fallback_profile='default', gateway='fixture',
                                stop_legacy=lambda: None, start_legacy=lambda: None)
    try:
        row = {'id': 'seed', 'profile': 'default'}
        runtime.start({'default': [row]}, timeout=2)
        if case == 'unused-profile':
            key = FactKey(runtime.scopes['unused'], 'AnnotationInput', 'turn')
            fact = runtime.store.snapshot(runtime.scopes['unused']).get(key)
            assert fact is None or fact.health != 'fresh'
            assert not (homes['unused'] / 'state.db').exists()
        if case in ('no-pr-info', 'failed-pr-info'):
            scope = runtime.scopes['default']
            key = view_key(scope, 'annotations.review', selector_for(scope, row))
            assert wait_until(lambda: runtime.store.snapshot(scope).get(key) is not None, timeout=3)
            assert runtime.store.snapshot(scope).get(key).health == 'unknown'
            # Optional unavailability is not a certified empty review result,
            # and it never admits legacy I/O or fails the whole response.
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            from tests.hermes_cli.annotation_fact_processes import request_guard
            app = FastAPI()
            @app.get('/sessions')
            def sessions():
                return review.annotate_review_wait([dict(row)])
            with TestClient(app) as client, request_guard():
                for _ in range(3):
                    response = client.get('/sessions')
                    assert response.status_code == 200
                    assert response.json() == [row]
            assert runtime.store.snapshot(scope).get(key).health == 'unknown'
            monkeypatch.setattr(review, '_fetch_override', lambda key: {
                'open': True, 'review_required': True, 'viewer': 'viewer',
                'author': 'viewer', 'requested': [], 'requested_at': 1})
            # Existing provider completion wakes the source without a DB write.
            review._refresh(('review-fixture', 'private', 7))
            assert wait_until(lambda: runtime.store.snapshot(scope).get(key).health == 'fresh', timeout=3)
            with TestClient(app) as client, request_guard():
                response = client.get('/sessions')
                assert response.status_code == 200
                assert response.json()[0]['review_wait'] == 'stuck'
    finally:
        runtime.close()
        db.close()


def test_remote_clock_ci_deploy_producer_projection_parity(monkeypatch):
    from hermes_cli.fact_annotation_kanban import _remote
    from hermes_cli.fact_annotation_views import _kanban
    from hermes_cli.kanban_external_activity import replica_markers, stamp_replica_counts
    from hermes_cli.kanban_activity_identity import row_ids
    now = time.time()
    cards = [dict(session='root', key='peer/default/' + kind, id=kind,
                  status='scheduled', title=kind + ' wait', parked_at=now - 60,
                  awaiting={'kind': kind, 'until': now + 600,
                            'ref': 'https://example.com/' + kind},
                  park={'outcome': 'scheduled', 'wake_at': now + 600,
                        'reason': 'wait', 'ended_at': now - 60})
             for kind in ('clock', 'ci', 'deploy')]
    legacy_keys = {'root': set()}
    for card in cards:
        legacy_keys['root'].update(replica_markers(card))
        legacy_keys['root'].add((card['key'], str(kanban._replica_bucket(card)), True))
    maps, native_keys = tuple({} for _ in range(8)), {}
    _remote(cards, maps, native_keys, {})
    assert native_keys == legacy_keys
    expected = {'id': 'root'}
    stamp_replica_counts([expected], legacy_keys, row_ids)
    captured = {'parts': (*maps[:4], {}, maps[5], maps[6], maps[7], native_keys, {}, {}, {}),
                'observed_at': now, 'external': {}}
    row = {'id': 'root'}
    _kanban(row, captured, now)
    assert {k: v for k, v in row.items() if k.startswith('kanban_external')} == {
        k: v for k, v in expected.items() if k.startswith('kanban_external')}
    assert row['kanban_external'] == 3
    assert row['kanban_external_unknown'] == 2
    assert len(row['kanban_external_cards']) == 3


def test_default_startup_never_admits_native(monkeypatch):
    from hermes_cli import fact_annotation_boot as boot
    from hermes_cli import web_server
    from fastapi.testclient import TestClient
    from tests.hermes_cli.test_serve_unloaded_sweeper import _quiet_boot
    _quiet_boot(monkeypatch)
    monkeypatch.delenv('HERMES_FACTS_ANNOTATION', raising=False)
    monkeypatch.setattr(boot, 'prepare', lambda **kw: pytest.fail('default admitted blocking startup'))
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli import kanban_annotation_prewarm as prewarm
    calls = []
    monkeypatch.setattr(host, 'start_holder', lambda role: calls.append('legacy'))
    monkeypatch.setattr(prewarm, 'start_annotation_prewarm', lambda: calls.append('prewarm'))
    with TestClient(web_server.app):
        assert getattr(web_server.app.state, 'annotation_facts', None) is None
    assert calls == ['prewarm', 'legacy']
