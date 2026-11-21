"""Shared setup for the kanban sync seam tests (core patch 0193). Each test
module wraps these generators in its own fixtures.

Sync is switched on the way a user switches it on: ``kanban.sync.enabled: true``
in the profile's ``config.yaml``, read by the loader at first connect.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync


def write_sync_config(home: Path, enabled: bool) -> None:
    (home / "config.yaml").write_text(f"kanban:\n  sync:\n    enabled: {'true' if enabled else 'false'}\n")


def write_routed_sync_config(home: Path, enabled: bool) -> None:
    """Real default route eligibility for tests driving automatic dispatch, not only sync."""
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    from hermes_cli.config_effective import load_user_config_effective
    write_sync_config(home, enabled)
    with (home / "config.yaml").open("a") as stream:
        stream.write(_managed_yaml())
    config = load_user_config_effective()
    assert config["kanban"]["sync"]["enabled"] is enabled
    assert config["model_routing"]["enabled"] is True


def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    sync.consume_dispatcher_wake()
    from hermes_cli import kanban_sync_client
    kanban_sync_client.reset_board_watch()  # kept read connections and repair memos belong to one test's files
    return home


def synced_conn(kanban_home):
    write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


def lease_client():
    client = FakeLeaseClient()
    previous = sync.set_lease_client(client)
    try:
        yield client
    finally:
        sync.set_lease_client(previous)


class FakeLeaseClient:
    """Stub Board service: ``mode`` is ``grant`` | ``deny`` | ``offline``."""

    def __init__(self) -> None:
        self.mode = "grant"
        self.renew_mode = "grant"
        self.lease_seconds: Optional[int] = None  # None = the requested ttl
        self.calls: list[tuple[str, dict]] = []

    def _answer(self, mode: str, kind: str, kw: dict) -> sync.Lease:
        self.calls.append((kind, kw))
        if mode == "offline":
            raise sync.LeaseUnavailable("connection refused")
        if mode == "deny":
            return sync.Lease(granted=False, run_uid=kw["run_uid"], holder="gw_other", reason="held")
        seconds = self.lease_seconds if self.lease_seconds is not None else kw["ttl_s"]
        return sync.Lease(granted=True, run_uid=kw["run_uid"], expires_ms=int((time.time() + seconds) * 1000),
                          holder=kw["gateway_id"])

    def acquire(self, **kw) -> sync.Lease:
        return self._answer(self.mode, "acquire", kw)

    def renew(self, **kw) -> sync.Lease:
        return self._answer(self.renew_mode, "renew", kw)

    def release(self, **kw) -> None:
        self.calls.append(("release", kw))

    def count(self, kind: str) -> int:
        return sum(1 for k, _ in self.calls if k == kind)
