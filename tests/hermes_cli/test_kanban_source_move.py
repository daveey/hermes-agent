"""workspace_source core PR-2: move / reservation (plan v2 §7), ``--reclaim``, portable capabilities, the CLI claim
refusal and ``hermes kanban source-release`` (v4.1 R3-a)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_source_cli as cli
from hermes_cli import kanban_source_move as ksm
from hermes_cli import kanban_workspace_source as kws
from tests.hermes_cli import kanban_sync_support as support

C1 = "e" * 40


def manifest(**over) -> dict:
    m = {"version": 1, "repository": {"id": 42, "full_name": "acme/widget", "url": "https://github.com/acme/widget"},
         "branch": "refs/heads/wt/t_m", "commit": C1, "base_commit": None, "published_at": 1,
         "manifest_revision": 2, "published_by": {"run_uid": "r1", "machine": "hub"}, "source_kind": "task_worktree",
         "artifacts": []}
    m.update(over)
    return m


def custody(**over) -> dict:
    return {"source_holder": "hub", "state": "published", "manifest_revision": 2, **over}


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def conn(kanban_home, monkeypatch):
    gen = support.synced_conn(kanban_home)
    c = next(gen)
    monkeypatch.setattr(kws, "settings", lambda config=None: kws.Settings(
        enabled=True, requested=True, clones={}, publish_on_park=True, push_timeout_s=30, preflight_timeout_s=60,
        materialize_timeout_s=None))
    yield c
    gen.close()


def card(conn, kind="worktree", source=None, cust=None) -> str:
    tid = kb.create_task(conn, title="m", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET applying=1")
        conn.execute("UPDATE tasks SET workspace_kind=?, workspace_path='/nowhere', workspace_source=?, "
                     "workspace_custody=? WHERE id=?", (kind, json.dumps(source) if source else None,
                                                       json.dumps(cust) if cust else None, tid))
        conn.execute("UPDATE kanban_sync_state SET applying=0")
    return tid


# --- parked move ------------------------------------------------------------------------------------------------

def test_workspace_bound_cards_never_move(conn):
    """Re-pinned (v2 §7): dir never moves; a worktree without portable custody does not move either."""
    assert ksm.parked_refusal(conn, card(conn, "dir"), me="hub")[0] == "workspace_bound"
    assert ksm.parked_refusal(conn, card(conn, "dir", manifest(), custody()), me="hub")[0] == "workspace_bound"
    assert ksm.parked_refusal(conn, card(conn), me="hub")[0] == "workspace_source_unpublished"


@pytest.mark.parametrize("cust,code", [
    (None, "workspace_source_not_on_board"),
    (custody(state="retained"), "custody_retained"),
    (custody(state="attached"), "custody_retained"),
    (custody(manifest_revision=1), "custody_retained"),
])
def test_parked_move_needs_portable_custody(conn, cust, code):
    assert ksm.parked_refusal(conn, card(conn, source=manifest(), cust=cust), me="worker-9")[0] == code


def test_parked_move_with_portable_custody_allowed_elsewhere(conn):
    for state in ("published", "unchanged"):
        assert ksm.parked_refusal(conn, card(conn, source=manifest(), cust=custody(state=state)), me="worker-9") is None


def test_parked_move_holder_needs_clean_local_source_at_commit(conn, monkeypatch):
    tid = card(conn, source=manifest(), cust=custody(source_holder="hub"))
    monkeypatch.setattr(ksm, "_local_matches", lambda path, commit: False)
    assert ksm.parked_refusal(conn, tid, me="hub")[0] == "workspace_source_unpublished"
    monkeypatch.setattr(ksm, "_local_matches", lambda path, commit: commit == C1)
    assert ksm.parked_refusal(conn, tid, me="hub") is None


def test_disabled_host_keeps_worktree_bound(conn, monkeypatch):
    tid = card(conn, source=manifest(), cust=custody())
    monkeypatch.setattr(kws, "settings", lambda config=None: kws.Settings(
        enabled=False, requested=False, clones={}, publish_on_park=True, push_timeout_s=30, preflight_timeout_s=60,
        materialize_timeout_s=None))
    assert ksm.parked_refusal(conn, tid, me="hub")[0] == "workspace_bound"


def test_move_check_uses_source_gate(conn):
    from hermes_cli import kanban_move as km
    sync.set_mode(conn, "primary")
    tid = card(conn)
    with pytest.raises(km.MoveRefused) as err:
        km.check(conn, tid, ["machine:worker-1"], machines={}, me="hub")
    assert err.value.code == "workspace_source_unpublished"


# --- running move: publish, then check --------------------------------------------------------------------------

def test_running_move_publishes_then_checks_fresh_result(conn, monkeypatch):
    from hermes_cli import kanban_move as km
    tid = card(conn)  # replica has no manifest yet: only the fresh publish makes it portable
    seen = {}

    def publish(conn_, task_id):
        return ({"run_uid": "u", "state": "published", "manifest_revision": 2, "commit": C1},
                (json.dumps(manifest()), kws.Custody(None, "published", 2)), "")

    def check(conn_, task_id, runs_on, **kw):
        seen["fresh"] = kw.get("fresh")
        assert ksm.parked_refusal(conn_, task_id, me="hub", fresh=kw["fresh"]) is None
        raise km.MoveRefused("stop_here", "checked")
    monkeypatch.setattr(ksm, "publish_for_move", publish)
    monkeypatch.setattr(km, "check", check)
    monkeypatch.setattr("hermes_cli.kanban_hub_admission_placement.gate_move", lambda *a, **kw: None)
    res = km.move_task(conn, tid, runs_on=["machine:worker-1"], reason="r", checkpoint="c")
    assert res["reason_code"] == "stop_here" and seen["fresh"][1].state == "published"


def test_running_move_publish_failure_keeps_run(conn, monkeypatch):
    from hermes_cli import kanban_move as km
    tid = card(conn)
    monkeypatch.setattr(ksm, "publish_for_move", lambda c, t: (None, None, "dirty_source"))
    monkeypatch.setattr(km, "check", lambda *a, **kw: pytest.fail("checked without a publish"))
    monkeypatch.setattr("hermes_cli.kanban_hub_admission_placement.gate_move", lambda *a, **kw: None)
    assert km.move_task(conn, tid, runs_on=["machine:worker-1"], reason="r", checkpoint="c")["reason_code"] == \
        "dirty_source"


@pytest.mark.parametrize("kind,source", [("worktree", None), ("scratch", manifest())])
def test_reclaim_refuses_source_cards(conn, monkeypatch, kind, source):
    from hermes_cli import kanban_move as km
    monkeypatch.setattr("hermes_cli.kanban_hub_admission_placement.gate_move", lambda *a, **kw: None)
    tid = card(conn, kind, source)
    res = km.move_reclaim(conn, tid, runs_on=["machine:worker-1"], reason="r")
    assert res["reason_code"] == "reclaim_source_unsupported"


# --- capabilities -----------------------------------------------------------------------------------------------

def test_portable_worktree_is_not_host_only(conn):
    from hermes_cli.kanban_capabilities import _required_capabilities
    portable = {"workspace_kind": "worktree", "workspace_source": json.dumps(manifest()),
                "workspace_custody": json.dumps(custody())}
    assert ksm.capability_kind(portable) == "worktree_source"
    assert "host_repo" not in _required_capabilities([], ksm.capability_kind(portable))
    assert "host_repo" in _required_capabilities(["host_repo"], ksm.capability_kind(portable))  # explicit stays
    assert ksm.capability_kind({**portable, "workspace_custody": json.dumps(custody(state="retained"))}) == "worktree"
    assert ksm.capability_kind({"workspace_kind": "dir"}) == "dir"


# create_task stores validate_assignment's merged list, so a worktree card's ``requires`` always holds the implicit
# ``host_repo`` (LIVE t_01a1d5b1c1798e2a's created event: ["terminal","file","git","host_repo","kanban"]).
CREATED_WORKTREE_REQUIRES = ["terminal", "file", "git", "host_repo", "kanban"]


def _stored_requires(conn, tid, requires):
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET requires=? WHERE id=?", (json.dumps(requires), tid))


def test_portable_worktree_moves_to_worker_without_host_repo_label(conn, monkeypatch):
    """LIVE t_01a1d5b1c1798e2a: published custody at rev 1, yet the move to machine:worker-2 was refused
    `capability_mismatch: machine worker-2 lacks capability:host_repo`, read from the stored requires."""
    from hermes_cli import kanban_move as km
    from hermes_cli import kanban_runs_on_capabilities as roc
    sync.set_mode(conn, "primary")
    monkeypatch.setattr(roc, "_remote_tools", lambda name: None)
    monkeypatch.setattr(ksm, "_local_matches", lambda path, commit: commit == C1)  # holder's tree is clean at C1
    machines = {"worker-2": {"state": "active", "pickup": "all", "labels": ["pool:worker", "capability:terminal"]}}
    tid = card(conn, source=manifest(), cust=custody(source_holder="worker-4"))
    _stored_requires(conn, tid, CREATED_WORKTREE_REQUIRES)
    plan = km.check(conn, tid, ["machine:worker-2"], machines=machines, me="worker-4", host_reason="registered clone")
    assert plan["matches"] == ["worker-2"]
    # Not portable (custody retained): the stored host_repo still binds the card to a host checkout.
    held = card(conn, source=manifest(), cust=custody(source_holder="worker-4", state="retained"))
    _stored_requires(conn, held, CREATED_WORKTREE_REQUIRES)
    with pytest.raises(km.MoveRefused) as err:
        km.check(conn, held, ["machine:worker-2"], machines=machines, me="worker-4", host_reason="registered clone")
    assert err.value.code == "custody_retained"


def test_effective_requires_drops_host_repo_only_when_portable(conn):  # conn: workspace_source enabled
    portable = {"workspace_kind": "worktree", "workspace_source": json.dumps(manifest()),
                "workspace_custody": json.dumps(custody())}
    assert ksm.effective_requires(portable, CREATED_WORKTREE_REQUIRES) == ["terminal", "file", "git", "kanban"]
    assert ksm.effective_requires(portable, ["host_repo", "ssh:mini"]) == ["ssh:mini"]
    retained = {**portable, "workspace_custody": json.dumps(custody(state="retained"))}
    assert ksm.effective_requires(retained, CREATED_WORKTREE_REQUIRES) == CREATED_WORKTREE_REQUIRES
    assert ksm.effective_requires({"workspace_kind": "dir"}, ["host_repo"]) == ["host_repo"]
    assert ksm.effective_requires(portable, None) == []


def test_pickup_claims_portable_worktree_on_profile_without_host_repo(conn, monkeypatch):
    """The pickup gate (claim_refusal) reads the same stored requires: a portable card runs on a profile whose
    terminal backend is not local (no host_repo); a retained one still needs it."""
    from hermes_cli import kanban_capabilities as caps
    from hermes_cli import kanban_runs_on_capabilities as roc
    monkeypatch.setattr(caps, "_profile_capabilities", lambda assignee, required=None: {"terminal", "file", "git",
                                                                                          "kanban"})
    monkeypatch.setattr("hermes_cli.kanban_placement_coordination.claim_refusal", lambda card, me: None)
    monkeypatch.setattr("hermes_cli.kanban_mandatory_pin.refusal", lambda card, me=None: None)
    base = {"id": "t_pick", "assignee": "worker", "workspace_kind": "worktree", "skills": None,
            "requires": json.dumps(CREATED_WORKTREE_REQUIRES), "workspace_source": json.dumps(manifest())}
    assert roc.claim_refusal({**base, "workspace_custody": json.dumps(custody())}, me="worker-2") is None
    assert "host_repo" in str(roc.claim_refusal({**base, "workspace_custody": json.dumps(custody(state="retained"))},
                                                me="worker-2"))


# --- CLI --------------------------------------------------------------------------------------------------------

def test_cli_claim_custody_card_refused(conn):
    tid = card(conn, source=manifest(), cust=custody())
    assert cli.claim_refusal(conn, tid).startswith(cli.CLAIM_REFUSED)
    assert cli.claim_refusal(conn, card(conn)) is None


def test_cli_claim_command_refuses(conn, kanban_home, capsys):
    from hermes_cli import kanban as kcli
    tid = card(conn, source=manifest(), cust=custody())
    assert kcli._cmd_claim(SimpleNamespace(task_id=tid, ttl=60)) != 0
    assert cli.CLAIM_REFUSED in capsys.readouterr().err
    assert kb.get_task(conn, tid).status != "running"


def test_unsynced_board_without_source_columns_claims_and_moves(kanban_home, capsys):
    """A board that never synced lacks the source columns: the CLI claim, the move/reclaim checks and source-release
    read them as NULL instead of failing on the missing column."""
    from hermes_cli import kanban as kcli
    with kbc.connect_closing() as plain:
        assert "workspace_source" not in {r[1] for r in plain.execute("PRAGMA table_info(tasks)")}
        tid = kb.create_task(plain, title="plain", assignee="worker")
        assert ksm.parked_refusal(plain, tid, me="hub") is None and ksm.reclaim_refusal(plain, tid) is None
        assert cli.source_release(plain, tid, "published", by="op", head=lambda *_: None, hub=_Hub())["ok"] is False
    assert kcli._cmd_claim(SimpleNamespace(task_id=tid, ttl=60)) == 0
    assert f"Claimed {tid}" in capsys.readouterr().out


class _Hub:
    def __init__(self):
        self.calls = []

    def leases(self, board, body):
        self.calls.append(body)
        return {"status": "applied"}


def test_source_release_verifies_remote_head_first(conn):
    tid = card(conn, source=manifest(), cust=custody(state="retained"))
    hub = _Hub()
    res = cli.source_release(conn, tid, "published", by="op", head=lambda url, ref: "f" * 40, hub=hub)
    assert res["reason_code"] == "remote_drift" and hub.calls == []
    res = cli.source_release(conn, tid, "published", by="op", head=lambda url, ref: None, hub=hub)
    assert res["reason_code"] == "remote_branch_missing" and hub.calls == []
    res = cli.source_release(conn, tid, "published", by="op",
                             head=lambda url, ref: C1 if ref == "refs/heads/wt/t_m" else None, hub=hub)
    assert res["ok"]
    (body,) = hub.calls
    assert (body["action"], body["task"], body["to_state"], body["expected_revision"], body["by"]) == (
        "source_release", tid, "published", 2, "op")


def test_source_release_refusals(conn):
    hub = _Hub()
    assert cli.source_release(conn, card(conn), "published", by="op", hub=hub)["reason_code"] == \
        "workspace_source_unavailable"
    assert cli.source_release(conn, "nope", "published", by="op", hub=hub)["reason_code"] == "not_found"
    assert cli.source_release(conn, "nope", "attached", by="op", hub=hub)["reason_code"] == "invalid"


def test_source_release_parser():
    from hermes_cli.kanban_parser import build_parser
    import argparse
    p = argparse.ArgumentParser()
    build_parser(p.add_subparsers(dest="cmd"))
    args = p.parse_args(["kanban", "source-release", "t_1", "--to", "unchanged"])
    assert (args.task_id, args.to) == ("t_1", "unchanged")
