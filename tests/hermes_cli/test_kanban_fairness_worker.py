"""Only capture observation settings refresh at ordinary worker turn admission."""
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("live", [{}, {"enabled": False}, {"enabled": True, "capture_id": "new"}])
def test_owner_hydrates_capture_without_changing_scheduler(tmp_path, monkeypatch, live):
    from gateway.session_managed_worker import _worker_config
    from gateway import run
    frozen = {"kanban": {"fairness_capture": {"enabled": True, "capture_id": "old"}, "max_running": 2}}
    policy = SimpleNamespace(ignore_user_config=False, config=lambda authority: frozen)
    monkeypatch.setattr(run, "_load_gateway_config", lambda path: {"kanban": {"fairness_capture": live, "max_running": 99}})
    result = _worker_config(SimpleNamespace(profile_id=str(tmp_path)), policy)
    assert result["kanban"]["fairness_capture"] == live
    assert result["kanban"]["max_running"] == 2


def test_bypass_worker_never_loads_live_capture(tmp_path, monkeypatch):
    from gateway.session_managed_worker import _worker_config
    from gateway import run
    policy = SimpleNamespace(ignore_user_config=True, config=lambda authority: {})
    monkeypatch.setattr(run, "_load_gateway_config", lambda path: pytest.fail("bypass profile read"))
    assert _worker_config(SimpleNamespace(profile_id=str(tmp_path)), policy) == {}
