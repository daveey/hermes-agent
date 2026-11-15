"""Review handoffs digest and preserve large artifacts before taking the board lock."""
if not __name__.startswith("tests."):
    raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")

import hashlib
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_metadata_files as helpers

kanban_home = helpers.kanban_home


@pytest.mark.parametrize("in_scratch", [True, False])
def test_review_large_artifact_is_read_and_copied_before_transaction(
    kanban_home, monkeypatch, tmp_path, in_scratch,
):
    from hermes_cli import kanban_artifact_prestage as prestage

    # Exercise the large-file branch without allocating a production-sized blob.
    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 4)
    content = b"large declared review deliverable"
    digest = hashlib.sha256(content).hexdigest()
    with kbc.connect_closing() as conn:
        task, ws, run = helpers._scratch_card(conn)
        source = (ws if in_scratch else tmp_path) / "review.bin"
        source.write_bytes(content)
        observations = []
        read = prestage._read

        def observe_read(path, dest):
            before = conn.in_transaction
            result = read(path, dest)
            observations.append((path, before, conn.in_transaction, result))
            if dest is not None:
                assert dest.read_bytes() == content
                assert not conn.in_transaction, "large copy completed under the board lock"
            return result

        monkeypatch.setattr(prestage, "_read", observe_read)
        error = None
        try:
            ok = kb.request_review(
                conn, task, summary="Review deliverable", expected_run_id=run,
                metadata={"artifacts": [str(source)]},
            )
        except kb.ArtifactPreservationError as exc:
            error = exc
            ok = False
        # Assert the actual IO observation first: dropping prestaging must fail
        # this assertion, not merely throw an unrelated transition/setup error.
        assert observations, "review handoff did not pre-read its large declared artifact"
        assert observations == [(source, False, False, (prestage.signature(source.stat()), digest))]
        assert error is None, str(error)
        assert ok
        assert kb.get_task(conn, task).status == "review"
        handoff = kb.latest_run(conn, task)
        assert handoff.outcome == "review_requested"
        record, = handoff.metadata["artifact_receipt"]
        assert record["path"] == str(source)
        assert record["size"] == len(content)
        assert record["sha256"] == digest
        assert source.read_bytes() == content
        if in_scratch:
            retained = Path(record["retained_path"])
            assert retained.is_relative_to(kb.task_attachments_dir(task))
            assert retained.read_bytes() == content
            assert list(retained.parent.iterdir()) == [retained]
        else:
            assert not record.get("retained_path")
