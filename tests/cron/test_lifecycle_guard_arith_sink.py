"""``$(( $(date +%s) ... ))`` is not a shell sink (t_01a1d67ff0075752).

David-MBP refused a read-only background watcher, ``bash -n S && bash S``, with "`…/hermes-sync.log`
is larger than the scan cap … a script the command executes must be scannable". S only greps the
log. The trigger was S's ``deadline=$(( $(date +%s) + 2700 ))``: the sink regex read the second
``(`` of ``$((`` as a subshell opener and ``$(date …)`` after it as a command-position substitution
whose output the shell runs. That made every quoted literal in S, and every span between two
quotes, a "printed" command. One such span (``" ~/.hermes/logs/hermes-sync.log | tail …\\n grep -E "``)
put the log in command position, and the over-cap log failed closed as an executed script.
Neither background mode, the ``bash -n S && bash S`` chain nor the pipe into tail/cut is needed.
``$(date +%s)`` prints only digits, so it alone is exempt (unless the command can rebind ``date``);
any other producer inside ``$((``/``((`` stays a sink, since bash runs code from its output
(review 5897778680).
"""

import json

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_heredoc import output_reaches_shell
from tools.terminal_tool_guards import gateway_lifecycle_block

# The refused script (David-MBP, 11:58 PST), its log path pointed at {log} and its shebang
# spelled the way this repo's shebang check requires.
_WATCHER = """#!/usr/bin/env bash
# Wait (max 45 min) for David-MBP's core to include PR #2546 (b22e3019a3) after the 11:58 PST expedite.
C=~/.hermes/hermes-agent
SHA=b22e3019a3173c8b1b7b8986bb61d503a973cac3
deadline=$(( $(date +%s) + 2700 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if git -C "$C" merge-base --is-ancestor "$SHA" HEAD 2>/dev/null; then
    echo "$(TZ=America/Los_Angeles date '+%F %T %Z') Mac core has #2546: HEAD $(git -C "$C" rev-parse --short=10 HEAD)"
    grep -F "core: gateway /Users/daveey/.hermes back on" {log} | tail -1 | cut -c1-200
    grep -E "expedited for b22e3019a3|core-drain: drain window idle" {log} | tail -2 | cut -c1-200
    exit 0
  fi
  sleep 30
done
echo "$(TZ=America/Los_Angeles date '+%F %T %Z') deadline: HEAD $(git -C "$C" rev-parse --short=10 HEAD) still lacks b22e3019a3"
grep -E "core-drain|expedit|cooling off|core: " {log} | tail -8 | cut -c1-220
exit 1
"""


def _big_log(tmp_path):
    log = tmp_path / "logs" / "hermes-sync.log"
    log.parent.mkdir()
    log.write_text("core: pass ok\n" * 90000, encoding="utf-8")  # ~1.2 MiB, over the scan cap
    return log


def _watcher(tmp_path, log):
    script = tmp_path / "mac_2546_deploy_wait.sh"
    script.write_text(_WATCHER.replace("{log}", str(log)), encoding="utf-8")
    return script


def test_refused_background_watcher_runs(tmp_path, monkeypatch):
    """The exact refused call: terminal guard, literal log path, log over the cap. Background
    and foreground calls share this guard (``terminal_tool._pre_exec_block``)."""
    from tools import process_registry

    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)
    script = _watcher(tmp_path, _big_log(tmp_path))

    blocked = gateway_lifecycle_block(
        command=f"bash -n {script} && bash {script}", env=None, env_type="local",
        cwd=str(tmp_path), workdir=str(tmp_path), session_key="arith-sink-test",
    )

    assert blocked is None, json.loads(blocked)["error"]


# Minimal forms: an arithmetic expansion holding a substitution, then two quoted reads of the log.
_ARITH_SHAPES = {
    "assign": "x=$(( $(date +%s) + 1 ))\n",
    "echo": "echo $(( $(date +%s) + 1 ))\n",
    "glued": "x=$(($(date +%s) + 1))\n",
    "arith-command": "(( $(date +%s) > 1 ))\n",
    "spaced": "x=$((  $(date +%s)  ))\n",
}
_READERS = {
    "grep": 'grep -F "a" {log}\ngrep -F "b" {log}\n',
    "tail-cut": 'grep "a" {log} | tail -1 | cut -c1-9\ngrep "b" {log} | tail -1\n',
    "wc-sort": 'wc -l "{log}"\nsort "{log}" | head -1\n',
    "awk": "awk '/a/' {log}\nawk '/b/' {log}\n",
}


