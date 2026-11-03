"""Workspace files named in handoff metadata outlive scratch cleanup. [daveey]"""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard


import json
import logging
import os
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_metadata_files as kmf


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _scratch_card(conn, title="replay"):
    t = kb.create_task(conn, title=title, assignee="default")
    ws = kbw.resolve_workspace(kb.get_task(conn, t))
    kbw.set_workspace_path(conn, t, ws)
    assert kb.claim_task(conn, t)
    return t, ws, kb.get_task(conn, t).current_run_id


def test_metadata_named_workspace_file_survives_reaper(kanban_home):
    with kbc.connect_closing() as conn:
        t, ws, run_id = _scratch_card(conn)
        report = ws / "NATIVE_REPLAY_REPORT_V4.md"
        report.write_text("| round | cpu_ms |\n| 1 | 42 |\n")
        (ws / "PLAN.md").write_text("plan\n")
        meta = {
            "replay_report": str(report),
            "notes": f"plan at {ws}/PLAN.md.",
            "replay_cpu_ms_per_120s": {"main": 42},
        }
        assert kb.complete_task(conn, t, summary="cut idle CPU", metadata=meta, expected_run_id=run_id)
        attachments = kb.list_attachments(conn, t)

    assert not ws.exists(), "the scratch workspace is still reaped"
    payload = json.loads(kc.run_slash(f"show {t} --json"))
    run_meta = payload["runs"][-1]["metadata"]
    kept = Path(run_meta["replay_report"])
    assert kept.parent == kb.task_attachments_dir(t)
    assert kept.read_text() == "| round | cpu_ms |\n| 1 | 42 |\n"
    plan = Path(run_meta["attached_from_workspace"][f"{ws}/PLAN.md"])
    assert run_meta["notes"] == f"plan at {plan}."
    assert plan.read_text() == "plan\n"
    assert run_meta["attached_from_workspace"][str(report)] == str(kept)
    assert run_meta["replay_cpu_ms_per_120s"] == {"main": 42}
    assert sorted(a.filename for a in attachments) == ["NATIVE_REPLAY_REPORT_V4.md", "PLAN.md"]


def test_files_over_total_cap_are_skipped_with_logged_reason(kanban_home, monkeypatch, caplog):
    monkeypatch.setattr(kmf, "METADATA_FILES_MAX_BYTES", 100)
    with kbc.connect_closing() as conn:
        t, ws, run_id = _scratch_card(conn)
        small, big = ws / "small.json", ws / "big.jsonl"
        small.write_text("{}")
        big.write_bytes(b"x" * 200)
        meta = {"rounds": str(big), "summary_file": str(small)}
        with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_metadata_files"):
            assert kb.complete_task(conn, t, summary="done", metadata=meta, expected_run_id=run_id)
        run = kb.latest_run(conn, t)

    assert run.metadata["rounds"] == str(big), "a skipped file keeps its original citation"
    assert "total cap" in run.metadata["attached_from_workspace_skipped"][str(big)]
    assert any(str(big) in r.getMessage() and "total cap" in r.getMessage() for r in caplog.records)
    assert Path(run.metadata["summary_file"]).read_text() == "{}"
    assert str(big) not in run.metadata["attached_from_workspace"]


def test_non_workspace_paths_are_untouched(kanban_home, tmp_path):
    outside = tmp_path / "elsewhere" / "report.md"
    outside.parent.mkdir()
    outside.write_text("not ours")
    with kbc.connect_closing() as conn:
        t, _ws, run_id = _scratch_card(conn)
        meta = {"report": str(outside), "pr": "https://github.com/daveey/hermes/pull/1", "n": 3}
        assert kb.complete_task(conn, t, summary="done", metadata=meta, expected_run_id=run_id)
        run = kb.latest_run(conn, t)
        assert kb.list_attachments(conn, t) == []
    assert run.metadata == {"report": str(outside), "pr": "https://github.com/daveey/hermes/pull/1", "n": 3}
    assert outside.read_text() == "not ours"
    assert not kb.task_attachments_dir(t).exists()


def test_review_handoff_keeps_metadata_named_file_past_reviewer_completion(kanban_home):
    with kbc.connect_closing() as conn:
        t, ws, run_id = _scratch_card(conn)
        raw = ws / "replay.jsonl"
        raw.write_text('{"round": 1}\n')
        assert kb.request_review(conn, t, summary="ready", metadata={"raw": str(raw)}, expected_run_id=run_id)
        assert kb.complete_task(conn, t, summary="approved")
        handoff = [r for r in kb.list_runs(conn, t) if r.outcome == "review_requested"][-1]
    assert not ws.exists()
    assert Path(handoff.metadata["raw"]).read_text() == '{"round": 1}\n'


def test_file_both_declared_and_named_is_copied_once(kanban_home):
    with kbc.connect_closing() as conn:
        t, ws, run_id = _scratch_card(conn)
        chart = ws / "chart.png"
        chart.write_bytes(b"png")
        meta = {"artifacts": [str(chart)], "chart": str(chart)}
        assert kb.complete_task(conn, t, summary="done", metadata=meta, expected_run_id=run_id)
        run = kb.latest_run(conn, t)
        attachments = kb.list_attachments(conn, t)
    assert [a.filename for a in attachments] == ["chart.png"]
    assert run.metadata["chart"] == run.metadata["artifacts"][0]
    assert Path(run.metadata["chart"]).read_bytes() == b"png"


