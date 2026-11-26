"""Exact invocation lookup is historical identity, never current release authority."""
import pytest

from hermes_cli import kanban_release_intent as intent
from tests.hermes_cli import test_owner_release_intent as fixtures

turn = fixtures.turn
journal = fixtures.journal
action = fixtures.action
gate = fixtures.gate


def invocation(**changes):
    return {k: v for k, v in action(**changes).items() if k != "home"}


def test_existing_never_prepares_or_writes(turn, journal):
    assert intent.existing(journal, invocation()) is None
    assert not journal.execute("SELECT 1 FROM sqlite_master WHERE name='native_owner_release_intents'").fetchone()
    body = intent.prepare(journal, action(), gate=gate())
    before = journal.total_changes
    assert intent.existing(journal, invocation()) == body
    assert journal.total_changes == before
    assert intent.existing(journal, invocation(runs=7)) is None


@pytest.mark.parametrize("mode", ["key", "digest", "bytes", "stage", "actor"])
def test_existing_refuses_corrupted_exact_identity(turn, journal, mode):
    body = intent.prepare(journal, action(), gate=gate())
    if mode == "key":
        journal.execute("UPDATE native_owner_release_intents SET action_key=?", ("f" * 64,))
    if mode == "digest":
        journal.execute("UPDATE native_owner_release_intents SET digest='changed'")
    if mode == "bytes":
        journal.execute("UPDATE native_owner_release_intents SET body=?", (intent.encode(body) + " ",))
    if mode == "stage":
        journal.execute("UPDATE native_owner_release_intents SET stage='created'")
    if mode == "actor":
        changed = {**body, "actor": {**body["actor"], "session": "other-chat"}}
        journal.execute("UPDATE native_owner_release_intents SET body=?,digest=?", (intent.encode(changed), intent.digest(changed)))
    journal.commit()
    before = journal.total_changes
    with pytest.raises(ValueError):
        intent.existing(journal, invocation())
    assert journal.total_changes == before


def test_existing_refuses_two_frozen_homes_instead_of_picking_latest(turn, journal):
    intent.prepare(journal, action(), gate=gate())
    intent.prepare(journal, action(home="other-home"), gate=gate())
    with pytest.raises(ValueError, match="ambiguous"):
        intent.existing(journal, invocation())


def test_existing_reauthenticates_revoked_native_custody(turn, journal):
    intent.prepare(journal, action(), gate=gate())
    turn.release()
    with pytest.raises(ValueError, match="custody"):
        intent.existing(journal, invocation())


def test_existing_reports_unrelated_malformed_journal_without_raw_sql_error(turn, journal):
    intent.prepare(journal, action(), gate=gate())
    journal.execute("UPDATE native_owner_release_intents SET body='not-json'")
    journal.commit()
    with pytest.raises(ValueError, match="unavailable or corrupt"):
        intent.existing(journal, invocation())


def test_existing_ignores_archived_predecessor_and_ack_binds_successor(turn, journal):
    old = intent.prepare(journal, action(), gate=gate())
    intent.advance(journal, old, expected="intent_journaled_unsent", stage="send_pending")
    intent.advance(journal, old, expected="send_pending", stage="expired")
    assert not intent.acknowledged_successor(journal, old, old["intent_uid"])
    successor = intent.prepare(journal, action(), gate=gate(), expired_intent=old["intent_uid"])
    assert intent.existing(journal, invocation()) == successor
    assert intent.acknowledged_successor(journal, successor, old["intent_uid"])
    with pytest.raises(ValueError):
        intent.acknowledged_successor(journal, successor, "foreign-expired")
