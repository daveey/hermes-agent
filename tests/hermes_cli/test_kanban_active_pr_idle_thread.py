"""Real collection threads must not refresh dispatcher-owned PR dictionaries."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_pr_acceptance as acceptance
from hermes_cli import kanban_pr_idle_checks as checks
from tests.hermes_cli import test_kanban_active_pr_idle_green as helpers


@pytest.mark.parametrize("state,head,expected", [
    ("open", helpers.SHA, True), ("closed", helpers.SHA, False), ("open", "b" * 40, False),
])
def test_real_collect_pool_uses_isolated_final_head_read(monkeypatch, state, head, expected):
    receipt = {"ok": True, "pr_state": "OPEN", "head_sha": helpers.SHA,
               "pr_url": helpers.URL, "checks": ["required"]}
    monkeypatch.setattr(acceptance, "collect_acceptance", lambda *a, **kw: receipt)
    monkeypatch.setattr(acceptance, "_assignee_profile_home", lambda a: "/profile")
    calls = []
    def api(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return {"state": state, "head": {"sha": head}}
    monkeypatch.setattr(acceptance, "_api", api)
    def forbidden(*a, **kw):
        pytest.fail("pool thread touched dispatcher PR cache")
    monkeypatch.setattr(prs, "pr_head", forbidden)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(checks._collect, helpers.URL, "a", helpers.SHA).result(timeout=5) is expected
    assert calls == [("repos/example/project/pulls/19", {"profile_home": "/profile"})]
