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


@pytest.mark.parametrize("provider_message", [
    "the model returned no explanation",
    "provider refusal category: cyber",
])
@pytest.mark.parametrize("clean_exit", [False, True])
@pytest.mark.parametrize("copy_kind", ["history", "standard"])
@pytest.mark.parametrize("ran_actions", [False, True])
def test_refusal_copy_tail_stops_without_generic_refused_to(
    live, monkeypatch, provider_message, clean_exit, copy_kind, ran_actions,
):
    from agent.turn_failure_copy import REFUSED_TURN_ACTIONS_WARNING, content_policy_copy
    from agent.turn_refusal_history import HISTORY_REFUSAL_COPY

    render = HISTORY_REFUSAL_COPY.format if copy_kind == "history" else content_policy_copy
    output = render(label="OpenAI", summary=provider_message)
    if ran_actions:
        output += "\n\n" + REFUSED_TURN_ACTIONS_WARNING
    output = output[-400:]
    tid, _ = _local_crash(live, monkeypatch)
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET max_retries=2 WHERE id=?", (tid,))
    monkeypatch.setattr(dispatch, "_classify_dead_worker", lambda *a, **kw:
                        dispatch._DeadWorker("clean_exit" if clean_exit else "nonzero_exit",
                                             0 if clean_exit else 1, "exit", "protocol_violation" if clean_exit else "crashed",
                                             {"worker_output": output}, protocol_violation=clean_exit))
    assert summary.classify_failure("crashed", {"worker_output": output}) == "provider_refusal"
    assert dispatch.detect_crashed_workers(live) == [tid]
    task = kb.get_task(live, tid)
    assert task.status == "triage"
    assert task.consecutive_failures == 1
    final = json.loads(live.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='failure_finalized'", (tid,),
    ).fetchone()[0])
    assert final["reason_code"] == "provider_refusal"
    notice = summary.describe_failure("failure_finalized", "fixture", final)
    assert f"'{provider_message}" in notice
    assert "Not a worker crash; no automatic retry" in notice
    assert dispatch.detect_crashed_workers(live) == []


@pytest.mark.parametrize("copy_kind,ran_actions,summary_length", [
    ("history", False, 236), ("history", True, 178),
    ("standard", False, 297), ("standard", True, 239),
])
def test_refusal_copies_preserve_prior_tail_match_boundary(copy_kind, ran_actions, summary_length):
    from agent.turn_failure_copy import REFUSED_TURN_ACTIONS_WARNING, content_policy_copy
    from agent.turn_refusal_history import HISTORY_REFUSAL_COPY

    render = HISTORY_REFUSAL_COPY.format if copy_kind == "history" else content_policy_copy
    output = render(label="OpenAI", summary="x" * summary_length)
    if ran_actions:
        output += "\n\n" + REFUSED_TURN_ACTIONS_WARNING
    assert summary.classify_failure("crashed", {"worker_output": output[-400:]}) == "provider_refusal"


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
