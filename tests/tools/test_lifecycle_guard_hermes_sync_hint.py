"""A refused hermes-sync run names the sanctioned in-gateway deploy route.

Regression for self-heal defect agent-d48ea04d62: hub chats ran
``~/.hermes/sync-runner/hermes-sync mods-pull`` inside the gateway, got the generic
"run it from a separate shell" refusal, and retried it wrapped in ``systemd-run``.
"""

import json

import pytest

from tools.terminal_tool_guards import gateway_lifecycle_block

_ROUTE = "git -C ~/code/hermes-mods merge --ff-only origin/main"


def _script(tmp_path, name, body):
    path = tmp_path / "sync-runner" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _refuse(tmp_path, monkeypatch, command):
    from tools import process_registry

    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)
    blocked = gateway_lifecycle_block(
        command=command, env=None, env_type="local", cwd=str(tmp_path), workdir=None,
        session_key="hermes-sync-hint-test",
    )
    assert blocked is not None
    return json.loads(blocked)["error"]


@pytest.mark.parametrize("kind", ["restart", "unscannable"])
def test_refused_hermes_sync_names_the_sanctioned_route(tmp_path, monkeypatch, kind):
    if kind == "restart":
        body = "#!/bin/sh\nhermes gateway restart\n"
    else:  # over the scan line cap, as the hub's 17962-line hermes-sync is
        body = "#!/bin/sh\n" + "true\n" * 20000
    script = _script(tmp_path, "hermes-sync", body)

    error = _refuse(tmp_path, monkeypatch, f"{script} mods-pull")

    assert _ROUTE in error
    assert "mods-reload:" in error
    assert "systemd-run" in error


def test_other_refusals_keep_the_generic_text(tmp_path, monkeypatch):
    script = _script(tmp_path, "deploy.sh", "#!/bin/sh\nhermes gateway restart\n")

    error = _refuse(tmp_path, monkeypatch, str(script))

    assert _ROUTE not in error
    assert "hermes gateway restart" in error


_RESTART = "#!/bin/sh\nhermes gateway restart\n"


@pytest.mark.parametrize("template", [
    "sudo -u ubuntu {script} mods-pull",
    "HERMES_HOME=/x nohup {script} mods-pull",
    "systemd-run --user --unit sync {script} mods-pull",
    "bash {script} mods-pull",
    "bash -c '{script} mods-pull'",
    "cd /tmp && {script} mods-pull",
])
def test_hint_follows_hermes_sync_through_wrappers(tmp_path, monkeypatch, template):
    script = _script(tmp_path, "hermes-sync", _RESTART)

    error = _refuse(tmp_path, monkeypatch, template.format(script=script))

    assert _ROUTE in error


@pytest.mark.parametrize("template", [
    "grep x hermes-sync; {deploy}",
    "{deploy} --log ~/.hermes/logs/hermes-sync.log",
    "echo 'hermes-sync mods-pull' && {deploy}",
])
def test_a_mere_mention_of_hermes_sync_gets_no_hint(tmp_path, monkeypatch, template):
    # The refused script is deploy.sh; hermes-sync only appears as an argument or in data.
    deploy = _script(tmp_path, "deploy.sh", _RESTART)

    error = _refuse(tmp_path, monkeypatch, template.format(deploy=deploy))

    assert "hermes gateway restart" in error
    assert _ROUTE not in error


def test_hint_names_the_root_sync_log_from_a_profile_gateway(tmp_path, monkeypatch):
    # hermes-sync logs under its own home (root ~/.hermes), not the gateway profile's home.
    home = tmp_path / "home"
    profile = home / ".hermes" / "profiles" / "coder"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    script = _script(tmp_path, "hermes-sync", _RESTART)

    error = _refuse(tmp_path, monkeypatch, f"{script} mods-pull")

    assert "~/.hermes/logs/hermes-sync.log" in error
    assert "profiles" not in error


# PR #2128 review nits: the hint must survive shell options that take a value and the reserved
# words that may precede a command (each case dropped the hint before executes_command_named).
@pytest.mark.parametrize("template", [
    "bash -o pipefail {script} mods-pull",
    "sh +o errexit -- {script} mods-pull",
    "bash -O extglob {script} mods-pull",
    # PR #2191 review nits: a clustered `o`/`O` and --rcfile/--init-file also take a value.
    "bash -euo pipefail {script} mods-pull",
    "bash -eo pipefail {script} mods-pull",
    "sh +eo errexit {script} mods-pull",
    "bash --rcfile /dev/null {script} mods-pull",
    "bash --init-file /dev/null {script} mods-pull",
])
def test_hint_survives_shell_options_with_values(tmp_path, monkeypatch, template):
    script = _script(tmp_path, "hermes-sync", _RESTART)

    error = _refuse(tmp_path, monkeypatch, template.format(script=script))

    assert _ROUTE in error


