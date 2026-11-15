"""Durable at-least-once monitor changes; no production monitor state is touched."""
from cron import jobs, monitor
import pytest


def test_pending_change_retries_original_diff_after_restart(tmp_path, monkeypatch, cron_owner):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    job = jobs.create_job(prompt="Watch", schedule="every 5m", monitor_url="https://example.com")
    legacy = monitor._snapshot_path(job["id"])  # what the pre-outbox writer left behind
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("A", encoding="utf-8")
    jobs.update_job(job["id"], {"monitor_state": {
        "last_output_hash": monitor.hash_monitor_output("A"), "last_changed_at": "legacy"}})
    monkeypatch.setattr(monitor, "_run_monitor_source", lambda job: (True, "B"))
    first = monitor.check_monitor(jobs.get_job(job["id"]))
    assert first.changed and "-A" in first.context_block and "+B" in first.context_block
    # No acknowledgement: quota refusal, crash, or incomplete run. Drop the input snapshot.
    retry = monitor.check_monitor(jobs.get_job(job["id"]))
    assert retry.changed, "unacknowledged B must not be suppressed"
    assert retry.context_block == first.context_block
    assert retry.change_id == first.change_id


def test_acknowledgement_preserves_successive_generation(tmp_path, monkeypatch, cron_owner):
    from cron import monitor_outbox
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    job = jobs.create_job(prompt="Watch", schedule="every 5m", monitor_url="https://example.com")
    source = ["A"]
    monkeypatch.setattr(monitor, "_run_monitor_source", lambda job: (True, source[0]))
    baseline = monitor.check_monitor(job)
    assert baseline.first_run
    assert monitor_outbox.acknowledge(job["id"], baseline.change_id)
    assert not monitor.check_monitor(job).changed  # stale job input cannot resurrect A
    source[0] = "B"
    b = monitor.check_monitor(job)
    source[0] = "C"
    assert monitor.check_monitor(job).change_id == b.change_id
    source[0] = "B"  # C was observed while B was pending; a reversion cannot erase C.
    assert monitor_outbox.acknowledge(job["id"], b.change_id)
    c = monitor.check_monitor(job)
    assert c.change_id != b.change_id
    assert "-B" in c.context_block and "+C" in c.context_block
    assert not monitor_outbox.acknowledge(job["id"], b.change_id)
    assert monitor.check_monitor(job).change_id == c.change_id
    assert monitor_outbox.acknowledge(job["id"], c.change_id)
    reverted = monitor.check_monitor(job)
    assert "-C" in reverted.context_block and "+B" in reverted.context_block
    assert monitor_outbox.acknowledge(job["id"], reverted.change_id)
    assert not monitor.check_monitor(job).changed


def test_missing_legacy_empty_snapshot_is_not_a_valid_baseline(tmp_path, monkeypatch, cron_owner):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    job = jobs.create_job(prompt="Watch", schedule="every 5m", monitor_url="https://example.com")
    jobs.update_job(job["id"], {"monitor_state": {"last_output_hash": monitor.hash_monitor_output("")}})
    monkeypatch.setattr(monitor, "_run_monitor_source", lambda job: (True, ""))
    outcome = monitor.check_monitor(job)
    assert not outcome.ok
    assert "snapshot" in outcome.error


def test_missing_job_fails_visibly(tmp_path, monkeypatch, cron_owner):
    from cron import monitor_outbox
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(monitor, "_run_monitor_source", lambda job: (True, "A"))
    assert not monitor.check_monitor({"id": "missing"}).ok
    with pytest.raises(ValueError, match="missing job"):
        monitor_outbox.acknowledge("missing", "generation")


def test_discard_requires_generation_and_reason(tmp_path, monkeypatch, cron_owner):
    from cron import monitor_outbox
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    job = jobs.create_job(prompt="Watch", schedule="every 5m", monitor_url="https://example.com")
    monkeypatch.setattr(monitor, "_run_monitor_source", lambda job: (True, "A"))
    first = monitor.check_monitor(job)
    assert hasattr(monitor_outbox, "discard"), "explicit discard must be distinct from successful ack"
    with pytest.raises(ValueError, match="reason"):
        monitor_outbox.discard(job["id"], first.change_id, "")
    assert not monitor_outbox.discard(job["id"], "stale", "operator accepted baseline")
    assert monitor_outbox.discard(job["id"], first.change_id, "operator accepted baseline")
    state = jobs.get_job(job["id"])["monitor_state"]
    assert state["last_discard"]["change_id"] == first.change_id
    assert state["last_discard"]["reason"] == "operator accepted baseline"
    assert not monitor.check_monitor(job).changed


@pytest.mark.parametrize("damage", ["queue", "old", "baseline", "version", "entry", "version_deleted"])
def test_outbox_structure_corruption_refuses(tmp_path, monkeypatch, cron_owner, damage):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    job = jobs.create_job(prompt="Watch", schedule="every 5m", monitor_url="https://example.com")
    monkeypatch.setattr(monitor, "_run_monitor_source", lambda job: (True, "A"))
    first = monitor.check_monitor(job)
    state = jobs.get_job(job["id"])["monitor_state"]
    mutations = {
        "queue": lambda: state.pop("pending_changes"),
        "old": lambda: state["pending_changes"][0].update(old="invented"),
        "baseline": lambda: state.update(last_output_hash="bad"),
        "version_deleted": lambda: state.pop("outbox_version"),
        "version": lambda: state.update(outbox_version=999),
        "entry": lambda: state["pending_changes"].__setitem__(0, None),
    }
    mutations[damage]()
    jobs.update_job(job["id"], {"monitor_state": state})
    outcome = monitor.check_monitor(job)
    assert not outcome.ok
    assert "monitor outbox" in outcome.error
    from cron.monitor_outbox import acknowledge
    with pytest.raises(ValueError, match="monitor outbox"):
        acknowledge(job["id"], first.change_id)


