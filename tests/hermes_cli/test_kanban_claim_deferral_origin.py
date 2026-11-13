"""A claim deferral carries who set it, and the skip words follow that origin, not the reason's name.

PR #2003 review 5888302963 (nit): the skip words picked "this host's admission gate" from a name-prefix list
(``hub_cpu_*``, ``hub_admission_overrun``, ``hub_capacity_full``), so this host's other refusals (the updater
hold, the disk admission refusals, ``worker_resources_unknown``, the worker-resource refusal codes) still read
"the Board deferred its claim", and a Board lease reason spelt like a local one would read as local.
"""
from types import SimpleNamespace

import pytest

from hermes_cli import admission_fence
from hermes_cli import kanban_admission as admission
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_disk_admission as disk
from hermes_cli import kanban_dispatch_skip as skips
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_runs_on_dispatch as support
from tests.hermes_cli.test_kanban_quota_episode import board as board  # noqa: PLC0414 -- pytest fixture re-export

gws = support.gws
svc = support.svc

LOCAL = "this host's admission gate deferred its claim"
BOARD = "the Board deferred its claim"


def _recorded_words(conn, tid):
    """Pop the deferral the producer left, record it as the dispatcher tick does, and word the current skip."""
    deferral = sync.pop_claim_deferral(tid)
    assert deferral is not None
    skips.record_tick(conn, SimpleNamespace(claim_deferred=[(tid, deferral)]))
    return skips.words(skips.latest(conn, tid))


def _update_hold(monkeypatch, tid):
    monkeypatch.setattr(disk, "refusal_reason", lambda: None)
    monkeypatch.setattr(admission_fence, "SUPPORTED", True)
    monkeypatch.setattr(admission_fence, "is_held", lambda _path: True)
    assert admission.admission_hold_set(tid)
    return "update_admission_hold"


def _disk_refusal(monkeypatch, tid):
    monkeypatch.setattr(disk, "refusal_reason", lambda: "worker_disk_policy_unknown")
    assert admission.disk_admission_refused(tid)
    return "worker_disk_policy_unknown"


def _worker_resources_unknown(_monkeypatch, tid):
    admission._defer(tid, "worker_resources_unknown")
    return "worker_resources_unknown"


@pytest.mark.parametrize("produce", [_update_hold, _disk_refusal, _worker_resources_unknown])
def test_a_local_gate_deferral_is_not_worded_as_a_board_deferral(board, monkeypatch, produce):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="held here", assignee="a")
        detail = produce(monkeypatch, tid)
        text = _recorded_words(conn, tid)
    assert text == f"skipped: {LOCAL} ({detail})"
    assert "Board" not in text


@pytest.mark.parametrize("reason", ["capacity", "hub_capacity_full"])
def test_a_board_lease_refusal_is_worded_as_the_board_whatever_its_name(gws, monkeypatch, reason):
    """The Board's own refusal stays "the Board", even under a name the local gate also uses."""
    hub, _ = gws
    hub.enter()
    tid = kb.create_task(hub.conn, title="refused by the Board", assignee="default")
    hub.round()

    def refuse(self, **kwargs):
        return sync.Lease(granted=False, run_uid=kwargs["run_uid"], reason=reason)

    monkeypatch.setattr(client.HubLeaseClient, "acquire", refuse)
    result = hub.tick(max_in_progress=1)
    assert result.claim_deferred == [(tid, reason)]
    assert [getattr(d, "origin", None) for _t, d in result.claim_deferred] == [sync.ORIGIN_BOARD]
    skips.record_tick(hub.conn, result)
    assert skips.words(skips.latest(hub.conn, tid)) == f"skipped: {BOARD} ({reason})"


def test_the_origin_rides_the_recorded_skip_and_the_notice_fields():
    deferral = sync.ClaimDeferral("update_admission_hold", sync.ORIGIN_LOCAL)
    assert deferral == "update_admission_hold" and isinstance(deferral, str)  # every str consumer is unchanged
    [(_tid, reason, detail, _until, extra)] = skips._tick_skips(SimpleNamespace(claim_deferred=[("t_x", deferral)]))
    assert (reason, detail, extra) == ("claim_deferred", "update_admission_hold", {"origin": "local"})
    fields = skips.notice_fields({"skip": {"reason": "claim_deferred", "detail": "x", "origin": "local"}}, 0)
    assert fields == {"dispatch_skip": {"reason": "claim_deferred", "detail": "x", "origin": "local"}}
    assert skips.words(fields["dispatch_skip"]) == f"skipped: {LOCAL} (x)"


def test_a_skip_without_an_origin_reads_as_the_board():
    """No name list: an unmarked detail (a plain str producer, a skip recorded before origins) keeps the default."""
    assert skips.words({"reason": "claim_deferred", "detail": "hub_cpu_unknown"}) == f"skipped: {BOARD} (hub_cpu_unknown)"
