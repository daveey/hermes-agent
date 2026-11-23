"""Real gateway normalization → detached probes → canonical tree. [daveey]"""
from tui_gateway import server
from tui_gateway import project_tree_capture


def test_native_capture_reuses_live_builder_normalization(monkeypatch):
    rows = ([{'id': 's1', 'cwd': '/fixture/repo', 'git_repo_root': '/fixture/repo'}], [], [], None)
    monkeypatch.setattr(server, '_project_tree_inputs', lambda *a, **k: rows)
    monkeypatch.setattr(server, '_canonical_workspace_path', lambda p: p)
    monkeypatch.setattr(server.git_probe, 'resolve', lambda path: None)
    monkeypatch.setattr(server.git_probe, 'warm_roots', lambda paths: None)
    monkeypatch.setattr(server, '_is_repo_junk', lambda path: False)
    monkeypatch.setattr(server, '_is_session_cwd_junk', lambda path: False)
    monkeypatch.setattr(server, '_dir_exists_cached', lambda path: True)
    options = dict(preview_limit=3, hydrate=False, session_limit=2000, include_discovered=False)
    expected = server._build_project_tree(None, **options)
    captured, active_id = server._capture_project_tree(None, **options)
    def forbidden(*a, **k):
        raise AssertionError('external read during pure replay')
    monkeypatch.setattr(server, '_project_tree_inputs', forbidden)
    monkeypatch.setattr(server.git_probe, 'resolve', forbidden)
    assert (project_tree_capture.project(captured), active_id) == expected
