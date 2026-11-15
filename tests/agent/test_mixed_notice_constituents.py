"""Mixed user runs preserve notice identity without sacrificing ordinary input."""
from copy import deepcopy

import pytest

from agent.agent_runtime_helpers import repair_message_sequence
from tests.agent import test_in_place_preflight_rewind as _rewind

session = _rewind.session


def notice(key, row, *, woken=False):
    msg = {"role": "user", "content": f"notice {key}", "_row_id": row,
           "display_kind": "kanban_notice", "display_metadata": {"notice_id": key}}
    if woken:
        msg["api_content"] = f"notice {key}\n[ask-gate] live state {key}"
    return msg


@pytest.mark.parametrize("woken_first", [False, True])
@pytest.mark.parametrize("interleaved", [False, True])
def test_plain_user_notice_pair(woken_first, interleaved):
    a, wake = notice("A", 2), notice("A", 4, woken=True)
    pair = [wake, a] if woken_first else [a, wake]
    if interleaved:
        pair.insert(1, notice("B", 3))
    rows = [{"role": "assistant", "content": "ok"},
            {"role": "user", "content": "unrelated request", "_row_id": 1}, *pair]
    original = deepcopy(rows)
    repair_message_sequence(None, rows)
    kept = rows[-1]
    assert kept["content"].count("notice A") == 1
    assert kept["content"].count("unrelated request") == 1
    assert "[ask-gate]" not in kept["content"]
    assert kept["api_content"].count("notice A") == 1
    assert kept["api_content"].count("[ask-gate] live state A") == 1
    assert "unrelated request" in kept["api_content"]
    if interleaved:
        assert kept["content"].count("notice B") == 1
        assert kept["api_content"].count("notice B") == 1
    assert {r["_row_id"] for r in original[1:]} == {kept["_row_id"], *kept["_absorbed_row_ids"]}
    assert repair_message_sequence(None, rows) == 0


def test_current_notice_override_does_not_reappend_deduplicated_turn():
    from types import SimpleNamespace
    from agent.session_persistence import _db_flush_row

    rows = [{"role": "assistant", "content": "ok"},
            {"role": "user", "content": "unrelated request"},
            notice("A", 1), notice("B", 2), notice("A", 3, woken=True)]
    repair_message_sequence(None, rows)
    agent = SimpleNamespace(_persist_user_message_override="notice A")
    written = _db_flush_row(agent, rows[-1], True)
    assert written["content"] == "unrelated request\n\nnotice A\n\nnotice B"
    assert written["api_content"].count("[ask-gate] live state A") == 1


def test_edited_constituent_is_not_superseded_by_old_identity():
    rows = [{"role": "assistant", "content": "ok"},
            {"role": "user", "content": "unrelated request"}, notice("A", 1)]
    repair_message_sequence(None, rows)
    rows[-1]["content"] = rows[-1]["content"].replace("notice A", "my edit!")
    rows.append(notice("A", 2, woken=True))
    repair_message_sequence(None, rows)
    assert "my edit!" in rows[-1]["content"]
    assert rows[-1]["content"].count("notice A") == 1


@pytest.mark.parametrize("bad_spans", [[None], [{"offsets": [-1, 2, 0, 2]}], "bad", []])
def test_malformed_metadata_never_discards_plain_input(bad_spans):
    from agent.agent_runtime_helpers_user_rows import NOTICE_PARTS

    rows = [{"role": "assistant", "content": "ok"},
            {"role": "user", "content": "unrelated request", "display_kind": "kanban_notice",
             "display_metadata": {"notice_id": "A", NOTICE_PARTS: bad_spans}}, notice("A", 2, woken=True)]
    repair_message_sequence(None, rows)
    assert "unrelated request" in rows[-1]["content"]
    assert "notice A" in rows[-1]["content"]