@pytest.mark.parametrize("arith", sorted(_ARITH_SHAPES))
def test_arithmetic_substitution_is_no_sink(arith):
    assert not output_reaches_shell(_ARITH_SHAPES[arith])


@pytest.mark.parametrize("reader", sorted(_READERS))
@pytest.mark.parametrize("arith", sorted(_ARITH_SHAPES))
def test_data_operand_after_arithmetic_is_not_an_executed_script(tmp_path, arith, reader):
    log = _big_log(tmp_path)
    command = _ARITH_SHAPES[arith] + _READERS[reader].replace("{log}", str(log))
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)
    script = tmp_path / "s.sh"
    script.write_text(command, encoding="utf-8")
    assert scan_gateway_lifecycle(f"bash -n {script} && bash {script}", cwd=str(tmp_path)) == (False, None)


# A real command-position substitution next to it stays a sink: `(` that opens a subshell, `$(`
# that opens a substitution, and an arithmetic expansion followed by a sink elsewhere.
@pytest.mark.parametrize("text", [
    "( $(x) )", "$( $(x) )", "a; ( $(x) )", "x=$(( 1 ))\n$(y)", "x=$(( $(y) ))\neval \"$z\"",
    "x=$(( $(y) )) | bash", "cat <( $(x) )",
])
def test_command_position_substitution_is_still_a_sink(text):
    assert output_reaches_shell(text)


@pytest.mark.parametrize("text", [
    "x=$(( $(y) + 1 ))", "(( $(y) > 1 ))", "x=$(( $(date +%F) ))", "x=$(( $(date) ))",
    "x=$(( $(date +%s; y) ))", "x=$(( $(dates +%s) ))",
])
def test_arithmetic_around_other_producers_is_still_a_sink(text):
    """Only ``$(date +%s)`` is exempt: bash runs code from other producers' output inside
    ``$((``/``((`` (review 5897778680)."""
    assert output_reaches_shell(text)


# Review 5897778680: real bash 5.2 runs the payload of each shape, so each must stay a sink
# and the over-cap script it runs must still refuse.
_BASH_RUNS_OUTPUT = [
    "x=$(( $(echo 'a[$(bash {big})]') ))",
    "x=$(( $(printf '%s' 'a[$(bash {big})]') ))",
    "(( $(echo 'a[$(bash {big})]') ))",
    "(($(echo bash {big})) )",
    "(($(echo bash {big})); true)",
]


@pytest.mark.parametrize("shape", _BASH_RUNS_OUTPUT)
def test_output_run_inside_arithmetic_still_fails_closed(tmp_path, shape):
    command = shape.replace("{big}", str(_big_log(tmp_path)))
    assert output_reaches_shell(command)
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe
    assert refusal and "larger than the scan cap" in refusal


# Anything that can rebind `date` drops the exemption, so the date producer is a sink again.
@pytest.mark.parametrize("prefix", [
    "date() { echo 'a[$(bash x)]'; }\n", "function date { :; }\n", "date () { :; }; ",
    "alias date=evil\n", "PATH=/tmp/evil:$PATH\n", "export PATH=/tmp/evil\n", "hash -p /tmp/evil date\n",
    "source lib.sh\n", ". lib.sh\n", "BASH_ENV=evil.sh ", "enable -f x date\n", "declare -f date\n",
])
def test_rebound_date_is_still_a_sink(prefix):
    assert output_reaches_shell(prefix + "x=$(( $(date +%s) + 1 ))\n")


def test_rebound_date_reading_a_big_log_fails_closed(tmp_path):
    log = _big_log(tmp_path)
    command = ("date() { echo 'a[$(bash x)]'; }\nx=$(( $(date +%s) + 1 ))\n"
               + _READERS["grep"].replace("{log}", str(log)))
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe
    assert refusal and "larger than the scan cap" in refusal


@pytest.mark.parametrize("command", [
    "bash {big}", "sh {big}", "source {big}", ". {big}", "bash < {big}", "cat {big} | bash",
    "x=$(( $(date +%s) + 1 ))\nbash {big}",
])
def test_executed_file_over_the_cap_still_fails_closed(tmp_path, command):
    big = _big_log(tmp_path)
    unsafe, refusal = scan_gateway_lifecycle(command.replace("{big}", str(big)), cwd=str(tmp_path))
    assert unsafe
    assert refusal and "larger than the scan cap" in refusal
