"""Historical projections bind to completed events, never latest arbitrary runs."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_completion_receipt_projection import read_completed
from tests.hermes_cli import test_kanban_completion_receipt as fixtures
from tests.hermes_state.test_completion_receipt import receipt

conn = fixtures.conn


def read(conn, tid):
    return read_completed(conn, tid, gateway="here", board="default")


def test_projection_preserves_exact_receipt_and_source(conn):
    tid = fixtures.task(conn)
    value = receipt("rejected")
    assert kb.complete_task(conn, tid, summary="Assessment finished, artifact rejected", metadata={"completion_receipt": value})
    out = read(conn, tid)
    assert out["completion_receipt"] == value
    source = out["completion_receipt_source"]
    assert source["ref"] == f"@task:default@here/{tid}"
    event = conn.execute("SELECT * FROM task_events WHERE id=?", (source["event_id"],)).fetchone()
    assert source["run_id"] == event["run_id"]
    assert source["completed_at"] == event["created_at"]
    import json
    assert json.loads(event["payload"])["completion_receipt_admission"] == 1
    # Returned dictionaries cannot mutate the canonical handoff.
    out["completion_receipt"]["items"][0]["disposition"] = "delivered"
    assert read(conn, tid)["completion_receipt"] == value


def test_later_noncompleted_run_never_replaces_historical_receipt(conn):
    tid = fixtures.task(conn)
    assert kb.complete_task(conn, tid, summary="Assessment complete", metadata={"completion_receipt": receipt("rejected")})
    before = read(conn, tid)
    with kb.write_txn(conn):
        kb._synthesize_ended_run(conn, tid, outcome="review", summary="Not acceptance",
                                metadata={"completion_receipt": receipt("delivered")})
    assert read(conn, tid) == before


@pytest.mark.parametrize("marker", [None, True, "1", 0, 2])
def test_old_or_unadmitted_metadata_is_not_a_receipt(conn, marker):
    tid = fixtures.task(conn)
    with kb.write_txn(conn):
        run = kb._synthesize_ended_run(conn, tid, outcome="completed", summary="Old writer",
                                     metadata={"completion_receipt": receipt()})
        kb._append_event(conn, tid, "completed", {"completion_receipt_admission": marker}, run_id=run)
    assert read(conn, tid) is None



def test_review_receipt_alone_is_not_delivery(conn):
    tid = fixtures.task(conn)
    assert kb.request_review(conn, tid, summary="Review requested", metadata={"completion_receipt": receipt()})
    assert read(conn, tid) is None


def test_legacy_completion_has_unspecified_acceptance(conn):
    tid = fixtures.task(conn)
    assert kb.complete_task(conn, tid, summary="Legacy finished")
    assert read(conn, tid) is None


def test_no_source_gateway_is_not_silently_local(conn):
    tid = fixtures.task(conn)
    with pytest.raises(ValueError, match="source gateway"):
        read_completed(conn, tid, gateway="", board="default")


def test_cross_task_run_mismatch_never_projects_foreign_receipt(conn):
    tid = fixtures.task(conn)
    other = fixtures.task(conn)
    assert kb.complete_task(conn, other, summary="Other assessment", metadata={"completion_receipt": receipt()})
    run_id = conn.execute("SELECT run_id FROM task_events WHERE task_id=? AND kind='completed'", (other,)).fetchone()[0]
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "completed", {}, run_id=run_id)
    assert read(conn, tid) is None
