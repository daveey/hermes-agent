"""Independent guard regressions through journal, private-cache and read entry paths."""
import copy
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_release_intent as intent
from hermes_cli import kanban_release_read as read
from hermes_cli import kanban_release_replica as replica
from tests.hermes_cli import test_owner_release_intent as journals
from tests.hermes_cli import test_owner_release_read as reads
from tests.hermes_cli import test_owner_release_certificate as certificates

turn = journals.turn
journal = journals.journal
proof = certificates.proof
store = certificates.store


def journal_rows(conn):
    return [tuple(row) for row in conn.execute("SELECT * FROM native_owner_release_intents")]


@pytest.mark.parametrize("mode", ["shadow", "off"])
def test_existing_journal_cannot_be_read_after_primary_loss(turn, journal, mode):
    body = intent.prepare(journal, journals.action(), gate=journals.gate())
    before = journal_rows(journal)
    sync.set_mode(journal, mode)
    with pytest.raises(ValueError):
        intent.stage(journal, body)
    assert journal_rows(journal) == before


def test_stage_rechecks_custody_after_journal_read(turn, journal, monkeypatch):
    body = intent.prepare(journal, journals.action(), gate=journals.gate())
    before = journal_rows(journal)
    saved = intent._saved

    def revoked(*args, **kwargs):
        row = saved(*args, **kwargs)
        turn.release()
        return row

    monkeypatch.setattr(intent, "_saved", revoked)
    with pytest.raises(ValueError):
        intent.stage(journal, body)
    assert journal_rows(journal) == before


def test_stage_rejects_boolean_version_even_with_equal_integer_value(turn, journal):
    body = intent.prepare(journal, journals.action(), gate=journals.gate())
    with pytest.raises(ValueError):
        intent.stage(journal, {**body, "version": True})
    assert intent.stage(journal, body) == "intent_journaled_unsent"


@pytest.mark.parametrize("change", [
    {"note": "x" * 2001}, {"note": " "}, {"note": 1},
    {"capability_episodes": [f"cap-{i}" for i in range(33)]},
    {"capability_episodes": ["bad coordinate"]},
])
def test_action_bounds_fail_before_admission(turn, journal, change):
    request = journals.gate()
    with pytest.raises(ValueError):
        intent.prepare(journal, journals.action(**change), gate=request)
    assert request.finish()["outcome"] == "no_board_commit"
    assert not journal.execute("SELECT 1 FROM sqlite_master WHERE name='native_owner_release_intents'").fetchone()


def test_resume_rejects_uid_column_drift_with_coherent_body_and_digest(turn, journal):
    body = intent.prepare(journal, journals.action(), gate=journals.gate())
    journal.execute("UPDATE native_owner_release_intents SET intent_uid='different-column-uid'")
    journal.commit()
    before = journal_rows(journal)
    with pytest.raises(ValueError):
        intent.prepare(journal, journals.action(), gate=journals.gate())
    assert journal_rows(journal) == before
    assert json.loads(before[0][2]) == body


def expire(conn, body):
    conn.execute("UPDATE native_owner_release_intents SET stage='expired' WHERE intent_uid=?", (body["intent_uid"],))
    conn.commit()


@pytest.mark.parametrize("missing", [False, True])
def test_acknowledgement_never_adopts_a_different_successor(turn, journal, missing):
    first = intent.prepare(journal, journals.action(), gate=journals.gate())
    expire(journal, first)
    second = intent.prepare(journal, journals.action(), gate=journals.gate(), expired_intent=first["intent_uid"])
    if missing:
        journal.execute("DELETE FROM native_owner_release_retries")
        journal.commit()
    else:
        expire(journal, second)
        intent.prepare(journal, journals.action(), gate=journals.gate(), expired_intent=second["intent_uid"])
    before = journal_rows(journal)
    with pytest.raises(ValueError):
        intent.prepare(journal, journals.action(), gate=journals.gate(), expired_intent=first["intent_uid"])
    assert journal_rows(journal) == before


