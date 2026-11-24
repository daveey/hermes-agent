"""Home-marker create regressions: collection hints, artifact custody and cache identity."""
from __future__ import annotations

from pathlib import Path
import subprocess
import threading
import time

import pytest

from hermes_cli import kanban_home_marker_placement as khm
from tests.hermes_cli import test_kanban_pool_queue as helpers

# Reuse the execution-policy seam as well as its gateway fixtures: these
# assertions intentionally inspect the execution-layer local INSERT/marker.
# Canonical hub ownership is covered by the unpatched composition suite.
execution_policy_layer = helpers.execution_policy_layer
svc = helpers.svc
w2 = helpers.w2
on_w2 = helpers.on_w2


@pytest.mark.parametrize("path", ["~/code/_wt", "~/code/_wt/", "~/code/repo/.worktrees"])
def test_collection_path_queues_with_actionable_note(on_w2, tmp_path, path):
    (tmp_path / path.removeprefix("~/")).mkdir(parents=True, exist_ok=True)
    out = helpers._queued(on_w2.conn, helpers._create(title=f"Inspect {path}"), [])
    assert helpers._home_marker(on_w2.conn, out["task_id"]) == {
        "decision": "queue", "class": "worktree-not-here", "hit": path}
    assert out["note"] == (
        f"{path} names the worktree collection, not one tree; name the tree to keep the card here.")


@pytest.mark.parametrize("prefix", ["~", "$HOME", "${HOME}", '"$HOME"', '"${HOME}"', "own", "foreign"])
def test_artifact_checks_preserve_home_identity(on_w2, tmp_path, monkeypatch, prefix):
    rel = ".hermes/kanban/workspaces/t_0000000000000042"
    (tmp_path / rel).mkdir(parents=True)
    home = f"/home/{tmp_path.name}" if prefix == "own" else (
        "/home/someoneelse" if prefix == "foreign" else prefix)
    raw = f"{home}/{rel}/log.txt"
    # Never depend on a real foreign account's filesystem in this regression.
    real_exists, checked = Path.exists, []

    def exists(path):
        checked.append(path)
        return False if str(path).startswith("/home/someoneelse/") else real_exists(path)

    monkeypatch.setattr(Path, "exists", exists)
    out = helpers._create(title=f"Inspect {raw}")
    if prefix == "foreign":
        helpers._queued(on_w2.conn, out, [helpers.HUB_WRITE])
        expected = "artifact-not-here"
        assert Path(f"/home/someoneelse/{rel}") in checked
    else:
        helpers._kept(on_w2.conn, out)
        expected = "a-artifact"
        assert tmp_path / rel in checked
    assert helpers._home_marker(on_w2.conn, out["task_id"])["class"] == expected


@pytest.mark.parametrize("kind", ["workspaces", "attachments"])
@pytest.mark.parametrize("present", [False, True])
def test_board_scoped_artifact_custody(on_w2, tmp_path, kind, present):
    rel = f".hermes/kanban/boards/research/{kind}/t_0000000000000043"
    if present:
        (tmp_path / rel).mkdir(parents=True)
    raw = f"~/{rel}/report.txt"
    out = helpers._create(title=f"Inspect {raw}")
    if present:
        helpers._kept(on_w2.conn, out)
    else:
        helpers._queued(on_w2.conn, out, [helpers.HUB_WRITE])
    assert helpers._home_marker(on_w2.conn, out["task_id"]) == {
        "decision": "keep" if present else "queue",
        "class": "a-artifact" if present else "artifact-not-here", "hit": raw}


@pytest.mark.parametrize("change", ["requires", "config", "replay"])
def test_create_cache_rechecks_changed_inputs_and_records_actual_decision(w2, monkeypatch, change):
    cfg = {"worker_queue": {"worker-2": helpers.POOL}, "extra_home_markers": []}
    machines = helpers._pool_machines()
    for name in ("worker-1", "worker-2"):
        machines[name] = {**machines[name], "labels": [*machines[name]["labels"], "capability:hub-read"]}
    helpers._on(monkeypatch, "worker-2", cfg, machines)
    original, warmed = khm.classify, []

    def classify(args, config, gateway):
        if not warmed:
            warmed.append(True)
            if change == "requires":
                prior = original({**args, "requires": ["hub:read"]}, config, gateway)
                assert prior["class"] == "d", prior  # hub:read clears 'on the hub', not bare ssh
            else:
                if change == "replay":
                    assert original(args, config, gateway)["decision"] == "queue"
                # Mutate the same list in place: the cache must snapshot configuration, not retain its identity.
                config["extra_home_markers"] = [r"\bgrafana\b"]
                prior = original(args, config, gateway)
                assert prior["class"] == "operator", prior
                config["extra_home_markers"].clear()
        return original(args, config, gateway)

    monkeypatch.setattr(khm, "classify", classify)
    out = helpers._queued(w2.conn, helpers._create(title="Inspect grafana on the hub via ssh"), [helpers.HUB_WRITE])
    assert warmed
    marker = helpers._home_marker(w2.conn, out["task_id"])
    assert marker == {"decision": "queue", "class": "b", "hit": "on the hub"}


def test_two_slow_remotes_share_create_probe_deadline(on_w2, tmp_path, monkeypatch):
    from hermes_cli import worktree_ops
    tree = helpers._pushed_clone(tmp_path, tmp_path / "code/_wt/slow")
    helpers._git(tree, "commit", "--allow-empty", "-m", "unpublished")
    helpers._git(tree, "remote", "add", "second", str(tmp_path / "remote.git"))
    original = worktree_ops._git_out
    calls = []
    budget = 0.8
    monkeypatch.setattr(khm, "PROBE_BUDGET_S", budget)

    def git_out(args, *pos, **kw):
        if args[0] != "ls-remote":
            return original(args, *pos, **kw)
        calls.append((args[-1], kw["timeout"]))
        if len(calls) == 1:
            threading.Event().wait(0.2)  # health: allow HX013 -- elapsed remote latency is the behavior under test
            return ""
        threading.Event().wait(kw["timeout"])  # health: allow HX013 -- emulate a remote consuming its timeout
        raise subprocess.TimeoutExpired(args, kw["timeout"])

    monkeypatch.setattr(worktree_ops, "_git_out", git_out)
    started = time.monotonic()
    state = khm._Probe().tree("~/code/_wt/slow")
    elapsed = time.monotonic() - started
    assert state and state.startswith("unchecked worktree"), state  # caller keeps unverified state
    assert len(calls) == 2, calls
    assert calls[1][1] < calls[0][1] - 0.15, calls
    assert elapsed < budget + 0.3, elapsed
