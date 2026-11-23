"""Real prepare with a many-profile roster is inert: no scan, source, owner or handle."""
from collections import Counter
import threading

from hermes_cli import fact_annotation_boot as boot
from tests.hermes_cli import test_kanban_session_activity as helpers

kanban_root = helpers.kanban_root


def test_eleven_profile_prepare_is_inert(kanban_root, tmp_path, monkeypatch):
    from hermes_cli import profiles
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli.web_routers import profiles as routes
    roster = []
    for i in range(11):
        home = tmp_path / f'p{i}'
        home.mkdir()
        roster.append(('default' if i == 0 else f'p{i}', home))
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(routes, '_profile_targets', lambda reason: roster)
    monkeypatch.setattr(host, 'suspend_holder', lambda: None)
    monkeypatch.setattr(host, 'resume_holder', lambda role: None)

    def scanned(*args, **kwargs):
        raise AssertionError('sessions reader seed scan')
    monkeypatch.setattr(routes, 'get_profiles_sessions', scanned)
    monkeypatch.setattr(routes, 'get_profiles_sessions_sidebar', scanned)
    monkeypatch.setattr(boot, 'seeds', scanned)
    before = Counter(t.name for t in threading.enumerate() if t.name.startswith('fact-'))
    runtime = boot.prepare()
    try:
        assert Counter(t.name for t in threading.enumerate() if t.name.startswith('fact-')) == before
        assert len(runtime.bindings) == 11 and runtime._started
        assert not runtime.handles and not runtime.owners and not runtime._registered
        assert all(b.real is None for b in runtime.bindings.values())
        assert all(b._mode == 'legacy' for b in runtime.bindings.values())
    finally:
        runtime.close()
