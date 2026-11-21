"""The first source registration may be the bound worker's completion call."""
import importlib
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_source_artifacts as artifacts
from hermes_cli import kanban_source_board as board
from tests.hermes_cli import test_kanban_source_registration as registration_helpers
from tests.hermes_cli.test_kanban_source_publish import _leaving_op
from tests.hermes_cli.test_kanban_sync_attachments import BlobService
from tests.tools.test_kanban_source_heartbeat import _bind
from tools import kanban_tools as kt

setup = registration_helpers.setup


_PARK = "SCHEDULED_UNTIL=2099-01-01T00:00:00Z wait"
# name: (handler, args, core module, core attr, landed status, carries artifacts)
ENDINGS = {
    "complete": ("_handle_complete", {"summary": "Completed source work and preserved its report"},
                 "hermes_cli.kanban_db", "complete_task", "done", True),
    "request_review": ("_handle_request_review", {"summary": "Implemented; please review"},
                       "hermes_cli.kanban_db", "request_review", "review", True),
    "block": ("_handle_block", {"kind": "time", "reason": _PARK, "clock_only": True, "awaiting": {"kind": "clock", "why": "test window"}},
              "hermes_cli.kanban_db", "block_task", "scheduled", False),
    "schedule": ("_handle_schedule", {"reason": _PARK, "clock_only": True, "awaiting": {"kind": "clock", "why": "test window"}},
                 "hermes_cli.kanban_db", "schedule_task", "scheduled", False),
    "continue": ("_handle_continue", {"checkpoint": "done so far; resume later"},
                 "hermes_cli.kanban_continuation", "continue_task", "ready", False),
    "move": ("_handle_move", {"runs_on": ["pool:worker"], "reason": "needs another machine",
                              "checkpoint": "resume elsewhere"},
             "hermes_cli.kanban_move", "move_task", "ready", False),
}


