"""Structural input reuse must not freeze the live origin projection."""
import json
import sqlite3
from contextlib import closing

import pytest

from hermes_cli import kanban_origin_ledger as ledger
from tests.hermes_cli import test_kanban_origin_ledger as fixtures

world = fixtures.world


def _prepare(world):
    path, replica, binding = world
    fixtures.publish(replica, binding)
    fixtures.accept(world)
    with sqlite3.connect(replica) as conn:
        conn.execute('CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)')
        conn.execute("INSERT INTO meta VALUES ('last_ok', '1000')")
        info = {'gateway': 'owner', 'task_scopes': [
            {'profile': 'work', 'board': 'build', 'state': 'ok', 'observed_at': 1000}]}
        conn.execute("INSERT INTO replica VALUES ('owner', 'gateway', 'info', 1, ?)",
                     (json.dumps(info),))
    with sqlite3.connect(path) as conn:
        raw = conn.execute('SELECT binding_json FROM origin_obligations').fetchone()[0]
    return path, replica, raw


def test_unchanged_binding_is_not_decoded_again_after_a_minute(world, monkeypatch):
    path, replica, raw = _prepare(world)
    calls = []
    original = json.loads

    def counted(value, *args, **kwargs):
        if value == raw:
            calls.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, 'loads', counted)
    first = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1001)
    calls.clear()
    second = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1061)
    assert second == first
    assert calls == [], 'unchanged structural binding decoded again'


def test_structural_reuse_does_not_hide_ledger_edit_or_result_mutation(world):
    path, replica, _ = _prepare(world)
    args = dict(replica=replica, gateway='origin', profile='default', now=1001)
    first = ledger.project(path, **args)
    first[0]['title'] = 'caller mutation'
    first[0]['legacy_aliases'].append('caller mutation')
    second = ledger.project(path, **args)
    assert second[0]['title'] != 'caller mutation'
    assert 'caller mutation' not in second[0]['legacy_aliases']
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE origin_obligations SET resolved_session='s_new_tip'")
    assert ledger.project(path, **args)[0]['session'] == 's_new_tip'
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE origin_obligations SET binding_json='{broken'")
    with pytest.raises(ValueError):
        ledger.project(path, **args)


def test_binding_cache_returns_owned_nested_mappings_and_retires_churn(world):
    from hermes_cli import kanban_origin_structure as structure

    _, _, raw = _prepare(world)
    structure.clear()
    original = json.loads(raw)
    value = structure.binding(raw)
    value['owner']['board'] = 'mutated'
    value['origin']['session'] = 'mutated'
    assert structure.binding(raw) == original
    for version in range(1, 2200):
        changed = {**original, 'version': version}
        text = json.dumps(changed)
        assert structure.binding(text) == changed
        assert structure.digest(text, changed) == ledger.binding_hash(changed)
    assert structure._binding.cache_info().currsize == 1024
    assert structure._digest.cache_info().currsize == 1024
    # Sustained churn still admits the most recent generation.
    before = structure._binding.cache_info().hits
    structure.binding(text)
    assert structure._binding.cache_info().hits == before + 1
    structure.clear()
    assert structure._binding.cache_info().currsize == 0
    assert structure._digest.cache_info().currsize == 0


def test_large_and_nontext_binding_inputs_are_not_retained(world):
    from hermes_cli import kanban_origin_structure as structure

    _, _, raw = _prepare(world)
    structure.clear()
    for text in (raw.encode(), ' ' * 5000 + raw):
        assert structure.binding(text) == json.loads(raw)
        assert structure.digest(text, json.loads(raw)) == ledger.binding_hash(json.loads(raw))
    assert structure._binding.cache_info().currsize == 0
    assert structure._digest.cache_info().currsize == 0


def test_exact_binding_text_changes_hash_and_origin_without_reset(world, monkeypatch):
    from hermes_cli import kanban_origin_structure as structure

    path, replica, raw = _prepare(world)
    structure.clear()
    original = json.loads(raw)
    first_digest = structure.digest(raw, original)
    changed = {**original, 'origin': {**original['origin'], 'session': 's_changed'}}
    changed_raw = json.dumps(changed)
    expected = ledger.binding_hash(changed)
    assert expected != first_digest
    calls = []
    real_hash = ledger.binding_hash

    def counted(value):
        calls.append(value)
        return real_hash(value)

    monkeypatch.setattr(ledger, 'binding_hash', counted)
    assert structure.digest(changed_raw, changed) == expected
    assert len(calls) == 1
    calls.clear()
    assert structure.digest(changed_raw, changed) == expected
    assert calls == []
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('UPDATE origin_obligations SET binding_json=?', (changed_raw,))
    rows = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1001)
    assert rows[0]['origin_session'] == 's_changed'
    assert structure.digest(raw, original) == first_digest


def test_warm_binding_does_not_freeze_freshness(world):
    path, replica, _ = _prepare(world)
    args = dict(replica=replica, gateway='origin', profile='default')
    first = ledger.project(path, **args, now=1001)
    assert first[0]['status'] != 'unknown'
    stale = ledger.project(path, **args, now=2000)
    assert stale[0]['status'] == 'unknown'
    assert ledger.project(path, **args, now=1001) == first
