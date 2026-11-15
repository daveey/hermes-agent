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


@pytest.mark.parametrize("body,kept", [
    ("Run hermes-hub outcomes --id original", "needs home coordination action"),
    ("Portable repo work: edit, test, PR.", None),
])
def test_bounced_hub_pin_with_deny_is_resolved(gws, hub_judge, body, kept):
    from hermes_cli import kanban_worker_node as wn
    hub, _worker, svc = gws
    tid = support._hub_card(hub, body=body)
    deny(hub, tid)
    with kb.write_txn(hub.conn):
        wn.set_runs_on(hub.conn, tid, [f"machine:{support.HUB}"])
    support._tick(hub)
    if kept:
        assert support._holder(svc, tid) == support.HUB
        assert support._events(hub.conn, tid, "hub_admission")[-1].get("kept") == kept
    else:
        assert support._holder(svc, tid) != support.HUB
        assert support._events(hub.conn, tid, "moved")
    assert hub_judge.calls == []


def test_unchanged_keep_does_not_append_each_screen(gws, hub_judge):
    hub, _worker, _svc = gws
    tid = support._hub_card(hub, body="Run hermes-hub outcomes --id original")
    deny(hub, tid)
    hac.screen(hub.conn)
    first = support._events(hub.conn, tid, "hub_admission")
    hac.screen(hub.conn)
    assert support._events(hub.conn, tid, "hub_admission") == first
    assert first[-1]["kept"] == "needs home coordination action"


@pytest.mark.parametrize("first,last,flagged", [
    ({"admit": False, "kept": "host workspace"}, {"admit": False}, True),
    ({"admit": False}, {"admit": True}, False),
    ({"admit": True}, {"admit": False}, True),
    ({"admit": False}, {"admit": False, "kept": "host workspace"}, False),
    ({"admit": False}, {"admit": False, "forced": True}, False),
])
def test_detector_uses_latest_preclaim_disposition(gws, first, last, flagged):
    hub, _worker, _svc = gws
    tid = support._hub_card(hub)
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, tid, "hub_admission", first)
        kb._append_event(hub.conn, tid, "hub_admission", last)
        kb._append_event(hub.conn, tid, "claimed", {"holder": support.HUB})
        # A later correction must not repair the historical census.
        kb._append_event(hub.conn, tid, "hub_admission", {"admit": True})
    found = [t for t, _at in hac.unjudged_claims(hub.conn, support.HUB)]
    assert (tid in found) is flagged


@pytest.mark.parametrize("kept", [None, "needs home coordination action"])
def test_detector_creation_fallback(gws, kept):
    hub, _worker, _svc = gws
    tid = support._hub_card(hub)
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, tid, "created", {"hub_admission": {"admit": False, "kept": kept}})
        kb._append_event(hub.conn, tid, "claimed", {"holder": support.HUB})
    found = [t for t, _at in hac.unjudged_claims(hub.conn, support.HUB)]
    assert (tid in found) is (kept is None)


@pytest.mark.parametrize("prior", [True, False])
def test_denied_without_queue_or_structural_keep_is_deferred(gws, hub_judge, monkeypatch, prior, caplog):
    from hermes_cli import kanban_hub_admission_pickup as pickup
    hub, _worker, svc = gws
    tid = support._hub_card(hub)
    if prior:
        deny(hub, tid)
    hub_judge.admit_next = False
    monkeypatch.setattr(pickup, "_target", lambda *_args: None)
    support._tick(hub)
    assert support._holder(svc, tid) != support.HUB
    assert not any(event.get("kept") for event in support._events(hub.conn, tid, "hub_admission"))
    assert "denied-card self-check failed" not in caplog.text


def test_failed_screen_defers_candidates_instead_of_claiming_denies(gws, monkeypatch):
    hub, _worker, svc = gws
    tid = support._hub_card(hub)
    deny(hub, tid)

    def broken(_conn):
        raise RuntimeError("screen unavailable")

    monkeypatch.setattr(hac, "screen", broken)
    support._tick(hub)
    assert support._holder(svc, tid) != support.HUB


def test_judge_outage_defers_all_unjudged_candidates(gws, monkeypatch):
    from hermes_cli import kanban_hub_admission as judge
    hub, _worker, svc = gws
    tids = [support._hub_card(hub, title=f"unjudged {n}") for n in range(2)]
    calls = []

    def unavailable(*_args, **_kwargs):
        calls.append(True)
        return judge.Verdict()

    monkeypatch.setattr(judge, "admit", unavailable)
    support._tick(hub)
    assert all(support._holder(svc, tid) != support.HUB for tid in tids)
    assert len(calls) == 1


@pytest.mark.parametrize("body", ["Portable repo work", "Finish ~/code/_wt/local-only"])
def test_prior_deny_changed_during_disposition_is_not_claimed(gws, hub_judge, monkeypatch, body):
    hub, _worker, svc = gws
    tid = support._hub_card(hub, body=body)
    deny(hub, tid)
    original = hac.keep_reason

    def changed(conn, card, fields, cfg):
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "commented", {"author": "concurrent editor"})
        return original(conn, card, fields, cfg)

    monkeypatch.setattr(hac, "keep_reason", changed)
    support._tick(hub)
    assert support._holder(svc, tid) != support.HUB
    assert not support._events(hub.conn, tid, "moved")
    assert not support._events(hub.conn, tid, "hub_admission")[-1].get("kept")


@pytest.mark.parametrize("labels,audit,eligible", [
    (["machine:elsewhere"], {"admit": False}, False),
    (["machine:hub"], {"admit": False, "forced": True}, False),
    (["machine:hub"], {"admit": True}, False),
    (["machine:hub"], None, False),
    (["machine:hub"], {"admit": False}, True),
])
def test_only_unforced_denied_own_pin_enters_screen(gws, labels, audit, eligible):
    from hermes_cli import kanban_worker_node as wn
    hub, _worker, _svc = gws
    tid = support._hub_card(hub)
    with kb.write_txn(hub.conn):
        wn.set_runs_on(hub.conn, tid, labels)
        if audit is not None:
            kb._append_event(hub.conn, tid, "hub_admission", audit)
    row = hub.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert hac._eligible(hub.conn, row) is eligible


def test_create_home_override_is_explicit_keep():
    from hermes_cli import kanban_hub_admission_placement as placement
    result = placement.pool_route(
        {"title": "Read request", "body": "Run hermes-hub outcomes --id original"},
        {}, "hub", {"admit": False}, [])
    assert result["hub_admission"].get("kept") == "needs home coordination action"
    assert "runs_on" not in result and "placement_policy" not in result
