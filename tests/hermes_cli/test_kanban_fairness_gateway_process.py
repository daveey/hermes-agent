"""Fresh-process gateway fairness boot follows process-service home, not inherited profile context."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[2]
CHILD = textwrap.dedent(
    """\
    import asyncio
    import json
    import os
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import patch

    root = Path(os.environ["PROCESS_HOME"])
    other = Path(os.environ["OTHER_HOME"])
    from hermes_constants import get_hermes_home, get_hermes_home_override, set_hermes_home_override
    set_hermes_home_override(str(other))
    assert get_hermes_home_override() == str(other)
    assert get_hermes_home() == other

    seen = {}

    def observe():
        from hermes_cli import kanban_fairness_runtime as runtime
        from hermes_cli.kanban_turn_urgency import read_owners
        from hermes_constants import get_hermes_home, get_hermes_home_override
        sessions = list(runtime._SESSIONS.values())
        seen.update(
            override=get_hermes_home_override(),
            home=str(get_hermes_home()),
            enabled=runtime.enabled(board="default"),
            keys=[list(key) for key in runtime._SESSIONS],
            identities=[dict(session.identity) for session in sessions],
            owners=sorted(read_owners(board="default", now_s=150, path=root / "missing")),
        )

    class FakeDispatcher:
        def __init__(self, kb, settings):
            self.successful_scopes = ()

        def tick_once(self, *, suppress_spawn=False):
            observe()
            return []

        def ready_nonempty(self):
            return False

        def auto_decompose_tick(self, per_tick):
            return 0

    async def no_sleep(delay):
        return None

    async def main():
        from gateway import kanban_watchers as watchers
        runner = watchers.GatewayKanbanWatchersMixin()
        runner._running = True

        async def stop(interval):
            runner._running = False

        runner._sleep_until_tick_or_sync_wake = stop
        lease = SimpleNamespace(should_yield=lambda: False, handle=None, release=lambda: None)
        settings = SimpleNamespace(interval=1.0)
        patches = [
            patch.object(watchers.asyncio, "sleep", no_sleep),
            patch.object(watchers, "_KanbanDispatcher", FakeDispatcher),
            patch("hermes_cli.kanban_db_dispatch.reap_worker_zombies", lambda: []),
            patch("hermes_cli.kanban_hub_cpu_alert.check", lambda: None),
            patch("hermes_cli.kanban_sync_outbox_age.check", lambda: None),
            patch("agent.estop.check_paused", lambda *a, **k: False),
            patch("agent.core_update_drain.check_drain_hold", lambda *a, **k: False),
        ]
        for item in patches:
            item.start()
        try:
            await runner._kanban_dispatch_while_held(
                settings, lambda: {"kanban": {"auto_decompose": False}}, None, lease)
        finally:
            for item in reversed(patches):
                item.stop()
        Path(os.environ["RECEIPT"]).write_text(json.dumps({
            **seen,
            "caller_override": get_hermes_home_override(),
            "caller_home": str(get_hermes_home()),
        }))

    asyncio.run(main())
    """
)


def _config(home, capture_id):
    return {"kanban": {"fairness_capture": {
        "enabled": True, "capture_id": capture_id, "directory": str(home / "capture")}}}


def test_dispatch_while_held_boots_process_home_not_inherited_profile(tmp_path):
    from hermes_cli import kanban_fairness_activation as activation
    from hermes_cli.kanban_fairness_store import load
    from tests.gateway.fixtures.local_recovery_probe import child_env

    root, other = tmp_path / "root", tmp_path / "other"
    (tmp_path / "user").mkdir()
    for home, capture_id, gateway, board_uid in (
            (root, "root-cap", "root-gw", "root-board"),
            (other, "other-cap", "other-gw", "other-board")):
        home.mkdir()
        cfg = _config(home, capture_id)
        (home / "config.yaml").write_text(json.dumps(cfg), encoding="utf-8")
        activation.arm(cfg, home=home, board="default", source_gateway=gateway, board_uid=board_uid)

    receipt = tmp_path / "receipt.json"
    env = child_env()
    env.update({k: os.environ[k] for k in ("TIRITH_ENABLED",) if k in os.environ})
    env.update(HOME=str(tmp_path / "user"), USERPROFILE=str(tmp_path / "user"),
               HERMES_HOME=str(root), PYTHONPATH=str(ROOT), PROCESS_HOME=str(root),
               OTHER_HOME=str(other), RECEIPT=str(receipt), HERMES_TEST_ISOLATION=str(root),
               HERMES_DISABLE_LAZY_INSTALLS="1", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-c", CHILD], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=90, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    seen = json.loads(receipt.read_text())
    assert seen["override"] is None
    assert seen["home"] == str(root)
    assert seen["enabled"] is True
    assert seen["owners"] == []
    assert len(seen["identities"]) == 1
    identity = seen["identities"][0]
    assert identity["capture_id"] == "root-cap"
    assert identity["source_gateway"] == "root-gw"
    assert identity["board_uid"] == "root-board"
    assert identity["clock_error_ns"] is None
    assert [key[1] for key in seen["keys"]] == [str(root)]
    assert seen["caller_override"] == str(other)
    assert seen["caller_home"] == str(other)
    epochs = list((root / "capture" / "runtime").iterdir())
    assert len(epochs) == 1
    assert load(epochs[0])["records"][0]["payload"]["outcome"] == "missing"
    assert not (other / "capture" / "runtime").exists()
