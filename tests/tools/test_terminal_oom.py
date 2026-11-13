"""A foreground command OOM-killed at the Kanban run's memory cap says so."""
import json
from types import SimpleNamespace

import pytest

from tools import terminal_oom
from tools.terminal_tool_result import finalize_foreground_result

RUN_UNIT = "hermes-worker-kanban-t_x-run-7.scope"


def _scope(tmp_path, monkeypatch, oom_kill=0, name=RUN_UNIT, procs=""):
    cgroup = tmp_path / "cg" / name
    cgroup.mkdir(parents=True, exist_ok=True)
    (cgroup / "memory.max").write_text("268435456\n")
    (cgroup / "memory.events").write_text(f"low 0\nhigh 0\nmax 3\noom 1\noom_kill {oom_kill}\n")
    (cgroup / "cgroup.procs").write_text(procs)
    monkeypatch.setattr(terminal_oom, "_CGROUP_ROOT", tmp_path / "cg")
    proc_cgroup = tmp_path / "proc_cgroup"
    proc_cgroup.write_text(f"0::/{name}\n")
    monkeypatch.setattr(terminal_oom, "_PROC_CGROUP", proc_cgroup)
    return cgroup


@pytest.fixture
def kernel(monkeypatch):
    """The kernel's OOM record per unit (``kernel.records[unit] = {...}``); ``kernel.calls`` records lookups."""
    state = SimpleNamespace(records={}, calls=[])

    def victim(unit, since):
        state.calls.append((unit, since))
        return state.records.get(unit)

    monkeypatch.setattr(terminal_oom, "_kernel_victim", victim)
    return state


def _finalize(result):
    return json.loads(finalize_foreground_result(
        command="python3 big.py", result=result, env=None, env_type="local", effective_task_id="t",
        task_id="t", session_id="s", session_key="k", workdir=None, command_cwd=None, approval_note=None))


def test_oom_killed_foreground_command_names_the_run_cap(tmp_path, monkeypatch, kernel):
    cgroup = _scope(tmp_path, monkeypatch, procs="100\n101\n")
    watch = terminal_oom.OomWatch()
    kernel.records[RUN_UNIT] = {"pid": 4242, "task": "python3", "limit_bytes": 268435456}  # born during the command
    (cgroup / "memory.events").write_text("oom 1\noom_kill 1\n")
    note = watch.note(137)
    assert note == ("killed by the OOM killer at the run's memory cap (256 MiB; kernel victim: python3 pid 4242); "
                    "run it under `systemd-run --user --scope -p MemoryMax=…` or reduce its footprint")
    assert kernel.calls == [(RUN_UNIT, watch.since)]  # the journal read starts at the command, not a day back
    out = _finalize({"output": "Killed", "returncode": 137, "oom_note": note})
    assert out["exit_code"] == 137
    assert out["exit_code_meaning"] == note


def test_concurrent_victim_is_not_pinned_on_the_command(tmp_path, monkeypatch, kernel):
    """A background job already in the scope was the kernel's victim: the note does not say the command was."""
    cgroup = _scope(tmp_path, monkeypatch, procs="100\n55\n")
    watch = terminal_oom.OomWatch()
    kernel.records[RUN_UNIT] = {"pid": 55, "task": "node", "limit_bytes": 268435456}
    (cgroup / "memory.events").write_text("oom_kill 1\n")
    note = watch.note(137)
    assert "already running before this command (node pid 55)" in note
    assert "concurrent command or background job" in note
    assert not note.startswith("killed by the OOM killer")


def test_no_kernel_record_softens_the_sigkill_wording(tmp_path, monkeypatch, kernel):
    cgroup = _scope(tmp_path, monkeypatch)
    watch = terminal_oom.OomWatch()
    (cgroup / "memory.events").write_text("oom_kill 1\n")
    note = watch.note(137)
    assert note.startswith("exited by SIGKILL while the OOM killer fired at the run's memory cap (256 MiB)")
    assert "probably the victim" in note


def test_survivable_command_in_its_own_scope_gets_the_note(tmp_path, monkeypatch, kernel):
    """Its own transient scope holds only the command, so the run scope's count never moves."""
    _scope(tmp_path, monkeypatch)
    unit = "hermes-worker-proc_ab12.scope"
    watch = terminal_oom.OomWatch(SimpleNamespace(session=SimpleNamespace(systemd_unit=unit)))
    kernel.records[unit] = {"pid": 77, "task": "cc1plus", "limit_bytes": 4294967296}
    note = watch.note(-15)  # its own scope keeps OOMPolicy=stop: systemd stops it
    assert note == ("killed by the OOM killer at its scope's memory cap (4096 MiB; kernel victim: cc1plus pid 77); "
                    "run it under `systemd-run --user --scope -p MemoryMax=…` or reduce its footprint")
    assert kernel.calls == [(unit, watch.since)]
    assert watch.note(0) is None


def test_sigkill_without_an_oom_kill_keeps_the_generic_note(tmp_path, monkeypatch, kernel):
    _scope(tmp_path, monkeypatch, oom_kill=2)
    watch = terminal_oom.OomWatch()
    assert watch.note(137) is None
    assert kernel.calls == []  # no count change: the kernel log is never read
    out = _finalize({"output": "Killed", "returncode": 137, "oom_note": None})
    assert "signal 9" in out["exit_code_meaning"]


def test_pipeline_masking_an_oom_kill_is_still_named(tmp_path, monkeypatch, kernel):
    cgroup = _scope(tmp_path, monkeypatch)
    watch = terminal_oom.OomWatch()
    (cgroup / "memory.events").write_text("oom_kill 1\n")
    assert "while this command ran" in watch.note(0)


def test_outside_a_kanban_run_scope_nothing_is_read(tmp_path, monkeypatch, kernel):
    _scope(tmp_path, monkeypatch, name="app-hermes.scope")
    watch = terminal_oom.OomWatch()
    assert watch.cgroup is None and watch.note(137) is None


def test_kernel_victim_reads_the_journal_from_the_watch_start(monkeypatch):
    import subprocess
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert terminal_oom._kernel_victim(RUN_UNIT, 1794550000.7) is None
    assert seen[0][0] == "journalctl" and seen[0][seen[0].index("-S") + 1] == "@1794550000"