@pytest.mark.parametrize("row_ids", [False, True])
@pytest.mark.parametrize("woken_first", [False, True])
def test_durable_mixed_run_flush_and_reload(tmp_path, row_ids, woken_first):
    from hermes_state import SessionDB
    from run_agent import AIAgent

    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("mixed", source="tui", model="test")
        db.append_message("mixed", "assistant", "ok")
        db.append_message("mixed", "user", "unrelated request")
        for row in ([notice("A", 1, woken=True), notice("B", 2), notice("A", 3)] if woken_first else
                    [notice("A", 1), notice("B", 2), notice("A", 3, woken=True)]):
            db.append_message("mixed", "user", row["content"], display_kind=row["display_kind"],
                              display_metadata=row["display_metadata"], api_content=row.get("api_content"))
        rows = db.get_messages_as_conversation("mixed", repair_alternation=True, include_row_ids=row_ids)
        agent = AIAgent.__new__(AIAgent)
        agent._session_db, agent.session_id = db, "mixed"
        agent._session_db_created = True
        agent._last_flushed_db_idx = 0
        agent._flushed_db_message_ids = set()
        agent._flushed_db_message_session_id = None
        agent._persist_disabled = False
        assert agent._flush_messages_to_session_db(rows)
        # Flush is append/rewrite, not compaction: originals remain until the
        # compaction transaction retires them. The supported replay repairs them.
        raw = db.get_messages_as_conversation("mixed", include_row_ids=row_ids)
        if not row_ids:  # snapshot/CAS replay rewrites; legacy addressed replay waits for compaction
            assert raw[1]["api_content"].count("[ask-gate] live state A") == 1
        loaded = db.get_messages_as_conversation("mixed", repair_alternation=True, include_row_ids=row_ids)
        assert len(loaded) == 2, (rows, loaded)
        assert loaded[-1]["content"] == "unrelated request\n\nnotice A\n\nnotice B"
        assert loaded[-1]["api_content"].count("[ask-gate] live state A") == 1
        assert "[ask-gate]" not in str(loaded[-1]["display_metadata"])
        loaded.append(notice("A", 999))
        repair_message_sequence(None, loaded)
        assert loaded[-1]["content"].count("notice A") == 1
        assert loaded[-1]["api_content"].count("[ask-gate] live state A") == 1


@pytest.mark.parametrize("surface", ["cli", "resume", "gateway"])
@pytest.mark.parametrize("woken_first", [False, True])
def test_mixed_notices_survive_compaction_and_retire_originals(session, surface, woken_first):
    from types import SimpleNamespace

    db, agent = session
    cli = SimpleNamespace(conversation_history=[])
    for n in range(1, 13):
        _rewind._turn(db, agent, cli, surface, n, 5_000)
    original_ids = [db.append_message("sid", "user", "unrelated request")]
    pair = [notice("A", 1), notice("A", 2, woken=True)]
    if woken_first:
        pair.reverse()
    pair.insert(1, notice("B", 3))
    for row in pair:
        original_ids.append(db.append_message("sid", "user", row["content"],
            display_kind=row["display_kind"], display_metadata=row["display_metadata"],
            api_content=row.get("api_content")))
    if surface == "cli":
        cli.conversation_history = db.get_messages_as_conversation("sid", repair_alternation=True)
    elif surface == "resume":
        cli.conversation_history = db.get_resume_conversations("sid")[0]
    _rewind._turn(db, agent, cli, surface, 13, 5_000)
    _rewind._turn(db, agent, cli, surface, 14, 200_000)
    _rewind._turn(db, agent, cli, surface, 15, 20_000)
    assert agent._last_compaction_in_place is True
    live = db.get_messages_as_conversation("sid")
    display = "\n".join(str(row.get("content", "")) for row in live)
    model = "\n".join(str(row.get("api_content", row.get("content", ""))) for row in live)
    for text in ("unrelated request", "notice A", "notice B"):
        assert display.count(text) == 1
        assert model.count(text) == 1
    assert model.count("[ask-gate] live state A") == 1
    assert "[ask-gate]" not in display
    for row_id in original_ids:
        assert tuple(db._conn.execute("SELECT active, compacted FROM messages WHERE id = ?", (row_id,)).fetchone()) == (0, 0)

