import os
import subprocess
import pytest
from cron.lifecycle_guard import scan_gateway_lifecycle

@pytest.mark.parametrize('command', [
    'bash --noprofile -l < "$S"',
    'P=$S; bash --noprofile -l < "$P"',
    'exec 3<"$S"; bash --noprofile -l <&3',
])
def test_environment_value_is_not_shell_source(tmp_path, monkeypatch, command):
    monkeypatch.setenv('S', '$T')
    monkeypatch.setenv('T', '/dev/null')
    script = tmp_path / '$T'
    script.write_text("printf 'REVIEW_LITERAL_EXECUTED\\n'\n")
    result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', command], cwd=tmp_path,
        env=dict(os.environ, HOME=str(tmp_path), BASH_ENV='/dev/null'), input='', text=True,
        capture_output=True, timeout=10, check=False)
    assert result.returncode == 0 and result.stdout == 'REVIEW_LITERAL_EXECUTED\n', result
    script.write_text('t ( bash huge.sh )\n')
    (tmp_path / 'huge.sh').write_text('# padding\n' * 130000)
    result = scan_gateway_lifecycle(command, cwd=str(tmp_path), session_aliases=frozenset({'zz'}))
    assert result[0], result


def test_regular_environment_path_controls(tmp_path, monkeypatch):
    script = tmp_path / 'input.sh'
    monkeypatch.setenv('S', str(script))
    script.write_text('t ( bash huge.sh )\n')
    (tmp_path / 'huge.sh').write_text('# padding\n' * 130000)
    command = 'bash --noprofile -l < "$S"'
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path), session_aliases=frozenset({'zz'}))[0]
    script.write_text('printf harmless\n')
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path), session_aliases=frozenset({'zz'})) == (False, None)
