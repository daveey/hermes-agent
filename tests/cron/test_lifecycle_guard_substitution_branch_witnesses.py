"""Branch witnesses for lifecycle_guard_substitution_output mutation survivors.

Each case is run in an isolated child that only calls ``computed_shell_input``
(or the named helper); no input is ever executed by a shell. A hang, import
error or crash is reported as a distinct failure so it cannot pass as a kill.
Ids are matrix mutant numbers. M069/M099/M166 are proven equivalent and have no
test; M106 does not exist on this head; M171 is deliberately excluded.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_CHILD = '''
import json, sys
from types import SimpleNamespace
from cron.lifecycle_guard_substitution_output import computed_shell_input
kind, command, aliases = json.loads(sys.argv[1])
if kind == 'input':
    try:
        out = repr(computed_shell_input(command, frozenset(aliases))[0])
    except Exception as exc:
        out = 'EXC:' + type(exc).__name__
elif kind == 'depth':
    from cron.lifecycle_guard_substitution_output import _shell_input_is_computed
    out = repr([_shell_input_is_computed(['bash', '-c', 'true'], [], depth=d) for d in (15, 16)])
else:
    from cron.lifecycle_guard_substitution_output import scan_computed_shell_input
    budget = SimpleNamespace(alias_names=frozenset(aliases), refusal=None)
    result = scan_computed_shell_input(
        command, executed=True, budget=budget,
        recurse=lambda *a, **k: False, cwd='.', source='s')
    out = repr((result, budget.refusal is not None))
print('RESULT=' + out)
'''


def _safe_env() -> dict:
    env = {'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8', 'PYTHONPATH': str(_REPO_ROOT)}
    for key in ('PATH', 'HOME', 'SYSTEMROOT', 'LANG', 'LC_ALL'):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _run(kind: str, command: str, aliases=()) -> str:
    try:
        done = subprocess.run(
            [sys.executable, '-c', _CHILD, json.dumps([kind, command, list(aliases)])],
            cwd=_REPO_ROOT, env=_safe_env(), capture_output=True,
            text=True, encoding='utf-8', errors='replace', timeout=20, check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f'TIMEOUT (not a witness result) for {command!r}')
    lines = [x for x in done.stdout.splitlines() if x.startswith('RESULT=')]
    assert lines, f'child crashed rc={done.returncode}: {done.stderr[-500:]}'
    return lines[-1][len('RESULT='):]


def _case(mutant, command, expected, aliases=(), tag=''):
    return pytest.param(command, expected, tuple(aliases), id=f'{mutant}{tag}')


_C = 'echo $(true); '
_X = 'exec 3<<<"$(echo code)"'

CASES = [
    _case('M022', _C + 'bash -sc "$(echo c)"', True),
    _case('M022', _C + 'bash -c -s "$(echo c)"', True, tag='-c-s'),
    _case('M022', _C + 'bash -s -c "$(echo c)"', True, tag='-s-c'),
    _case('M024', _C + 'bash -c <<<"$(echo c)"', False),
    _case('M024', _C + 'bash -sc <<<"$(echo c)"', False, tag='-sc'),
    _case('M026', _C + 'bash -c <<<"$(echo c)"', False),
    _case('M041', 'cat f.sh | $(true) foo <&-', True),
    _case('M041', 'cat f.sh | $(true) foo 0<&-', True, tag='-fd0'),
    _case('M078', _C + 'if true; then exec 0<clean.sh; fi; $(true) foo', True),
    _case('M078', _C + '{ true; exec 0<clean.sh; }; $(true) foo', True, tag='-brace'),
    _case('M078', _C + 'true && exec 0<clean.sh; $(true) foo', True, tag='-and'),
    _case('M085', _C + '"case" x in x) ' + _X + ';; esac; bash <&3', True),
    _case('M085', _X + '; "for" x in a; do exec 3<&-; done; bash <&3', False, tag='-for'),
    _case('M094', _C + '{ ' + _X + '; "else"; bash <&3; }', True),
    _case('M096', _C + '{ ' + _X + '; echo else; bash <&3; }', True),
    _case('M097', _C + 'else; bash <&3', False),
    _case('M103', _C + 'case x in x) ' + _X + '; bash <&3;; esac', True),
    _case('M104', _X + '; echo; (true x); { exec 3<&-; }; bash <&3', False),
    _case('M107', _C + 'function f; ( c d ) ; { ' + _X + '; }; bash <&3', True),
    _case('M108', _C + 'x=(a b); { ' + _X + '; }; bash <&3', True),
    _case('M108', _C + 'a=1 f() { ' + _X + '; }; f; bash <&3', True, tag='-assign'),
    _case('M116', _X + '; if (bash <&3); then :; fi', True),
    _case('M116', _X + '; ! (bash <&3)', True, tag='-bang'),
    _case('M116', _X + '; time (bash <&3)', True, tag='-time'),
    _case('M117', _X + '; t (bash <&3)', True, aliases=('t',)),
    _case('M121', _C + 'g() { bash <&3; exec 3<&-; }; f() { g; exec 3<&-; }; ' + _X + '; f', True),
    _case('M129', _C + 'comm$(true)', False),
    _case('M129', _C + 'ex$(true)', False, tag='-ex'),
    _case('M129', _C + '$(true)and', False, tag='-suffix'),
    _case('M139', _C + '{ true; true & ' + _X + '; bash <&3; }', True),
    _case('M142', _C + '! time f() { ' + _X + '; }; f; bash <&3', True),
    _case('M143', _C + 'function () { ' + _X + '; }; bash <&3', True),
    _case('M147', 'X=$(echo code); echo "it\'s"; bash -c $X', True),
    _case('M152', 'echo X=$(true); bash -c "$X"', False),
    _case('M155', 'exec 0<<<"$(echo c)"; case x in x) :;; "esac"|bash) :;; esac', False),
    _case('M156', 'exec 0<<<"$(echo c)"; case x in x) :;; bash) :;; esac', False),
    _case('M159', _C + 'case x in x) true; true && ' + _X + '; bash <&3;; esac', True),
    _case('M160', _C + 'case x in x) true & ' + _X + '; bash <&3;; esac', True),
    _case('M165', 'X=$(echo code); echo \'a"b\'; bash -c $X', True),
    # M110/M111: MALFORMED-INPUT CHARACTERIZATION ONLY. No valid-shell witness
    # is known; these pin the current fail-safe result on invalid shell syntax.
    _case('M110-malformed', _C + 'f ( ; { ' + _X + '; } ) ; bash <&3', False),
    _case('M111-malformed', _C + 'function f; ) { ' + _X + '; }; bash <&3', False),
]


@pytest.mark.parametrize(('command', 'expected', 'aliases'), CASES)
def test_branch_witness(command, expected, aliases):
    assert _run('input', command, aliases) == repr(expected)


def test_m027_depth_guard_without_output_binding():
    # Depth 16 must stay False when no OUTPUT binding exists; a mutant that
    # drops the any(_OUTPUT ...) clause returns True.
    assert _run('depth', '') == '[False, False]'


def test_m074_alias_names_budget_refusal():
    assert _run('scan', 'exec 3<<<"$(echo code)"; t (bash <&3)', ('t',)) == '(True, True)'
