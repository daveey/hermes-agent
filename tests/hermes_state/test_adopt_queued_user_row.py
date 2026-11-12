"""adopt_queued_user_row_in_place: the drain's one-row path for a queued submit (t_01a1ceb141b10c7c)."""
import json

from hermes_state import SessionDB
from hermes_state_message_content import adopt_queued_user_row_in_place as _adopt


def _meta(db, row_id):
    with db._lock:
        row = db._conn.execute("SELECT display_metadata, active FROM messages WHERE id = ?", (row_id,)).fetchone()
    return (json.loads(row[0]) if row[0] else None), row[1]


def _db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s", source="scratch")
    return db


def test_last_marked_row_is_unmarked_in_place_keeping_other_keys(tmp_path):
    db = _db(tmp_path)
    rid = db.append_message("s", "user", content="hi", display_metadata={"_queued_prompt": True, "reaction": "+1"})
    assert _adopt(db, "s", rid) == 1
    assert _meta(db, rid) == ({"reaction": "+1"}, 1)
    assert db.retire_undrained_queue_rows("s") == 0


def test_marker_only_metadata_becomes_null(tmp_path):
    db = _db(tmp_path)
    rid = db.append_message("s", "user", content="hi", display_metadata={"_queued_prompt": True})
    assert _adopt(db, "s", rid) == 1
    assert _meta(db, rid) == (None, 1)


def test_a_later_active_row_refuses(tmp_path):
    db = _db(tmp_path)
    rid = db.append_message("s", "user", content="hi", display_metadata={"_queued_prompt": True})
    db.append_message("s", "assistant", content="later")
    assert _adopt(db, "s", rid) == 0
    assert _meta(db, rid) == ({"_queued_prompt": True}, 1)


def test_a_later_inactive_row_does_not_refuse(tmp_path):
    db = _db(tmp_path)
    rid = db.append_message("s", "user", content="hi", display_metadata={"_queued_prompt": True})
    later = db.append_message("s", "assistant", content="retired")
    db.deactivate_message("s", later)
    assert _adopt(db, "s", rid) == 1


def test_a_later_row_in_another_session_does_not_refuse(tmp_path):
    db = _db(tmp_path)
    db.create_session("other", source="scratch")
    rid = db.append_message("s", "user", content="hi", display_metadata={"_queued_prompt": True})
    db.append_message("other", "assistant", content="elsewhere")
    assert _adopt(db, "s", rid) == 1


def test_unmarked_inactive_wrong_session_or_role_rows_refuse(tmp_path):
    db = _db(tmp_path)
    first = db.append_message("s", "user", content="first", display_metadata={"_queued_prompt": True})
    assert first == 1
    for bad in (0, -1, True, None, "1"):  # True / "1" would bind as row 1 without the guard
        assert _adopt(db, "s", bad) == 0, bad
    assert _meta(db, first) == ({"_queued_prompt": True}, 1)
    plain = db.append_message("s", "user", content="plain")
    assert _adopt(db, "s", plain) == 0, "not a queued accept row"
    gone = db.append_message("s", "user", content="gone", display_metadata={"_queued_prompt": True})
    db.deactivate_message("s", gone)
    assert _adopt(db, "s", gone) == 0, "inactive"
    asst = db.append_message("s", "assistant", content="a", display_metadata={"_queued_prompt": True})
    assert _adopt(db, "s", asst) == 0, "not a user row"
    mine = db.append_message("s", "user", content="mine", display_metadata={"_queued_prompt": True})
    assert _adopt(db, "other", mine) == 0, "wrong session"
    assert _adopt(db, "", mine) == 0
    assert _meta(db, mine) == ({"_queued_prompt": True}, 1)
