"""A worker's PR comment keeps its owner across sync: provenance is the run's stable UID.

``writer_run_id`` is a source-database ``task_runs.id``. Sync remaps ``task_events.run_id``
but leaves the JSON payload unchanged, so on a replica that integer can name another
card's run. ``writer_run_uid`` is portable; when present it decides, with no integer
fallback. [daveey]
"""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_guard_helpers import worker_comment
from tests.hermes_cli.test_kanban_dependency_review import PR, parked

FYI = "https://github.com/example/repo/pull/99"


@pytest.fixture
def boards(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    with kbc.connect(home / "source.db") as source, kbc.connect(home / "target.db") as target:
        # Offset the destination's integer run namespace: source run 1 is target run 4.
        for _ in range(3):
            kb.claim_task(target, kb.create_task(target, title="local history", assignee="dev"))
        yield source, target


def transport(source, target):
    """Real outbox -> apply; no destination re-add of any comment."""
    changes = [sync.RemoteChange(i, op["table_name"], op["row_uid"], op["op"],
                                op["fields"], i, op["hlc"], "source")
               for i, op in enumerate(sync.outbox(source), 1)]
    sync.apply_remote_changes(target, changes, new_cursor=len(changes), observers=False)


def commented(conn, task):
    rows = conn.execute("SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'commented' "
                        "ORDER BY id", (task,)).fetchall()
    return [(r["id"], json.loads(r["payload"])) for r in rows]


def rewrite(conn, event_id, mutate):
    row = conn.execute("SELECT payload FROM task_events WHERE id = ?", (event_id,)).fetchone()
    payload = json.loads(row["payload"])
    mutate(payload)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), event_id))


