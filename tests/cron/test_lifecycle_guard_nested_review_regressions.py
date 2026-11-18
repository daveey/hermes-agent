"""Real-shell execution and clean twins for nested-frame review regressions."""
import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tests.cron.test_lifecycle_guard_fd_redirections import _bash_runs, _tree

DEEP = '${x:-$(' * 199 + 'true' + ')}' * 199
RUN = [
    'echo ' + DEEP + '; bash ./r.sh',
    'echo "${x:-${y:-"\'"} "$(true\nbash ./r.sh)"}"',
]
RUN += [
    'echo "${x:-${y:-"\'"} $(echo "$(true\nbash ./r.sh)")}"',
    'echo "${x:-${y:-${z:-"\'"}} $(echo "$(true\nbash ./r.sh)")}"',
]
CLEAN = [
    "echo '" + DEEP + "'; python3 ./clean.py",
    *(command.replace('bash ./r.sh', 'python3 ./clean.py') for command in RUN[1:]),
]


@pytest.mark.parametrize('command', RUN)
def test_review_run(tmp_path, command):
    _tree(tmp_path)
    assert _bash_runs(tmp_path, command)
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe
    if command == RUN[0]:
        assert refusal == 'the substitution nesting limit was reached'


@pytest.mark.parametrize('command', CLEAN)
def test_review_clean(tmp_path, command):
    _tree(tmp_path)
    assert not _bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)
