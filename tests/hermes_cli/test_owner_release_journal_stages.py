"""Durable one-shot journal progression, with fresh native custody on each use."""
import pytest

from hermes_cli import kanban_release_intent as intent
from tests.hermes_cli import test_owner_release_intent as fixtures

turn = fixtures.turn
journal = fixtures.journal
action = fixtures.action
gate = fixtures.gate


def test_real_reopen_retains_stage_and_never_reissues_expired_intent(turn, journal):
    from hermes_cli import kanban_db_connect as kbc
    body = intent.prepare(journal, action(), gate=gate())
    assert intent.stage(journal, body) == "intent_journaled_unsent"
    assert intent.advance(journal, body, expected="intent_journaled_unsent", stage="send_pending")
    assert intent.advance(journal, body, expected="send_pending", stage="authorized_pending_home")
    assert intent.advance(journal, body, expected="authorized_pending_home", stage="expired")
    with kbc.connect_closing() as reopened:
        assert intent.prepare(reopened, action(), gate=gate()) == body
        assert intent.stage(reopened, body) == "expired"
        with pytest.raises(ValueError, match="transition"):
            intent.advance(reopened, body, expected="expired", stage="send_pending")
        assert reopened.execute("SELECT COUNT(*) FROM native_owner_release_intents").fetchone()[0] == 1


def test_explicit_expiry_ack_reissues_same_action_once_and_preserves_history(turn, journal):
    body = intent.prepare(journal, action(), gate=gate())
    intent.advance(journal, body, expected="intent_journaled_unsent", stage="send_pending")
    intent.advance(journal, body, expected="send_pending", stage="expired")
    fresh = intent.prepare(journal, action(), gate=gate(), expired_intent=body["intent_uid"])
    assert fresh["intent_uid"] != body["intent_uid"]
    assert {k: v for k, v in fresh.items() if k != "intent_uid"} == {k: v for k, v in body.items() if k != "intent_uid"}
    assert intent.stage(journal, body) == "expired"
    assert intent.stage(journal, fresh) == "intent_journaled_unsent"
    assert intent.prepare(journal, action(), gate=gate(), expired_intent=body["intent_uid"]) == fresh
    intent.advance(journal, fresh, expected="intent_journaled_unsent", stage="send_pending")
    intent.advance(journal, fresh, expected="send_pending", stage="expired")
    assert intent.prepare(journal, action(), gate=gate(), expired_intent=body["intent_uid"]) == fresh
    assert intent.stage(journal, fresh) == "expired"
    assert journal.execute("SELECT COUNT(*) FROM native_owner_release_intents").fetchone()[0] == 2


@pytest.mark.parametrize("ack", ["same", "foreign"])
def test_unknown_send_cannot_be_replaced_even_with_expiry_ack(turn, journal, ack):
    body = intent.prepare(journal, action(), gate=gate())
    intent.advance(journal, body, expected="intent_journaled_unsent", stage="send_pending")
    before = tuple(journal.execute("SELECT * FROM native_owner_release_intents").fetchone())
    with pytest.raises(ValueError, match="expired"):
        intent.prepare(journal, action(), gate=gate(), expired_intent=body["intent_uid"] if ack == "same" else "other")
    assert tuple(journal.execute("SELECT * FROM native_owner_release_intents").fetchone()) == before


@pytest.mark.parametrize("stage", ["refused", "authorized_pending_home", "consumed"])
def test_expiry_ack_never_replaces_nonexpired_state(turn, journal, stage):
    body = intent.prepare(journal, action(), gate=gate())
    intent.advance(journal, body, expected="intent_journaled_unsent", stage="send_pending")
    if stage == "refused":
        intent.advance(journal, body, expected="send_pending", stage=stage)
    else:
        intent.advance(journal, body, expected="send_pending", stage="authorized_pending_home")
        if stage == "consumed":
            intent.advance(journal, body, expected="authorized_pending_home", stage=stage)
    before = tuple(journal.execute("SELECT * FROM native_owner_release_intents").fetchone())
    with pytest.raises(ValueError, match="expired"):
        intent.prepare(journal, action(), gate=gate(), expired_intent=body["intent_uid"])
    assert tuple(journal.execute("SELECT * FROM native_owner_release_intents").fetchone()) == before


