"""Linux manager custody for failed-result observers, outside worker cgroups."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from hermes_platform.resolver import locate_command


def arm_service(context, *, startup_seconds=5.0):
    """Acknowledge initialized board access only after systemd takes custody.

    No provider credentials are transferred to the user manager. An unavailable
    manager raises rather than claiming that a detached process is supervised.
    """
    from tools.environments.local import build_subprocess_env
    from tools.process_registry import systemd_user_bus_env
    from hermes_cli._startup_fast import project_root_str

    binary = locate_command('systemd-run').command
    if not binary:
        raise RuntimeError('failed-result observer needs systemd-run')
    directory = Path(tempfile.mkdtemp(prefix='failed-result-', dir=Path(context['db']).parent))
    ready = directory / 'ready'
    context = {**context, 'ready_path': str(ready)}
    unit = f'hermes-failed-result-{os.getpid()}-{time.monotonic_ns()}.service'
    context['observer_unit'] = unit
    env = build_subprocess_env(extra=systemd_user_bus_env())
    command = [*binary, '--user', '--quiet', '--collect', f'--unit={unit}',
               '--property=Type=exec', '--property=Slice=app.slice',
               '--property=Restart=on-failure', '--property=RestartSec=500ms',
               '--property=RestartForceExitStatus=SIGTERM SIGHUP SIGINT SIGPIPE',
               '--property=StartLimitIntervalSec=0',
               f'--working-directory={project_root_str()}']
    # A manager-created service does not inherit its submitter's environment.
    # Keep profile/path resolution, without forwarding secrets or worker pins.
    for key in ('PATH', 'PYTHONPATH', 'HERMES_HOME', 'HERMES_KANBAN_HOME', 'HERMES_KANBAN_BOARD',
                'HOME', 'LANG', 'TZ', 'TMPDIR'):
        if key in env:
            command.append(f'--setenv={key}={env[key]}')
    command += ['--', sys.executable, '-m', 'hermes_cli.kanban_failed_result_observer', json.dumps(context)]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding='utf-8', timeout=startup_seconds, env=env, check=False)
    except subprocess.TimeoutExpired as exc:
        # Submission may have succeeded: do not destroy the acknowledgment path.
        raise RuntimeError('failed-result observer service submission timed out') from exc
    if result.returncode:
        ready.unlink(missing_ok=True)
        directory.rmdir()
        raise RuntimeError(f'failed-result observer service refused: {result.stderr[:200]}')
    deadline = time.monotonic() + startup_seconds
    while time.monotonic() < deadline:
        try:
            if ready.read_text(encoding='utf-8-sig') == unit:
                return unit
        except FileNotFoundError:
            pass
        time.sleep(.05)
    raise RuntimeError('failed-result observer service did not acknowledge startup')