@pytest.mark.parametrize("ending", list(ENDINGS))
@pytest.mark.parametrize("prior_state", ["none", "attached", "published"])
def test_ending_registers_initial_source_before_ending_preflight(ending, prior_state, setup, tmp_path, monkeypatch):
    handler, base_args, core_mod, core_attr, landed, with_artifacts = ENDINGS[ending]
    conn, boundary, tid, source, scratch, _home, clone = setup
    from hermes_cli import kanban_sync_client as client
    client.ensure_identity(conn, "default", "worker")
    run_id = kb.get_task(conn, tid).current_run_id
    _bind(monkeypatch, conn, tid, run_id, "owner")
    # Keep real Git validation and publication, but route the fixture's GitHub
    # URL to a local bare repository (no network, no production source changes).
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--bare", str(clone), str(origin)],
                   check=True, capture_output=True)
    registration_helpers.git(clone, "remote", "set-url", "origin", str(origin))
    from hermes_cli import kanban_source_clones as clones
    monkeypatch.setattr(clones, "canonical_github", lambda url: "example/source" if url == str(origin) else None)
    service = BlobService()
    blob_hub = service.hub("worker")
    blob_hub.leases = boundary.hub.leases
    boundary.hub = blob_hub
    # A bounded HTTP transport is the only additional external boundary seam;
    # metadata outbox push, selection, snapshot, and blob transfer remain real.
    monkeypatch.setattr(artifacts, "bounded_transport", lambda hub, bound: hub)
    from hermes_cli import kanban_sync_attachments as blobs
    read = blobs._read

    def read_fixture(path, row, board_id):
        try:
            return read(path, row, board_id)
        except ValueError as exc:
            raise AssertionError(f"fixture blob read failed: {exc}; path={path}; board={board_id}") from exc

    monkeypatch.setattr(blobs, "_read", read_fixture)
    monkeypatch.setattr("psutil.process_iter", lambda: iter(()))
    from hermes_cli import kanban_worker_node as nodes
    monkeypatch.setattr(nodes, "registry", lambda **kw: {"machines": {
        "destination": {"state": "active", "pickup": "all", "labels": ["pool:worker"]},
    }})
    heartbeat = Mock(side_effect=AssertionError("completion must not call kanban_heartbeat"))
    monkeypatch.setattr(kt, "_handle_heartbeat", heartbeat)
    report = scratch / "report.txt"
    report.write_bytes(b"completion-only source evidence")
    assert board.attached_run(conn, run_id) is None
    assert boundary.actions("source_attach", "publish_source") == []

    # Observe admission at core entry without replacing the real completion or
    # its source preflight. Check after it returns, so RED exercises the ending.
    core = importlib.import_module(core_mod)
    real_core = getattr(core, core_attr)
    admitted = []

    def observe_core(*args, **kwargs):
        run = board.attached_run(conn, run_id)
        admitted.append((run, artifacts.selected(conn, tid, run.run_uid) if run else None))
        return real_core(*args, **kwargs)

    monkeypatch.setattr(core, core_attr, observe_core)
    args = {"task_id": tid, **base_args, "source_path": str(source)}
    if with_artifacts:
        args["artifacts"] = [str(report)]
    if prior_state != "none":
        from hermes_cli import kanban_source_registration as registration
        from hermes_cli import kanban_source_preflight as preflight
        from tools.kanban_tools_source import stage_completion
        prior = registration.register_initial(conn, tid, str(source), expected_run_id=run_id)
        if prior_state == "published":
            if with_artifacts:
                stage_completion(conn, tid, {"artifacts": [str(report)]}, expected_run_id=run_id)
            assert preflight.compute_receipt(conn, tid, prior)["state"] == "published"
    out = json.loads(getattr(kt, handler)(args))
    heartbeat.assert_not_called()
    assert out.get("ok") is True, out
    assert kb.get_task(conn, tid).status == landed
    assert len(admitted) == 1, "must traverse the real ending core exactly once"
    run_at_preflight, chosen_at_preflight = admitted[0]
    assert run_at_preflight is not None, (
        f"{ending}(source_path) finished without initial attachment before core preflight; "
        f"response={out!r}, source_actions={boundary.actions('source_attach', 'publish_source')!r}"
    )
    assert (run_at_preflight.mode, run_at_preflight.state, run_at_preflight.path) == (
        "initial", "attached", str(source))
    assert run_at_preflight.source_protocol == 2
    if with_artifacts:
        assert len(chosen_at_preflight) == 1, "artifact selection must precede source preflight"
    else:
        assert chosen_at_preflight is None, "no selection is distinct from explicitly selecting []"
    run = board.attached_run(conn, run_id)
    assert artifacts.selected(conn, tid, run.run_uid) == chosen_at_preflight
    assert run.manifest["version"] == 2
    assert run.manifest["source_kind"] == "scratch_external"
    assert run.manifest["manifest_revision"] == 1
    assert run.manifest["artifacts"] == (chosen_at_preflight or [])
    assert boundary.actions("source_attach", "publish_source") == ["source_attach", "publish_source"]
    assert boundary.body("source_attach")["mode"] == "initial"
    assert boundary.body("publish_source")["expected_revision"] == 0
    receipts = [op["fields"]["source_receipt"] for op in _leaving_op(conn, tid, landed)
                if "source_receipt" in op["fields"]]
    assert len(receipts) == 1, "the ending must carry exactly one source receipt"
    receipt = receipts[0]
    assert (receipt["state"], receipt["manifest_revision"], receipt["run_uid"]) == (
        "unchanged" if prior_state == "published" else "published", 1, run.run_uid)
    if with_artifacts:
        assert service.blobs[chosen_at_preflight[0]["attachment_uid"]] == b"completion-only source evidence"
        assert Path(kb.list_attachments(conn, tid)[0].stored_path).read_bytes() == b"completion-only source evidence"
    else:
        assert service.blobs == {} and kb.list_attachments(conn, tid) == []


@pytest.mark.parametrize("ending", list(ENDINGS))
@pytest.mark.parametrize("identity", ["foreign", "unbound", "stale", "operator"])
def test_ending_rejects_unbound_source_before_any_side_effect(ending, identity, setup, monkeypatch):
    handler, base_args, _, _, _, _ = ENDINGS[ending]
    conn, boundary, tid, source, _, _, _ = setup
    run_id = kb.get_task(conn, tid).current_run_id
    _bind(monkeypatch, conn, tid, run_id, identity)
    out = json.loads(getattr(kt, handler)({"task_id": tid, **base_args, "source_path": str(source)}))
    assert "error" in out, out
    assert kb.get_task(conn, tid).status == "running"
    assert board.attached_run(conn, run_id) is None
    assert boundary.actions("source_attach", "publish_source") == []
    assert kb.list_attachments(conn, tid) == []


