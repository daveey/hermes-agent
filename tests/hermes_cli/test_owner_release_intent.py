"""Native release journals bind uncertain network retries to one live originating chat."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_release_intent as intent, kanban_db_connect as kbc
from hermes_cli.kanban_origin_cli_gate import LaunchGate
from tests.hermes_cli import test_origin_native_admission as fixtures

turn = fixtures.turn


@pytest.fixture
def journal(turn):
    with kbc.connect_closing() as conn:
        yield conn


def action(**changes):
    return {"board": "default", "task_id": "t_target", "home": "elsewhere", "episode": "episode-now",
            "capability_episodes": ["cap-old"], "runs": 1, "note": "continue exact scope", **changes}


def gate():
    return LaunchGate().dispatch(operation="release")


def test_native_journal_freezes_actor_and_ids_before_first_network(turn, journal):
    saved = intent.prepare(journal, action(), gate=gate())
    row = journal.execute("SELECT * FROM native_owner_release_intents").fetchone()
    assert json.loads(row["body"]) == saved
    assert saved["actor"] == {"gateway": "here", "profile": "default", "session": turn.agent.session_id}
    assert saved["version"] == 1 and saved["intent_uid"]
    assert row["digest"] == intent.digest(saved)
    assert row["stage"] == "intent_journaled_unsent"
    assert not journal.in_transaction


def test_exact_native_resume_reuses_every_byte_and_identity(turn, journal):
    first = intent.prepare(journal, action(), gate=gate())
    before = tuple(journal.execute("SELECT * FROM native_owner_release_intents").fetchone())
    second = intent.prepare(journal, action(), gate=gate())
    assert second == first
    assert tuple(journal.execute("SELECT * FROM native_owner_release_intents").fetchone()) == before
    assert journal.execute("SELECT COUNT(*) FROM native_owner_release_intents").fetchone()[0] == 1


def test_retirement_before_admission_writes_no_journal(turn, journal):
    launch = LaunchGate()
    request = launch.dispatch(operation="release")
    launch.retire()
    with pytest.raises(ValueError, match="retired"):
        intent.prepare(journal, action(), gate=request)
    assert journal.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='native_owner_release_intents'").fetchone()[0] == 0


def test_revoked_native_turn_cannot_read_or_resend_journal(turn, journal):
    intent.prepare(journal, action(), gate=gate())
    before = tuple(journal.execute("SELECT * FROM native_owner_release_intents").fetchone())
    turn.release()
    with pytest.raises(ValueError, match="native"):
        intent.prepare(journal, action(), gate=gate())
    assert tuple(journal.execute("SELECT * FROM native_owner_release_intents").fetchone()) == before


@pytest.mark.parametrize("changes", [{"actor": {"session": "forged"}}, {"writer": "human"},
    {"intent_uid": "chosen"}, {"session": "forged"}, {"seen": 123}, {"runs": True}, {"runs": 0},
    {"episode": ""}, {"capability_episodes": ["cap-old", "cap-old"]},
    {"capability_episodes": ["episode-now"]}, {"capability_episodes": "cap-old"}, {"note": ""},
])
def test_child_identity_and_malformed_actions_never_enter_journal(turn, journal, changes):
    with pytest.raises(ValueError):
        intent.prepare(journal, action(**changes), gate=gate())
    assert journal.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='native_owner_release_intents'").fetchone()[0] == 0


def test_no_native_custody_even_with_forged_environment(turn, journal, monkeypatch):
    turn.release()
    monkeypatch.setenv("HERMES_SESSION_ID", "owning-chat")
    monkeypatch.setenv("HERMES_ORIGIN_GATEWAY", "local")
    with pytest.raises(ValueError, match="native"):
        intent.prepare(journal, action(), gate=gate())
    assert journal.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='native_owner_release_intents'").fetchone()[0] == 0


def test_new_episode_does_not_reuse_old_identity(turn, journal):
    old = intent.prepare(journal, action(), gate=gate())
    new = intent.prepare(journal, action(episode="episode-next"), gate=gate())
    assert old["intent_uid"] != new["intent_uid"]
    assert old["episode"] == "episode-now" and new["episode"] == "episode-next"


def test_stale_saved_actor_cannot_be_replayed_by_current_native_chat(turn, journal):
    intent.prepare(journal, action(), gate=gate())
    row = journal.execute("SELECT * FROM native_owner_release_intents").fetchone()
    body = json.loads(row["body"])
    body["actor"]["session"] = "different-chat"
    journal.execute("UPDATE native_owner_release_intents SET body=?, digest=?", (intent.encode(body), intent.digest(body)))
    journal.commit()
    with pytest.raises(ValueError, match="actor"):
        intent.prepare(journal, action(), gate=gate())


def test_corrupt_journal_is_not_repaired_by_reissuing_authority(turn, journal):
    intent.prepare(journal, action(), gate=gate())
    journal.execute("UPDATE native_owner_release_intents SET digest='wrong'")
    journal.commit()
    with pytest.raises(ValueError, match="journal"):
        intent.prepare(journal, action(), gate=gate())
    assert journal.execute("SELECT COUNT(*) FROM native_owner_release_intents").fetchone()[0] == 1


def test_journal_survives_connection_loss_without_reissuing_identity(turn):
    with kbc.connect_closing() as first:
        saved = intent.prepare(first, action(), gate=gate())
    with kbc.connect_closing() as recovered:
        assert intent.prepare(recovered, action(), gate=gate()) == saved
        assert recovered.execute("SELECT COUNT(*) FROM native_owner_release_intents").fetchone()[0] == 1


def test_delegated_child_cannot_resume_native_parent_intent(turn, journal):
    from agent.delegation_context import delegated_child_context
    intent.prepare(journal, action(), gate=gate())
    before = tuple(journal.execute("SELECT * FROM native_owner_release_intents").fetchone())
    with delegated_child_context(), pytest.raises(ValueError, match="native"):
        intent.prepare(journal, action(), gate=gate())
    assert tuple(journal.execute("SELECT * FROM native_owner_release_intents").fetchone()) == before


@pytest.mark.parametrize("when", ["before_admit", "before_commit"])
def test_turn_revocation_during_journal_rolls_back_all_bytes(turn, journal, monkeypatch, when):
    from agent.origin_turn_custody import CapturedOrigin
    validated = CapturedOrigin.validated
    calls = []
    def revoke(captured):
        calls.append(None)
        if len(calls) == (1 if when == "before_admit" else 2):
            turn.release()
        return validated(captured)
    monkeypatch.setattr(CapturedOrigin, "validated", revoke)
    request = gate()
    with pytest.raises(ValueError, match="native"):
        intent.prepare(journal, action(), gate=request)
    assert not journal.in_transaction
    assert journal.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='native_owner_release_intents'").fetchone()[0] == 0
    assert request.finish()["outcome"] == ("no_board_commit" if when == "before_admit" else "outcome_unknown")


def test_creation_gate_cannot_admit_release_journal(turn, journal):
    request = LaunchGate().dispatch()
    with pytest.raises(ValueError, match="creation"):
        intent.prepare(journal, action(), gate=request)
    assert request.finish()["outcome"] == "no_board_commit"
    assert journal.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='native_owner_release_intents'").fetchone()[0] == 0


@pytest.mark.parametrize("wrong_store", ["nonprimary", "other_board"])
def test_journal_requires_exact_primary_board(turn, journal, wrong_store):
    if wrong_store == "nonprimary":
        with sqlite3.connect(":memory:") as conn, pytest.raises(ValueError, match="primary"):
            intent.prepare(conn, action(), gate=gate())
        return
    with pytest.raises(ValueError, match="primary"):
        intent.prepare(journal, action(board="other"), gate=gate())
    assert journal.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='native_owner_release_intents'").fetchone()[0] == 0


def test_late_revocation_can_leave_only_an_inert_journal(turn, journal, monkeypatch):
    from contextlib import contextmanager
    from hermes_cli import kanban_db as kb
    write = kb.write_txn
    @contextmanager
    def revoke_before_commit(*args, **kwargs):
        with write(*args, **kwargs):
            yield
            turn.release()
    monkeypatch.setattr(kb, "write_txn", revoke_before_commit)
    saved = intent.prepare(journal, action(), gate=gate())
    assert journal.execute("SELECT intent_uid FROM native_owner_release_intents").fetchone()[0] == saved["intent_uid"]
    with pytest.raises(ValueError, match="native"):
        intent.prepare(journal, action(), gate=gate())
