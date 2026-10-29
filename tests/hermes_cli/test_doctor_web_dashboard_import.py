"""``hermes doctor`` reports a dashboard web surface that dies at import (#124214).

The optional ``web`` extra pins fastapi/starlette in lockstep, but the standalone starlette
security pin ships in several other extras — so a venv can end up with starlette 1.x beside an
older fastapi. ``hermes dashboard`` then dies constructing ``FastAPI(...)`` with a TypeError
(not an ImportError), so the module's own lazy-install fallback never fires, and the process
exits before printing anything. The doctor probe imports the web surface in a subprocess so the
crash lands in the report instead of the user's terminal.
"""

from __future__ import annotations


from hermes_cli import doctor_platform as dp
from hermes_cli.doctor_report import Finding


class _FakeCompleted:
    def __init__(self, returncode: int, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr


def _run_check(monkeypatch, completed, recorded):
    def fake_run(*args, **kwargs):
        recorded.append((args, kwargs))
        return completed

    monkeypatch.setattr(dp.subprocess, "run", fake_run)
    finding = Finding()
    dp._check_web_dashboard_import.__wrapped__(should_fix=False, f=finding)
    return finding


def test_drifted_pair_is_a_reported_issue(capsys, monkeypatch):
    stderr = (
        'File ".../fastapi/routing.py", line 835, in __init__\n'
        "TypeError: Router.__init__() got an unexpected keyword argument 'on_startup'\n"
    )
    recorded: list = []
    finding = _run_check(monkeypatch, _FakeCompleted(1, stderr), recorded)

    out = capsys.readouterr().out
    assert "Dashboard web surface" in out and "✗" in out
    assert "Router.__init__() got an unexpected keyword argument 'on_startup'" in out
    assert any("hermes pm repair" in i for i in finding.issues)


def test_probe_runs_without_lazy_installs(capsys, monkeypatch):
    recorded: list = []
    _run_check(monkeypatch, _FakeCompleted(0, ""), recorded)

    (args, kwargs) = recorded[0]
    from hermes_cli._launchers import runtime_command
    from hermes_cli.doctor import PROJECT_ROOT

    assert args[0] == runtime_command(PROJECT_ROOT, code="import hermes_cli.web_server")
    env = kwargs["env"]
    assert env["HERMES_DISABLE_LAZY_INSTALLS"] == "1"
    assert kwargs["timeout"] >= 60


def test_probe_loads_the_dependency_generation_like_the_runtime():
    """The store interpreter runs isolated; only ``hermes_bootstrap`` adds the dependency generation. A bare
    ``python -c 'import hermes_cli.web_server'`` failed "No module named 'ruamel'" on every store install
    while the dashboard served 200 (2026-10-29)."""
    import subprocess

    from hermes_cli._launchers import runtime_command
    from hermes_cli.doctor import PROJECT_ROOT

    argv = runtime_command(PROJECT_ROOT, code="import ruamel.yaml, hermes_cli.web_server; print('ok')")
    assert "-I" in argv and "import hermes_bootstrap" in argv[argv.index("-c") + 1]
    proc = subprocess.run(argv, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120, check=False,
                          env={**__import__("os").environ, "HERMES_DISABLE_LAZY_INSTALLS": "1"})
    assert proc.returncode == 0 and proc.stdout.strip().endswith("ok"), proc.stderr[-2000:]
