"""Exercise actual argparse/dispatch for the narrow operator surface."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_diagnostic_correction_controls as controls
from tests.hermes_cli import test_kanban_edit_operator_fields as fixtures

kanban_home = fixtures.kanban_home
correction_case = controls.correction_case


def test_cli_applies_exact_request_file(correction_case, tmp_path, capsys):
    conn, tid, request = correction_case
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    assert fixtures._main(["correct-diagnostics", tid, "--request-file", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["request_id"] == request["request_id"]
    task = kb.get_task(conn, tid)
    assert task is not None and task.body == 'Diagnostic "error"'


@pytest.mark.parametrize("payload", [[], {"force": True}, {"reason": "only"}])
def test_cli_invalid_request_has_no_mutations(correction_case, tmp_path, capsys, payload):
    conn, tid, _ = correction_case
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = list(conn.iterdump())
    assert fixtures._main(["correct-diagnostics", tid, "--request-file", str(path)]) != 0
    assert "correction request" in capsys.readouterr().err
    assert list(conn.iterdump()) == before


def test_cli_worker_cannot_open_request(correction_case, monkeypatch, capsys):
    conn, tid, _ = correction_case
    monkeypatch.setenv("HERMES_KANBAN_TASK", "fixture-worker")
    before = list(conn.iterdump())
    assert fixtures._main(["correct-diagnostics", tid, "--request-file", "/nonexistent"]) != 0
    assert "non-worker operator" in capsys.readouterr().err
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("raw", [b"not json", b"\xff", b"x" * 200001], ids=["json", "utf8", "size"])
def test_cli_bad_file_bytes(correction_case, tmp_path, capsys, raw):
    conn, tid, _ = correction_case
    path = tmp_path / "bad-bytes.json"
    path.write_bytes(raw)
    before = list(conn.iterdump())
    assert fixtures._main(["correct-diagnostics", tid, "--request-file", str(path)]) == 2
    assert "invalid correction request" in capsys.readouterr().err
    assert list(conn.iterdump()) == before


def test_cli_and_db_delegation_fences(correction_case, kanban_home, monkeypatch, capsys):
    from hermes_cli.kanban_diagnostic_correction import correct_diagnostics

    conn, tid, request = correction_case
    before = list(conn.iterdump())
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(kanban_home))
    assert fixtures._main(["correct-diagnostics", tid, "--request-file", "/nonexistent"]) != 0
    assert "cannot mutate" in capsys.readouterr().err
    with pytest.raises(PermissionError, match="(?i)delegat|fenc"):
        correct_diagnostics(conn, tid, **request)
    assert list(conn.iterdump()) == before

