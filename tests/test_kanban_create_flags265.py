"""POST preserves mandatory fields until refusal, before opening a board."""
from __future__ import annotations
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import pytest
from tests import test_kanban_extraction141_rest as f

home = f.home
client = f.client

@pytest.mark.parametrize('flag', f.FLAGS)
def test_create_refuses_flags_before_any_board_effect(client, monkeypatch, flag):
    before = f._snapshot()
    opened = f._record_board_conn(monkeypatch)
    for value in f.REFUSED_VALUES:
        response = client.post('/tasks', json={'title': 'Rejected requirement', flag: value})
        assert response.status_code == 400, (flag, value, response.text)
        assert 'model-neutral' in response.text
        assert opened == []
        assert f._snapshot() == before

@pytest.mark.parametrize('value', f.ACCEPTED_VALUES)
def test_create_inert_flags_never_reach_db(client, monkeypatch, value):
    seen = f._record_create_kwargs(monkeypatch)
    response = client.post('/tasks', json={'title': 'Inert requirement', 'assignee':'worker',
        'model_override':'class:deep', 'provider_override':'auto', **{key:value for key in f.FLAGS}})
    assert response.status_code == 200, response.text
    assert len(seen) == 1 and not set(f.FLAGS) & set(seen[0])
    row = f._task_row(response.json()['task']['id'])
    assert row['model_override'] == 'class:deep' and row['model_pin_mandatory'] is None

META_FLAGS = ('pin_mandatory', 'require_model')  # the nested spellings PATCH and bulk already refuse

@pytest.mark.parametrize('flag', META_FLAGS)
def test_create_refuses_metadata_flags_before_any_board_effect(client, monkeypatch, flag):
    before = f._snapshot()
    opened = f._record_board_conn(monkeypatch)
    for value in f.REFUSED_VALUES:
        response = client.post('/tasks', json={'title': 'Rejected nested requirement', 'metadata': {flag: value}})
        assert response.status_code == 400, (flag, value, response.text)
        assert 'model-neutral' in response.text
        assert opened == []
        assert f._snapshot() == before

@pytest.mark.parametrize('value', f.ACCEPTED_VALUES)
def test_create_inert_metadata_never_reaches_db(client, monkeypatch, value):
    seen = f._record_create_kwargs(monkeypatch)
    response = client.post('/tasks', json={'title': 'Inert nested requirement', 'assignee': 'worker',
        'model_override': 'class:deep', 'metadata': {'note': 'kept out', **{key: value for key in META_FLAGS}}})
    assert response.status_code == 200, response.text
    assert len(seen) == 1 and 'metadata' not in seen[0]
    row = f._task_row(response.json()['task']['id'])
    assert row['model_override'] == 'class:deep' and row['model_pin_mandatory'] is None
