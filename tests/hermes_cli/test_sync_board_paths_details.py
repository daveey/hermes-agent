"""Board discovery still returns the same paths; split details never install probe maps."""
from hermes_cli import kanban_db, kanban_db_boards, kanban_sync_client as client
from hermes_cli import kanban_sync_cpu as cpu, loop_accounting as la


def test_discovery_split_and_filter_do_not_replace_loop_map(tmp_path, monkeypatch):
    from hermes_cli import kanban_sync_probe_diag as diag
    monkeypatch.setattr(kanban_db, "kanban_home", lambda: tmp_path)
    default = tmp_path / "kanban.db"
    default.touch()
    local = tmp_path / "local.db"
    monkeypatch.setattr(kanban_db_boards, "named_board_db_files", lambda: [("local", local)])
    links = tmp_path / "kanban" / client.sync.LINKS_DIR
    links.mkdir(parents=True)
    for name in ("remote", "local", "bad name"):
        (links / f"{name}.db").touch()
    config = {"fixture": True}
    calls = []
    monkeypatch.setattr(client.sync, "sync_config", lambda: config)
    monkeypatch.setattr(client.sync, "board_selected", lambda p, c: calls.append((p, c)) or p != str(local))
    records = []
    monkeypatch.setattr(la, "record", lambda name, **kw: records.append((name, kw)))
    diag.enter()
    try:
        diag.install([("original", default)])
        assert cpu.step(client.board_paths) == [("default", default), ("remote", links / "remote.db")]
        diag.note(default, None)
        assert [n for n, _ in records if n.startswith("sync.detail/")] == [
            "sync.detail/board_paths.enumerate", "sync.detail/board_paths.links", "sync.detail/board_paths.filter"]
        assert all(not kw["outer"] for n, kw in records if n.startswith("sync.detail/"))
        assert "sync.probe_count/slug/original" in [n for n, _ in records]
        assert len(calls) == 3 and all(c is config for _, c in calls)
    finally:
        diag.leave()


def test_missing_default_and_links(tmp_path, monkeypatch):
    monkeypatch.setattr(kanban_db, "kanban_home", lambda: tmp_path)
    monkeypatch.setattr(kanban_db_boards, "named_board_db_files", list)
    monkeypatch.setattr(client.sync, "sync_config", dict)
    assert client.board_paths() == []
