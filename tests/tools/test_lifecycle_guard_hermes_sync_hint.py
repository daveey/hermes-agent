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
