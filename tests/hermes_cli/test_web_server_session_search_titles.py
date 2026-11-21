"""GET /api/sessions/search must match session titles, not only ids and FTS5 message content.

Regression for #66242: the search endpoint composed only
``search_sessions_by_id`` (id/lineage LIKE) with ``search_messages`` (FTS5 over
message content), so a term that lived solely in a manually-set
``sessions.title`` returned zero results. The DB layer already had title
matching — ``list_sessions_rich(search_query=...)`` LIKE-matches titles across
the whole compression chain — and the endpoint never called it. The fix
searches titles before content through that existing helper via the same
add_lineage_result dedup path (ID hits retain first priority).

These tests drive the real SessionDB through the real route handler (no fake
DB) so the title lane is exercised end to end, including the SQLite
read-only connection the endpoint opens.
"""

import asyncio
import threading

import pytest

import hermes_cli.web_server_sessions as _web_server_sessions
import hermes_cli.web_routers.sessions as _rt_sessions
from hermes_state import SessionDB


@pytest.fixture
def real_db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    # A session whose message content never contains the search needle —
    # only its (manually set) title does.
    db.create_session(session_id="titled_session", source="cli", model="m")
    db.set_session_title("titled_session", "Building the Modpack Server")
    db.append_message("titled_session", role="user", content="hello world")
    # A content-matching session: the needle appears in message text only.
    db.create_session(session_id="content_session", source="cli", model="m")
    db.append_message("content_session", role="user", content="modpack build notes")
    # An unrelated session that must never surface.
    db.create_session(session_id="other_session", source="cli", model="m")
    db.append_message("other_session", role="user", content="completely unrelated")
    yield db
    try:
        db.close()
    except Exception:
        pass


@pytest.fixture
def db_path_for_profile(real_db, monkeypatch):
    """Point the endpoint's profile DB opener at the real temp state.db."""
    monkeypatch.setattr(
        _web_server_sessions, "_session_db_path_for_profile", lambda profile: real_db.db_path
    )


@pytest.mark.parametrize("with_id_match", [False, True])
def test_search_title_precedes_crowded_content_hits(real_db, db_path_for_profile, with_id_match):
    real_db.create_session(session_id="title_only", source="cli", model="m")
    real_db.set_session_title("title_only", "Face")
    real_db.append_message("title_only", role="user", content="hello world")
    if with_id_match:
        real_db.create_session(session_id="Face-id", source="cli", model="m")
        real_db.append_message("Face-id", role="user", content="direct match")
    for index in range(30):
        sid = f"content-{index}"
        real_db.create_session(session_id=sid, source="cli", model="m")
        real_db.append_message(sid, role="user", content="face in the crowd")

    response = asyncio.run(_rt_sessions.search_sessions(q="Face", limit=20))
    ids = [row["session_id"] for row in response["results"]]
    assert len(ids) == len(set(ids)) == 20
    expected_prefix = ["Face-id", "title_only"] if with_id_match else ["title_only"]
    assert ids[:len(expected_prefix)] == expected_prefix
    assert all(sid.startswith("content-") for sid in ids[len(expected_prefix):])


def test_search_finds_session_by_partial_title(db_path_for_profile):
    """A term that appears ONLY in the title is findable (#66242); the snippet
    falls back to the session preview, and rows carry the same shape as id hits."""
    response = asyncio.run(_rt_sessions.search_sessions(q="Modpack", limit=20))
    ids = [row["session_id"] for row in response["results"]]
    assert "titled_session" in ids
    row = next(r for r in response["results"] if r["session_id"] == "titled_session")
    assert row["snippet"]  # preview or the title-match fallback text
    assert row["title"] == "Building the Modpack Server"


def test_search_title_lane_precedes_content_hits(db_path_for_profile):
    """Title hits outrank content-only hits; all lanes share lineage dedup."""
    response = asyncio.run(_rt_sessions.search_sessions(q="modpack", limit=20))
    ids = [row["session_id"] for row in response["results"]]
    assert set(ids) == {"content_session", "titled_session"}
    assert ids[0] == "titled_session"  # Title hit outranks content-only hits
    assert "other_session" not in ids


def test_search_title_lane_respects_limit(db_path_for_profile):
    """The merged result stays capped at limit across all lanes."""
    response = asyncio.run(_rt_sessions.search_sessions(q="modpack", limit=1))
    assert len(response["results"]) == 1


def test_search_title_lane_runs_off_event_loop(db_path_for_profile, monkeypatch):
    """The title backfill runs on the endpoint's worker thread, not the loop
    thread (#60747 discipline): the whole _search body already runs in
    asyncio.to_thread, so the real SessionDB calls must never land on the loop."""
    loop_thread = threading.get_ident()
    db_threads: list[int] = []
    real_search_messages = SessionDB.search_messages

    def spy(self, *args, **kwargs):
        db_threads.append(threading.get_ident())
        return real_search_messages(self, *args, **kwargs)

    monkeypatch.setattr(SessionDB, "search_messages", spy)
    asyncio.run(_rt_sessions.search_sessions(q="Modpack", limit=20))
    assert db_threads
    assert all(tid != loop_thread for tid in db_threads)
