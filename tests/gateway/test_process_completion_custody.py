"""A gateway-watched completion survives a gateway restart that lands between its exit and the watcher tick.

Regression for proc_0fcf936312c4 (local chat 20261027_134353_5ab65c, 11-12): the job's exit was
recorded in its result receipt, but no completion admission was ever made and no turn ran.
``_move_to_finished`` writes the receipt, drops the job from ``_running`` and from the checkpoint,
and only then can the 5 s watcher see it. A restart in that gap left nothing to re-adopt, because a
receipt counts as "observed" and only the in-memory watcher could deliver.

The receipt now carries the owed delivery (``tools/process_registry_obligations.py``). These tests
use the real local SessionAuthority, the real watcher and real admission rows. No chat is created
beyond the fixture's own local session, and no model is called.
"""
import json

import pytest

from tests.gateway.test_session_end_delegations import _admissions, _local, _notes
from tools import process_registry as pr_mod
from tools.process_registry import ProcessRegistry, ProcessSession
from tools.process_registry_obligations import OBLIGATION_KEY

STARTED = 1794505311.363151


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    """Local authority + a registry standing in for each gateway process (patched as the module global)."""
    from tools import process_registry_notifications

    # This fixture verifies durable note delivery, not the optional resumed model turn.
    monkeypatch.setattr(process_registry_notifications, "completion_resumes_parent", lambda evt: False)
    holder = {}

    def restart():
        """A new gateway process: fresh registry, then startup recovery exactly as run_startup calls it."""
        registry = ProcessRegistry()
        monkeypatch.setattr(pr_mod, "process_registry", registry)
        registry.recover_from_checkpoint(owner="gateway")
        holder["registry"] = registry
        return registry

    async def setup():
        runner, authority, ref, _actor = await _local(tmp_path, monkeypatch)
        live = authority.sessions[ref.session_id]
        target = runner.session_store.lookup_by_session_key(live.route).session_id
        holder.update(runner=runner, authority=authority, ref=ref, live=live, target=target, home=tmp_path)
        return holder
    holder["setup"], holder["restart"] = setup, restart
    yield holder
    if "authority" in holder:
        holder["authority"].db.close()


def _job(h, sid="proc_0fcf936312c4", *, notify=True, key=None, output="RESULT_ok\n", exit_code=0):
    """A gateway-watched background job of the local chat, running in the current registry."""
    live = h["live"]
    registry = h.get("registry") or h["restart"]()
    session = ProcessSession(
        id=sid, command="hermes update --yes", task_id="t", owner_task_id="t",
        session_key=key or live.route, started_at=STARTED, notify_on_complete=notify,
        watcher_platform=live.source.platform.value, watcher_chat_id=str(live.source.chat_id or ""),
        watcher_user_id=str(live.source.user_id or ""), watcher_thread_id=str(live.source.thread_id or ""),
        watcher_interval=5, parent_session_id=h["target"], persist_on_release=True)
    with registry._lock:
        registry._running[sid] = session
    session.append_output(output)
    return registry, session


def _settle_exit(registry, session, exit_code=0):
    """The reader's exit path: receipt + checkpoint settlement + in-memory publish, no watcher tick."""
    assert registry._finish_exited(session, exit_code) is True


def _receipt(h, sid="proc_0fcf936312c4") -> dict:
    path = h["home"] / "logs" / "process-results" / f"{sid}.json"
    return json.loads(path.read_text())


async def _run_watchers(h, registry):
    watchers, registry.pending_watchers = registry.pending_watchers, []
    for watcher in watchers:
        watcher["check_interval"] = 0
        await h["runner"]._run_process_watcher(watcher)
    return watchers


def _completion_admissions(h, sid="proc_"):
    return [r for r in _admissions(h["authority"], h["ref"]) if sid in json.dumps(r["payload"])]


