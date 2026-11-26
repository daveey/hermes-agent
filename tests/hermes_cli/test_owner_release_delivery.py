"""Frozen native carrier delivery; publication is not HOME application."""
import copy
import json

import pytest

from hermes_cli import kanban_release_intent as intent
from tests.hermes_cli import test_owner_release_read as fixtures

turn = fixtures.turn
journal = fixtures.journal


def acknowledged(journal):
    from hermes_cli import kanban_release_read as read
    body = fixtures.prepared(journal)
    hub = fixtures.Hub(body)
    first = read.prepare(journal, hub, body, seen=None)
    read.confirm(journal, hub, first.confirmation)
    nonce = first["release_read"]["nonce"]
    read.prepare(journal, hub, body, seen=nonce)
    return body, hub, nonce


class CarrierHub:
    def __init__(self, conn, body):
        self.conn, self.body = conn, body
        from types import SimpleNamespace
        self.ident = SimpleNamespace(gateway=body['actor']['gateway'])
        self.sent, self.row = [], None
        self.state = "never_existed"
        self.drop = False

    def owner_release_application(self, board, uid):
        from hermes_cli.kanban_sync_client import HubError
        raise HubError(404, 'fixture not applied')

    def row_evidence(self, board, table, uid, **kw):
        assert not self.conn.in_transaction
        return {"board": board, "table": table, "uid": uid, "state": self.state,
                "row": copy.deepcopy(self.row), "tombstone": None, "floor": 0}

    def ops(self, board, ops):
        assert not self.conn.in_transaction
        saved = self.conn.execute("SELECT wire,stage FROM native_owner_release_deliveries").fetchone()
        assert json.loads(saved[0]) == ops[0] and saved[1] == "send_pending"
        self.sent.append(copy.deepcopy(ops[0]))
        wire = ops[0]
        self.row = {**wire["fields"], "uid": wire["uid"], "origin_gateway": self.body["actor"]["gateway"],
                    "deleted_at": None, "hlc": wire["hlc"], "seq": 1, "version": 1}
        self.state = "live"
        if self.drop:
            raise OSError("reply lost after commit")
        return {"results": [{"status": "applied"}]}


def test_frozen_delivery_reopen_reconciles_lost_reply_before_current_comments(turn, journal):
    from hermes_cli import kanban_db_connect as kbc, kanban_release_delivery as delivery
    body, _, nonce = acknowledged(journal)
    frozen = delivery.prepare(journal, body, nonce=nonce)
    assert frozen["uid"] != body["intent_uid"] != frozen["client_op_id"]
    hub = CarrierHub(journal, body)
    hub.drop = True
    with pytest.raises(OSError, match="reply lost"):
        delivery.send(journal, hub, body)
    before = tuple(journal.execute("SELECT * FROM native_owner_release_deliveries").fetchone())
    with kbc.connect_closing() as reopened:
        hub.conn = reopened
        result = delivery.send(reopened, hub, body)
        assert result["release_stage"] == "acknowledged_pending_home"
        assert result["publication_stage"] == "published"
        assert result["delivery_uid"] == frozen["uid"]
    assert len(hub.sent) == 1
    assert intent.stage(journal, body) == "delivery_pending"
    assert tuple(journal.execute("SELECT * FROM native_owner_release_deliveries").fetchone()) != before
    assert delivery.prepare(journal, body, nonce=nonce) == frozen


@pytest.mark.parametrize("failure", ["unconfirmed", "orphan_ack", "not_acknowledged", "nonce", "rows", "revoked"])
def test_only_exact_acknowledged_read_can_freeze_delivery(turn, journal, failure):
    from hermes_cli import kanban_release_read as read, kanban_release_delivery as delivery
    body = fixtures.prepared(journal)
    hub = fixtures.Hub(body)
    first = read.prepare(journal, hub, body, seen=None)
    nonce = first["release_read"]["nonce"]
    if failure != "unconfirmed":
        read.confirm(journal, hub, first.confirmation)
    if failure not in {"unconfirmed", "not_acknowledged"}:
        read.prepare(journal, hub, body, seen=nonce)
    if failure == "orphan_ack":
        journal.execute("UPDATE native_owner_release_reads SET displayed_invocation=NULL")
        journal.commit()
    if failure == "nonce":
        nonce = "0" * 64
    if failure == "rows":
        journal.execute("UPDATE native_owner_release_reads SET rows_bytes='{}'")
        journal.commit()
    if failure == "revoked":
        turn.release()
    with pytest.raises(ValueError):
        delivery.prepare(journal, body, nonce=nonce)
    assert not journal.execute("SELECT 1 FROM sqlite_master WHERE name='native_owner_release_deliveries'").fetchone()


