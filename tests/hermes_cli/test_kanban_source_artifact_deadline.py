"""Ending metadata, selected blobs and Git share one preflight budget."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_source_artifacts as artifacts
from hermes_cli import kanban_source_publish as pub
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_source_artifact_staging as staging
from tests.hermes_cli import test_kanban_source_publish as publishing

stores = staging.stores
world = publishing.world
board = publishing.board
card = publishing.card


@pytest.mark.parametrize("old_deadline,expected", [(None, 125.0), (110.0, 110.0), (140.0, 125.0)])
def test_ending_transfer_caps_private_client(stores, tmp_path, monkeypatch, old_deadline, expected):
    _, _, _, conn, _, tid, _ = stores
    src = tmp_path / "report.txt"
    src.write_bytes(b"evidence")
    entries = artifacts.stage(conn, tid, [str(src)], board="default", run_uid="run-one")
    shared = SimpleNamespace(ident=object(), timeout=30, deadline=old_deadline)
    monkeypatch.setattr(sync, "_client", lambda: SimpleNamespace(hub=shared))
    monkeypatch.setattr(artifacts.time, "monotonic", lambda: 100.0)
    seen = []
    monkeypatch.setattr(client, "push", lambda c, h, *a, **kw: seen.append(h))

    def ensure(c, task, selected, hub, board):
        seen.append(hub)
        return selected

    monkeypatch.setattr(artifacts, "ensure", ensure)
    assert artifacts.portable(conn, tid, None, "run-one", bound=pub._Deadline(25, clock=lambda: 100.0)) == entries
    assert len(seen) == 2 and seen[0] is seen[1] and seen[0] is not shared
    assert seen[0].ident is shared.ident and seen[0].deadline == expected
    assert shared.deadline == old_deadline


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("expire_at,expected_blobs", [("before", 0), ("metadata", 0), ("blob", 1)])
def test_expired_budget_stops_next_transfer(stores, tmp_path, monkeypatch, expire_at, expected_blobs, count):
    _, _, _, conn, _, tid, _ = stores
    paths = [tmp_path / "one.txt", tmp_path / "two.txt"][:count]
    for path in paths:
        path.write_bytes(b"evidence")
    entries = artifacts.stage(conn, tid, list(map(str, paths)), board="default", run_uid="run-one")
    now = [100.0]
    bound = pub._Deadline(25, clock=lambda: now[0])
    if expire_at == "before":
        now[0] = 126.0
    shared = SimpleNamespace(ident=object(), timeout=30, deadline=None)
    monkeypatch.setattr(sync, "_client", lambda: SimpleNamespace(hub=shared))
    seen = []

    def push(*a, **kw):
        if expire_at == "metadata":
            now[0] = 126.0

    def ensure(c, task, selected, hub, board):
        seen.extend(selected)
        if expire_at == "blob":
            now[0] = 126.0
        return selected

    monkeypatch.setattr(client, "push", push)
    monkeypatch.setattr(artifacts, "ensure", ensure)
    with pytest.raises(pub.PublishRefused, match="preflight_timeout"):
        artifacts.portable(conn, tid, None, "run-one", bound=bound)
    assert len(seen) == expected_blobs
    assert artifacts.selected(conn, tid, "run-one") == entries


@pytest.mark.parametrize("direct", [False, True])
def test_actual_publish_paths_supply_same_git_deadline(card, monkeypatch, direct):
    seen = []
    real_observe = pub.observe

    def portable(*a, bound=None, **kw):
        assert bound is not None, "artifact I/O must be capped before it starts"
        seen.append(bound)
        return []

    def observe(bound, *a, **kw):
        seen.append(bound)
        return real_observe(bound, *a, **kw)

    monkeypatch.setattr(artifacts, "portable", portable)
    monkeypatch.setattr(pub, "observe", observe)
    if direct:
        from hermes_cli import kanban_db as kb
        pub.publish(card.conn, kb.get_task(card.conn, card.id), card.run(), card.cfg,
                    pub._Deadline(90), machine="hub")
    else:
        assert card.receipt()["state"] == "published"
    assert len(seen) >= 3
    assert all(bound is seen[0] for bound in seen)
