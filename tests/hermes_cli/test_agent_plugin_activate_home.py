"""``POST /api/dashboard/agent-plugins/activate`` loads the plugin under the home it names.

A Desktop serve backend launched with ``--profile <named>`` also hosts the default profile (its
host record lists ``default``). hermes-sync activates new hermes-mods plugins for the ROOT home.
The handler used to map ``default`` to "no scope", which left ``get_hermes_home()`` at the launch
profile, so every activation for the root home was refused with 400 "Home is not served by this
backend." and the backend kept running stale plugin hooks.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server

HEADER = "X-Hermes-Session-Token"


@pytest.fixture
def named_launch(monkeypatch):
    """A backend whose process home is ``<root>/profiles/shadow`` (``hermes --profile shadow serve``)."""
    from hermes_constants import get_default_hermes_root, get_hermes_home
    import hermes_cli.plugins_activation as pa

    root = Path(get_default_hermes_root())
    shadow = root / "profiles" / "shadow"
    shadow.mkdir(parents=True, exist_ok=True)
    (shadow / "config.yaml").write_text("model: test-model\n", encoding="utf-8")
    (root / "config.yaml").touch()
    monkeypatch.setenv("HERMES_HOME", str(shadow))
    assert Path(get_default_hermes_root()) == root  # the launch profile still resolves its root

    seen: list[Path] = []

    def _record(name):
        seen.append(Path(get_hermes_home()))
        return {"name": name, "activated_now": {"hooks": ["probe"]}, "deferred": {}}

    monkeypatch.setattr(pa, "load_and_go_live", _record)
    return {"root": root, "shadow": shadow, "seen": seen,
            "client": TestClient(web_server.app)}


def _activate(client, home: Path):
    return client.post("/api/dashboard/agent-plugins/activate",
                       headers={HEADER: web_server._SESSION_TOKEN},
                       json={"name": "probe", "home": str(home)})


def test_default_root_home_activates_on_a_named_profile_backend(named_launch):
    resp = _activate(named_launch["client"], named_launch["root"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert [p.resolve() for p in named_launch["seen"]] == [named_launch["root"].resolve()]


def test_launch_profile_home_still_activates_under_itself(named_launch):
    resp = _activate(named_launch["client"], named_launch["shadow"])
    assert resp.status_code == 200, resp.text
    assert [p.resolve() for p in named_launch["seen"]] == [named_launch["shadow"].resolve()]


def test_non_profile_home_is_still_refused(named_launch, tmp_path):
    resp = _activate(named_launch["client"], tmp_path / "not-a-home")
    assert resp.status_code == 400
    assert named_launch["seen"] == []
