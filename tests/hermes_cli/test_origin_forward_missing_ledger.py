"""Real adapter classification: absence is terminal, unavailable evidence retries."""
import io
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.hermes_cli import test_origin_forward_authority as authority_helpers
from tests.hermes_cli import test_origin_forward_decision as decision_helpers

authority = authority_helpers.authority
forwarding = decision_helpers.forwarding


@pytest.mark.parametrize("case", ["absent", "absent_parent", "dangling", "directory", "corrupt", "permission", "open_race"])
def test_adapter_missing_vs_unavailable(forwarding, authority, tmp_path, monkeypatch, capsys, case):
    import hermes_constants
    from hermes_cli import kanban_origin_forward_adapter as adapter
    from hermes_cli import kanban_origin_native as native
    from hermes_cli import kanban_sync_client as sync
    from hermes_cli import profiles

    home = tmp_path / "home"
    home.mkdir()
    ledger = home / "kanban/origin-ledger.db"
    if case != "absent_parent":
        ledger.parent.mkdir()
    setup = {
        "dangling": lambda: ledger.symlink_to(home / "missing-target"),
        "directory": ledger.mkdir,
        "corrupt": lambda: ledger.write_bytes(b"not a SQLite database" * 20),
        "permission": lambda: ledger.write_bytes(authority[0].read_bytes()),
        "open_race": lambda: ledger.write_bytes(authority[0].read_bytes()),
    }
    if case in setup:
        setup[case]()

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(adapter, "_gateway", lambda _: "macbook")
    from hermes_state import SessionDB
    with SessionDB(home / 'state.db') as sessions:
        sessions.create_session('chat-1', 'desktop')
    monkeypatch.setattr(native, "lookup", lambda *a, **kw: None)
    monkeypatch.setattr(sync, "load_identity", dict)
    monkeypatch.setattr(sync, "Hub", lambda _: SimpleNamespace(snapshot_page=lambda *a, **kw: {
        "table": "leases", "rows": [authority[2]], "more": False}))
    monkeypatch.setattr(adapter.time, "time", lambda: 1)
    if case == "permission":
        original = Path.lstat

        def denied(path, *args, **kwargs):
            if path == ledger:
                raise PermissionError("inaccessible evidence")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", denied)
    elif case == "open_race":
        original_connect = sqlite3.connect

        def failed_open(database, *args, **kwargs):
            if str(database).endswith("origin-ledger.db?mode=ro"):
                raise sqlite3.OperationalError("unavailable after stat")
            return original_connect(database, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", failed_open)

    absent = case in ("absent", "absent_parent")
    for _ in range(2):
        monkeypatch.setattr(adapter.sys, "stdin", io.StringIO(json.dumps(forwarding[0])))
        assert adapter.command(SimpleNamespace(request_file="-")) == 0
        assert json.loads(capsys.readouterr().out) == {
            "status": "rejected" if absent else "pending",
            "code": "origin_forward_binding_missing" if absent else "origin_forward_evidence_unavailable",
            "handoff_complete": False,
        }
    if absent:
        assert not ledger.exists()
    with sqlite3.connect(home / "kanban/origin-forward.db") as db:
        assert db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
