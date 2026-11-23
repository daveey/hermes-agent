"""Actual parser/dispatch coverage for trusted owner-local text CAS."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_edit_operator_fields as fixtures
from tests.hermes_cli.test_kanban_text_edit import request

kanban_home = fixtures.kanban_home


def test_cli_apply_replay_without_init(kanban_home, tmp_path, monkeypatch, capsys):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Text fixture", body="old")
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request()))
    monkeypatch.setattr(kb, "init_db", lambda: pytest.fail("must not initialize board"))
    assert fixtures._main(["edit-text", tid, "--request-file", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["disposition"] == "applied" and result["event_uid"]
    with kbc.connect_closing() as conn:
        snapshot = list(conn.iterdump())
    assert fixtures._main(["edit-text", tid, "--request-file", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["disposition"] == "replayed"
    with kbc.connect_closing() as conn:
        assert list(conn.iterdump()) == snapshot


@pytest.mark.parametrize("raw", ['{"request_id":"a","request_id":"b"}', '{"expected":{"title":1,"title":2}}',
                                 '{"value":NaN}', '{"value":Infinity}', 'x' * 500001, '\ud800'],
                         ids=["duplicate-root", "duplicate-nested", "nan", "infinity", "oversize", "surrogate"])
def test_cli_bad_file_no_write(kanban_home, tmp_path, capsys, raw):
    path = tmp_path / "bad.json"
    path.write_bytes(raw.encode("utf-8", errors="surrogatepass"))
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Text fixture", body="old")
        before = list(conn.iterdump())
    assert fixtures._main(["edit-text", tid, "--request-file", str(path)]) == 2
    assert json.loads(capsys.readouterr().out)["disposition"] == "invalid_request"
    with kbc.connect_closing() as conn:
        assert list(conn.iterdump()) == before


def test_cli_worker_denied(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_WORKER_TASK", "fixture-worker")
    assert fixtures._main(["edit-text", "fixture", "--request-file", "/nonexistent"]) != 0


def test_cli_stale_preserves_snapshot(kanban_home, tmp_path, capsys):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Text fixture", body="concurrent")
        before = list(conn.iterdump())
    path = tmp_path / "stale.json"
    path.write_text(json.dumps(request()))
    assert fixtures._main(["edit-text", tid, "--request-file", str(path)]) == 1
    assert json.loads(capsys.readouterr().out)["disposition"] == "stale_preimage"
    with kbc.connect_closing() as conn:
        assert list(conn.iterdump()) == before
