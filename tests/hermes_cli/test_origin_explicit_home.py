"""Explicit owner-board overlays use only the caller's home, never process scope."""
from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import kanban_origin_local as local
from hermes_cli import kanban_origin_ledger as ledger


def test_explicit_local_binding_reads_authorized_board(tmp_path, monkeypatch):
    home = tmp_path / 'authorized'
    home.mkdir()
    binding = {'owner': {'gateway': 'gw', 'profile': 'research', 'board': 'default', 'task_id': 't_owned'},
               'origin': {'gateway': 'gw', 'profile': 'research', 'session': 'chat'}}
    with sqlite3.connect(home / 'kanban.db') as conn:
        conn.execute('CREATE TABLE tasks (id TEXT, status TEXT)')
        conn.execute("INSERT INTO tasks VALUES ('t_owned', 'running')")
        conn.execute('CREATE TABLE origin_creation_intents (task_id TEXT, binding_json TEXT)')
        conn.execute('INSERT INTO origin_creation_intents VALUES (?, ?)', ('t_owned', json.dumps(binding)))

    def forbidden(*args, **kwargs):
        pytest.fail('explicit local owner overlay consulted process home')

    monkeypatch.setattr('hermes_constants.get_hermes_home', forbidden)
    monkeypatch.setattr('hermes_cli.profiles.get_active_profile_name', forbidden)
    monkeypatch.setattr('hermes_cli.kanban_db.kanban_home', forbidden)
    monkeypatch.setattr('hermes_cli.kanban_db.kanban_db_path', forbidden)
    monkeypatch.setattr(ledger, 'observe_owner_task', lambda conn, task, now: task)
    row = local.observe_local_binding(binding, profile='research', now=100, home=home)
    assert (row['id'], row['status']) == ('t_owned', 'running')
    assert local.observe_local_binding(binding, profile='other', now=100, home=home) is None
    # Existing matching-marker requirement remains authoritative even inside a trusted home.
    with sqlite3.connect(home / 'kanban.db') as conn:
        conn.execute('DELETE FROM origin_creation_intents')
    assert local.observe_local_binding(binding, profile='research', now=100, home=home) is None


def test_explicit_local_overlay_refuses_outside_board(tmp_path):
    home = tmp_path / 'authorized'
    home.mkdir()
    (home / 'kanban.db').symlink_to(tmp_path / 'foreign.db')
    binding = {'owner': {'profile': 'research', 'board': 'default', 'task_id': 't_owned'}}
    assert local.observe_local_binding(binding, profile='research', now=100, home=home) is None
    binding['owner']['board'] = '../foreign'
    assert local.observe_local_binding(binding, profile='research', now=100, home=home) is None