def rebind_proof(proof):
    """Keep all cross-field hashes/identities coherent around the guard under test."""
    digest = intent.digest(proof["intent"])
    proof["digest"] = proof["consume"]["digest"] = digest
    payload = json.loads(proof["renewal"]["payload"])
    payload.update(owner_release_digest=digest, actor_gateway=proof["intent"]["actor"]["gateway"],
                   request_uid=proof["consume"]["client_op_id"])
    proof["renewal"]["uid"] = proof["consume"]["client_op_id"]
    proof["renewal"]["payload"] = json.dumps(payload)


@pytest.mark.parametrize("fault", ["self-home", "same-operation", "boolean-renewal", "float-renewal"])
def test_private_certification_refuses_coherent_invalid_proofs_without_cache(proof, store, fault):
    conn, _ = store
    if fault == "self-home":
        proof["intent"]["actor"]["gateway"] = proof["intent"]["home"]
    elif fault == "same-operation":
        proof["consume"]["client_op_id"] = proof["intent"]["intent_uid"]
    else:
        proof["renewal"]["version"] = True if fault == "boolean-renewal" else 1.0
    rebind_proof(proof)
    with pytest.raises(ValueError):
        replica.certify(conn, certificates.Hub(proof), "example", task="t_target", intent_uid="intent-1")
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (replica.TABLE,)).fetchone()


@pytest.mark.parametrize("stage", ["intent_journaled_unsent", "send_pending", "delivery_pending", "consumed", "applied", "expired", "refused"])
def test_read_prepare_never_reopens_other_stages(turn, journal, stage):
    body = reads.prepared(journal)
    journal.execute("UPDATE native_owner_release_intents SET stage=?", (stage,))
    journal.commit()
    before = journal_rows(journal)
    with pytest.raises(ValueError):
        read.prepare(journal, reads.Hub(body), body, seen=None)
    assert journal_rows(journal) == before
    assert not journal.execute("SELECT 1 FROM sqlite_master WHERE name='native_owner_release_reads'").fetchone()


@pytest.mark.parametrize("stage", ["authorized_pending_home", "delivery_pending", "consumed", "applied", "expired", "refused"])
def test_display_confirmation_never_confirms_after_stage_change(turn, journal, stage):
    body = reads.prepared(journal)
    hub = reads.Hub(body)
    shown = read.prepare(journal, hub, body, seen=None)
    journal.execute("UPDATE native_owner_release_intents SET stage=?", (stage,))
    journal.commit()
    before = tuple(journal.execute("SELECT * FROM native_owner_release_reads").fetchone())
    with pytest.raises(ValueError):
        read.confirm(journal, hub, shown.confirmation)
    assert tuple(journal.execute("SELECT * FROM native_owner_release_reads").fetchone()) == before
    assert intent.stage(journal, body) == stage


def test_display_confirmation_requires_exact_parent_type_not_duck_fields(turn, journal):
    body = reads.prepared(journal)
    hub = reads.Hub(body)
    shown = read.prepare(journal, hub, body, seen=None)
    fake = SimpleNamespace(**vars(shown.confirmation))
    before = tuple(journal.execute("SELECT * FROM native_owner_release_reads").fetchone())
    with pytest.raises(ValueError):
        read.confirm(journal, hub, fake)
    assert tuple(journal.execute("SELECT * FROM native_owner_release_reads").fetchone()) == before
    assert read.confirm(journal, hub, shown.confirmation)["display_confirmed"]


def test_private_inventory_requires_exact_unnormalized_intent_bytes(turn, journal):
    body = intent.prepare(journal, journals.action(capability_episodes=["cap-a", "cap-b"]), gate=journals.gate())
    intent.advance(journal, body, expected="intent_journaled_unsent", stage="send_pending")
    intent.advance(journal, body, expected="send_pending", stage="authorized_pending_home")
    hub = reads.Hub(body)
    hub.value["intent"] = copy.deepcopy(body)
    hub.value["intent"]["capability_episodes"].reverse()
    assert intent.validate(hub.value["intent"]) == body
    with pytest.raises(ValueError):
        read.prepare(journal, hub, body, seen=None)
    assert not journal.execute("SELECT 1 FROM sqlite_master WHERE name='native_owner_release_reads'").fetchone()
