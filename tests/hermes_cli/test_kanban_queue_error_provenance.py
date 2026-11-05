"""Board-error provenance: stage + allowlisted transport subtype, never raw messages/urls/attributes."""
import http.client
import json
import urllib.error
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_queue_reason as qr
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_worker_reserve_client as fixtures


@pytest.fixture(autouse=True)
def _confirm_on_first_pass(monkeypatch):
    """These tests pin the provenance of the recorded board_error, not the transient-failure debounce
    (tests/hermes_cli/test_kanban_queue_transient_debounce.py): confirm a transient failure on its first pass."""
    from hermes_cli import kanban_queue_reason

    monkeypatch.setattr(kanban_queue_reason, "CONFIRM_PASSES", 1)


conn = fixtures.conn
CFG = {"worker_pools": {"hub": ["worker-1"]}}
SECRET = "https://user:hunter2-SECRET@hub.invalid/api?token=SECRET"


def _event_blobs(conn, tid):
    return [r[0] for r in conn.execute("SELECT payload FROM task_events WHERE task_id=?", (tid,)).fetchall()]


def test_hub_deadline_raises_board_budget_exhausted(monkeypatch):
    monkeypatch.setattr(client, "time", SimpleNamespace(monotonic=lambda: 100.0))
    hub = client.Hub(client.Identity("http://example.invalid", "synthetic", "hub"), deadline=99.0)
    with pytest.raises(client.BoardBudgetExhausted) as info:
        hub._remaining()
    assert isinstance(info.value, TimeoutError)  # existing `except OSError`/TimeoutError callers unchanged
    assert qr.transport_kind(info.value) == "budget_exhausted"
    assert qr.transport_kind(urllib.error.URLError(info.value)) == "budget_exhausted"


def test_provenance_does_not_change_deduplication(conn):
    tid = fixtures.acknowledged(conn)
    assert qr.record_error(conn, tid, "hub", error=TimeoutError(SECRET), stage="pull")
    before = _event_blobs(conn, tid)
    assert not qr.record_error(conn, tid, "hub", error=ConnectionResetError(SECRET), stage="reserve")
    assert _event_blobs(conn, tid) == before


@pytest.mark.parametrize("error, kind", [
    (TimeoutError("timed out " + SECRET), "timeout"),
    (urllib.error.URLError(TimeoutError(SECRET)), "timeout"),

    (http.client.RemoteDisconnected(SECRET), "disconnected"),
    (ConnectionResetError(104, SECRET), "reset"),
    (ConnectionRefusedError(111, SECRET), "refused"),
    (OSError(SECRET), "other"),
    (urllib.error.URLError(SECRET), "other"),  # reason is a str, not an exception: never unwrapped
])
def test_transport_kind_is_allowlisted(error, kind):
    assert qr.transport_kind(error) == kind


@pytest.mark.parametrize("stage", ["pull", "reserve", "repin"])
def test_record_error_persists_stage_and_transport_without_secrets(conn, stage):
    tid = fixtures.acknowledged(conn)
    assert qr.record_error(conn, tid, "hub", error=urllib.error.URLError(ConnectionResetError(SECRET)), stage=stage)
    value = qr.latest(conn, tid)
    assert value["error_class"] == "OSError"  # unchanged field
    assert value["stage"] == stage and value["transport"] == "reset"
    assert all("SECRET" not in blob and "hunter2" not in blob for blob in _event_blobs(conn, tid))


def test_unknown_exception_and_stage_are_not_serialized(conn):
    class Weird(Exception):
        url = SECRET
    tid = fixtures.acknowledged(conn)
    assert qr.record_error(conn, tid, "hub", error=Weird(SECRET), stage="not-a-stage")
    value = qr.latest(conn, tid)
    assert value["error_class"] == "unknown" and "stage" not in value and "transport" not in value
    assert all("SECRET" not in blob for blob in _event_blobs(conn, tid))


def test_legacy_call_without_stage_is_unchanged(conn):
    tid = fixtures.acknowledged(conn)
    assert qr.record_error(conn, tid, "hub", status=500)
    value = qr.latest(conn, tid)
    assert "stage" not in value and "transport" not in value and value["http_status"] == 500


def test_record_pull_error_propagates_pull_stage(conn):
    tid = fixtures.acknowledged(conn)
    qr.record_pull_error(conn, "hub", error=http.client.RemoteDisconnected(SECRET))
    value = qr.latest(conn, tid)
    assert value["stage"] == "pull" and value["transport"] == "disconnected"


@pytest.mark.parametrize("action", ["reserve", "repin"])
def test_reevaluate_passes_stage(conn, monkeypatch, action):
    tid = fixtures.acknowledged(conn)
    hub = fixtures.RecordingHub()
    def fail(board, **body):
        raise ConnectionRefusedError(111, SECRET)
    setattr(hub, action, fail)
    monkeypatch.setattr(client, "pull", lambda *a, **kw: None)
    if action == "repin":
        monkeypatch.setattr(wp, "requested_move", lambda conn, task_id: ["worker-1"])
    assert wp.reevaluate(conn, hub, "default", "hub", cfg=CFG) == 0
    value = qr.latest(conn, tid)
    assert value["stage"] == action and value["transport"] == "refused"
    assert all("SECRET" not in json.dumps(blob) for blob in _event_blobs(conn, tid))
