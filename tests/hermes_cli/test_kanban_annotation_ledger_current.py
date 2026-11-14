"""Raw owner-current equality survives display-only task normalization."""
import json

import pytest

from hermes_cli import kanban_annotation_ledger as ledger
from hermes_cli import kanban_annotation_semantic as semantic
from tests.hermes_cli.test_kanban_annotation_ledger import _add, _binding, _conn, _snap
from tests.hermes_cli.test_kanban_annotation_semantic import signed
from tests.hermes_cli.test_kanban_annotation_semantic_rows import view


def setup_snapshot(tmp_path):
    conn = _conn(tmp_path)
    task = signed(id="t1")
    _add(conn, _binding(), task=json.dumps(task), version=1, released=1)
    return _snap(conn), task, ("ogw", "op/board/t1")


def test_matching_is_not_freshness_authority(tmp_path):
    snapshot, task, key = setup_snapshot(tmp_path)
    assert ledger.current_matches(snapshot, view(1, {key: task})) == ((snapshot.obligations[0]["binding_id"], True),)


def test_liveness_changes_still_invalidate_raw_current_verdict(tmp_path):
    snapshot, task, key = setup_snapshot(tmp_path)
    changed = signed(id="t1", activity_at=2)
    assert semantic.task_token(task) == semantic.task_token(changed)
    before = ledger.current_matches(snapshot, view(1, {key: task}))
    after = ledger.current_matches(snapshot, view(1, {key: changed}))
    assert before != after and after[0][1] is False


def test_version_only_and_absence(tmp_path):
    snapshot, task, key = setup_snapshot(tmp_path)
    assert ledger.current_matches(snapshot, view(2, {key: task}))[0][1] is False
    assert ledger.current_matches(snapshot, view(1, {}))[0][1] is False


def test_json_formatting_not_raw_byte_equality(tmp_path):
    snapshot, task, key = setup_snapshot(tmp_path)
    rows = view(1, {key: task})
    from hermes_cli.kanban_replica_rows import pack
    rows.by_key[key] = (*key, pack(json.dumps(task, indent=3)), 0, 1)
    assert ledger.current_matches(snapshot, rows)[0][1] is True


@pytest.mark.parametrize("bad", ["{", "null", "[]", '{"extra":NaN}'])
def test_bad_ledger_task_unkeyable(tmp_path, bad):
    conn = _conn(tmp_path)
    _add(conn, _binding(), task=bad, version=1)
    snapshot = _snap(conn)
    with pytest.raises(ValueError):
        ledger.current_matches(snapshot, view(1, {}))


def test_no_ledger_writes(tmp_path):
    snapshot, task, key = setup_snapshot(tmp_path)
    # The operation consumes immutable snapshots only; it has no connection.
    result = ledger.current_matches(snapshot, view(1, {key: task}))
    assert isinstance(result, tuple) and result[0][1] is True
