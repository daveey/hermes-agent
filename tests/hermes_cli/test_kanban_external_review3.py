"""PR verdicts require positive success evidence, including real parked-card wakes."""
import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_external_watch as kew
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import test_kanban_external_review as r
from tests.hermes_cli import test_kanban_external_watch as h

kanban_home = h.kanban_home


@pytest.mark.parametrize("required", [True, False])
@pytest.mark.parametrize("conclusion,outcome", [
    ("stale", "failed"), (None, "unknown"), ("", "unknown"), ("future_result", "unknown"),
    ("success", "succeeded"), ("neutral", "succeeded"), ("skipped", "succeeded"),
    ("failure", "failed"), ("cancelled", "cancelled"),
])
def test_pr_conclusion_controls_real_wake(monkeypatch, kanban_home, required, conclusion, outcome):
    """A completed check is not necessarily successful; unknown results retain the park."""
    runs = [r._run(1, "completed", "success" if required else conclusion)]
    kw = {"required": {"required"}, "checks": [r._check(1, "required", conclusion=conclusion)]} if required else {}
    poll = r._pr(monkeypatch, runs, **kw)
    with kbc.connect_closing() as conn:
        tid = h.park(conn, ref=f"https://github.com/{h.REPO}/pull/9")
        kew.apply_poll(conn, tid, poll, now=h.T0, adapter="github_pr_checks", scope=h.REPO)
        wakes = h.events(conn, tid, kwc.CONDITION_MET_EVENT)
        assert poll["job"] == outcome
        assert poll["service"] == "ok"
        if outcome == "unknown":
            assert not wakes
            assert h.status(conn, tid) == "scheduled"
            assert kew.card_state(conn, tid, now=h.T0)["state"] == "external_unknown"
        else:
            assert len(wakes) == 1
            assert wakes[0]["external_result"]["outcome"] == outcome
            assert h.status(conn, tid) == "ready"
