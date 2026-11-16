"""Remote-only card changes refresh authoritative stored chat outcomes."""
import pytest

import hermes_state_outcome as so
from hermes_cli import kanban_session_activity as ksa
from tests.tui_gateway import test_session_outcome_notices as helpers
from tui_gateway import notice_fanout as nf

db = helpers.db
CHAT = helpers.CHAT


@pytest.fixture
def remote(monkeypatch):
    rows = []
    def read(ids, statuses, stale_ok):
        # Display lists stay stale-friendly; empty outcomes also confirm strict counts.
        assert isinstance(stale_ok, bool)
        assert statuses == ksa.LISTED_STATUSES
        return list(rows)
    monkeypatch.setattr(ksa, "_remote_cards", read)
    return rows


def card(chat=CHAT, status="running"):
    return {"key": "worker/default/t_remote", "id": "t_remote", "board": "default",
            "gateway": "worker", "session": chat, "status": status, "title": "remote work"}


def run(db):
    return nf.card_change_pass(session_db=helpers._opener(db), slugs=[])


def test_remote_appears_changes_and_finishes(db, remote):
    assert so.refresh_outcome(CHAT, "manual", db=db)["kind"] == "done"
    assert run(db) == []
    remote.append(card())
    assert len(run(db)) == 1
    assert so.get_outcome(CHAT, db=db)["kind"] == "waiting"
    assert run(db) == []
    remote[0]["status"] = "scheduled"
    assert len(run(db)) == 1
    assert so.get_outcome(CHAT, db=db)["on"][0]["state"] == "scheduled"
    remote.clear()
    assert len(run(db)) == 1
    assert so.get_outcome(CHAT, db=db)["kind"] == "done"


def test_first_pass_seeds_only(db, remote, monkeypatch, caplog):
    remote.append(card())
    monkeypatch.setattr(so, "refresh_outcomes_quietly", lambda *a, **k: pytest.fail("unexpected refresh"))
    assert run(db) == []
    assert run(db) == []
    assert "remote card-change outcome pass failed" not in caplog.text


def test_failed_read_preserves_snapshot(db, remote, monkeypatch):
    remote.append(card())
    run(db)
    with monkeypatch.context() as patch:
        def fail(*a, **k):
            raise OSError("replica unavailable")
        patch.setattr(ksa, "_remote_cards", fail)
        patch.setattr(so, "refresh_outcomes_quietly", lambda *a, **k: pytest.fail("failed read refreshed"))
        assert run(db) == []
    remote.clear()
    assert len(run(db)) == 1


@pytest.mark.parametrize("how", ["archived", "moved"])
def test_skipped_chat_not_refreshed(db, remote, monkeypatch, how):
    run(db)
    if how == "archived":
        db.set_session_archived(CHAT, True)
    else:
        db.end_session(CHAT, "moved_to_gateway:elsewhere")
    monkeypatch.setattr(so, "refresh_outcomes_quietly", lambda *a, **k: pytest.fail("unexpected refresh"))
    remote.append(card())
    assert [r["skipped"] for r in run(db)] == [how]


def test_missing_chat_and_unlisted_cards(db, remote):
    run(db)
    remote.extend([card("absent"), card(status="done"), card(status="unknown"),
                   {**card(status="done"), "unfinished": True}])
    assert run(db) == []


def test_unknown_unfinished_and_compression_tip(db, remote, monkeypatch):
    run(db)
    remote.append({**card(status="unknown"), "unfinished": True})
    seen = []
    monkeypatch.setattr(so, "refresh_outcomes_quietly", lambda ids, trigger, **kw: (seen.append((ids, trigger, kw)) or 1))
    monkeypatch.setattr(db, "get_compression_tip", lambda sid: "compressed-tip")
    assert len(run(db)) == 1
    assert seen == [(["compressed-tip"], "card_change", {"db": db})]


