"""Premanifest successors carry only their exact predecessor's selected bytes."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_source_artifacts as artifacts
from hermes_cli import kanban_source_registration as registration
from tests.hermes_cli import test_kanban_source_registration as helpers

setup = helpers.setup


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("fault", [None, "foreign_predecessor", "lease_changed", "missing_row"])
def test_successor_inherits_explicit_selection_on_replay(setup, tmp_path, empty, fault, monkeypatch):
    conn, _b, tid, source, _scratch, _home, _clone = setup
    old = registration.register_initial(conn, tid, str(source))
    path = tmp_path / "evidence.txt"
    path.write_bytes(b"pending bytes")
    expected = artifacts.stage(conn, tid, [] if empty else [str(path)], board="default", run_uid=old.run_uid)
    # An unrelated historical selection must not become the successor's selection.
    artifacts.stage(conn, tid, [], board="default", run_uid="unrelated")
    path.unlink()
    conn.execute("UPDATE task_runs SET status='released', ended_at=? WHERE id=?", (int(time.time()), old.run_id))
    conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, claim_expires=NULL WHERE id=?", (tid,))
    conn.commit()
    assert kb.claim_task(conn, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
    uid = sync.run_uid(conn, kb.get_task(conn, tid).current_run_id)
    grant = {"status": "applied", "source_predecessor": {"task_id": tid, "run_uid": old.run_uid, "machine": old.machine},
             "lease": {"task_id": tid, "run_uid": uid, "holder": old.machine, "expires": int(time.time() * 1000) + 900000}}
    custody = {"attached_run_uid": None, "source_holder": old.machine, "source_protocol": 2,
               "manifest_revision": 0, "state": "retained", "retained_run_uid": old.run_uid}
    if fault == "foreign_predecessor":
        grant["source_predecessor"]["run_uid"] = "unrelated"
    elif fault == "lease_changed":
        persist = registration._persist_observation

        def expired(*args, **kwargs):
            conn.execute("UPDATE tasks SET lease_expires=1 WHERE id=?", (tid,))
            conn.commit()
            return persist(*args, **kwargs)

        monkeypatch.setattr(registration, "_persist_observation", expired)
    elif fault == "missing_row" and not empty:
        kb.delete_attachment(conn, kb.list_attachments(conn, tid)[0].id)
    if fault and (fault != "missing_row" or not empty):
        from hermes_cli import kanban_source_launch as launch
        with pytest.raises(launch.AttachRefused):
            registration.resume_premanifest(conn, tid, grant, custody)
        assert artifacts.selected(conn, tid, uid) is None
        return
    registration.resume_premanifest(conn, tid, grant, custody)
    assert artifacts.selected(conn, tid, uid) == expected
    # Once the successor intentionally changes its selection, attach retry cannot overwrite it.
    artifacts.stage(conn, tid, [], board="default", run_uid=uid)
    registration.resume_premanifest(conn, tid, grant, custody)
    assert artifacts.selected(conn, tid, uid) == []
    assert artifacts.selected(conn, tid, old.run_uid) == expected
