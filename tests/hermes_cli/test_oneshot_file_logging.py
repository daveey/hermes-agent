"""The real ``hermes -z`` client keeps stdout clean; its gateway logs the actual turn.

The former test patched an in-process runner no production entry point used. Exercise
CLI -> gateway -> loopback provider instead: a reply alone cannot prove file logging.
"""
import subprocess
import sys

from tests.gateway.test_unified_gateway_native_live import ROOT, daemon

pytest_plugins = ["tests.gateway.test_unified_gateway_native_live"]  # its ``harness`` and ``peer`` fixtures


def test_oneshot_logs_reach_agent_log_but_not_console(harness):
    home, env = harness['home'], harness['env']
    with daemon(home, env, 'oneshot-gateway.log'):
        result = subprocess.run(
            [sys.executable, '-m', 'hermes_cli.main', '-z', 'FILE_LOGGING_PROBE',
             '--in', str(harness['work']), '-t', ''],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=90, check=False,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert result.stdout == 'NATIVE_ACK_FILE_LOGGING_PROBE\n'
        assert result.stderr == ''
        assert any(r['text'] == 'FILE_LOGGING_PROBE' for r in harness['peer'].requests)
    agent_log = (home / 'logs' / 'agent.log').read_text(encoding='utf-8')
    assert 'API call #1' in agent_log
    assert 'native-model' in agent_log
    assert 'Turn ended' in agent_log