@pytest.mark.parametrize("state", ["unknown", "deleted", "other"])
def test_uncertain_or_deleted_evidence_never_resends_or_mints(turn, journal, state):
    from hermes_cli import kanban_release_delivery as delivery
    body, _, nonce = acknowledged(journal)
    frozen = delivery.prepare(journal, body, nonce=nonce)
    hub = CarrierHub(journal, body)
    hub.state = state
    with pytest.raises(ValueError, match="publication"):
        delivery.send(journal, hub, body)
    assert hub.sent == []
    assert delivery.existing(journal, body) == frozen


@pytest.mark.parametrize("change", [{"origin_gateway": "forged"}, {"task_id": "other"}, {"run_uid": "run"},
                                    {"payload": "{}"}, {"kind": "scope_renewed"}, {"created_at": True},
                                    {"hlc": "changed"}, {"uid": "other"}, {"deleted_at": 1}])
def test_canonical_publication_requires_all_frozen_fields(turn, journal, change):
    from hermes_cli import kanban_release_delivery as delivery
    body, _, nonce = acknowledged(journal)
    frozen = delivery.prepare(journal, body, nonce=nonce)
    hub = CarrierHub(journal, body)
    delivery.send(journal, hub, body)
    hub.row.update(change)
    with pytest.raises(ValueError, match="publication"):
        delivery.send(journal, hub, body)
    assert len(hub.sent) == 1
    assert delivery.existing(journal, body) == frozen


def test_existing_delivery_is_immutable_despite_new_ack_journal(turn, journal):
    from hermes_cli import kanban_release_delivery as delivery
    body, _, nonce = acknowledged(journal)
    frozen = delivery.prepare(journal, body, nonce=nonce)
    journal.execute("UPDATE native_owner_release_reads SET nonce=?,rows_bytes='[]',acknowledged=0", ("a" * 64,))
    journal.commit()
    assert delivery.existing(journal, body) == frozen
    assert delivery.prepare(journal, body, nonce="a" * 64) == frozen
    assert journal.execute("SELECT count(*) FROM native_owner_release_deliveries").fetchone()[0] == 1


def test_native_send_reconciles_carrier_before_mutable_authorization(turn, journal, monkeypatch):
    from hermes_cli import kanban_release_delivery as delivery, kanban_release_native as native
    body, _, nonce = acknowledged(journal)
    delivery.prepare(journal, body, nonce=nonce)
    hub = CarrierHub(journal, body)
    def unavailable(*a, **kw):
        pytest.fail("frozen carrier must reconcile before mutable authorization")
    monkeypatch.setattr(native, "_proof", unavailable)
    result = native._send(journal, hub, body)
    assert result["publication_stage"] == "published"
    assert len(hub.sent) == 1


@pytest.mark.parametrize("damage", ["stage", "missing_row", "missing_table", "duplicate", "digest", "wire"])
def test_corrupt_or_missing_delivery_cannot_mint_replacement(turn, journal, damage):
    from hermes_cli import kanban_release_delivery as delivery
    body, _, nonce = acknowledged(journal)
    delivery.prepare(journal, body, nonce=nonce)
    if damage == "duplicate":
        journal.execute("DROP INDEX idx_native_release_delivery_active")
    statements = {
        "stage": "UPDATE native_owner_release_deliveries SET stage='corrupt'",
        "missing_row": "DELETE FROM native_owner_release_deliveries",
        "missing_table": "DROP TABLE native_owner_release_deliveries",
        "duplicate": "INSERT INTO native_owner_release_deliveries SELECT 'other',intent_uid,wire,digest,stage FROM native_owner_release_deliveries",
        "digest": "UPDATE native_owner_release_deliveries SET digest='wrong'",
        "wire": "UPDATE native_owner_release_deliveries SET wire='{}'",
    }
    journal.execute(statements[damage])
    journal.commit()
    with pytest.raises(ValueError):
        delivery.existing(journal, body)
    with pytest.raises(ValueError):
        delivery.prepare(journal, body, nonce=nonce)
    assert intent.stage(journal, body) == "delivery_pending"


