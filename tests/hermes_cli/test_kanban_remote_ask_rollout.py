import time
import pytest
from tests.tui_gateway import test_kanban_remote_wake as remote
from tui_gateway import kanban_unloaded as ku
from tui_gateway import kanban_remote_notices as krn

home = remote.home


def _sweeper(monkeypatch):
    wakes = []
    def wake(tip, text, metadata):
        wakes.append(tip); remote._turn_writes_row(tip, text, metadata); return 'started'
    monkeypatch.setattr(ku, 'wake_turn', wake)
    monkeypatch.setattr(ku, '_spawn_thread', lambda job: job())
    return wakes, lambda: ku.sweep_remote_once(set(), grace_s=0, gateway='macbook')


@pytest.mark.parametrize('human', [True, False])
@pytest.mark.parametrize('age', [600, 5 * 86400])
def test_rollout_blocked_card_gains_ask_notice(home, monkeypatch, human, age):
    at = time.time() - age
    card = remote._blocked_on_david('t_blk', at)
    if not human:
        card.pop('human_request'); card['block_kind'] = 'dependency'
    remote._publish(home, [card], arrived_ago=age)
    wakes, sweep = _sweeper(monkeypatch)
    sweep()
    before = len(remote._notice_rows())
    # owner upgrades: same block, now with an ask envelope
    card2 = {**card, 'ask_notice': {'event_id': 4242, 'kind': 'blocked', 'created_at': at, 'payload': {'reason': 'x'}}}
    remote._publish(home, [card2], arrived_ago=0)
    sweep(); sweep()
    after = len(remote._notice_rows())
    print('HUMAN', human, 'AGE', age, 'BEFORE', before, 'AFTER', after, 'WAKES', wakes)
    assert after == before, (before, after)
