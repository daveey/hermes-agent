"""Substitution quote frames must survive flat comment preprocessing."""
import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from cron.lifecycle_guard_tokens import _iter_command_token_lists
from tests.cron.test_lifecycle_guard_fd_redirections import _bash_runs, _tree


@pytest.mark.parametrize('body', [
    'echo " # "; SCRIPT',
    'printf " # "; SCRIPT',
    'case x in x) echo " # ";; esac; SCRIPT',
    'echo " # " >/dev/null; SCRIPT',
])
@pytest.mark.parametrize('wrapper', ['echo "$(BODY)"', 'echo "$(echo "$(BODY)")"'])
@pytest.mark.parametrize('script', ['bash ./r.sh', 'bash 2>$(echo /dev/null) < ./r.sh'])
@pytest.mark.parametrize('dangerous', [True, False])
def test_nested_quote_comment_boundary(tmp_path, body, wrapper, script, dangerous, request):
    # Existing quoted-output walker false positive, independent of tokenization.
    if (not dangerous and body.startswith('case ') and wrapper.count('$(') == 2
            and '2>' in script):
        request.node.add_marker(pytest.mark.xfail(
            strict=True, reason='t_01a1dc4541941587: stderr output scanned as a script'))
    _tree(tmp_path)
    if not dangerous:
        script = script.replace('bash', 'python3').replace('./r.sh', './clean.py')
    command = wrapper.replace('BODY', body.replace('SCRIPT', script))
    assert _bash_runs(tmp_path, command) is dangerous
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0] is dangerous


@pytest.mark.parametrize('opener', ['$\\\n(', '$\\\n\\\n('])
@pytest.mark.parametrize('wrapper', ['echo "OPENERBODY)"', 'echo "$(echo "OPENERBODY)")"'])
@pytest.mark.parametrize('body', [
    'echo " # "; bash ./r.sh',
    'bash 2>$(echo /dev/null) < ./r.sh',
])
@pytest.mark.parametrize('dangerous', [True, False])
def test_continued_substitution_openers(tmp_path, opener, wrapper, body, dangerous):
    _tree(tmp_path)
    if not dangerous:
        body = body.replace('bash', 'python3').replace('./r.sh', './clean.py')
    command = wrapper.replace('OPENER', opener).replace('BODY', body)
    assert _bash_runs(tmp_path, command) is dangerous
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0] is dangerous


@pytest.mark.parametrize('opener', ['$\\\n((', '$(\\\n(', '$\\\n(\\\n('])
@pytest.mark.parametrize('dangerous', [True, False])
def test_continued_arithmetic_opener(tmp_path, opener, dangerous):
    _tree(tmp_path)
    script = 'bash ./r.sh' if dangerous else 'python3 ./clean.py'
    command = 'echo "' + opener + '$( ' + script + '; echo 1) + 1))"'
    assert _bash_runs(tmp_path, command) is dangerous
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0] is dangerous


def test_separation_precedes_flat_comment_stripping():
    command = 'echo "$(echo " # "; bash ./r.sh)"'
    assert any('./r.sh' in words for words in _iter_command_token_lists(command))


@pytest.mark.parametrize('command', [
    '# $(bash ./r.sh)\npython3 ./clean.py',
    'echo "$(echo ok # bash ./r.sh\n)"',
    "echo '$(echo \" # \"; bash ./r.sh)'",
    "echo '$\\\n(bash ./r.sh)'",
    'echo "\\$\\\n(bash ./r.sh)"',
    '# $\\\n(echo ignored)\npython3 ./clean.py',
])
def test_inert_comment_and_single_quote_controls(tmp_path, command):
    _tree(tmp_path)
    assert not _bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)