def test_expiry_ack_cannot_select_another_action_or_actor(turn, journal):
    body = intent.prepare(journal, action(), gate=gate())
    intent.advance(journal, body, expected="intent_journaled_unsent", stage="send_pending")
    intent.advance(journal, body, expected="send_pending", stage="expired")
    with pytest.raises(ValueError, match="expired"):
        intent.prepare(journal, action(note="different action"), gate=gate(), expired_intent=body["intent_uid"])
    changed = {**body, "actor": {**body["actor"], "session": "other-chat"}}
    journal.execute("UPDATE native_owner_release_intents SET body=?, digest=?", (intent.encode(changed), intent.digest(changed)))
    journal.commit()
    with pytest.raises(ValueError, match="actor"):
        intent.prepare(journal, action(), gate=gate(), expired_intent=body["intent_uid"])
    assert journal.execute("SELECT COUNT(*) FROM native_owner_release_intents").fetchone()[0] == 1


def test_progress_is_compare_and_swap_not_backward_or_skip(turn, journal):
    body = intent.prepare(journal, action(), gate=gate())
    assert intent.advance(journal, body, expected="intent_journaled_unsent", stage="send_pending")
    assert not intent.advance(journal, body, expected="intent_journaled_unsent", stage="send_pending")
    assert intent.stage(journal, body) == "send_pending"
    with pytest.raises(ValueError, match="transition"):
        intent.advance(journal, body, expected="send_pending", stage="applied")
    assert intent.advance(journal, body, expected="send_pending", stage="authorized_pending_home")
    assert intent.advance(journal, body, expected="authorized_pending_home", stage="read_first_pending")
    assert intent.advance(journal, body, expected="read_first_pending", stage="consumed")
    assert intent.advance(journal, body, expected="consumed", stage="applied")
    assert intent.prepare(journal, action(), gate=gate()) == body
    assert intent.stage(journal, body) == "applied"


@pytest.mark.parametrize("stage", ["unknown", "", "created"])
def test_invalid_saved_stage_refuses_prepare_and_read(turn, journal, stage):
    body = intent.prepare(journal, action(), gate=gate())
    journal.execute("UPDATE native_owner_release_intents SET stage=?", (stage,))
    journal.commit()
    with pytest.raises(ValueError, match="stage"):
        intent.prepare(journal, action(), gate=gate())
    with pytest.raises(ValueError, match="stage"):
        intent.stage(journal, body)


@pytest.mark.parametrize("operation", ["read", "advance"])
def test_revocation_prevents_all_progress_and_read(turn, journal, operation):
    body = intent.prepare(journal, action(), gate=gate())
    before = tuple(journal.execute("SELECT * FROM native_owner_release_intents").fetchone())
    turn.release()
    with pytest.raises(ValueError, match="native"):
        if operation == "read":
            intent.stage(journal, body)
        else:
            intent.advance(journal, body, expected="intent_journaled_unsent", stage="send_pending")
    assert tuple(journal.execute("SELECT * FROM native_owner_release_intents").fetchone()) == before


def test_mismatched_body_cannot_advance_another_journal(turn, journal):
    body = intent.prepare(journal, action(), gate=gate())
    with pytest.raises(ValueError, match="journal"):
        intent.advance(journal, {**body, "note": "changed"}, expected="intent_journaled_unsent", stage="send_pending")
    assert intent.stage(journal, body) == "intent_journaled_unsent"


def test_corrupt_digest_cannot_be_hidden_by_stage_transition(turn, journal):
    body = intent.prepare(journal, action(), gate=gate())
    journal.execute("UPDATE native_owner_release_intents SET digest='wrong'")
    journal.commit()
    with pytest.raises(ValueError, match="journal"):
        intent.advance(journal, body, expected="intent_journaled_unsent", stage="send_pending")


def test_failed_stage_commit_rolls_back(turn, journal):
    import sqlite3
    body = intent.prepare(journal, action(), gate=gate())
    journal.execute("CREATE TRIGGER reject_progress BEFORE UPDATE ON native_owner_release_intents BEGIN SELECT RAISE(ABORT, 'reject'); END")
    journal.commit()
    with pytest.raises(sqlite3.IntegrityError, match="reject"):
        intent.advance(journal, body, expected="intent_journaled_unsent", stage="send_pending")
    assert not journal.in_transaction
    assert intent.stage(journal, body) == "intent_journaled_unsent"
