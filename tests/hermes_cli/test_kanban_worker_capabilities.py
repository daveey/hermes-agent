"""Admission must inspect the assignee, not the creating worker's frozen policy.

Regression for the Upkeep release child: a shell-capable creator admitted a cli=[]
assignee, whose retry then rejected every capable profile as tool-less.
"""
import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("caller_tools", [[], ["terminal", "file"]])
@pytest.mark.parametrize("target_tools", [[], ["terminal", "file"], ["hermes-cli"]])
def test_frozen_worker_create_uses_target_policy(tmp_path, caller_tools, target_tools):
    home = tmp_path / ".hermes"
    target = home / "profiles" / "ci-sweeper"
    target.mkdir(parents=True)
    config = {"platform_toolsets": {"cli": target_tools}, "terminal": {"backend": "local"}}
    (target / "config.yaml").write_text(json.dumps(config))
    # Both default and a named profile must resolve independently of the caller.
    (home / "config.yaml").write_text(json.dumps(config))
    snapshot = {"platform_toolsets": {"cli": caller_tools}, "terminal": {"backend": "docker"}}
    script = tmp_path / "probe.py"
    script.write_text(f"import sys; sys.path.insert(0, {str(ROOT)!r})\n" + textwrap.dedent(f"""
        import json
        from pathlib import Path
        from agent.safe_worker_policy import _bind_worker_config, worker_config_snapshot
        snapshot = {snapshot!r}
        _bind_worker_config(snapshot)
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_capabilities import CapabilityMismatchError
        from hermes_cli.config_effective import load_user_config_effective
        from hermes_cli.config import load_config_readonly
        kb.init_db()
        from hermes_cli.kanban_db_connect import connect_closing
        with connect_closing() as conn:
            for assignee in ('default', 'ci-sweeper'):
                before = conn.execute('SELECT count(*) FROM tasks').fetchone()[0]
                try:
                    tid = kb.create_task(conn, title='Release containing approved SHA; no app install',
                                         assignee=assignee, requires=['terminal', 'git'])
                except CapabilityMismatchError as exc:
                    assert not {bool(target_tools)!r}, str(exc)
                    assert exc.missing == ['terminal', 'git'], exc.missing
                    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == before
                else:
                    assert {bool(target_tools)!r}, 'tool-less release worker was admitted'
                    assert kb.get_task(conn, tid).requires == ['terminal', 'git']
        # Inspection must not write backups or change the worker's own execution authority.
        assert not list(Path({str(home)!r}).rglob('backups/config'))
        assert worker_config_snapshot() == snapshot
        assert load_user_config_effective()['platform_toolsets'] == snapshot['platform_toolsets']
        assert load_config_readonly()['platform_toolsets'] == snapshot['platform_toolsets']
        print('TARGET_POLICY_OK')
    """))
    from tests.gateway.fixtures.local_recovery_probe import child_env
    env = child_env()
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), HERMES_HOME=str(home),
               HERMES_DISABLE_LAZY_INSTALLS="1")
    proc = subprocess.run([sys.executable, str(script)], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=90, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "TARGET_POLICY_OK" in proc.stdout
