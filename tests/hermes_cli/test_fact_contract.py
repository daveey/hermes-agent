"""Identity-bearing reviewed schemas remain immutable. [daveey]"""

import pytest

from hermes_cli.fact_contract import Fact, FactKey, Scope, SourceId


def test_task_relationships_and_source_vectors_accept_immutable_identities():
    scope = Scope("install", "profile", "board")
    key = FactKey(scope, "TaskFact", "local:t1")
    parent = FactKey(scope, "TaskFact", "remote:t2")
    source = SourceId(scope, "tasks", "local")
    fact = Fact(key, {"parents": [parent]}, source_vector=[{"source": source, "cursor": "v1"}])
    assert fact.value["parents"] == (parent,)
    assert fact.source_vector[0]["source"] == source
    with pytest.raises(TypeError):
        fact.source_vector[0]["cursor"] = "v2"


def test_identity_fields_cannot_smuggle_mutability():
    with pytest.raises(TypeError):
        Scope([], "profile")
    scope = Scope("install", "profile")
    with pytest.raises(TypeError):
        FactKey(scope, "TaskFact", [])
    with pytest.raises(TypeError):
        SourceId(scope, [], "local")
