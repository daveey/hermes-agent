"""An ordering-parent park survives sync run-id remapping, matched by stable run UID."""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_guard_helpers import worker_comment

PR = "https://github.com/example/repo/pull/10"


@pytest.fixture
def boards(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    with kbc.connect(home / "source.db") as source, kbc.connect(home / "target.db") as target:
        # Offset the destination's integer run namespace without touching any transported row.
        for _ in range(3):
            kb.claim_task(target, kb.create_task(target, title="local history", assignee="dev"))
        yield source, target


def park(conn, monkeypatch):
    clock = [int(time.time()) - 1000]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    task = kb.create_task(conn, title="finish existing PR", assignee="dev")
    kb.claim_task(conn, task)
    run = kb.get_task(conn, task).current_run_id
    worker_comment(conn, task, "dev", PR)
    comment_at = clock[0]
    parent = kb.create_task(conn, title="ordering prerequisite", assignee="dev")
    kb.link_tasks(conn, parent, task, expected_child_run_id=run)
    clock[0] += 10
    assert kb.block_task(conn, task, kind="dependency", reason="ordering", expected_run_id=run)
    clock[0] += 10
    kb.claim_task(conn, parent)
    assert kb.complete_task(conn, parent, summary="ordering work finished")
    kb.recompute_ready(conn)
    assert kb.get_task(conn, task).status == "ready"
    return task, parent, comment_at, clock


def transport(source, target, task, comment_at, clock):
    changes = [sync.RemoteChange(i, op["table_name"], op["row_uid"], op["op"],
                                op["fields"], i, op["hlc"], "source")
               for i, op in enumerate(sync.outbox(source), 1)]
    sync.apply_remote_changes(target, changes, new_cursor=len(changes), observers=False)
    # No destination re-add: the owner's PR comment keeps its writer by run uid across
    # the remap (test_kanban_pr_comment_writer_uid_sync).
    del task, comment_at, clock


def event(conn, task):
    return conn.execute("SELECT * FROM task_events WHERE task_id = ? AND kind = 'dependency_wait'",
                        (task,)).fetchone()


def test_local_ordering_park_released(boards, monkeypatch):
    source, _ = boards
    task, _, _, _ = park(source, monkeypatch)
    assert prs.active_pr_reason(source, task, int(time.time())) is None


def test_synced_ordering_park_released_despite_run_id_remap(boards, monkeypatch):
    source, target = boards
    task, _, comment_at, clock = park(source, monkeypatch)
    transport(source, target, task, comment_at, clock)
    assert event(source, task)["run_id"] != event(target, task)["run_id"]
    assert event(source, task)["payload"] == event(target, task)["payload"]
    assert prs.active_pr_reason(target, task, int(time.time())) is None


def _rewrite(conn, task, mutate):
    row = event(conn, task)
    payload = json.loads(row["payload"])
    mutate(payload["review_checkpoint"])
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), row["id"]))


@pytest.mark.parametrize("bad_uid", [None, "", 42, "unrelated-run"])
@pytest.mark.parametrize("remapped", [False, True])
def test_bad_uid_stays_held_even_if_integer_matches(boards, monkeypatch, bad_uid, remapped):
    source, target = boards
    task, _, comment_at, clock = park(source, monkeypatch)
    _rewrite(source, task, lambda c: c.__setitem__("run_uid", bad_uid))
    conn = source
    if remapped:
        transport(source, target, task, comment_at, clock)
        conn = target
    assert prs.active_pr_reason(conn, task, int(time.time())) == "active_pr"


def test_foreign_real_uid_stays_held(boards, monkeypatch):
    source, target = boards
    task, parent, comment_at, clock = park(source, monkeypatch)
    other = source.execute("SELECT uid FROM task_runs WHERE task_id = ?", (parent,)).fetchone()[0]
    _rewrite(source, task, lambda c: c.__setitem__("run_uid", other))
    transport(source, target, task, comment_at, clock)
    assert prs.active_pr_reason(target, task, int(time.time())) == "active_pr"


@pytest.mark.parametrize("remapped", [False, True])
def test_legacy_uidless_checkpoint_only_matches_original_id(boards, monkeypatch, remapped):
    source, target = boards
    task, _, comment_at, clock = park(source, monkeypatch)
    _rewrite(source, task, lambda c: c.pop("run_uid", None))
    conn = source
    if remapped:
        transport(source, target, task, comment_at, clock)
        conn = target
    assert prs.active_pr_reason(conn, task, int(time.time())) == ("active_pr" if remapped else None)


@pytest.mark.parametrize("remapped", [False, True])
def test_second_claim_consumes_the_handoff(boards, monkeypatch, remapped):
    source, target = boards
    task, _, comment_at, clock = park(source, monkeypatch)
    conn = source
    if remapped:
        transport(source, target, task, comment_at, clock)
        conn = target
    assert kb.claim_task(conn, task) is not None
    assert prs.active_pr_reason(conn, task, int(time.time())) == "active_pr"
