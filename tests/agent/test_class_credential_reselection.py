"""Reselection must preserve funding, sticky credentials, and model benches."""
from types import SimpleNamespace

from agent.credential_pool import CredentialPool, PooledCredential
from agent.model_routing_runtime import reselect_class_credential
from hermes_cli.auth import write_credential_pool

MODEL = "claude-sonnet-5-5"


def _agent(monkeypatch, tmp_path, *, billing_group="subscription", benched=False):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rows = [PooledCredential(
        provider="anthropic", id=f"row{i}", label=f"account{i}", source="manual",
        auth_type="oauth", access_token=f"fixture-token-{i}", priority=i,
        model_cooldowns=({MODEL if i == 0 else "another-model": 4102444800.0}
                         if benched else {}),
    ) for i in range(2)]
    write_credential_pool("anthropic", [row.to_dict() for row in rows])
    pool = CredentialPool("anthropic", rows)
    pool._strategy = "round_robin"
    adopted = []
    agent = SimpleNamespace(model=MODEL, model_routing={"billing_group": billing_group},
                            _credential_pool=pool, _swap_credential=adopted.append)
    return agent, pool, adopted


def test_paid_route_does_not_select_or_adopt_a_subscription_row(monkeypatch, tmp_path):
    agent, pool, adopted = _agent(monkeypatch, tmp_path, billing_group="credit-funded")
    assert pool.current() is None

    reselect_class_credential(agent)

    assert pool.current() is None
    assert adopted == []
    assert [row.request_count for row in sorted(pool.entries(), key=lambda row: row.id)] == [0, 0]


def test_selected_subscription_stays_sticky_even_with_round_robin(monkeypatch, tmp_path):
    agent, pool, adopted = _agent(monkeypatch, tmp_path)
    selected = pool.select(model=MODEL)
    assert selected.id == "row0"

    reselect_class_credential(agent)

    assert pool.current().id == selected.id
    assert adopted == []
    assert [row.request_count for row in sorted(pool.entries(), key=lambda row: row.id)] == [1, 0]


def test_unselected_subscription_avoids_row_benched_for_requested_model(monkeypatch, tmp_path):
    agent, pool, adopted = _agent(monkeypatch, tmp_path, benched=True)
    assert pool.current() is None

    reselect_class_credential(agent)

    current = pool.current()
    assert current is not None, "a bench for another model must not block this retry"
    assert current.id == "row1"
    assert [row.id for row in adopted] == ["row1"]
    assert [row.request_count for row in sorted(pool.entries(), key=lambda row: row.id)] == [0, 1]
    # The higher-priority row is not globally exhausted: other models can use it.
    assert pool.select(model="another-model").id == "row0"