def test_done_pending_release_stays_observed_until_released(db, remote, monkeypatch):
    """[daveey] A done card whose link the owner has not released yet is still the chat's obligation: its move from
    running is a change, and the release (the card leaving the projection) is another, so the outcome refreshes then."""
    remote.append(card())
    run(db)
    seen = []
    monkeypatch.setattr(so, "refresh_outcomes_quietly", lambda ids, trigger, **kw: (seen.append(trigger) or 1))
    remote[0] = {**card(status="done_pending_release"), "unfinished": True}
    assert len(run(db)) == 1
    assert run(db) == []  # unchanged while pending
    remote.clear()
    assert len(run(db)) == 1 and seen == ["card_change", "card_change"]


@pytest.mark.parametrize("failure", ["read", "write", "missing"])
def test_failed_chat_retries_unchanged_projection(db, remote, monkeypatch, failure):
    so.refresh_outcome(CHAT, "manual", db=db)
    run(db)
    remote.append(card())
    with monkeypatch.context() as patch:
        if failure == "read":
            def fail(*a):
                raise OSError("temporary session read")
            patch.setattr(nf, "chat_skip_reason", fail)
        elif failure == "write":
            patch.setattr(so, "refresh_outcomes_quietly", lambda *a, **k: 0)
        else:
            patch.setattr(db, "get_session", lambda *a: None)
        assert run(db) == []
    assert so.get_outcome(CHAT, db=db)["kind"] == "done"
    assert len(run(db)) == 1
    assert so.get_outcome(CHAT, db=db)["kind"] == "waiting"
    assert run(db) == []
    remote.clear()
    with monkeypatch.context() as patch:
        patch.setattr(so, "refresh_outcomes_quietly", lambda *a, **k: 0)
        assert run(db) == []
    assert len(run(db)) == 1
    assert so.get_outcome(CHAT, db=db)["kind"] == "done"
    assert run(db) == []


def test_failed_first_chat_does_not_starve_later_chat(db, remote, monkeypatch):
    run(db)
    remote.extend([card("000-failing"), card()])
    original = nf.chat_skip_reason
    def skip(db, chat):
        if chat == "000-failing":
            raise OSError("temporary session read")
        return original(db, chat)
    from contextlib import contextmanager
    @contextmanager
    def opener(chat):
        yield db, chat
    monkeypatch.setattr(nf, "chat_skip_reason", skip)
    rows = nf.card_change_pass(session_db=opener, slugs=[])
    assert [r["chat_id"] for r in rows] == [CHAT]
    assert so.get_outcome(CHAT, db=db)["kind"] == "waiting"


def test_origin_alias_and_duplicate_rows_refresh_once(db, remote):
    run(db)
    row = {**card("absent"), "origin_session": CHAT}
    remote.extend([row, dict(row)])
    assert len(run(db)) == 1
    assert so.get_outcome(CHAT, db=db)["kind"] == "waiting"


# --- the pass is skipped while its inputs are unchanged (t_01a1dfa94f2251cd) ------------------------------------------

@pytest.fixture
def gated(db, remote, monkeypatch):
    """A controllable input key; ``reads`` counts the passes that actually read the projection."""
    ledger, key, reads = [], [("gw", "default", ())], []
    monkeypatch.setattr(nf, "_remote_inputs_key", lambda: None if key[0] is None else (key[0], list(ledger)))
    real = remote

    def read(ids, statuses, stale_ok):
        reads.append(1)
        return list(real)
    monkeypatch.setattr(ksa, "_remote_cards", read)
    return ledger, key, reads


def test_unchanged_inputs_skip_the_remote_read(db, remote, gated):
    _ledger, _key, reads = gated
    run(db)  # seeds
    run(db)
    n = len(reads)
    for _ in range(3):
        assert run(db) == []
    assert len(reads) == n  # three more passes, no read


def test_a_changed_input_runs_the_pass_and_a_card_change_is_fanned_out(db, remote, gated):
    _ledger, key, _reads = gated
    assert so.refresh_outcome(CHAT, "manual", db=db)["kind"] == "done"
    run(db)
    run(db)
    remote.append(card())
    key[0] = ("gw", "default", ("revision", 2))
    assert len(run(db)) == 1
    assert so.get_outcome(CHAT, db=db)["kind"] == "waiting"


