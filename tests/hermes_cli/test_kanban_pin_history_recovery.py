"""Historical pin-wall receipts are not live quota evidence after pool recovery."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_llm_pacing_dispatch as lpd
from hermes_cli import kanban_pin_reroute as pr
from hermes_cli import kanban_provider_walls as walls
from tests.hermes_cli import test_kanban_model_tier_paced_reroute as pacing
from tests.hermes_cli import test_kanban_pin_reroute as pins
from tests.hermes_cli import test_kanban_quota_episode as quota

board = quota.board
clock = pacing.clock


def _history(conn, tid, kind, until):
    payload = ({'walled_provider': pins.PIN[1], 'resets_at': until, 'episode_id': 213}
               if kind == 'model_reroute' else
               {'provider': pins.PIN[1], 'until': until, 'episode_id': 213})
    kb._append_event(conn, tid, kind, payload)
    conn.commit()


@pytest.mark.parametrize('kind', ['model_reroute', 'quota_episode'])
@pytest.mark.parametrize('peer_held', [False, True])
def test_recovered_pin_ignores_historical_wall(board, clock, monkeypatch, all_assignees_spawnable,
                                             kind, peer_held):
    pins._deep(monkeypatch)
    pacing._pools(clock, openai_codex=('paced', None),
                  anthropic=('held' if peer_held else 'paced', pacing.HELD_FAR))
    calls = []

    def room(provider, model):
        calls.append((model, provider))
        return (model, provider) == pins.PIN

    monkeypatch.setattr(walls, 'live_quota_room', room)
    with kbc.connect() as conn:
        tid = pins._pinned_card(conn)
        _history(conn, tid, kind, clock[0] + pacing.HELD_FAR)
        before = conn.total_changes
        assert pacing._route(conn, tid) == (None, None, 'pinned')  # keep the row's explicit pin
        assert conn.total_changes == before
        assert lpd.candidate_providers(conn, kb.get_task(conn, tid), 'ready') == [pins.PIN[1]]
        assert pins._tick(conn) == [pins.PIN]
        assert pins.PIN in calls
        assert len(pins._events(conn, tid, kind)) == 1
        assert pins._events(conn, tid, 'run_route')[-1]['tier_reason'] == 'pinned'


@pytest.mark.parametrize('live_evidence', ['unknown', 'wall', 'current', 'provider_file'])
def test_history_is_retained_without_recovery_or_with_current_wall(board, clock, monkeypatch, live_evidence):
    _, cfg = pins._deep(monkeypatch)
    until = clock[0] + pacing.HELD_FAR
    monkeypatch.setattr(walls, 'live_quota_room', lambda *_: live_evidence != 'unknown')
    if live_evidence == 'wall':
        monkeypatch.setattr(walls, 'live_quota_wall', lambda p, m: until if (m, p) == pins.PIN else None)
    if live_evidence == 'provider_file':
        monkeypatch.setattr(walls, 'active_walls', lambda **_: {pins.PIN[1]: until})
    with kbc.connect() as conn:
        tid = pins._pinned_card(conn)
        _history(conn, tid, 'model_reroute', until)
        current = {pins.PIN[1]: until} if live_evidence == 'current' else None
        assert pr.pinned_route(conn, kb.get_task(conn, tid), lane='ready', now=clock[0],
                               cfg=cfg, current=current) == pins.PEER


def test_no_history_needs_no_recovery_read(board, clock, monkeypatch):
    _, cfg = pins._deep(monkeypatch)

    def unexpected(*_):
        pytest.fail('No historical pin wall needs a recovery probe')

    monkeypatch.setattr(walls, 'live_quota_room', unexpected)
    with kbc.connect() as conn:
        tid = pins._pinned_card(conn)
        assert pr.pinned_route(conn, kb.get_task(conn, tid), lane='ready', now=clock[0], cfg=cfg) is None


def test_unreadable_pool_does_not_disprove_history(board, clock, monkeypatch):
    _, cfg = pins._deep(monkeypatch)

    def unreadable(_provider):
        raise OSError('pool read failed')

    monkeypatch.setattr(walls, '_oauth_pool', unreadable)
    with kbc.connect() as conn:
        tid = pins._pinned_card(conn)
        _history(conn, tid, 'model_reroute', clock[0] + pacing.HELD_FAR)
        assert pr.pinned_route(conn, kb.get_task(conn, tid), lane='ready', now=clock[0], cfg=cfg) == pins.PEER
