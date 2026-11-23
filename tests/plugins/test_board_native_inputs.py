"""Real board-source captures and memory-only canonical renderers. [daveey]"""
import builtins
import json
import socket
import subprocess
import time

import pytest

from hermes_cli.fact_contract import freeze
from plugins.kanban.dashboard import board_fact_views
from tests.plugins import test_kanban_board_shadow as helpers

shadow_board = helpers.shadow_board


@pytest.mark.parametrize('archived,cap', [(False, 0), (False, 1), (True, 80)])
def test_native_board_capture_has_typed_detached_parity(shadow_board, monkeypatch, archived, cap):
    from hermes_cli import fact_board_native as board_native_inputs
    monkeypatch.setattr(time, 'time', lambda: 1_700_000_000.0)
    api = shadow_board['mod']
    selector = board_fact_views.board_selector(include_archived=archived, done_limit=cap)
    expected = helpers._read(shadow_board, archived=archived, done_limit=cap)
    captured = freeze(board_native_inputs.capture(api, None, 'board', selector))
    assert captured['observations']['local']['content'] is not None
    assert 'replica' in captured['observations']

    def forbidden(*args, **kwargs):
        pytest.fail('board renderer reopened a source')

    with monkeypatch.context() as guard:
        guard.setattr(builtins, 'open', forbidden)
        guard.setattr(socket, 'socket', forbidden)
        guard.setattr(subprocess, 'Popen', forbidden)
        guard.setattr(api, '_conn', forbidden)
        actual = board_native_inputs.project(api, 'board', captured['value'])
    assert json.dumps(actual, separators=(',', ':')) == json.dumps(expected, separators=(',', ':'))


def test_native_capture_unknown_view_refuses_before_source_read(shadow_board, monkeypatch):
    from hermes_cli import fact_board_native as board_native_inputs
    monkeypatch.setattr(shadow_board['mod'], '_conn', lambda *a, **k: pytest.fail('unexpected source read'))
    with pytest.raises(ValueError, match='unknown board view'):
        board_native_inputs.capture(shadow_board['mod'], None, 'unknown', {})


def test_board_input_bounds_body_before_publication_without_changing_wire(shadow_board, monkeypatch):
    from contextlib import closing
    from hermes_cli import kanban_db_connect
    from hermes_cli import fact_board_native as board_native_inputs
    from plugins.kanban.dashboard import board_read
    monkeypatch.setattr(time, 'time', lambda: 1_700_000_000.0)
    with closing(kanban_db_connect.connect()) as conn, conn:
        conn.execute('UPDATE tasks SET body = ? WHERE id = ?', ('body' * 100_000, shadow_board['live_id']))
    expected = helpers._read(shadow_board)
    captured = board_native_inputs.capture(shadow_board['mod'], None, 'board', board_fact_views.board_selector())
    fields = captured['value']['local']['rows']['cards'][0][0]
    assert len(fields['body']) == board_read._CARD_SUMMARY_PREVIEW_CHARS
    assert fields['body_truncated'] is True
    assert board_native_inputs.project(shadow_board['mod'], 'board', freeze(captured['value'])) == expected


@pytest.mark.parametrize('view,selector', [('projects', {}), ('boards', {'include_archived': False}),
                                           ('boards', {'include_archived': True})])
def test_native_catalog_capture_retains_canonical_fields_order_and_nulls(shadow_board, view, selector):
    from hermes_cli import projects_db
    from hermes_cli import fact_board_native as board_native_inputs
    with projects_db.connect_closing() as conn:
        projects_db.create_project(conn, name='Zeta', slug='zeta', icon=None, color=None)
        projects_db.create_project(conn, name='Alpha', slug='alpha', icon='tree', color='blue')
        conn.commit()
    api = shadow_board['mod']
    expected = api.list_kanban_projects() if view == 'projects' else api.list_boards(**selector)
    captured = freeze(board_native_inputs.capture(api, None, view, selector))
    actual = board_native_inputs.project(api, view, captured['value'])
    assert json.dumps(actual, separators=(',', ':')) == json.dumps(expected, separators=(',', ':'))
