"""A literal echo spelling is not proof of a non-evaluating consumer."""

import shlex
import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_heredoc import output_reaches_shell


_DEFINITIONS = [
    'echo() { bash -c "$1"; };',
    'function echo { bash -c "$1"; };',
    'ec\\\nho () { bash -c "$1"; };',
    'shopt -s expand_aliases; alias echo="bash -c"\n',
    'source DEFINITIONS;',
    '. DEFINITIONS;',
    'sour"ce" DEFINITIONS;',
    'loader=source; $loader DEFINITIONS;',
    'loader=source; ${loader} DEFINITIONS;',
]


@pytest.mark.parametrize("definition", _DEFINITIONS)
@pytest.mark.parametrize("producer", ["$(printf %s PAYLOAD)", "`printf %s PAYLOAD`"])
def test_rebound_echo_real_shell_and_public_scanner(tmp_path, definition, producer):
    marker = tmp_path / "marker"
    payload = tmp_path / "payload"
    payload.write_text("#!/bin/bash\nprintf reached > " + shlex.quote(str(marker)) + "\n", encoding="utf-8")
    payload.chmod(0o700)
    definitions = tmp_path / "definitions"
    definitions.write_text('echo() { bash -c "$1"; }\n', encoding="utf-8")
    prefix = definition.replace("DEFINITIONS", shlex.quote(str(definitions)))
    command = prefix + '\necho "(' + producer.replace("PAYLOAD", shlex.quote(str(payload))) + ')"\n'
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run([bash, "--noprofile", "--norc", "-c", command],
                            capture_output=True, text=True, encoding="utf-8", timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "reached"
    assert output_reaches_shell(command)
    # The public walk must still follow printed producer output, including its
    # secondary data-argument-masked sink check. Never execute this large fixture.
    large = tmp_path / "large"
    large.write_text("# padding\n" * 150000, encoding="utf-8")
    script = tmp_path / "script.sh"
    script.write_text(prefix + '\necho "($(python3 <<\'EOF\'\n'
                      + f'print("bash {large}")\nEOF\n))"\n', encoding="utf-8")
    unsafe, reason = scan_gateway_lifecycle(f"bash -l {shlex.quote(str(script))}")
    assert unsafe, reason
    assert reason is not None and "cap" in reason.lower(), reason


@pytest.mark.parametrize("prefix", [
    'enable -n echo;', 'hash -p /somewhere/echo echo;',
    'trap callback DEBUG;', 'export -f echo;',
    'declare -f echo;', 'typeset -f echo;',
    '\'echo\' () { :; };', 'ec"ho" () { :; };', 'ec\\ho () { :; };',
    '$loader definitions;', '${loader} definitions;',
    'sour"ce" definitions;', 'al"ias" echo=consumer;',
    'e\\nable -n echo;', 'helper () { :; }; helper;',
])
def test_uncertain_binding_retains_sink(prefix):
    assert output_reaches_shell(prefix + ' echo "($(printf payload))"')


def test_original_binding_context_survives_secondary_mask(monkeypatch):
    from cron import lifecycle_guard as guard
    from cron.lifecycle_guard_output import iter_printed_shell_payloads

    masked = 'echo "($(printf payload))"'
    original = 'echo() { bash -c "$1"; }; ' + masked
    assert not output_reaches_shell(masked)
    assert output_reaches_shell(masked, binding_source=original)
    monkeypatch.setattr(guard, "_mask_data_sink_arguments", lambda _: masked)
    assert list(iter_printed_shell_payloads(original))


def test_heredoc_mask_keeps_original_binding_uncertainty():
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    command = 'python3 <<\'EOF\'\nprint("source")\nEOF\necho "($(printf payload))"'
    assert strip_inert_heredoc_bodies(command) == command

