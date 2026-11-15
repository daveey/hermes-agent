"""Contract input errors must name the repair, not recommend waiting for CI."""
import pytest

from hermes_cli import kanban_pr_acceptance as gate


@pytest.mark.parametrize(
    "contract,published,classification",
    [
        ("acme/repo", None, "missing"),
        ("acme/repo", "https://github.com/other/repo/pull/7", "contract_mismatch"),
        ("https://github.com/acme/repo/pull/7", "https://github.com/acme/repo/pull/8", "contract_mismatch"),
    ],
)
def test_contract_refusal_names_expected_and_supplied_without_ci_advice(monkeypatch, contract, published, classification):
    def no_api(*args, **kwargs):
        pytest.fail("invalid contract input must not query GitHub")

    monkeypatch.setattr(gate, "_api", no_api)
    receipt = gate.collect_acceptance(contract, published)
    assert receipt["ok"] is False
    assert receipt["classification"] == classification
    assert receipt["completion_contract"] == contract
    assert receipt["pr_url"] == published
    assert contract in receipt["detail"]
    assert (published or "not supplied") in receipt["detail"]
    assert "metadata.published_pr" in receipt["recovery"]
    assert "wait" not in receipt["recovery"]
    assert "kanban_block" not in receipt["recovery"]
    assert "rerun" not in receipt["recovery"]


@pytest.mark.parametrize("published", [None, "https://github.com/acme/repo/pull/7"])
def test_exact_contract_still_reaches_github_checks(monkeypatch, published):
    calls = []

    def closed_pr(endpoint, **kwargs):
        calls.append(endpoint)
        return {"data": {"repository": {"pullRequest": {
            "headRefOid": "a" * 40, "baseRefName": "main", "state": "CLOSED",
        }}}}

    monkeypatch.setattr(gate, "_api", closed_pr)
    receipt = gate.collect_acceptance("https://github.com/acme/repo/pull/7", published)
    assert receipt["ok"] is True
    assert receipt["classification"] == "closed"
    assert calls == ["graphql"]


def test_mismatch_persists_actionable_refusal_without_rebinding(tmp_path, monkeypatch):
    import json

    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    expected = "https://github.com/acme/repo/pull/7"
    supplied = "https://github.com/other/repo/pull/8"
    with connect() as conn:
        tid = kb.create_task(conn, title="Publish", completion_contract=expected)
        assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": supplied})
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.last_failure_error is not None
        assert task.status != "done"
        assert task.completion_contract == expected
        assert "contract_mismatch" in task.last_failure_error
        assert expected in task.last_failure_error
        assert supplied in task.last_failure_error
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,),
        ).fetchone()
        receipt = json.loads(row[0])
        assert receipt["completion_contract"] == expected
        assert receipt["pr_url"] == supplied
        assert receipt["ok"] is False
