"""Detached canonical projection: no source access or implicit clock reads."""
from copy import deepcopy

from hermes_cli import kanban_session_activity as activity
from tests.hermes_cli.test_kanban_annotation_facts import forbidden, no_io


def test_captured_projection_matches_canonical_lineage_and_external(monkeypatch):
    from hermes_cli import kanban_activity_projection as projection
    monkeypatch.setattr(activity.time, 'time', lambda: 1000.0)
    keys = {('default/t1', 'active', True), ('remote/p/default/t2', 'blocked', False)}
    parts = ({'s': 1, 'ancestor': 1}, {'s': 1, 'ancestor': 1}, {'s': 1}, {'s': 1},
             {'s': {'t1'}, 'ancestor': {'t1'}}, {'s': 1, 'ancestor': 1}, {}, {},
             {'s': keys, 'ancestor': keys}, {'s': {'default/t1', 'extra/t3'}}, {'s': 1}, {})
    external = {'ancestor': {'counts': [1, 1, 0], 'cards': [
        {'task_id': 't1', 'title': 'waiting', 'external': {'state': 'external_unknown', 'observed_at': 990}}]}}
    monkeypatch.setattr(activity, '_annotation_snapshot', lambda **kwargs: parts)
    monkeypatch.setattr(activity._annotation_copy, 'observed_at', lambda parts: 998.0)
    monkeypatch.setattr(activity._bounded, 'unavailable', lambda *args: False)
    def stamp(rows, *args):
        activity._external_activity.stamp_rows(rows, external, activity._identity.row_ids)
        return rows
    monkeypatch.setattr(activity._external_activity, 'stamp_boards', stamp)
    source = [{'id': 's', '_lineage_ids': ['ancestor'], 'title': 'keep',
               'execution_observations': [{'source': 'foreign', 'state': 'waiting'}]},
              {'id': 'empty', 'kanban_held': 9, 'kanban_unknown': True, 'kanban_external': 2}]
    expected = activity.annotate_kanban_activity.__wrapped__(deepcopy(source))
    assert expected[0]['kanban_active'] == 2
    assert expected[0]['kanban_blocked'] == 1
    assert expected[0]['kanban_waiting_on'] == ['extra/t3', 't1']
    assert expected[0]['kanban_external_unknown'] == 1
    assert expected[0]['execution_observations'][0]['source'] == 'foreign'
    assert 'kanban_held' not in expected[1]
    monkeypatch.setattr(activity, '_annotation_snapshot', forbidden)
    monkeypatch.setattr(activity, '_open_ro', forbidden)
    monkeypatch.setattr(activity, 'attention_task_counts', forbidden)
    monkeypatch.setattr(activity.time, 'time', forbidden)
    rows = deepcopy(source)
    before = deepcopy(parts)
    with no_io(monkeypatch):
        projection.project_kanban_activity(rows, parts=parts, observed_at=expected[0]['kanban_observed_at'],
                                           now=1000.0, external=external)
    assert rows == expected
    assert parts == before


def test_unavailable_projection_preserves_canonical_unknown(monkeypatch):
    from hermes_cli import kanban_activity_projection as projection
    monkeypatch.setattr(activity.time, 'time', lambda: 1000.0)
    source = [{'id': 's', 'kanban_active': 1, 'kanban_waiting_on': ['t1'], 'kanban_observed_at': 900,
               'execution_observations': [{'source': 'foreign', 'state': 'waiting'}]}]
    expected = activity._mark_kanban_unknown(deepcopy(source))
    monkeypatch.setattr(activity.time, 'time', forbidden)
    rows = deepcopy(source)
    with no_io(monkeypatch):
        projection.project_kanban_unknown(rows, now=1000.0)
    assert rows == expected
    assert rows[0]['kanban_unknown'] is True
    assert 'kanban_active' not in rows[0]