@pytest.mark.parametrize("stage", ["expired", "refused", "consumed", "applied"])
def test_terminal_intent_never_resends_pending_carrier(turn, journal, stage):
    from hermes_cli import kanban_release_delivery as delivery, kanban_release_native as native
    body, _, nonce = acknowledged(journal)
    delivery.prepare(journal, body, nonce=nonce)
    if stage == "applied":
        intent.advance(journal, body, expected="delivery_pending", stage="consumed")
        intent.advance(journal, body, expected="consumed", stage=stage)
    else:
        intent.advance(journal, body, expected="delivery_pending", stage=stage)
    hub = CarrierHub(journal, body)
    if stage == "applied":
        with pytest.raises(ValueError, match="result missing"):
            native._send(journal, hub, body)
        assert hub.sent == []
        return
    result = native._send(journal, hub, body)
    assert hub.sent == []
    assert result["release_stage"] == {"consumed": "acknowledged_pending_home"}.get(stage, stage)
    assert result["ok"] == (stage not in {"expired", "refused"})


def test_final_escaped_carrier_bound_refuses_before_any_journal_or_clock_commit(turn, journal):
    from hermes_cli import kanban_release_read as read, kanban_release_delivery as delivery
    body = fixtures.prepared(journal)
    hub = fixtures.Hub(body)
    hub.value["comments"] = [fixtures.comment("é" * 60000, task=body["task_id"])]
    first = read.prepare(journal, hub, body, seen=None)
    read.confirm(journal, hub, first.confirmation)
    nonce = first["release_read"]["nonce"]
    read.prepare(journal, hub, body, seen=nonce)
    before = journal.execute("SELECT hlc_last FROM kanban_sync_state").fetchone()[0]
    with pytest.raises(ValueError, match="operation bound"):
        delivery.prepare(journal, body, nonce=nonce)
    assert not journal.execute("SELECT 1 FROM sqlite_master WHERE name='native_owner_release_deliveries'").fetchone()
    assert journal.execute("SELECT hlc_last FROM kanban_sync_state").fetchone()[0] == before
    assert intent.stage(journal, body) == "read_first_pending"


def test_published_carrier_cannot_resend_after_disappearance(turn, journal):
    from hermes_cli import kanban_release_delivery as delivery
    body, _, nonce = acknowledged(journal)
    delivery.prepare(journal, body, nonce=nonce)
    hub = CarrierHub(journal, body)
    delivery.send(journal, hub, body)
    hub.row, hub.state = None, "never_existed"
    with pytest.raises(ValueError, match="disappeared"):
        delivery.send(journal, hub, body)
    assert len(hub.sent) == 1


@pytest.mark.parametrize("change", [{"board": "other"}, {"table": "tasks"}, {"uid": "other"},
                                    {"floor": 1}, {"floor": False}, {"tombstone": {}}, {"row": {}}])
def test_absence_requires_exact_uncompacted_canonical_evidence(turn, journal, monkeypatch, change):
    from hermes_cli import kanban_release_delivery as delivery
    body, _, nonce = acknowledged(journal)
    delivery.prepare(journal, body, nonce=nonce)
    hub = CarrierHub(journal, body)
    original = hub.row_evidence
    monkeypatch.setattr(hub, "row_evidence", lambda *a, **kw: {**original(*a, **kw), **change})
    with pytest.raises(ValueError, match="publication"):
        delivery.send(journal, hub, body)
    assert hub.sent == []