def test_a_new_ledger_projection_runs_the_pass(db, remote, gated):
    ledger, _key, reads = gated
    run(db)
    run(db)
    n = len(reads)
    ledger.append({"key": "x"})
    run(db)
    assert len(reads) > n


def test_unvouched_inputs_never_skip(db, remote, gated):
    _ledger, key, reads = gated
    key[0] = None
    run(db)
    n = len(reads)
    run(db)
    run(db)
    assert len(reads) > n


def test_a_failed_refresh_is_retried_not_memoised(db, remote, gated, monkeypatch):
    _ledger, key, reads = gated
    run(db)
    run(db)
    remote.append(card())
    key[0] = ("gw", "default", ("revision", 3))
    with monkeypatch.context() as patch:
        patch.setattr(so, "refresh_outcomes_quietly", lambda *a, **k: False)
        assert run(db) == []
    n = len(reads)
    assert len(run(db)) == 1  # same inputs, but the chat was never acknowledged: the pass runs again
    assert len(reads) > n


# --- the real key over the real ``_remote_links`` at the 30 s host-pass cadence (t_01a1e16c9e6427ce) ---------------

@pytest.fixture
def real_key(db, remote, monkeypatch):
    """The real ``_remote_inputs_key`` over the real ``_remote_links`` (warm ``stale_ok`` entry, refresh run inline),
    a clock that moves 30 s per pass, and a projector that builds NEW dicts of ``ledger`` content on every call."""
    import time as _time
    from types import SimpleNamespace
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as ksc

    clock, ledger, projections, reads = [1000.0], [], [], []
    monkeypatch.setattr(sync, "sync_config", lambda: {"links_reader": "replica"})
    monkeypatch.setattr(ksc, "load_identity", lambda: SimpleNamespace(gateway="gw"))
    monkeypatch.setattr(ksc, "board_paths", list)
    monkeypatch.setattr(ksa, "time", SimpleNamespace(**{**vars(_time), "monotonic": lambda: clock[0]}))
    monkeypatch.setattr(ksa, "_links_cache", {})
    monkeypatch.setattr(ksa, "_project_remote_links", lambda: projections.append(1) or [dict(c) for c in ledger])
    monkeypatch.setattr(ksa, "_refresh_links_in_background", ksa._refresh_links_scope)
    monkeypatch.setattr(ksa, "_remote_cards", lambda ids, statuses, stale_ok: reads.append(1) or list(remote))

    def tick():
        clock[0] += 30.0  # INTERVAL_S: past _LINKS_MAX_AGE_S, so every warm read republishes
        return run(db)
    return ledger, projections, reads, tick


def test_unchanged_ledger_content_skips_across_republished_projections(db, remote, real_key):
    ledger, projections, reads, tick = real_key
    ledger.append({**card(), "profile": "default", "ownership_state": "qualified"})
    tick()  # seeds
    tick()  # reads and memoises
    n, p = len(reads), len(projections)
    for _ in range(3):
        assert tick() == []
    assert len(projections) > p  # new card objects every pass ...
    assert len(reads) == n  # ... and the pass never re-read the remote cards


@pytest.mark.parametrize("change", [{"key": "worker/default/t_other"}, {"status": "blocked"},
                                    {"session": "other-chat"}, {"origin_session": "origin-chat"},
                                    {"profile": "bb-tuner"}, {"gateway": "worker-2"}, {"released": True}])
def test_a_ledger_content_change_runs_the_pass(db, remote, real_key, change):
    ledger, _projections, reads, tick = real_key
    ledger.append({**card(), "profile": "default", "ownership_state": "qualified"})
    tick()
    tick()
    tick()  # the projection republished before this pass: settled on the memo
    n = len(reads)
    assert tick() == [] and len(reads) == n
    ledger[0] = {**ledger[0], **change}
    tick()  # warm read returns the old projection and refreshes inline; the next pass sees the change
    tick()
    assert len(reads) > n
