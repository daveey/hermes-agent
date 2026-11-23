"""Explicit host wire and admission contracts."""
from dataclasses import make_dataclass

import pytest

from hermes_cli.fact_contract import Fact, FactKey, Position, Scope, SourceId
from hermes_cli.fact_store import FactStore
from hermes_cli.fact_wire import decode, encode

SCOPE = Scope("install", "alpha")
KEY = FactKey(SCOPE, "TaskFact", "one")


@pytest.mark.parametrize("deadline", [True, "soon", float("inf"), float("nan")])
def test_bad_deadline_is_rejected_before_publication(deadline):
    store = FactStore()
    with pytest.raises(ValueError, match="invalid_validity_deadline"):
        store.publish([Fact(KEY, {}, health="fresh", valid_until=deadline)])
    assert store.snapshot(SCOPE).rev == 0


def test_wire_preserves_contract_identities_and_rejects_namesakes():
    source = SourceId(SCOPE, "tasks", "db")
    fact = Fact(KEY, {"scope": SCOPE, "relation": KEY, "source": source}, source_vector=(source,))
    assert decode(encode(fact)) == fact
    namesake = make_dataclass("Scope", [("install_id", str), ("profile_id", str)])
    with pytest.raises(TypeError):
        encode(namesake("install", "alpha"))


def test_default_ring_and_established_subscriber_entry_caps():
    store = FactStore()
    cursor = Position(store._epoch, 0)
    sub = store.subscribe(SCOPE, (), None, cursor, lambda _: None)
    # Clear the empty initial delta, then fill a real established mailbox.
    sub.drain()
    for i in range(4097):
        store.publish([Fact(KEY, {"n": i})])
    assert len(store._ring) == 4096
    assert store.changes(SCOPE, cursor).reset
    assert sub.reset_pending and not sub.queue and sub.queued_bytes == 0
    sub.close()
