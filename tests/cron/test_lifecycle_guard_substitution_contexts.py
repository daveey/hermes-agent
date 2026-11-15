"""Command substitutions inside parameter expansions and redirection targets are walked.

Real bash (3.2 and 5.x) runs the substitution in ``"${x:-$(bash S)}"`` and ``>"$(bash S)"``
before the outer command, even when the redirection then fails. Each row runs real bash with a
fake ``hermes`` on PATH first, so a row that bash would not execute proves nothing.
"""
from __future__ import annotations

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tests.cron.test_lifecycle_guard_fd_redirections import _bash_runs, _tree

PREFIXES = {
    'plain': '',
    'midword': 'echo a#b; ',
    'comment-quote': "echo hi # it's\n",
    'heredoc': "cat <<'EOF'\ndon't\nEOF\n",
    'ansi': "printf %s $'it\\'s'\n",
}
WORDS = {
    'param-default': 'echo "${{x:-{p}}}"',
    'param-assign': 'echo "${{x:={p}}}"',
    'param-alternate': 'x=1; echo "${{x:+{p}}}"',
    'param-strip': 'x=a; echo "${{x#{p}}}"',
    'param-unquoted': 'echo ${{x:-{p}}}',
    'param-inner-quoted': 'echo ${{x:-"{p}"}}',
    'redir-out': 'echo 1 >"{p}"',
    'redir-append': 'echo 1 >>"{p}"',
    'redir-in': 'cat <"{p}"',
    'redir-stderr': 'echo 1 2>"{p}"',
    'quoted-arg': 'echo "{p}"',
    'quoted-assign': 'x="{p}"',
    'here-string': 'cat <<<"{p}"',
    # An apostrophe inside double quotes is literal; it must not start a single-quoted span.
    'param-apostrophe': 'echo "it\'s ${{x:-{p}}}"',
    'redir-apostrophe': 'echo 1 >"it\'s{p}"',
}
PAYLOADS = {'dollar': '$({s})', 'backtick': '`{s}`'}


def _rows(script: str, words=WORDS):
    return [pytest.param(PREFIXES[pre] + words[w].format(p=PAYLOADS[p].format(s=script)),
                         id=f'{pre}-{w}-{p}')
            for pre in PREFIXES for w in words for p in PAYLOADS]


@pytest.mark.parametrize('command', _rows('bash ./r.sh'))
def test_substitution_in_word_context_refused(tmp_path, command):
    _tree(tmp_path)
    assert _bash_runs(tmp_path, command), 'bash must run the restart, or the row proves nothing'
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0] is True


@pytest.mark.parametrize('command', _rows(
    'bash ./r.sh', {k: WORDS[k] for k in ('param-default', 'redir-out')}))
def test_oversize_script_in_word_context_fails_closed(tmp_path, command):
    _tree(tmp_path)
    (tmp_path / 'r.sh').write_text('touch ./ran\n' + '#' * (1 << 20) + '\n')
    _bash_runs(tmp_path, command)
    assert (tmp_path / 'ran').exists(), 'bash must run the oversize script'
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe or 'larger than the scan cap' in (refusal or '')


@pytest.mark.parametrize('command', [
    r'echo 1 >"`bash \"./r.sh\"`"',
    r'x="`bash \"./r.sh\"`"',
])
def test_double_quoted_backtick_unescapes_quotes(tmp_path, command):
    _tree(tmp_path)
    assert _bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0] is True


@pytest.mark.parametrize('command', _rows('python3 ./clean.py'))
def test_clean_substitution_in_word_context_allowed(tmp_path, command):
    _tree(tmp_path)
    assert not _bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize('command', [
    "echo '${x:-$(bash ./r.sh)}'",
    "echo 1 >'$(bash ./r.sh)'",
    r'echo "${x:-\$(bash ./r.sh)}"',
    r'echo 1 >"\$(bash ./r.sh)"',
    r"echo $'${x:-$(bash ./r.sh)}'",
])
def test_inert_substitution_text_in_word_context_allowed(tmp_path, command):
    _tree(tmp_path)
    assert not _bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)
