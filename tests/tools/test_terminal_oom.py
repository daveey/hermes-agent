"""A foreground command OOM-killed at the Kanban run's memory cap says so."""
import json

from tools import terminal_oom
from tools.terminal_tool_result import finalize_foreground_result


def _scope(tmp_path, monkeypatch, oom_kill=0, name="hermes-worker-kanban-t_x-run-7.scope"):
    cgroup = tmp_path / "cg" / name
    cgroup.mkdir(parents=True, exist_ok=True)
    (cgroup / "memory.max").write_text("268435456\n")
    (cgroup / "memory.events").write_text(f"low 0\nhigh 0\nmax 3\noom 1\noom_kill {oom_kill}\n")
    monkeypatch.setattr(terminal_oom, "_CGROUP_ROOT", tmp_path / "cg")
    proc_cgroup = tmp_path / "proc_cgroup"
    proc_cgroup.write_text(f"0::/{name}\n")
    monkeypatch.setattr(terminal_oom, "_PROC_CGROUP", proc_cgroup)
    return cgroup


def _finalize(result):
    return json.loads(finalize_foreground_result(
        command="python3 big.py", result=result, env=None, env_type="local", effective_task_id="t",
        task_id="t", session_id="s", session_key="k", workdir=None, command_cwd=None, approval_note=None))


def test_oom_killed_foreground_command_names_the_run_cap(tmp_path, monkeypatch):
    cgroup = _scope(tmp_path, monkeypatch)
    watch = terminal_oom.OomWatch()
    (cgroup / "memory.events").write_text("oom 1\noom_kill 1\n")
    note = watch.note(137)
    assert note == ("killed by the OOM killer at the run's memory cap (256 MiB); run it under "
                    "`systemd-run --user --scope -p MemoryMax=…` or reduce its footprint")
    out = _finalize({"output": "Killed", "returncode": 137, "oom_note": note})
    assert out["exit_code"] == 137
    assert out["exit_code_meaning"] == note


def test_sigkill_without_an_oom_kill_keeps_the_generic_note(tmp_path, monkeypatch):
    _scope(tmp_path, monkeypatch, oom_kill=2)
    watch = terminal_oom.OomWatch()
    assert watch.note(137) is None
    out = _finalize({"output": "Killed", "returncode": 137, "oom_note": None})
    assert "signal 9" in out["exit_code_meaning"]


def test_pipeline_masking_an_oom_kill_is_still_named(tmp_path, monkeypatch):
    cgroup = _scope(tmp_path, monkeypatch)
    watch = terminal_oom.OomWatch()
    (cgroup / "memory.events").write_text("oom_kill 1\n")
    assert "while this command ran" in watch.note(0)


def test_outside_a_kanban_run_scope_nothing_is_read(tmp_path, monkeypatch):
    _scope(tmp_path, monkeypatch, name="app-hermes.scope")
    watch = terminal_oom.OomWatch()
    assert watch.cgroup is None and watch.note(137) is None
