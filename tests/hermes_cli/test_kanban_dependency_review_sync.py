"""Review continuation survives database-local run ID remapping, not identity mismatches."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.test_kanban_dependency_review import HEAD, PR, finish_reviews, parked


@pytest.fixture
def boards(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    with kbc.connect(home / "source.db") as source, kbc.connect(home / "target.db") as target:
        # Force a different local integer namespace without changing any transported row.
        for _ in range(3):
            task = kb.create_task(target, title="local history", assignee="dev")
            kb.claim_task(target, task)
        yield source, target


def transport(source, target):
    changes = [sync.RemoteChange(i, op["table_name"], op["row_uid"], op["op"],
                                op["fields"], i, op["hlc"], "source")
               for i, op in enumerate(sync.outbox(source), 1)]
    sync.apply_remote_changes(target, changes, new_cursor=len(changes), observers=False)
    # No destination re-add: the owner's PR comment keeps its writer by run uid across
    # the remap (test_kanban_pr_comment_writer_uid_sync).


def checkpoint_row(conn, task):
    return conn.execute("SELECT * FROM task_events WHERE task_id = ? AND kind = 'dependency_wait'",
                        (task,)).fetchone()


@pytest.mark.parametrize("key", ["reviewed_sha", "head"])
@pytest.mark.parametrize("verdict", ["changes_required", "approved"])
def test_sync_remap_preserves_one_review_continuation(boards, key, verdict):
    source, target = boards
    task, reviews = parked(source)
    finish_reviews(source, reviews, metadata={"verdict": verdict, key: HEAD, "pr": PR})
    transport(source, target)
    src, dst = checkpoint_row(source, task), checkpoint_row(target, task)
    assert src["run_id"] != dst["run_id"]
    assert src["payload"] == dst["payload"]
    assert dispatch.check_respawn_guard(target, task) is None
    assert kb.claim_task(target, task) is not None
    assert dispatch.check_respawn_guard(target, task) == "active_pr"
    assert kb.reclaim_task(target, task, reason="crash", signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(target, task) == "active_pr"


@pytest.mark.parametrize("key", ["reviewed_sha", "head"])
def test_local_change_request_accepts_either_exact_head_key(boards, key):
    source, _ = boards
    task, reviews = parked(source)
    finish_reviews(source, reviews, metadata={"verdict": "changes_required", key: HEAD, "pr": PR})
    assert dispatch.check_respawn_guard(source, task) is None


@pytest.mark.parametrize("bad_uid", [None, "", 42, "unrelated-run"])
def test_uid_mismatch_never_falls_back_to_matching_integer(boards, bad_uid):
    source, _ = boards
    task, reviews = parked(source)
    finish_reviews(source, reviews)
    row = checkpoint_row(source, task)
    payload = json.loads(row["payload"])
    payload["review_checkpoint"]["run_uid"] = bad_uid
    with kb.write_txn(source):
        source.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), row["id"]))
    assert dispatch.check_respawn_guard(source, task) == "active_pr"


@pytest.mark.parametrize("mode", ["other_pr", "short_head", "other_assignee", "unowned"])
def test_synced_invalid_review_or_owner_stays_held(boards, mode):
    source, target = boards
    task, reviews = parked(source, authenticated=mode != "unowned")
    finish_reviews(source, reviews, metadata={"verdict": "changes_required",
                   "head": HEAD[:8] if mode == "short_head" else HEAD,
                   "pr": PR + "1" if mode == "other_pr" else PR})
    if mode == "other_assignee":
        kb.assign_task(source, task, "other")
    transport(source, target)
    assert dispatch.check_respawn_guard(target, task) == "active_pr"


@pytest.mark.parametrize("remapped", [False, True])
def test_legacy_checkpoint_only_accepts_original_local_id(boards, remapped):
    source, target = boards
    task, reviews = parked(source)
    finish_reviews(source, reviews)
    row = checkpoint_row(source, task)
    payload = json.loads(row["payload"])
    payload["review_checkpoint"].pop("run_uid", None)
    with kb.write_txn(source):
        source.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), row["id"]))
    if remapped:
        transport(source, target)
    assert dispatch.check_respawn_guard(target if remapped else source, task) == ("active_pr" if remapped else None)


def test_real_other_run_uid_cannot_authorize_owner_checkpoint(boards):
    source, _ = boards
    task, reviews = parked(source)
    finish_reviews(source, reviews)
    row = checkpoint_row(source, task)
    payload = json.loads(row["payload"])
    other_uid = source.execute("SELECT uid FROM task_runs WHERE task_id = ?", (reviews[0],)).fetchone()[0]
    payload["review_checkpoint"]["run_uid"] = other_uid
    with kb.write_txn(source):
        source.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), row["id"]))
    assert dispatch.check_respawn_guard(source, task) == "active_pr"
