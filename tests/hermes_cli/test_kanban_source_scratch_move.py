"""workspace_source v2: a scratch card owning an external worktree moves and reclaims like a worktree card.

A scratch card's source custody lives on the Board, not in its ``workspace_kind``: a move that skipped the publish,
or a parked move / reclaim that ignored scratch custody, re-pinned the card while its custody stayed retained on
this machine, stranding it (the successor is refused ``custody_retained``)."""

from __future__ import annotations

import pytest

from hermes_cli import kanban_source_board as kbs
from hermes_cli import kanban_source_move as ksm
from hermes_cli import kanban_source_preflight as pre
from tests.hermes_cli import test_kanban_source_move as base

C1, card, custody, manifest = base.C1, base.card, base.custody, base.manifest
conn, kanban_home = base.conn, base.kanban_home  # shared pytest fixtures

SCRATCH = manifest(version=2, source_kind="scratch_external")


def _journal(conn, tid, run_uid="r1", path="/src/ext"):
    kbs.record_run(conn, kbs.AttachedRun(run_id=77, task_id=tid, run_uid=run_uid, mode="initial", manifest_revision=0,
                                         branch=f"wt/{tid}", path=path, pre_head=C1, manifest=None, state="attached",
                                         machine="hub", source_protocol=2))


def test_running_scratch_source_move_publishes(conn, monkeypatch):
    tid = card(conn, "scratch")
    receipt = {"run_uid": "u", "state": "published", "manifest_revision": 2, "commit": C1}
    monkeypatch.setattr(pre, "receipt_for_current_run", lambda c, t, publish=None: (5, receipt))
    monkeypatch.setattr(kbs, "attached_run", lambda c, run_id: type("R", (), {"manifest": SCRATCH})())
    got, fresh, code = ksm.publish_for_move(conn, tid)
    assert (got, code) == (receipt, "") and fresh[0] == SCRATCH and fresh[1].state == "published"


def test_running_scratch_move_publish_refusal_keeps_run(conn, monkeypatch):
    tid = card(conn, "scratch")
    retained = {"run_uid": "u", "state": "retained", "manifest_revision": 0, "commit": None, "reason": "dirty_source"}
    monkeypatch.setattr(pre, "receipt_for_current_run", lambda c, t, publish=None: (5, retained))
    assert ksm.publish_for_move(conn, tid) == (None, None, "dirty_source")


def test_plain_scratch_card_moves_without_source(conn):
    assert ksm.publish_for_move(conn, card(conn, "scratch")) == (None, None, "")
    assert ksm.parked_refusal(conn, card(conn, "scratch"), me="hub") is None


@pytest.mark.parametrize("source,cust,code", [
    (None, custody(state="retained", manifest_revision=0), "workspace_source_unpublished"),
    (SCRATCH, custody(state="retained"), "custody_retained"),
    (SCRATCH, custody(state="attached"), "custody_retained"),
    (SCRATCH, None, "workspace_source_not_on_board"),
])
def test_parked_scratch_source_card_needs_portable_custody(conn, source, cust, code):
    assert ksm.parked_refusal(conn, card(conn, "scratch", source, cust), me="worker-9")[0] == code


def test_parked_scratch_portable_custody_moves_elsewhere(conn):
    assert ksm.parked_refusal(conn, card(conn, "scratch", SCRATCH, custody()), me="worker-9") is None


def test_parked_scratch_holder_checks_published_external_worktree(conn, monkeypatch):
    """The holder checks the run's external worktree (the publisher's journal path), never the scratch cwd."""
    seen = []
    monkeypatch.setattr(ksm, "_local_matches", lambda path, commit: seen.append(path) or False)
    tid = card(conn, "scratch", SCRATCH, custody())
    _journal(conn, tid)
    assert ksm.parked_refusal(conn, tid, me="hub")[0] == "workspace_source_unpublished"
    assert seen == ["/src/ext"]
    monkeypatch.setattr(ksm, "_local_matches", lambda path, commit: path == "/src/ext" and commit == C1)
    assert ksm.parked_refusal(conn, tid, me="hub") is None


def test_parked_scratch_holder_without_journal_fails_closed(conn, monkeypatch):
    monkeypatch.setattr(ksm, "_local_matches", lambda path, commit: True)
    tid = card(conn, "scratch", SCRATCH, custody())
    assert ksm.parked_refusal(conn, tid, me="hub")[0] == "workspace_source_unpublished"


def _attached_at(conn, tid, run_uid, path, revision, source):
    kbs.record_run(conn, kbs.AttachedRun(run_id=88, task_id=tid, run_uid=run_uid, mode="return_home",
                                         manifest_revision=revision, branch=f"wt/{tid}", path=path, pre_head=C1,
                                         manifest=source, state="attached", machine="hub", source_protocol=2))


def test_parked_scratch_holder_after_unchanged_ending_checks_its_own_run(conn, monkeypatch):
    """Composed managed-launch finding: a successor that ends unchanged becomes the holder, but the manifest still
    names the original publisher's run. The holder checks the external worktree of ITS run that attached at exactly
    this manifest; before the fix it found no journal row and refused every parked move from the holder."""
    seen = []
    monkeypatch.setattr(ksm, "_local_matches", lambda path, commit: seen.append((path, commit)) or True)
    tid = card(conn, "scratch", SCRATCH, custody(state="unchanged"))
    _attached_at(conn, tid, "successor", "/succ/src", 2, SCRATCH)
    assert ksm.parked_refusal(conn, tid, me="hub") is None
    assert seen == [("/succ/src", C1)]


@pytest.mark.parametrize("revision,commit", [(1, C1), (2, "d" * 40)])
def test_parked_scratch_holder_ignores_runs_attached_at_another_manifest(conn, monkeypatch, revision, commit):
    monkeypatch.setattr(ksm, "_local_matches", lambda path, commit: True)
    tid = card(conn, "scratch", SCRATCH, custody(state="unchanged"))
    _attached_at(conn, tid, "older", "/old/src", revision, {**SCRATCH, "manifest_revision": revision, "commit": commit})
    assert ksm.parked_refusal(conn, tid, me="hub")[0] == "workspace_source_unpublished"


@pytest.mark.parametrize("source,cust", [
    (None, custody(state="retained", manifest_revision=0)),
    (None, custody(state="attached", manifest_revision=0)),
])
def test_reclaim_refuses_scratch_premanifest_custody(conn, monkeypatch, source, cust):
    from hermes_cli import kanban_move as km
    monkeypatch.setattr("hermes_cli.kanban_hub_admission_placement.gate_move", lambda *a, **kw: None)
    tid = card(conn, "scratch", source, cust)
    assert km.move_reclaim(conn, tid, runs_on=["machine:worker-1"], reason="r")["reason_code"] == \
        "reclaim_source_unsupported"


def test_reclaim_plain_scratch_unaffected(conn):
    assert ksm.reclaim_refusal(conn, card(conn, "scratch")) is None
