"""Live and replica card detail preserve recorded pacing without local capacity claims. [daveey]"""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_queue_position as queue
from tests.plugins.test_kanban_foreign_card_api import (
    make_replica_card, enable_sync, _live_card,
)
from tests.plugins.test_kanban_foreign_card_api import api as api, kanban_home as kanban_home  # noqa: PLC0414 -- fixtures


# A hold three days out: still active, and never today (today's holds print without a date). A fixed epoch went
# red at its own UTC date and would drop the hold once it passed. [daveey]
_UNTIL = time.time() + 3 * 86400
_UNTIL_TEXT = time.strftime('%Y-%m-%d %H:%MZ', time.gmtime(int(_UNTIL)))


@pytest.mark.parametrize('replica', [False, True])
def test_card_detail_names_pacing_provider_and_reset(kanban_home, api, monkeypatch, replica):
    tid = make_replica_card(kanban_home) if replica else _live_card()
    if replica:
        enable_sync(monkeypatch)
        monkeypatch.setattr(queue, '_position', lambda *_a, **_kw: pytest.fail('replica evaluated reader placement'))
    path = kanban_home / 'kanban' / sync.SHADOW_DIR / 'default.db' if replica else None
    with kbc.connect(path) as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'dispatch_skipped', {
                'reason': 'llm_paced', 'detail': 'xai-oauth held', 'until': _UNTIL,
                'providers': [{'provider': 'xai-oauth', 'state': 'held', 'limit': 0}]})
    result = api.get(f'/api/plugins/kanban/tasks/{tid}')
    assert result.status_code == 200, result.text
    body = result.json()
    assert body['source'] == ('replica' if replica else 'live')
    assert body['queue']['lane'] == 'llm_pacing'
    assert f'LLM quota pacing (xai-oauth held pool) until {_UNTIL_TEXT}' in body['queue_note']
    assert 'worker' not in body['queue_note'] and 'slot' not in body['queue_note']
    if replica:
        assert 'capacity_note' not in body and 'wait_observation' not in body
