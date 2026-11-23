"""Remote collection scheduling is independent of its returned claim list."""
from tui_gateway import kanban_remote_notices as krn
from tests.tui_gateway import test_kanban_remote_notices as helpers
import pytest


@pytest.mark.parametrize("mode", ["bounded", "unobserved", "unknown_gateway"])
def test_incomplete_capture_never_certifies_quiet(tmp_path, monkeypatch, mode):
    now = 100000.0
    card = helpers._card("t_1", "chat", status="done", at=now)
    path = helpers._hub(tmp_path, [card])
    if mode == "bounded":
        monkeypatch.setattr(krn, "MAX_PER_POLL", 0)
    else:
        if mode == "unobserved":
            card["task"]["completed_at"] = 0
        monkeypatch.setattr(krn, "_chat_index", lambda *a, **k: (
            "" if mode == "unknown_gateway" else "macbook",
            {} if mode == "unknown_gateway" else {"chat": [dict(card, gw="hub")]}, {}))
    result = krn.collect("chat", "default", hub_db=path, ledger=tmp_path / "ledger.db", now=now)
    assert not result
    assert result.incomplete and result.next_deadline is not None



@pytest.mark.parametrize("failure", ["group", "item"])
def test_presentation_failure_cannot_orphan_a_claim(tmp_path, monkeypatch, failure):
    from tui_gateway import kanban_remote_late

    now = 100000.0
    path = helpers._hub(tmp_path, [helpers._card("t_1", "chat", status="done", at=now)])
    ledger = tmp_path / "ledger.db"

    def broken(*args, **kwargs):
        raise ValueError("presentation unavailable")

    if failure == "group":
        monkeypatch.setattr(kanban_remote_late, "collect_groups", broken)
    else:
        monkeypatch.setattr(krn, "_notice_item", broken)
    result = krn.collect("chat", "default", hub_db=path, ledger=ledger, now=now)
    assert result.incomplete
    if failure == "group":
        assert len(result) == 1
        assert krn.ack(result[0], ledger=ledger)
    else:
        assert not result
        assert helpers._ledger_row(ledger, "chat", "hub/default/default/t_1") is None


def test_live_holder_is_not_quiet_until_receipt_commits(tmp_path):
    now = 100000.0
    path = helpers._hub(tmp_path, [helpers._card("t_1", "chat", status="done", at=now)])
    ledger = tmp_path / "ledger.db"
    first = krn.collect("chat", "default", hub_db=path, ledger=ledger, now=now)
    held = krn.collect("chat", "default", hub_db=path, ledger=ledger, now=now)
    assert not held
    assert held.incomplete and held.next_deadline is not None
    assert krn.ack(first[0], ledger=ledger)
    done = krn.collect("chat", "default", hub_db=path, ledger=ledger, now=now)
    assert not done
    assert done.quiet and done.next_deadline is None



def test_index_defer_is_an_obligation_even_with_no_cards(tmp_path, monkeypatch):
    path = helpers._hub(tmp_path, [])
    deferred = []

    def index(*args, on_defer, **kwargs):
        on_defer()
        return "", {}, {}

    monkeypatch.setattr(krn, "_chat_index", index)
    result = krn.collect("chat", "default", hub_db=path, ledger=tmp_path / "ledger.db",
                         on_defer=lambda: deferred.append(True))
    assert result == []
    assert result.incomplete and result.next_deadline is not None
    assert deferred == [True]


def test_later_failure_retains_committed_claim_for_caller(tmp_path, monkeypatch):
    now = 100000.0
    cards = [helpers._card(f"t_{n}", "chat", status="done", at=now) for n in (1, 2)]
    path = helpers._hub(tmp_path, cards)
    ledger = tmp_path / "ledger.db"
    original = krn._claim_notice
    calls = []

    def claim(*args, **kwargs):
        calls.append(kwargs["ref"])
        if len(calls) == 2:
            raise OSError("second claim failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(krn, "_claim_notice", claim)
    result = krn.collect("chat", "default", hub_db=path, ledger=ledger, now=now)
    assert len(result) == 1
    assert result.incomplete
    assert helpers._ledger_row(ledger, "chat", result[0]["ref"])["delivered"] == 0
    assert krn.ack(result[0], ledger=ledger)



def test_missing_replica_is_incomplete_not_quiet(tmp_path, monkeypatch):
    monkeypatch.setattr(krn.time, "monotonic", lambda: 100.0)
    result = krn.collect("chat", "default", hub_db=tmp_path / "missing.db")
    assert result == []
    assert result.incomplete
    assert not result.quiet
    assert result.next_deadline == 101.0
    assert not (tmp_path / "missing.db").exists()