@pytest.mark.parametrize("template", [
    "time {script} mods-pull",
    "time -p {script} mods-pull",
    "! {script} mods-pull",
    "if true; then {script} mods-pull; fi",
    "if {script} mods-pull; then echo ok; fi",
    "while false; do {script} mods-pull; done",
    "{{ {script} mods-pull; }}",
    # PR #2191 review nits: external time(1) after an assignment or wrapper, and `coproc`.
    "x=1 time {script} mods-pull",
    "env time -f %e {script} mods-pull",
    "coproc {script} mods-pull",
    # `coproc CMD ARGS` runs CMD even when CMD reads like a coproc NAME.
    "coproc sudo -u root {script} mods-pull",
])
def test_hint_survives_reserved_words(tmp_path, monkeypatch, template):
    script = _script(tmp_path, "hermes-sync", _RESTART)

    error = _refuse(tmp_path, monkeypatch, template.format(script=script))

    assert _ROUTE in error


def test_a_shell_option_value_is_not_the_script(tmp_path, monkeypatch):
    # `-o` consumes `pipefail`; deploy.sh is the script and hermes-sync only its argument.
    deploy = _script(tmp_path, "deploy.sh", _RESTART)

    error = _refuse(tmp_path, monkeypatch, f"bash -o pipefail {deploy} hermes-sync")

    assert "hermes gateway restart" in error
    assert _ROUTE not in error


@pytest.mark.parametrize("template", [
    "bash -euo pipefail {deploy} hermes-sync",
    "bash --rcfile {deploy} -i",
])
def test_clustered_option_value_and_rcfile_stay_refused(tmp_path, monkeypatch, template):
    # The clustered `-o` value is not the script, and an rc file the shell sources is still scanned.
    deploy = _script(tmp_path, "deploy.sh", _RESTART)

    error = _refuse(tmp_path, monkeypatch, template.format(deploy=deploy))

    assert "hermes gateway restart" in error
    assert _ROUTE not in error


@pytest.mark.parametrize(("command", "expected"), [
    ("hermes-sync mods-pull", True),
    ("bash -O extglob /x/hermes-sync", True),
    ("time -p -- hermes-sync", True),
    ("then sudo -u ubuntu hermes-sync", True),
    ("bash -c 'if true; then hermes-sync; fi'", True),
    ("python3 /x/hermes-sync", True),
    ("coproc hermes-sync", True),
    ("coproc SYNC { hermes-sync; }", True),
    ("/usr/bin/time -f %e hermes-sync", True),
    ("bash -c 'echo hermes-sync'", False),
    ("grep hermes-sync log", False),
    ("echo time hermes-sync", False),
    ("fi hermes-sync", False),
    # PR #2191 review nits: clustered `o`/`O` and --rcfile/--init-file consume a value; external
    # time(1) after an assignment or wrapper; the shared keyword walk peels `coproc`.
    ("bash -euo pipefail /x/hermes-sync", True),
    ("bash -eo pipefail -- /x/hermes-sync", True),
    ("sh +eo errexit /x/hermes-sync", True),
    ("bash -oO pipefail extglob /x/hermes-sync", True),
    ("bash --rcfile /etc/rc /x/hermes-sync", True),
    ("bash --init-file /etc/rc /x/hermes-sync", True),
    ("bash -euo pipefail /x/deploy.sh hermes-sync", False),
    # PR #2249 review nit: zsh's clustered `-O` (CORRECT) takes no value; its `-o NAME` does.
    ("zsh -eO /x/hermes-sync", True),
    ("zsh -eo errexit /x/hermes-sync", True),
    ("bash -eO extglob /x/hermes-sync", True),
    ("x=1 time hermes-sync", True),
    ("env time -f %e hermes-sync", True),
    ("sudo -u ubuntu time -o /tmp/t -- nohup hermes-sync", True),
    ("coproc hermes-sync mods-pull", True),
    ("coproc SYNC { hermes-sync; }", True),
    ("time -- -p hermes-sync", False),
    ("function hermes-sync { true; }", False),
    # Post-merge check of PR #2249: after a lone `-` the shell's next word is the script even
    # when it looks like an option, and `coproc CMD ARGS` runs CMD (a NAME needs a `{` group).
    ("bash - -x /x/hermes-sync", False),
    ("coproc sudo -u root hermes-sync", True),
])
def test_executes_command_named(command, expected):
    from cron.lifecycle_guard import executes_command_named

    assert executes_command_named(command, "hermes-sync") is expected
