"""Known child outcomes must reach the ledger before slow teardown can be killed."""
from types import SimpleNamespace

from tests.tools import test_async_delegation_child_row_end as fixtures
from tools import async_delegation as ad
from tools.delegate_tool_child_run import _ChildRun

_clean_state, home = fixtures._clean_state, fixtures.home


def test_settle_checkpoints_result_before_cleanup(home, monkeypatch):
    from tools import delegate_tool_resumable as resumable
    fixtures._seed_sessions(home)
    fixtures._dead_owner_unit({"0": "child-1"})
    monkeypatch.setattr(resumable, "stamp_entry", lambda run, entry: None)
    run = _ChildRun(child=SimpleNamespace(_delegation_unit_id="deleg-x"), parent_agent=None,
                    task_index=0, goal="g", subagent_id="child-1", child_progress_cb=None)
    entry = {"task_index": 0, "subagent_id": "child-1", "status": "completed", "summary": "verified result"}
    assert run.settle(entry) == entry
    # No close_resources or join has run; simulate process death now.
    monkeypatch.setattr(ad, "_owner_liveness", lambda: lambda pid, started: False)
    assert ad.recover_abandoned_delegations() == 1
    with ad._DB_LOCK, ad._transaction() as conn:
        import json
        result = json.loads(conn.execute("SELECT result_json FROM async_delegations WHERE delegation_id='deleg-x'").fetchone()[0])
    assert result["results"] == [entry]