def test_workspace_paths_that_resolve_outside_are_not_copied(kanban_home, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    with kbc.connect_closing() as conn:
        t, ws, run_id = _scratch_card(conn)
        link = ws / "link.txt"
        link.symlink_to(secret)
        dotted = f"{ws}/{os.path.relpath(secret, ws)}"
        meta = {"link": str(link), "dotted": dotted}
        assert kb.complete_task(conn, t, summary="done", metadata=meta, expected_run_id=run_id)
        run = kb.latest_run(conn, t)
        assert kb.list_attachments(conn, t) == []
    assert run.metadata["link"] == str(link)
    assert run.metadata["dotted"] == dotted
    skipped = run.metadata[kmf.SKIPPED_KEY]
    assert "outside" in skipped[str(link)] and "outside" in skipped[dotted], skipped
    assert not kb.task_attachments_dir(t).exists()


def _spaced(conn):
    t, ws, run_id = _scratch_card(conn)
    report = ws / "replay report v2.md"
    report.write_text("rows\n")
    assert kb.complete_task(conn, t, summary="done", metadata={"report": str(report)}, expected_run_id=run_id)
    return t, kb.latest_run(conn, t)


def test_a_whole_value_path_with_spaces_is_copied(kanban_home):
    with kbc.connect_closing() as conn:
        t, run = _spaced(conn)
        attachments = kb.list_attachments(conn, t)
    kept = Path(run.metadata["report"])
    assert kept.parent == kb.task_attachments_dir(t)
    assert kept.read_text() == "rows\n"
    assert [a.filename for a in attachments] == ["replay report v2.md"], "copied once"


def test_a_whole_value_path_with_spaces_records_no_phantom_skip(kanban_home):
    with kbc.connect_closing() as conn:
        _t, run = _spaced(conn)
    assert kmf.SKIPPED_KEY not in run.metadata, run.metadata.get(kmf.SKIPPED_KEY)


def test_a_value_naming_two_paths_copies_both(kanban_home):
    with kbc.connect_closing() as conn:
        t, ws, run_id = _scratch_card(conn)
        (ws / "a.md").write_text("a\n")
        (ws / "b.md").write_text("b\n")
        meta = {"files": f"{ws}/a.md {ws}/b.md"}
        assert kb.complete_task(conn, t, summary="done", metadata=meta, expected_run_id=run_id)
        run = kb.latest_run(conn, t)
        attachments = kb.list_attachments(conn, t)
    assert kmf.SKIPPED_KEY not in run.metadata, run.metadata.get(kmf.SKIPPED_KEY)
    assert sorted(a.filename for a in attachments) == ["a.md", "b.md"]
    a, b = (Path(p) for p in run.metadata["files"].split(" "))
    assert (a.read_text(), b.read_text()) == ("a\n", "b\n")
    assert a.parent == b.parent == kb.task_attachments_dir(t)


def _assert_named_file_left_alone(conn, t, ws, run_id):
    report = ws / "REPORT.md"
    report.write_text("rows\n")
    meta = {"report": str(report), "notes": f"see {report}."}
    assert kb.complete_task(conn, t, summary="done", metadata=meta, expected_run_id=run_id)
    run = kb.latest_run(conn, t)
    assert kb.list_attachments(conn, t) == []
    assert run.metadata == meta, "neither copied nor rewritten"
    assert not kb.task_attachments_dir(t).exists()
    assert report.read_text() == "rows\n"


@pytest.mark.parametrize("kind", ["dir", "worktree"])
def test_dir_and_worktree_workspace_files_are_not_copied(kanban_home, kind):
    # Under the managed workspaces root, so only the workspace kind keeps them out.
    ws = kanban_home / "kanban" / "workspaces" / f"shared-{kind}"
    ws.mkdir(parents=True)
    with kbc.connect_closing() as conn:
        t = kb.create_task(conn, title="replay", assignee="default")
        # Set the kind directly: creating one would also check host capabilities.
        conn.execute(
            "UPDATE tasks SET workspace_kind = ?, workspace_path = ? WHERE id = ?", (kind, str(ws), t),
        )
        conn.commit()
        assert kb.claim_task(conn, t)
        _assert_named_file_left_alone(conn, t, ws, kb.get_task(conn, t).current_run_id)


def test_scratch_workspace_outside_managed_storage_is_not_copied(kanban_home, tmp_path):
    ws = tmp_path / "checkout"
    ws.mkdir()
    with kbc.connect_closing() as conn:
        t = kb.create_task(conn, title="replay", assignee="default")
        kbw.set_workspace_path(conn, t, ws)
        assert kb.claim_task(conn, t)
        _assert_named_file_left_alone(conn, t, ws, kb.get_task(conn, t).current_run_id)
