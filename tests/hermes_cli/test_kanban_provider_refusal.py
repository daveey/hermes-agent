"""A provider content refusal stops, rather than reroutes, the first failed run."""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_failure_summary as summary
from tests.hermes_cli.test_kanban_local_failure_receipt import _local_crash, live as live  # noqa: PLC0414 -- pytest fixture re-export


OUTPUT = json.loads((Path(__file__).parent / "fixtures/provider_refusal_ev1957427.json").read_text())["worker_output"]


@pytest.mark.parametrize("clean_exit", [False, True])
def test_first_refusal_stops_and_uses_shared_notice_classifier(live, monkeypatch, clean_exit):
    tid, rid = _local_crash(live, monkeypatch)
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET max_retries=2 WHERE id=?", (tid,))
        kb._append_event(live, tid, "run_route", {"provider": "openai-codex", "model": "gpt-6.1-sol"}, run_id=rid)
    monkeypatch.setattr(dispatch, "_classify_dead_worker", lambda *a, **kw:
                        dispatch._DeadWorker("clean_exit" if clean_exit else "nonzero_exit",
                                             0 if clean_exit else 1, "exit", "protocol_violation" if clean_exit else "crashed",
                                             {"worker_output": OUTPUT}, protocol_violation=clean_exit))
    calls = []
    classify = summary.classify_failure

    def observe(kind, payload):
        result = classify(kind, payload)
        calls.append(result)
        return result

    monkeypatch.setattr(summary, "classify_failure", observe)
    assert dispatch.detect_crashed_workers(live) == [tid]
    task = kb.get_task(live, tid)
    assert task.status == "triage"
    assert task.consecutive_failures == 1
    assert task.block_kind != "transient"
    assert task.human_request is None
    events = list(live.execute("SELECT kind,payload FROM task_events WHERE task_id=? AND kind IN ('gave_up','failure_finalized') ORDER BY id", (tid,)))
    assert [e["kind"] for e in events] == ["gave_up", "failure_finalized"]
    final = json.loads(events[-1]["payload"])
    assert final["reason_code"] == "provider_refusal"
    assert final["final_status"] == "triage"
    assert "provider_refusal" in calls
    calls.clear()
    text = summary.describe_failure("failure_finalized", "fixture", final)
    assert calls == ["provider_refusal"]
    assert f"Provider openai-codex (gpt-6.1-sol) refused the content of run {rid}" in text
    assert "This content was flagged for possible cybersecurity risk" in text
    assert "Not a worker crash; no automatic retry" in text
    assert "then promote it, or archive it" in text
    assert "exited unexpectedly" not in text
    assert "no user action identified" not in text
    assert dispatch.detect_crashed_workers(live) == []
    assert kb.get_task(live, tid).consecutive_failures == 1
    kb.recompute_ready(live)
    assert kb.get_task(live, tid).status == "triage"


@pytest.mark.parametrize("clean_exit", [False, True])
def test_primary_release_carries_refusal_stop_without_mirror_edits(live, clean_exit):
    from hermes_cli import kanban_db_sync as sync
    from tests.hermes_cli.test_kanban_failure_transport import _running
    tid, rid = _running(live)
    dead = dispatch._DeadWorker("clean_exit" if clean_exit else "nonzero_exit",
                               0 if clean_exit else 1, "exit", "protocol_violation" if clean_exit else "crashed",
                               {"worker_output": OUTPUT}, protocol_violation=clean_exit)
    dead.run_id = rid
    with kb.write_txn(live):
        dispatch._account_crashes(live, [(tid, 987654, "fixture", dead)], allow_nested=True)
    fields = json.loads(live.execute("SELECT fields FROM kanban_sync_outbox WHERE op='release'").fetchone()[0])
    assert fields["run_uid"] == sync.run_uid(live, rid)
    assert fields.get("protocol_violation", False) is False
    assert fields["force_trip"] is True
    assert fields["infrastructure"] is False
    assert fields["block_kind"] == "capability"
    assert fields["extra"]["reason_code"] == "provider_refusal"
    assert fields["extra"]["worker_output"] == OUTPUT
    assert kb.get_task(live, tid).consecutive_failures == 0  # Board alone charges it


def test_refusals_do_not_create_a_systemic_crash_fix_card(live, monkeypatch):
    from hermes_cli import kanban_systemic_crash as systemic
    tids = [_local_crash(live, monkeypatch)[0] for _ in range(3)]
    monkeypatch.setattr(dispatch, "_classify_dead_worker", lambda *a, **kw:
                        dispatch._DeadWorker("nonzero_exit", 1, OUTPUT, "crashed", {"worker_output": OUTPUT}))

    def unexpected(*args, **kwargs):
        raise AssertionError("a content refusal is not a systemic worker crash")

    monkeypatch.setattr(systemic, "ensure_fix_card", unexpected)
    assert sorted(dispatch.detect_crashed_workers(live)) == sorted(tids)
    assert all(kb.get_task(live, tid).status == "triage" for tid in tids)


def test_notice_quotes_provider_message_after_long_preamble():
    message = "This content was flagged for possible cybersecurity risk. " + "detail " * 40
    text = summary.describe_failure("crashed", "fixture", {
        "worker_output": "launcher context " * 30 + "Provider said: " + message,
        "provider": "openai-codex", "model": "gpt-6.1-sol", "run_number": 203994,
    })
    assert f"run 203994: '{message[:200]}'. Not a worker crash" in text
    assert "launcher context" not in text


@pytest.mark.parametrize("error", ["sqlite refused to open file", OUTPUT])
def test_infrastructure_failure_stays_uncharged(live, monkeypatch, error):
    tid, rid = _local_crash(live, monkeypatch)
    assert dispatch._record_task_failure(
        live, tid, error, outcome="reclaimed", infrastructure=True, run_id=rid,
    ) is False
    assert kb.get_task(live, tid).consecutive_failures == 0
    assert kb.get_task(live, tid).status != "triage"


@pytest.mark.parametrize("output", [
    "plain nonzero exit",
    "hermes-core-wt ship refused to land PR #3993: missing review",
    "ssh: the remote host refused to connect; retry later",
    "sqlite refused to open file",
])
def test_nonrefusal_keeps_retry_budget(live, monkeypatch, output):
    tid, _ = _local_crash(live, monkeypatch)
    monkeypatch.setattr(dispatch, "_classify_dead_worker", lambda *a, **kw:
                        dispatch._DeadWorker("nonzero_exit", 1, "exit", "crashed", {"worker_output": output}))
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET max_retries=2 WHERE id=?", (tid,))
    assert dispatch.detect_crashed_workers(live) == [tid]
    assert kb.get_task(live, tid).status == "ready"
    assert kb.get_task(live, tid).consecutive_failures == 1
    with kb.write_txn(live):
        rid = live.execute("INSERT INTO task_runs(task_id,profile,status,started_at) VALUES (?,'worker','running',2)", (tid,)).lastrowid
        live.execute("UPDATE tasks SET status='running',current_run_id=?,worker_pid=987654,claim_lock=? WHERE id=?", (rid, kb._host_prefix() + "fixture", tid))
    assert dispatch.detect_crashed_workers(live) == [tid]
    assert kb.get_task(live, tid).status == "triage"
    assert kb.get_task(live, tid).consecutive_failures == 2