def run_task(conn, run_id):
    return conn.execute("SELECT task_id FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0]


def test_worker_comment_stamps_run_uid(boards):
    source, _ = boards
    task, _ = parked(source)
    (_, payload), = commented(source, task)
    run = source.execute("SELECT id, uid FROM task_runs WHERE task_id = ?", (task,)).fetchone()
    assert payload["writer_run_id"] == run["id"]
    assert payload["writer_run_uid"] == run["uid"] and run["uid"]


def test_synced_owner_pr_comment_keeps_active_pr(boards):
    source, target = boards
    task, _ = parked(source)
    assert dispatch.check_respawn_guard(source, task) == "active_pr"
    transport(source, target)
    (_, payload), = commented(target, task)
    # Precondition: on the replica the payload integer names ANOTHER card's run.
    assert run_task(target, payload["writer_run_id"]) != task
    assert prs._foreign_pr_comment_ids(target, task) == set()
    assert dispatch.check_respawn_guard(target, task) == "active_pr"


def test_live_bridge_own_ids_resolve_by_uid_on_replica(boards):
    source, target = boards
    task, _ = parked(source)
    transport(source, target)
    (_, payload), = commented(target, task)
    local_run = target.execute("SELECT id FROM task_runs WHERE task_id = ?", (task,)).fetchone()[0]
    assert local_run != payload["writer_run_id"]
    assert kb.own_comment_ids(target, task, local_run) == {payload["comment_id"]}
    # The colliding integer's run (another card) does not claim this card's comment.
    assert kb.own_comment_ids(target, task, payload["writer_run_id"]) == set()
    src_run = source.execute("SELECT id FROM task_runs WHERE task_id = ?", (task,)).fetchone()[0]
    assert kb.own_comment_ids(source, task, src_run) == {payload["comment_id"]}


@pytest.mark.parametrize("synced", [False, True])
def test_foreign_card_worker_comment_with_real_uid_stays_fyi(boards, synced):
    """Another card's worker writes on this card: a real foreign uid stays foreign."""
    source, target = boards
    owner = kb.create_task(source, title="owner without a PR", assignee="dev")
    kb.claim_task(source, owner)
    other = kb.create_task(source, title="sibling", assignee="dev")
    kb.claim_task(source, other)
    other_run = source.execute("SELECT id FROM task_runs WHERE task_id = ?", (other,)).fetchone()[0]
    with pytest.MonkeyPatch.context() as env:
        env.setenv("HERMES_KANBAN_TASK", owner)  # stamped as the sibling's run on this card
        env.setenv("HERMES_KANBAN_RUN_ID", str(other_run))
        kb.add_comment(source, owner, "dev", f"FYI {FYI}")
    (_, payload), = commented(source, owner)
    assert payload["writer_run_uid"] == source.execute(
        "SELECT uid FROM task_runs WHERE id = ?", (other_run,)).fetchone()[0]
    conn = source
    if synced:
        transport(source, target)
        conn = target
    assert prs._foreign_pr_comment_ids(conn, owner) != set()
    assert dispatch.check_respawn_guard(conn, owner) is None


@pytest.mark.parametrize("synced", [False, True])
def test_foreign_uid_wins_over_owning_integer(boards, synced):
    """uid present and naming another card's run: foreign, even if the int names the owner."""
    source, target = boards
    task, reviews = parked(source)
    kb.claim_task(source, reviews[0])
    foreign_uid = source.execute("SELECT uid FROM task_runs WHERE task_id = ?", (reviews[0],)).fetchone()[0]
    (event_id, _), = commented(source, task)
    rewrite(source, event_id, lambda p: p.__setitem__("writer_run_uid", foreign_uid))
    conn = source
    if synced:
        transport(source, target)
        conn = target
    assert dispatch.check_respawn_guard(conn, task) is None


@pytest.mark.parametrize("bad_uid", [None, "", 42, "unknown-run-uid"])
def test_present_invalid_uid_never_falls_back_to_foreign_integer(boards, bad_uid):
    """A present but unusable uid fails closed (owned), never reverting to a foreign int."""
    source, _ = boards
    task, reviews = parked(source)
    kb.claim_task(source, reviews[0])
    foreign_run = source.execute("SELECT id FROM task_runs WHERE task_id = ?", (reviews[0],)).fetchone()[0]
    (event_id, _), = commented(source, task)
    rewrite(source, event_id, lambda p: p.update(writer_run_uid=bad_uid, writer_run_id=foreign_run))
    assert dispatch.check_respawn_guard(source, task) == "active_pr"


@pytest.mark.parametrize("synced", [False, True])
def test_legacy_uidless_comment_keeps_integer_behaviour(boards, synced):
    source, target = boards
    task, _ = parked(source)
    (event_id, _), = commented(source, task)
    rewrite(source, event_id, lambda p: p.pop("writer_run_uid", None))
    conn = source
    if synced:
        transport(source, target)
        conn = target
    # Legacy int: own run locally (held); on the replica it names another card's run (FYI),
    # which is today's documented legacy behaviour.
    assert dispatch.check_respawn_guard(conn, task) == (None if synced else "active_pr")


def test_unattributed_destination_comment_is_fyi(boards):
    """A chat/operator comment written on the replica carries no writer: FYI, not a hold."""
    source, target = boards
    task = kb.create_task(source, title="no worker PR yet", assignee="dev")
    kb.claim_task(source, task)
    transport(source, target)
    kb.add_comment(target, task, "dev", f"FYI {FYI}")
    assert dispatch.check_respawn_guard(target, task) is None
    # ...and it does not mask a synced owner publication either.
    owner, _ = parked(source)
    transport(source, target)
    kb.add_comment(target, owner, "dev", f"FYI {FYI}")
    assert dispatch.check_respawn_guard(target, owner) == "active_pr"


def test_worker_comment_helper_matches_tool_provenance(boards):
    source, _ = boards
    task = kb.create_task(source, title="t", assignee="dev")
    kb.claim_task(source, task)
    worker_comment(source, task, "dev", PR)
    (_, payload), = commented(source, task)
    assert set(payload) >= {"writer_run_id", "writer_run_uid"}
