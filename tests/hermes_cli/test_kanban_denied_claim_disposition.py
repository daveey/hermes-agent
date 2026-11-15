"""Previously judged denies require an actionable claim-time disposition."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_hub_admission_claim as hac
from tests.hermes_cli import test_kanban_hub_admission_claim as support

hub_judge = support.hub_judge
gws = support.gws
_claim_screen_first = support._claim_screen_first


def deny(hub, tid):
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, tid, "hub_admission", {"admit": False, "why": "portable"})


def test_fresh_prior_deny_moves_before_claim(gws, hub_judge):
    hub, _worker, svc = gws
    tid = support._hub_card(hub)
    deny(hub, tid)
    support._tick(hub)
    assert support._holder(svc, tid) != support.HUB
    assert support._events(hub.conn, tid, "moved")
    assert hub_judge.calls == []


@pytest.mark.parametrize("body,reason", [
    ("Finish ~/code/_wt/local-only", "hub worktree path in the brief"),
    ("Run hermes-hub outcomes --id original", "needs home coordination action"),
])
def test_prior_deny_records_current_keep(gws, hub_judge, body, reason):
    hub, _worker, svc = gws
    tid = support._hub_card(hub, body=body)
    deny(hub, tid)
    support._tick(hub)
    assert support._holder(svc, tid) == support.HUB
    latest = support._events(hub.conn, tid, "hub_admission")[-1]
    assert latest.get("kept") == reason
    assert hub_judge.calls == []


def test_detector_counts_denied_claim_without_keep(gws):
    hub, _worker, _svc = gws
    tid = support._hub_card(hub)
    deny(hub, tid)
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, tid, "claimed", {"holder": support.HUB})
    assert tid in [t for t, _at in hac.unjudged_claims(hub.conn, support.HUB)]


def test_create_home_override_is_explicit_keep():
    from hermes_cli import kanban_hub_admission_placement as placement
    result = placement.pool_route(
        {"title": "Read request", "body": "Run hermes-hub outcomes --id original"},
        {}, "hub", {"admit": False}, [])
    assert result["hub_admission"].get("kept") == "needs home coordination action"
    assert "runs_on" not in result and "placement_policy" not in result
