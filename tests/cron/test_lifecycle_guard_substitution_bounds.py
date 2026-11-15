"""Case delimiters and bounded substitution parsing; shell checks use fake hermes."""
from __future__ import annotations

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from cron.lifecycle_guard_substitutions import SUBSTITUTION, separate_substitutions
from tests.cron.test_lifecycle_guard_fd_redirections import _bash_runs, _tree


@pytest.mark.parametrize('body', [
    'case x in x) bash ./r.sh;; esac',
    'case x in (x) bash ./r.sh;; esac',
    'case x in y) true;; x|z) bash ./r.sh;; esac',
    'case x in x) case y in y) bash ./r.sh;; esac;; esac',
    'case x in (x) case y in (y) bash ./r.sh;; esac;; esac',
    'case x in (x) case y in z) true;; y) bash ./r.sh;; esac;; esac',
    'case x in x) true;;& x) bash ./r.sh;; esac',
])
def test_case_pattern_delimiters(tmp_path, body):
    _tree(tmp_path)
    command = f'echo "$({body})"; echo after'
    assert separate_substitutions(command) == (f'echo "{SUBSTITUTION}"; echo after', [body])
    assert _bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0]
    clean = command.replace('bash ./r.sh', 'python3 ./clean.py')
    assert not _bash_runs(tmp_path, clean)
    assert scan_gateway_lifecycle(clean, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize('body', ['echo case', 'echo esac', 'echo "case x in x)"', "echo 'case'"])
def test_case_arguments_do_not_swallow_following_commands(tmp_path, body):
    _tree(tmp_path)
    command = f'echo "$({body})"; bash ./r.sh'
    assert separate_substitutions(command) == (f'echo "{SUBSTITUTION}"; bash ./r.sh', [body])
    assert _bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0]


@pytest.mark.parametrize('body', [
    'case x in @(x)) echo ok;; esac',
    'case in in in) echo ok;; esac',
    '"printf" case x in x',
    '`printf true` case x in x',
    'case x in x) echo "case";; esac',
    'case x in (x) case y in y) true;; esac;; z) echo ok;; esac',
    'case x in @(x)) true;; z) echo ok;; esac',
    'case x in @(x)|case|in) echo ok;; esac',
    'true; case x in x) echo ok;; esac',
])
def test_case_lexical_controls(body):
    command = f'echo "$({body})"; echo later'
    assert separate_substitutions(command) == (f'echo "{SUBSTITUTION}"; echo later', [body])


@pytest.mark.parametrize('opener,closer', [('$(echo ', ')'), ('$((', '))')])
def test_extreme_nesting_refuses_without_recursion_error(opener, closer):
    command = opener * 1200 + 'true' + closer * 1200
    unsafe, reason = scan_gateway_lifecycle(command)
    assert unsafe
    assert reason and 'substitution nesting' in reason


def test_backtick_body_walk_is_bounded():
    # Each legacy backtick body reveals a fresh dollar substitution to the walker.
    command = 'echo ' + '$(echo ' * 1200 + '`true`' + ')' * 1200
    unsafe, reason = scan_gateway_lifecycle(command)
    assert unsafe
    assert reason and 'substitution nesting' in reason


@pytest.mark.parametrize('command', ['echo "$(echo $(true))"', 'echo $((1 + $((2 + 3))))',
                                     '# ' + '$(' * 1200], ids=['command', 'arithmetic', 'comment'])
def test_clean_and_inert_nesting_controls(command):
    assert scan_gateway_lifecycle(command) == (False, None)


def test_single_quoted_nesting_is_lexically_inert():
    # The full walk deliberately also scans printed literals as shell payloads;
    # the lexical separator itself must not count inert dollar signs as nesting.
    command = "echo '" + '$(' * 1200 + "'"
    assert separate_substitutions(command) == (command, [])


@pytest.mark.parametrize('layer', ['delimiter', 'arithmetic', 'body-walk'])
def test_exact_nesting_boundary(layer):
    from cron.lifecycle_guard import _iter_command_token_lists
    from cron.lifecycle_guard_substitutions import (
        MAX_SUBSTITUTION_DEPTH, SubstitutionNestingLimit, _paren_end,
    )

    def parse(depth):
        if layer == 'delimiter':
            return _paren_end('$(true)', 0, depth)
        if layer == 'arithmetic':
            return separate_substitutions('true', _nesting=depth)
        return list(_iter_command_token_lists('true', _nesting=depth))

    assert parse(MAX_SUBSTITUTION_DEPTH - 1)
    with pytest.raises(SubstitutionNestingLimit, match='substitution nesting'):
        parse(MAX_SUBSTITUTION_DEPTH)

