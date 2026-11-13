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
])
def test_hint_survives_shell_options_with_values(tmp_path, monkeypatch, template):
    script = _script(tmp_path, "hermes-sync", _RESTART)

    error = _refuse(tmp_path, monkeypatch, template.format(script=script))

    assert _ROUTE in error


# The referenced-script walk does not yet peel reserved words, so these are not refused today
# (follow-up card); the hint itself must already name the route once they are.
@pytest.mark.parametrize("template", [
    "time {script} mods-pull",
    "time -p {script} mods-pull",
    "! {script} mods-pull",
    "if true; then {script} mods-pull; fi",
    "if {script} mods-pull; then echo ok; fi",
    "while false; do {script} mods-pull; done",
    "{{ {script} mods-pull; }}",
])
def test_hint_survives_reserved_words(tmp_path, template):
    from tools.terminal_tool_guards import _refusal_hint

    script = _script(tmp_path, "hermes-sync", _RESTART)

    assert _ROUTE in _refusal_hint(template.format(script=script))


def test_a_shell_option_value_is_not_the_script(tmp_path, monkeypatch):
    # `-o` consumes `pipefail`; deploy.sh is the script and hermes-sync only its argument.
    deploy = _script(tmp_path, "deploy.sh", _RESTART)

    error = _refuse(tmp_path, monkeypatch, f"bash -o pipefail {deploy} hermes-sync")

    assert "hermes gateway restart" in error
    assert _ROUTE not in error


@pytest.mark.parametrize(("command", "expected"), [
    ("hermes-sync mods-pull", True),
    ("bash -O extglob /x/hermes-sync", True),
    ("time -p -- hermes-sync", True),
    ("then sudo -u ubuntu hermes-sync", True),
    ("bash -c 'if true; then hermes-sync; fi'", True),
    ("python3 /x/hermes-sync", True),
    ("bash -c 'echo hermes-sync'", False),
    ("grep hermes-sync log", False),
    ("echo time hermes-sync", False),
    ("fi hermes-sync", False),
])
def test_executes_command_named(command, expected):
    from cron.lifecycle_guard import executes_command_named

    assert executes_command_named(command, "hermes-sync") is expected
