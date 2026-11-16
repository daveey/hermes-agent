import os
import subprocess
import pytest
from cron.lifecycle_guard import scan_gateway_lifecycle

@pytest.mark.parametrize('definition', [
    'run()', 'ru\\\nn()', 'r\\\nu\\\nn()', 'function ru\\\nn',
    'function ru\\\nn()', 'run\\\n()',
])
@pytest.mark.parametrize('names', [frozenset({'zz'}), frozenset(), None])
def test_function_input_review(tmp_path, definition, names):
    command = definition + ' { bash --noprofile -l; }; run < input.sh'
    script = tmp_path / 'input.sh'
    startup = tmp_path / 'startup.sh'
    startup.write_text("shopt -s expand_aliases\nalias t='true; '\n")
    (tmp_path / 'marker.sh').write_text("printf 'REVIEW_FUNCTION_EXECUTED\\n'\n")
    script.write_text('t ( bash marker.sh )\n')
    result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', command], cwd=tmp_path, env=dict(os.environ, BASH_ENV=str(startup)), capture_output=True, text=True, timeout=5, check=False)
    assert (result.returncode, result.stdout, result.stderr) == (0, 'REVIEW_FUNCTION_EXECUTED\n', '')
    script.write_text('printf harmless\\n\n')
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path), session_aliases=names) == (False, None)
    script.write_text('t ( bash huge.sh )\n')
    (tmp_path / 'huge.sh').write_text('# padding\n' * 130000)
    verdict = scan_gateway_lifecycle(command, cwd=str(tmp_path), session_aliases=names)
    assert verdict[0], (definition, names, verdict)