@pytest.mark.parametrize("field,value", [("new", "corrupt"), ("old", None), ("hash", "bad"),
                                        ("id", ""), ("first_run", "yes"), ("attempts", True),
                                        ("attempts", -1)])
def test_corrupt_pending_fails_visibly(tmp_path, monkeypatch, cron_owner, field, value):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    job = jobs.create_job(prompt="Watch", schedule="every 5m", monitor_url="https://example.com")
    monkeypatch.setattr(monitor, "_run_monitor_source", lambda job: (True, "A"))
    first = monitor.check_monitor(job)
    state = jobs.get_job(job["id"])["monitor_state"]
    state["pending_changes"][0][field] = value
    jobs.update_job(job["id"], {"monitor_state": state})
    outcome = monitor.check_monitor(job)
    assert not outcome.ok
    assert "monitor outbox" in outcome.error
    from cron.monitor_outbox import acknowledge
    with pytest.raises(ValueError, match="monitor outbox"):
        acknowledge(job["id"], first.change_id)


def test_retries_are_observable_with_stable_id(tmp_path, monkeypatch, cron_owner, caplog):
    import logging
    from cron import monitor_outbox
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    job = jobs.create_job(prompt="Watch", schedule="every 5m", monitor_url="https://example.com")
    monkeypatch.setattr(monitor, "_run_monitor_source", lambda job: (True, "A"))
    first = monitor.check_monitor(job)
    assert first.attempt == 1
    with caplog.at_level(logging.WARNING, logger="cron.monitor"):
        retry = monitor.check_monitor(job)
    assert retry.attempt == 2 and retry.change_id == first.change_id
    assert any(first.change_id in r.getMessage() and "attempt 2" in r.getMessage()
               for r in caplog.records)
    pending = jobs.get_job(job["id"])["monitor_state"]["pending_changes"][0]
    assert pending["attempts"] == 2
    assert monitor_outbox.acknowledge(job["id"], retry.change_id)
    assert not monitor.check_monitor(job).changed


def test_crash_after_staging_reoffers_generation_in_new_process(tmp_path, monkeypatch, cron_owner):
    """A process that dies between staging and acknowledgement leaves the generation durable."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "mon.sh").write_text("echo 'state B'\n")
    job = jobs.create_job(prompt="Watch", schedule="every 5m", monitor_script="mon.sh")
    child = (
        "import os\nfrom cron import jobs, monitor\n"
        f"outcome = monitor.check_monitor(jobs.get_job({job['id']!r}))\n"
        "print(outcome.change_id, flush=True)\n"
        "os._exit(9)  # crash before any agent ran or acknowledged\n")
    root = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ, HERMES_HOME=str(tmp_path), PYTHONPATH=root)
    proc = subprocess.run([sys.executable, "-c", child], env=env, cwd=root,
                          capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 9, proc.stderr
    staged = proc.stdout.strip().splitlines()[-1]
    assert len(staged) == 32
    retry = monitor.check_monitor(jobs.get_job(job["id"]))
    assert retry.changed and retry.first_run
    assert retry.change_id == staged and retry.attempt == 2


def test_failed_staging_write_is_a_visible_source_error(tmp_path, monkeypatch, cron_owner):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    job = jobs.create_job(prompt="Watch", schedule="every 5m", monitor_url="https://example.com")
    monkeypatch.setattr(monitor, "_run_monitor_source", lambda job: (True, "A"))

    def disk_full(*_a, **_k):
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as m:
        m.setattr(jobs, "save_jobs", disk_full)
        outcome = monitor.check_monitor(job)
    assert not outcome.ok and "monitor outbox" in outcome.error
    assert "pending_changes" not in (jobs.get_job(job["id"]).get("monitor_state") or {})
    assert monitor.check_monitor(job).attempt == 1


@pytest.mark.parametrize("source", [(True, "A"), (False, "boom")])
def test_gate_never_carries_a_stale_change_id(tmp_path, monkeypatch, cron_owner, source):
    """A reused job dict must not let a later agent run acknowledge a generation it wasn't offered."""
    from cron import monitor_outbox, scheduler
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    job = jobs.create_job(prompt="Watch", schedule="every 5m", monitor_url="https://example.com")
    monkeypatch.setattr(monitor, "_run_monitor_source", lambda job: (True, "A"))
    first = monitor.check_monitor(job)
    assert monitor_outbox.acknowledge(job["id"], first.change_id)
    monkeypatch.setattr(monitor, "_run_monitor_source", lambda job: source)
    job["_monitor_change_id"] = first.change_id
    early, _extra, _ctx = scheduler._apply_monitor_gate(job, job["id"], "watch", None)
    assert early is not None
    assert "_monitor_change_id" not in job
