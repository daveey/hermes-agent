"""A quota hold's deadline is when a fallback route opens, not only when the walled route resets. [daveey]

Hub 2026-11-14 (t_01a1d95da6382c64): unpinned default cards hit a 6.5-day codex wall while anthropic, the
class's only other route, sat behind a session wall until 09:20Z. The guard correctly held them and they
rerouted to anthropic minutes after 09:20Z, but every deadline it reported (pickup skip, stuck warning, chat
wait) read the codex reset on 11-20, so the hold was filed as "no same-class fallback for the default model".
"""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_provider_walls as walls
from hermes_cli.kanban_db_dispatch_clock import guard_hold_until
from hermes_cli.kanban_quota_episode import episode, release_at
from tests.hermes_cli.test_kanban_quota_episode import board as board, reap, routing  # noqa: PLC0414 -- the self-alias is load-bearing: it suppresses F811 for the pytest fixture parameter shadowing this import
from tests.hermes_cli.test_kanban_quota_episode_walls import _clock, _codex_wall


def _provider_file(monkeypatch, entries):
    """``active_walls`` as the provider file answers it: only resets still in the future at ``now``."""
    monkeypatch.setattr(walls, 'active_walls', lambda *, now=None, **_k: {
        p: until for p, until in entries.items() if now is None or until > now})


def _walled_default_card(conn, clock, codex_reset):
    tid = kb.create_task(conn, title='default card on a far codex wall', assignee='a')
    reap(conn, tid, provider='openai-codex', **_codex_wall(codex_reset))
    assert episode(conn, tid)['until'] == codex_reset
    clock[0] += 301
    return tid


def test_hold_deadline_is_the_fallback_routes_reset(board, monkeypatch, all_assignees_spawnable):
    clock = _clock(monkeypatch)
    routing(monkeypatch)
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    codex_reset = clock[0] + int(6.5 * 86400)
    anthropic_reset = float(clock[0] + 3 * 3600)
    _provider_file(monkeypatch, {'anthropic': anthropic_reset})
    with kbc.connect() as conn:
        tid = _walled_default_card(conn, clock, codex_reset)
        # Both routes of the class are walled: hold, as before.
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        # The hold ends when anthropic resets, not when codex does.
        assert release_at(conn, kb.get_task(conn, tid), clock[0]) == anthropic_reset
        assert guard_hold_until(conn, tid, 'subscription_quota_reset', clock[0]) == int(anthropic_reset)
        # And the dispatcher agrees: just past that reset the card runs on the fallback route.
        clock[0] = int(anthropic_reset) + 1
        assert dispatch.check_respawn_guard(conn, tid) is None
        seen = []
        dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: seen.append((task.model_override,
                                                                              task.provider_override)))
        assert seen == [('claude-sonnet-5-5', 'anthropic')]
        tier = kb._json_dict(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='model_tier' "
                                          "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0])
        assert tier['provider'] == 'anthropic' and tier['reason'].endswith('+quota_reroute')


def test_hold_deadline_stays_the_wall_when_no_fallback_opens_first(board, monkeypatch):
    """Control: the fallback's wall outlasts codex's (or the class has no other route): the codex reset."""
    clock = _clock(monkeypatch)
    routing(monkeypatch)
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    codex_reset = clock[0] + 2 * 86400
    _provider_file(monkeypatch, {'anthropic': float(clock[0] + 3 * 86400)})
    with kbc.connect() as conn:
        tid = _walled_default_card(conn, clock, codex_reset)
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        assert guard_hold_until(conn, tid, 'subscription_quota_reset', clock[0]) == codex_reset


def test_hold_deadline_of_a_card_with_no_alternate_is_its_wall(board, monkeypatch):
    clock = _clock(monkeypatch)
    routing(monkeypatch, blocked=True)
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    _provider_file(monkeypatch, {'openai-codex': float(clock[0] + 3600)})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='only anthropic', assignee='a')
        reap(conn, tid, text='usage limit resets in ~11h', provider='anthropic')
        until = episode(conn, tid)['until']
        clock[0] += 301
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        assert guard_hold_until(conn, tid, 'subscription_quota_reset', clock[0]) == int(until)
