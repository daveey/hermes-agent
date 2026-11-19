"""Regression witnesses for substitution-output uncertainty branches."""
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _assert_isolated_script_completes(script: str, label: str) -> None:
    """Run helper code in a child. A hung helper fails the assertion, not the runner."""
    try:
        completed = subprocess.run(
            [sys.executable, '-c', script],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        completed = None
    assert completed is not None, f'isolated child did not complete for {label}'
    assert completed.returncode == 0, completed.stderr


def _assert_isolated_child_completes(command: str, expected: bool) -> None:
    script = (
        'from cron.lifecycle_guard_substitution_output import computed_shell_input\n'
        f'assert computed_shell_input({command!r}, frozenset())[0] is {expected}\n'
    )
    _assert_isolated_script_completes(script, command)


@pytest.mark.parametrize(('command', 'expected'), [
    ('exec 3<<<"$(echo code)"; "" -c :', False),
    ('exec 3<<<"$(echo code)"; bash "bash <&3"', False),
    ('exec 3<<<"$(echo code)"; $(true) -s', True),
    ('source clean.sh "$(echo data)"', False),
    ('eval : "$(echo code)"', True),
    ('e$(true)val :', True),
    ('data$(echo name) bash clean.sh', False),
    ('X=$(echo code); bash -c "$Y"', False),
    ('exec 0<<<"$(echo code)"; bash -s arg', True),
    # M120: drop `function_effects == previous_functions`. The fixpoint stops
    # early, so the later function redefinition's $(...) exec 3 is missed.
    ('echo $(true); f; f() { exec 3<<<"$(echo code)"; }; bash <&3', True),
    # M170: drop-filter `_OUTPUT in value`. A clean fd 3 from f() is wrongly
    # marked as computed output, so the original returns False.
    ('echo $(true); f() { exec 3<clean.sh; }; f; bash <&3', False),
    # M122: drop `match`. With no match, `match[1]` raises TypeError.
    ('X=$(echo code); echo "$?"', False),
])
def test_mutation_witnesses(command, expected):
    _assert_isolated_child_completes(command, expected)


def test_single_quoted_backslash_does_not_escape_next_quote():
    command = "X=$(echo code); bash -c '" + chr(92) + "'" + '"$X"'
    _assert_isolated_child_completes(command, True)


def test_depth_guard_is_fail_closed_at_budget():
    script = (
        'from cron.lifecycle_guard_substitution_output import _OUTPUT, _shell_input_is_computed\n'
        "assert _shell_input_is_computed(['bash', '-c', ':'], [], opened={'3': _OUTPUT}, depth=16)\n"
        "assert not _shell_input_is_computed(['bash', '-c', ':'], [], opened={'3': _OUTPUT}, depth=15)\n"
    )
    _assert_isolated_script_completes(script, 'depth-guard')


@pytest.mark.parametrize('command', [
    # M031: negate `previous == uncertain` — empty growth never breaks.
    pytest.param('echo $(true)', id='M031'),
    # M066: negate descriptor-walk loop/function/entry convergence.
    pytest.param('echo $(true); :', id='M066'),
])
def test_fixpoint_loops_complete_in_isolated_child(command):
    _assert_isolated_child_completes(command, False)