@pytest.mark.asyncio
async def test_restart_between_exit_and_watcher_tick_admits_the_exact_completion_once(rig):
    h = await rig["setup"]()
    registry, session = _job(h, output="build ok\nRESULT_proc_0fcf936312c4 done\n")
    _settle_exit(registry, session, 0)
    receipt = _receipt(h)
    assert receipt["exit_code"] == 0 and OBLIGATION_KEY in receipt, "the owed notice rides the receipt write"
    assert "proc_0fcf936312c4" not in json.dumps(
        json.loads((h["home"] / "processes.json").read_text())), "the checkpoint entry is gone"

    # The gateway is replaced before the watcher's first tick (its 5 s sleep): never run it.
    registry = h["restart"]()
    watchers = await _run_watchers(h, registry)
    assert [w["session_id"] for w in watchers] == ["proc_0fcf936312c4"], "restart lost custody of the completion"
    rows = _completion_admissions(h)
    assert len(rows) == 1, rows
    text = rows[0]["payload"]["text"]
    assert "RESULT_proc_0fcf936312c4" in text and "exit" in text.lower()
    assert '"completion","proc_0fcf936312c4",1794505311.363151' in json.dumps(rows[0]["payload"]).replace("\\", "")
    assert OBLIGATION_KEY not in _receipt(h), "admission acknowledges the obligation"

    # Acknowledged: later starts never offer it again.
    for _ in range(2):
        registry = h["restart"]()
        assert await _run_watchers(h, registry) == []
    assert len(_completion_admissions(h)) == 1
    await h["authority"]._drain(h["ref"])
    notes = _notes(h["authority"], h["target"])
    assert len(notes) == 1 and "RESULT_proc_0fcf936312c4" in str(notes[0]["content"])
    # The exact-ID recovery tool sees the real admission row and refuses to owe it again.
    from tools.process_registry_obligations import reopen
    refused = reopen("proc_0fcf936312c4", platform="local", chat_id=str(h["live"].source.chat_id or "x"))
    assert not refused["ok"] and "already admitted as" in refused["error"], refused


@pytest.mark.asyncio
async def test_lost_acknowledgement_replay_resolves_to_the_existing_admission(rig, monkeypatch):
    """Admission committed, ack never written (gateway killed after admitting): the next start re-arms it,
    and the producer identity finds the durable admission instead of starting a second turn."""
    h = await rig["setup"]()
    registry, session = _job(h)
    _settle_exit(registry, session)
    owed_receipt = (h["home"] / "logs" / "process-results" / "proc_0fcf936312c4.json").read_text()
    registry = h["restart"]()
    await _run_watchers(h, registry)
    assert len(_completion_admissions(h)) == 1 and OBLIGATION_KEY not in _receipt(h)
    # The ack never reached disk: the receipt still says "owed" at the next start.
    (h["home"] / "logs" / "process-results" / "proc_0fcf936312c4.json").write_text(owed_receipt)
    registry = h["restart"]()
    assert len(await _run_watchers(h, registry)) == 1
    assert len(_completion_admissions(h)) == 1, "a replay must not admit a second turn"
    assert OBLIGATION_KEY not in _receipt(h)


