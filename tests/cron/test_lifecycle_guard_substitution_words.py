"""Executed substitutions retain their outer word and have an independent command walk."""
from __future__ import annotations

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tests.cron.test_lifecycle_guard_fd_redirections import _bash_runs, _tree

RUN = [
    'echo $((bash ./r.sh); echo hi)',
    '$(true) bash ./r.sh',
    'bash $(true) ./r.sh',
    'bash $(true) < ./r.sh',
    'bash `true` ./r.sh',
    'echo "$(cat <<\'EOF\'\n`\nEOF\n)"; bash ./r.sh',
    r'''echo "$(printf %s $'\'')"; bash ./r.sh''',
    r'''echo "$(printf %s $'\''; bash ./r.sh)"''',
    'echo "$(cat <<\'EOF\'\n(\nEOF\n)"; bash ./r.sh',
    'echo "$(true;# )\nbash ./r.sh)"',
    'echo "$(case x in x) bash ./r.sh;; esac)"',
    'bash 2>$(echo /dev/null) < ./r.sh',
    'bash 2>/dev/$(echo null) < ./r.sh',
    'bash 2>&$((1+1)) < ./r.sh',
    'bash {a[$((1+1))]}>/dev/null < ./r.sh',
    'bash {a[`echo 1`]}>/dev/null < ./r.sh',
    'x=`echo 1` bash ./r.sh',
    'echo "$(bash ./r.sh)"',
    'echo "`bash ./r.sh`"',
    'echo "$(bash ./r.sh)"]}>/dev/null',
    '`bash ./r.sh`',
    'echo $(echo "$(bash ./r.sh)")',
    'echo "$(echo ")"; bash ./r.sh)"',
    'echo $((1 + $(bash ./r.sh; echo 1)))',
    'bash 2>$(printf "%s" /dev/null) < ./r.sh',
    'bash 2>$(\necho /dev/null\n) < ./r.sh',
]


@pytest.mark.parametrize('command', RUN)
def test_substitution_execution(tmp_path, command):
    _tree(tmp_path)
    assert _bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0]


@pytest.mark.parametrize('command', [c.replace('bash', 'python3').replace('./r.sh', './clean.py')
                                     for c in RUN])
def test_clean_substitution_execution(tmp_path, command):
    _tree(tmp_path)
    assert not _bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize('command', [
    "echo '$(bash ./r.sh)'", "echo '`bash ./r.sh`'",
    r'echo "\$(bash ./r.sh)"', r'echo \`bash ./r.sh\`',
    '# $(bash ./r.sh)', 'echo ok # `bash ./r.sh`',
    r'''echo "`echo \"; bash ./r.sh; echo \"`"''',
])
def test_inert_substitutions(tmp_path, command):
    _tree(tmp_path)
    assert not _bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize('command', ['`bash ./r.sh`', 'echo "`bash ./r.sh`"',
                                     'x=`bash ./r.sh`', '`t ( bash ./r.sh )`'])
def test_backticks_walk_oversize_scripts(tmp_path, command):
    _tree(tmp_path)
    (tmp_path / 'r.sh').write_text('# padding\n' * 120000 + 'hermes gateway "restart"\n')
    prefix = "shopt -s expand_aliases\nalias t='time '\n"
    assert _bash_runs(tmp_path, prefix + command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path), session_aliases={'t': 'time '})[0]


@pytest.mark.parametrize('word', [
    '"$(true)"', "'literal'$(true)", 'prefix$(true)', '$(true)suffix',
    r'prefix\ $(true)', r'\ $(true)', '""$(true)', '$((1+1))',
])
def test_empty_variant_retains_nonempty_or_quoted_words(word):
    from cron.lifecycle_guard_substitutions import separate_substitutions

    command = f'bash {word} ./r.sh'
    assert (separate_substitutions(command, empty_unquoted=True)[0]
            == separate_substitutions(command)[0])


@pytest.mark.parametrize('operator', ['<', '>', '2>', '2>&', '>|', '<<', '<<<'])
@pytest.mark.parametrize('spacing', ['', ' '])
def test_empty_variant_retains_redirection_operands(operator, spacing):
    from cron.lifecycle_guard_substitutions import separate_substitutions

    command = f'bash {operator}{spacing}$(true) < ./r.sh'
    assert (separate_substitutions(command, empty_unquoted=True)[0]
            == separate_substitutions(command)[0])


@pytest.mark.parametrize('word', ['$(true)', '`true`', '$(true)`true`'])
def test_empty_variant_drops_only_whole_unquoted_words(word):
    from cron.lifecycle_guard_substitutions import separate_substitutions

    assert separate_substitutions(f'bash {word} < ./r.sh', empty_unquoted=True)[0] == 'bash  < ./r.sh'


def test_ansi_c_quotes_keep_expansion_text_inert():
    from cron.lifecycle_guard_substitutions import separate_substitutions

    command = r'''echo $'\'$(bash ./r.sh)`bash ./r.sh`)' '''
    assert separate_substitutions(command) == (command, [])
