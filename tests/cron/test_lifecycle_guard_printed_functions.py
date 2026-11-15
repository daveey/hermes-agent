"""Card t_01a1dd07a08dbd73: a function's printed output flows into the startup shell its call
pipes to (``f() { printf 'T'; }; f | bash -l``), though definition and call are separate printed
units. The nested shell parses that text with alias names of its own, so ``t (`` may be a
subshell there: the db path is executed and the guard must refuse to inspect the SQLite file."""
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from cron.lifecycle_guard_nested_shells import _printed_pipeline_units, _unit_nested_stdin_flow
from tests.cron import test_lifecycle_guard_session_aliases as aliases

live_kanban_db = aliases.live_kanban_db

_PRINT = "printf 't ( bash {db} )'"


@pytest.mark.parametrize("shape", [
    "f() {{ {p}; }}; f | bash -l",  # the card's repro
    "function f {{ {p}; }}; f | bash -l",
    "function f() {{ {p}; }}; f | bash -l",
    "f()\n{{ {p}; }}\nf | bash -l",
    "function f\n{{ {p}; }}\nf | bash -l",
    "f() ( {p} ); f | cat | bash -l",
    "f() if :; then {p}; fi; f | bash -l",
    "f() {{ {p}; }}; f |& zsh",
    "f() {{ {p}; }}; f | ssh h",
    "f() {{ {p}; }}\nf |\nbash -l",
    "f() {{ {p}; }}; {{ f; }} | bash -l",
    "f() {{ {p}; }}; f | {{ cat; bash -l; }}",
    "{{ f() {{ {p}; }}; }}; f | bash -l",  # defined inside a group
    "g() {{ {p}; }}; f() {{ g; }}; f | bash -l",  # called through another function
    "f() {{ g; }}; g() {{ {p}; }}; f | bash -l",  # callee defined after the caller
    "f() {{ printf ':'; }}; f() {{ {p}; }}; f | bash -l",  # redefinition: the body that runs
    "f() {{ {p}; }}; f() {{ printf ':'; }}; f | bash -l",  # either may run (order, control)
    "f() {{ {p}; [ -n \"$1\" ] || f x; }}; f | bash -l",  # recursion terminates
    "f() {{ {p}; }}; echo \"$(f)\" | bash -l",
    "f() {{ {p}; }}; echo `f` | bash -l",
    "f() {{ python3 - <<'PY'\nprint('t ( bash {db} )')\nPY\n}}; f | bash -l",
])
def test_function_producer_feeds_startup_shell(tmp_path, live_kanban_db, shape):
    text = shape.format(p=_PRINT.format(db=live_kanban_db), db=live_kanban_db)
    unsafe, refusal = scan_gateway_lifecycle(
        text, cwd=str(tmp_path), session_aliases=frozenset({"zz"}))
    assert unsafe is True and refusal is not None and "SQLite" in refusal


@pytest.mark.parametrize("shape", [
    "f() {{ {p}; }}; f | bash",  # a plain shell holds the session's aliases only
    "f() {{ {p}; }}; f | bash -s",
    "f() {{ {p}; }}; f | bash -l -c ':'",
    "f() {{ {p}; }}; f; printf ':' | bash -l",  # f prints to the terminal, not the shell
    "f() {{ {p}; }}; f && printf ':' | bash -l",
    "f() {{ {p}; }}\nf\nprintf ':' | bash -l",
    "f() {{ {p}; }}; g() {{ printf ':'; }}; g | bash -l",  # an unrelated definition
    "f() {{ {p}; }}; printf ':' | bash -l | f",  # downstream of the shell
    "f() {{ {p}; }}; ff | bash -l",  # a longer name is no call
    "f() {{ printf ':'; }}; f | bash -l",
    "f() {{ {p}; }}; f | cat; printf ':' | bash -l",
])
@pytest.mark.parametrize("names", [frozenset(), frozenset({"zz"}), aliases._STOCK_ALIASES])
def test_function_output_outside_startup_shell_stays_allowed(tmp_path, live_kanban_db, shape, names):
    text = shape.format(p=_PRINT.format(db=live_kanban_db))
    assert scan_gateway_lifecycle(text, cwd=str(tmp_path), session_aliases=names) == (False, None)


@pytest.mark.parametrize("names", [frozenset({"t"}), None])
def test_session_alias_still_refuses_plain_shell(tmp_path, live_kanban_db, names):
    """Control: with `t` a session alias (or unknown names) even a plain shell refuses."""
    text = "f() { " + _PRINT.format(db=live_kanban_db) + "; }; f | bash"
    unsafe, refusal = scan_gateway_lifecycle(text, cwd=str(tmp_path), session_aliases=names)
    assert unsafe is True and refusal is not None


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shape", [
    "f() { printf 'echo FED\\n'; }; f | bash --noprofile --norc",
    "function f\n{ printf 'echo FED\\n'; }\nf | bash --noprofile --norc",
    "g() { printf 'echo FED\\n'; }; f() { g; }; f | bash --noprofile --norc",
    "f() { printf ':\\n'; }; f() { printf 'echo FED\\n'; }; f | bash --noprofile --norc",
    "f() { printf 'echo FED\\n'; }; echo \"$(f)\" | bash --noprofile --norc",
    "f() ( printf 'echo FED\\n' ); f | cat | bash --noprofile --norc",
])
def test_real_bash_function_output_reaches_piped_shell(tmp_path, shape):
    """Harmless marker: the consumer shell executes what the called function printed."""
    result = subprocess.run(["bash", "--noprofile", "--norc", "-c", shape], cwd=tmp_path,
                            capture_output=True, text=True, encoding="utf-8", timeout=10,
                            check=True)
    assert result.stdout == "FED\n"


@pytest.mark.platforms("posix")
def test_real_bash_uncalled_function_output_does_not_reach_shell(tmp_path):
    text = "f() { printf 'echo FED\\n'; }; f; printf ':\\n' | bash --noprofile --norc"
    result = subprocess.run(["bash", "--noprofile", "--norc", "-c", text], cwd=tmp_path,
                            capture_output=True, text=True, encoding="utf-8", timeout=10,
                            check=True)
    assert result.stdout == "echo FED\n"  # printed, never executed


@pytest.mark.parametrize("definition", ["function f", "function f()", "f()"])
def test_function_keyword_body_is_one_unit(definition):
    units = _printed_pipeline_units(definition + " { printf ':'; echo x; }; f | bash -l")
    assert len(units) == 2
    roles = [role for role, _word in units[0].events]
    assert roles.count("open") - roles.count("close") == 0
    assert ("close", "}") in units[0].events
    assert units[1].text() == "f | bash -l"


def test_upstream_end_stops_before_downstream_words():
    unit, = _printed_pipeline_units("g | bash -l | f")
    feeds, _body, end = _unit_nested_stdin_flow(unit)
    assert feeds
    assert ("word", "g") in unit.events[:end]
    assert ("word", "f") not in unit.events[:end]
