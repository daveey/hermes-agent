"""R4-F F5: run_one_job reserves at entry so the claim/execution preamble is covered before the agent reservation."""
import pytest

from cron import scheduler
from hermes_cli import local_work
from hermes_cli.local_work import Registry
from hermes_constants import hermes_home_key


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reg = Registry(homes=[hermes_home_key(tmp_path)])
    monkeypatch.setattr(local_work, "_registry", reg)
    return reg


def test_run_one_job_preamble_runs_under_a_reservation_released_on_exit(registry, monkeypatch):
    seen = []

    def create_execution(job_id, **kwargs):
        seen.append(registry.snapshot())
        raise RuntimeError("stop after the preamble's first step")

    monkeypatch.setattr(scheduler, "create_execution", create_execution)
    with pytest.raises(RuntimeError, match="stop after"):
        scheduler.run_one_job({"id": "job-1"})
    [snap] = seen
    assert snap.active == 1 and snap.per_kind == {"cron_fire": 1}  # reserved BEFORE the execution attach
    assert registry.snapshot().active == 0  # released on exit, even on failure
    assert "unmatched-finish" not in registry.snapshot().unknowns


def test_run_one_job_runs_unchanged_without_a_registry(monkeypatch):
    monkeypatch.setattr(local_work, "_registry", None)
    monkeypatch.setattr(scheduler, "create_execution", lambda *a, **k: (_ for _ in ()).throw(KeyError("x")))
    with pytest.raises(KeyError):
        scheduler.run_one_job({"id": "job-2"})
