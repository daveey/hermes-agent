"""A fact-subsystem failure must never break the session lists. [daveey]

Annotations (kanban/goal/cron/...) are optional enrichment of rows the session DB already served. With the
fact owner active but a projection unavailable (demand capacity, cold/expired fact, owner transfer) or the
facts path raising, ``/api/profiles/sessions`` and the sidebar answer 200 with the legacy DB rows; kanban rows
are marked ``kanban_unknown`` so no client renders them as "no work". Legacy annotation readers are never
re-opened behind the active owner fence. The session-kanban-tasks strip stays an honest 503, never a 500.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_annotation_facts as annotations

from tests.hermes_cli import test_profiles_sidebar_failed_load as _base

# Reuse the real-route fixtures by name (assignment, not import, so pytest resolves them in this module).
_uncached_sidebar = _base._uncached_sidebar
profiles_on_disk = _base.profiles_on_disk
client = _base.client
_seed_session = _base._seed_session


class _Failing:
    def __init__(self, error):
        self.error = error
        self.calls = []

    def annotate(self, family, rows, legacy, **kwargs):
        self.calls.append(family)
        raise self.error


ERRORS = [
    annotations.AnnotationUnavailable('annotation demand unavailable'),
    annotations.AnnotationUnavailable('annotation projection unavailable'),
    annotations.AnnotationUnavailable('annotation owner transfer active or binding missing'),
]


@pytest.fixture
def facts_unavailable(monkeypatch):
    def install(error):
        failing = _Failing(error)
        monkeypatch.setattr(annotations, '_native_lookup', lambda profile: failing)
        # The legacy readers must not run behind the (simulated) active owner fence.
        monkeypatch.setattr(annotations, '_legacy_call', lambda *a, **k: pytest.fail('legacy annotation reader ran'))
        return failing
    return install


@pytest.mark.parametrize('error', ERRORS, ids=lambda e: str(e))
def test_sessions_list_and_sidebar_stay_200_when_annotations_unavailable(
        client, profiles_on_disk, facts_unavailable, error):
    _seed_session(profiles_on_disk['default'], 'chat-one')
    failing = facts_unavailable(error)

    listing = client.get('/api/profiles/sessions', params={'profile': 'all'})
    assert listing.status_code == 200, listing.text
    rows = listing.json()['sessions']
    assert [row['id'] for row in rows] == ['chat-one']
    assert rows[0].get('kanban_unknown') is True
    assert 'kanban' in failing.calls

    sidebar = client.get('/api/profiles/sessions/sidebar', params={'recents_profile': 'default'})
    assert sidebar.status_code == 200, sidebar.text
    recents = sidebar.json()['recents']
    assert [row['id'] for row in recents['sessions']] == ['chat-one']
    assert recents['sessions'][0].get('kanban_unknown') is True
    assert not recents.get('failed')


def test_single_profile_session_routes_stay_200(client, profiles_on_disk, facts_unavailable):
    _seed_session(profiles_on_disk['default'], 'chat-one')
    facts_unavailable(ERRORS[0])
    listing = client.get('/api/sessions')
    assert listing.status_code == 200, listing.text
    assert [row['id'] for row in listing.json()['sessions']] == ['chat-one']


def test_best_effort_leaves_rows_unmutated_and_passes_other_errors_through():
    rows = [{'id': 'a'}]

    def unavailable(rows, **kwargs):
        raise annotations.AnnotationUnavailable('annotation demand unavailable')

    assert annotations.best_effort(unavailable, 'goal')(rows) is rows
    assert rows == [{'id': 'a'}]
    marked = annotations.best_effort(unavailable, 'kanban')(rows, display=True)
    assert marked[0]['id'] == 'a' and marked[0].get('kanban_unknown') is True

    def broken(rows, **kwargs):
        raise KeyError('a programming error is not an unavailable projection')

    with pytest.raises(KeyError):
        annotations.best_effort(broken, 'goal')(rows)


def test_facts_path_exception_becomes_unavailable_not_a_request_fault():
    from hermes_cli.fact_contract import Scope

    class Boom(annotations.AnnotationFacts):
        def __init__(self):
            self._lock = __import__('threading').RLock()
            self._closed, self._mode, self._needs_restart = False, 'facts', False

        def _apply(self, family, rows, **kwargs):
            raise RuntimeError('reducer exploded')

    with pytest.raises(annotations.AnnotationUnavailable, match='facts failed'):
        Boom().annotate('kanban', [{'id': 'a'}], None)
    assert Scope  # fixture import kept explicit for readers


def test_session_kanban_tasks_links_outage_is_503_not_500(client, monkeypatch):
    from hermes_cli.fact_remote_links import LinksUnavailable
    from hermes_cli.web_routers import sessions as sessions_routes

    def unavailable(*args, **kwargs):
        raise LinksUnavailable('remote links projection unavailable', error_code='links_capture_pending')

    monkeypatch.setattr(sessions_routes, 'session_kanban_tasks', unavailable)
    response = client.get('/api/session-kanban-tasks', params={'ids': 'chat-one'})
    assert response.status_code == 503, response.text
    assert response.headers.get('retry-after') == '5'