@pytest.mark.asyncio
async def test_refusal_and_repeated_restarts_keep_custody_until_admitted(rig, monkeypatch):
    h = await rig["setup"]()
    registry, session = _job(h)
    _settle_exit(registry, session)
    # Repeated restarts with no watcher run in between: each start still finds it.
    for _ in range(3):
        registry = h["restart"]()
    import gateway.run_notifications as rn
    monkeypatch.setattr(rn, "_MAX_COMPLETION_INJECTION_ATTEMPTS", 1)

    async def refuse(text, evt):
        return False
    sent = []

    async def plain(*args, **kwargs):
        sent.append(args)
    runner = h["runner"]
    runner._enqueue_process_completion_notification = refuse
    runner._send_watcher_message = plain
    assert len(await _run_watchers(h, registry)) == 1
    assert _completion_admissions(h) == [] and OBLIGATION_KEY in _receipt(h), "a refusal keeps custody"
    del runner._enqueue_process_completion_notification, runner._send_watcher_message
    registry = h["restart"]()
    assert len(await _run_watchers(h, registry)) == 1
    assert len(_completion_admissions(h)) == 1 and OBLIGATION_KEY not in _receipt(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("consume", ["wait", "read_log"])
async def test_wait_or_log_consumption_suppresses_the_notice_across_restart(rig, consume):
    h = await rig["setup"]()
    registry, session = _job(h)
    _settle_exit(registry, session)
    result = registry.wait(session.id, timeout=5) if consume == "wait" else registry.read_log(session.id)
    assert "RESULT_ok" in json.dumps(result)
    assert OBLIGATION_KEY not in _receipt(h)
    registry = h["restart"]()
    assert await _run_watchers(h, registry) == []
    assert _completion_admissions(h) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("consume_output", [True, False])
async def test_kill_that_races_the_reader_exit_settles_only_an_explicit_kill(rig, consume_output):
    """The reader records a plain exit (owed receipt) during the kill's signal wait; the kill then rewrites
    the receipt as killed. An explicit kill handed the output to its caller, so nothing is owed across a
    restart; a bulk cleanup kill (consume_output=False) keeps the notice owed."""
    h = await rig["setup"]()
    registry, session = _job(h)

    def reader_exits_mid_signal(sess, consume):
        _settle_exit(registry, sess, 0)
        assert OBLIGATION_KEY in _receipt(h)
    registry._signal_kill = reader_exits_mid_signal
    result = registry.kill_process(session.id, consume_output=consume_output)
    assert result["status"] == "killed" and _receipt(h)["completion_reason"] == "killed"
    assert (OBLIGATION_KEY in _receipt(h)) is (not consume_output)
    registry = h["restart"]()
    assert len(await _run_watchers(h, registry)) == (0 if consume_output else 1)
    assert len(_completion_admissions(h)) == (0 if consume_output else 1)


def test_owed_receipts_survive_retention_and_a_malformed_one_skips_alone(tmp_path, monkeypatch):
    """Count pruning keeps owed receipts; a broken owed receipt does not stop recovery of the rest;
    a subagent's handoff note is redacted before it reaches the receipt."""
    import os
    from tools import process_registry_results as results
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(results, "MAX_RETAINED_RESULTS", 2)
    registry = ProcessRegistry()
    secret = "sk-proj-" + "A1b2C3d4" * 6
    for n, sid in enumerate(("proc_eeeeeeeeeeee", "proc_ffffffffffff")):
        session = ProcessSession(id=sid, command="true", task_id="t", session_key="agent:main:local:dm:x",
                                 started_at=STARTED + n, notify_on_complete=True, watcher_platform="local",
                                 watcher_chat_id="x", watcher_interval=5, handoff_note=f"key {secret}")
        with registry._lock:
            registry._running[sid] = session
        registry._finish_exited(session, 0)
    directory = tmp_path / "logs" / "process-results"
    owed = json.loads((directory / "proc_eeeeeeeeeeee.json").read_text())
    assert secret not in json.dumps(owed) and "key " in owed[OBLIGATION_KEY]["handoff_note"]
    for n in range(3):  # newer ordinary receipts push both owed ones past the count cap
        (directory / f"proc_00000000000{n}.json").write_text(json.dumps({"id": f"proc_00000000000{n}"}))
    broken = json.loads((directory / "proc_eeeeeeeeeeee.json").read_text())
    del broken["exit_code"]
    (directory / "proc_eeeeeeeeeeee.json").write_text(json.dumps(broken))
    import time
    old = time.time() - 3600
    for sid in ("proc_eeeeeeeeeeee", "proc_ffffffffffff"):  # older than the 3 ordinary ones, inside retention
        os.utime(directory / f"{sid}.json", (old, old))
    results._result_paths()
    assert (directory / "proc_eeeeeeeeeeee.json").exists() and (directory / "proc_ffffffffffff.json").exists()
    assert len(list(directory.glob("proc_0*"))) == 2, "ordinary receipts past the cap are still pruned"
    gateway = ProcessRegistry()
    gateway.recover_from_checkpoint(owner="gateway")
    assert [w["session_id"] for w in gateway.pending_watchers] == ["proc_ffffffffffff"]


@pytest.mark.asyncio
async def test_no_notify_and_other_profile_jobs_do_not_wake(rig):
    h = await rig["setup"]()
    registry, quiet = _job(h, "proc_aaaaaaaaaaaa", notify=False)
    _settle_exit(registry, quiet)
    registry, foreign = _job(h, "proc_bbbbbbbbbbbb", key="agent:other:local:dm:20261027_134353_5ab65c")
    _settle_exit(registry, foreign)
    assert OBLIGATION_KEY not in _receipt(h, "proc_aaaaaaaaaaaa")
    # A multiplexed gateway starting under profile "main": agent:other:* belongs to profile "other".
    registry = ProcessRegistry()
    registry.profile_scoped_checkpoints = True
    pr_mod.process_registry = registry
    registry.recover_from_checkpoint(owner="gateway")
    assert [w["session_id"] for w in registry.pending_watchers] == []
    assert OBLIGATION_KEY in _receipt(h, "proc_bbbbbbbbbbbb"), "another profile's custody is left for it"
    assert _receipt(h, "proc_bbbbbbbbbbbb")[OBLIGATION_KEY]["recoveries"] == 0
    assert _completion_admissions(h) == []


def test_only_a_messaging_gateway_rearms_owed_completions(tmp_path, monkeypatch):
    """Managed workers (owner=None) and the Desktop backend (owner=tui) never take custody."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry = ProcessRegistry()
    session = ProcessSession(id="proc_cccccccccccc", command="true", task_id="t", session_key="agent:main:local:dm:x",
                             started_at=STARTED, notify_on_complete=True, watcher_platform="local",
                             watcher_chat_id="x", watcher_interval=5)
    with registry._lock:
        registry._running[session.id] = session
    registry._finish_exited(session, 0)
    for owner in (None, "tui"):
        other = ProcessRegistry()
        other.recover_from_checkpoint(owner=owner)
        assert other.pending_watchers == []
    gateway = ProcessRegistry()
    gateway.recover_from_checkpoint(owner="gateway")
    assert [w["session_id"] for w in gateway.pending_watchers] == ["proc_cccccccccccc"]


def test_reopen_marks_one_exact_receipt_and_refuses_an_admitted_one(tmp_path, monkeypatch, capsys):
    """The supported recovery for a completion lost before this fix: exact id, no chat post, no SQL write."""
    from tools import process_registry_obligations as ob
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_state import SessionDB
    SessionDB(tmp_path / "state.db").close()
    directory = tmp_path / "logs" / "process-results"
    directory.mkdir(parents=True)
    legacy = {"id": "proc_dddddddddddd", "command": "hermes update", "cwd": None, "task_id": "t",
              "owner_task_id": "t", "session_key": "agent:main:local:dm:20261027_134353_5ab65c",
              "parent_session_id": "20261027_134353_5ab65c", "started_at": STARTED, "exit_code": 0,
              "completion_reason": "exited", "termination_source": "", "notify_on_complete": True,
              "output": "done"}
    (directory / "proc_dddddddddddd.json").write_text(json.dumps(legacy))
    assert ob.main(["reopen", "proc_dddd", "--platform", "local", "--chat-id", "x"]) == 1  # exact ids only
    assert ob.main(["reopen", "proc_dddddddddddd", "--platform", "local",
                    "--chat-id", "20261027_134353_5ab65c"]) == 0
    owed = json.loads((directory / "proc_dddddddddddd.json").read_text())[OBLIGATION_KEY]
    assert owed["watcher_chat_id"] == "20261027_134353_5ab65c" and owed["recoveries"] == 0
    armed = ProcessRegistry()
    armed.recover_from_checkpoint(owner="gateway")
    assert [(w["session_id"], w["chat_id"], w["platform"]) for w in armed.pending_watchers] == [
        ("proc_dddddddddddd", "20261027_134353_5ab65c", "local")]
    recovered = armed.get("proc_dddddddddddd")
    assert recovered is not None and recovered.exit_code == 0 and recovered.exited
    assert ob.main(["reopen", "proc_dddddddddddd", "--platform", "local", "--chat-id", "x"]) == 1  # already owed
    monkeypatch.setattr(ob, "_admitted", lambda record: "adm_1")
    (directory / "proc_dddddddddddd.json").write_text(json.dumps(legacy))
    assert ob.main(["reopen", "proc_dddddddddddd", "--platform", "local", "--chat-id", "x"]) == 1
    assert "already admitted as adm_1" in capsys.readouterr().out
    registry = ProcessRegistry()
    registry.recover_from_checkpoint(owner="gateway")
    assert registry.pending_watchers == []