@pytest.mark.parametrize("ending", list(ENDINGS))
def test_unanswered_ending_initial_keeps_pending_and_does_not_end(ending, setup, monkeypatch):
    handler, base_args, _, _, _, _ = ENDINGS[ending]
    conn, boundary, tid, source, _, _, _ = setup
    run_id = kb.get_task(conn, tid).current_run_id
    _bind(monkeypatch, conn, tid, run_id, "owner")
    boundary.offline.add("source_attach")
    out = json.loads(getattr(kt, handler)({"task_id": tid, **base_args, "source_path": str(source)}))
    assert "error" in out, out
    assert kb.get_task(conn, tid).status == "running"
    run = board.attached_run(conn, run_id)
    assert run is not None and run.state == "pending" and run.path == str(source)
    assert boundary.actions("publish_source") == []
    assert kb.list_attachments(conn, tid) == []


@pytest.mark.parametrize("path", [None, "", " ", "relative/worktree", 7])
def test_ending_explicit_path_must_be_absolute_nonempty(path, setup):
    from tools.kanban_tools_source import register_for_ending
    conn, boundary, tid, _, _, _, _ = setup
    run_id = kb.get_task(conn, tid).current_run_id
    with pytest.raises(ValueError, match="absolute path"):
        register_for_ending(conn, tid, {"source_path": path}, worker_task=tid, expected_run_id=run_id)
    assert board.attached_run(conn, run_id) is None
    assert boundary.actions("source_attach") == []


def test_omitted_ending_path_does_not_discover_any_source(setup, monkeypatch):
    from tools import kanban_tools_source as source_tools
    conn, boundary, tid, _, _, _, _ = setup
    tripwire = Mock(side_effect=AssertionError("omitted source must not register"))
    monkeypatch.setattr(source_tools, "heartbeat_source", tripwire)
    source_tools.register_for_ending(conn, tid, {}, worker_task=tid,
                                     expected_run_id=kb.get_task(conn, tid).current_run_id)
    tripwire.assert_not_called()
    assert boundary.actions("source_attach") == []


def test_ending_cannot_replace_existing_source_identity(setup, monkeypatch):
    from hermes_cli import kanban_source_registration as registration
    conn, boundary, tid, source, scratch, _, _ = setup
    run = registration.register_initial(conn, tid, str(source))
    _bind(monkeypatch, conn, tid, run.run_id, "owner")
    calls = len(boundary.calls)
    out = json.loads(kt._handle_complete({"task_id": tid, "summary": "done",
                                         "source_path": str(scratch)}))
    assert "error" in out, out
    assert kb.get_task(conn, tid).status == "running"
    assert board.attached_run(conn, run.run_id) == run
    assert len(boundary.calls) == calls


def test_only_approved_ending_schemas_expose_source_path():
    from tools import kanban_tools_schemas as schemas
    for ending in ENDINGS:
        schema = getattr(schemas, f"KANBAN_{ending.upper()}_SCHEMA")
        assert "source_path" in schema["parameters"]["properties"]
    assert "source_path" not in schemas.KANBAN_REQUEST_CHANGES_SCHEMA["parameters"]["properties"]


@pytest.mark.parametrize("ending", ["block", "schedule"])
def test_capability_routing_never_silently_drops_explicit_source(ending, setup, monkeypatch):
    conn, boundary, tid, source, _, _, _ = setup
    run_id = kb.get_task(conn, tid).current_run_id
    _bind(monkeypatch, conn, tid, run_id, "owner")
    route = Mock(side_effect=AssertionError("must refuse before creating an owner or parking"))
    monkeypatch.setattr(kt, "_route_needs", route)
    out = json.loads(getattr(kt, ENDINGS[ending][0])({
        "task_id": tid, "reason": "needs remote access", "needs": ["ssh:example"], "source_path": str(source),
    }))
    assert "source_path cannot be combined" in out["error"], out
    route.assert_not_called()
    assert kb.get_task(conn, tid).status == "running"
    assert board.attached_run(conn, run_id) is None
    assert boundary.actions("source_attach") == []


@pytest.mark.parametrize("ending", list(ENDINGS))
def test_reviewer_ending_cannot_initial_attach(ending, setup, monkeypatch):
    from tests.hermes_cli.test_kanban_source_initial_lane import review_lane
    conn, boundary, tid, source, _, _, _ = setup
    run_id = kb.get_task(conn, tid).current_run_id
    _bind(monkeypatch, conn, tid, run_id, "owner")
    review_lane(conn, tid)
    handler, base_args, _, _, _, _ = ENDINGS[ending]
    out = json.loads(getattr(kt, handler)({"task_id": tid, **base_args, "source_path": str(source)}))
    assert "error" in out, out
    assert kb.get_task(conn, tid).status == "running"
    assert board.attached_run(conn, run_id) is None
    assert boundary.actions("source_attach") == []

