"""Durable quota episodes and a read-only decision shared by admission and routing.

Runs, not polling ticks, advance backoff. A non-wall ended run is the episode
boundary; no task failure counters or explicit model pins are changed here.

Provider custody: the attempt's own receipt names the concrete provider that hit the
wall (a class route is only allocated inside the worker), the admission receipt names
the one it was sent to; both are held until the deadline. A provider nobody recorded
is unknown, and an unknown wall can never be proven different from the next route.
"""
import logging
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 7200


def episode(conn, task_id):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_quota_receipt import receipt_deadline, receipt_route, valid_deadline
    runs = []
    for row in conn.execute(
        "SELECT id, outcome, ended_at FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC, id DESC", (task_id,)):
        if row['outcome'] != 'rate_limited':
            break
        runs.append(row)
    if not runs:
        return None
    latest = runs[0]
    stored = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='quota_episode' "
        "ORDER BY id DESC LIMIT 1", (task_id, latest['id'])).fetchone()
    if stored:
        return kb._json_dict(stored[0])
    reset = receipt_deadline(conn, task_id, latest['id'])
    cooldown = max(0, kb._resolve_rate_limit_cooldown_seconds())
    delay = min(MAX_BACKOFF_SECONDS, cooldown * 2 ** min(len(runs) - 1, 20))
    ended = kb._to_epoch(latest['ended_at'])
    # An unreadable end stamp anchors backoff at now; an unrepresentable sum holds one full cap.
    backoff = valid_deadline((ended if ended is not None else time.time()) + delay)
    # A hint extends the wait, never shortens it below the doubling backoff ('retry after 5s' x N).
    found = [t for t in (reset, backoff) if t is not None]
    until = max(found) if found else None
    route = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='run_route' "
        "ORDER BY id DESC LIMIT 1", (task_id, latest['id'])).fetchone()
    route = kb._json_dict(route[0]) if route else {}
    admitted = route.get('provider') if isinstance(route.get('provider'), str) and route.get('provider') else None
    actual = receipt_route(conn, task_id, latest['id'])
    actual, admitted = (p.strip().lower() if p else None for p in (actual, admitted))
    providers = sorted({p for p in (actual, admitted) if p})
    return {'episode_id': runs[-1]['id'], 'run_id': latest['id'], 'count': len(runs),
            'until': until if until is not None else time.time() + MAX_BACKOFF_SECONDS,
            'hinted': reset is not None, 'provider': actual or admitted, 'providers': providers,
            'model': route.get('model')}


def _providers(state):
    found = state.get('providers')
    if isinstance(found, list):
        return [p for p in found if isinstance(p, str) and p]
    return [state['provider']] if state.get('provider') else []


def _outstanding(conn, task_id, state, now):
    """Every wall of this episode still before its deadline (earlier providers included)."""
    from hermes_cli import kanban_db as kb
    out = [state] if state['until'] > now else []
    for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='quota_episode' AND run_id>=?",
        (task_id, state['episode_id'])):
        prior = kb._json_dict(row[0])
        if isinstance(prior.get('until'), (int, float)) and prior['until'] > now:
            out.append(prior)
    return out


def _base_route(task, base):
    """The concrete ``(model, provider)`` admission would run *task* on with *base*, or None when unknown."""
    from agent.model_routing import is_class_selector
    from hermes_cli.kanban_model_tier import profile_route
    model, provider = base[:2]
    if not model:
        model, provider = getattr(task, 'model_override', None), getattr(task, 'provider_override', None)
    if not model:
        model, provider = profile_route(getattr(task, 'assignee', None))
    if not model or is_class_selector(model) or not provider or str(provider).lower() == 'auto':
        return None
    return str(model), str(provider).strip().lower()


def _walled_elsewhere(model, provider, now):
    """Deadline of a wall on *provider* that this episode never saw (another card's, or the live pool's)."""
    from hermes_cli import kanban_provider_walls as pw
    known = pw.active_walls(now=now).get(provider)
    return known if known is not None else pw.live_quota_wall(provider, model)


def _strong_fallback(task, base, walls, cfg, now):
    """Main's strong-tier fallback for an unpinned default card, judged against this episode's walls too.

    Used when no class alternate exists (routing off, or the default model is in no class): without it the
    card waited out the whole wall although the strong tier had room."""
    from copy import copy
    from hermes_cli.kanban_model_tier import load_tier_config
    from hermes_cli.kanban_provider_walls import retier_for_walls
    if task.model_override or task.provider_override or base[2] != 'default' or not base[0]:
        return None
    probe = copy(task)
    probe.model_override, probe.provider_override = base[:2]
    moved = retier_for_walls(probe, load_tier_config() if cfg is None else cfg, now=now, extra_walls=walls)
    return moved[:2] if moved else None


def hold_until(conn, task_id, now=None):
    """Latest deadline this task's quota episode still holds (every provider it hit), or None."""
    state = episode(conn, task_id)
    if not state:
        return None
    now = time.time() if now is None else now
    outstanding = _outstanding(conn, task_id, state, now)
    return max(s['until'] for s in outstanding) if outstanding else None


def alternate_route(conn, task, walls, base, now):
    """Non-allocating probe using the worker's billing/capability eligibility."""
    from agent.model_routing import eligible_routes, validate_policy, ModelRoutingError
    from agent.model_routing_agent import class_requirements
    from hermes_cli.config import is_provider_enabled
    from hermes_cli.kanban_model_assignment import _assignee_config
    from hermes_cli.kanban_provider_walls import active_walls
    model, _provider, reason = base
    if (task.model_override or task.provider_override or reason not in {'default', 'off', 'profile_out_of_scope'}
            or not walls):
        return None
    config = _assignee_config(task.assignee) or {}
    policy = config.get('model_routing') or {}
    try:
        validate_policy(policy)
    except ModelRoutingError:
        return None
    if not policy.get('enabled'):
        return None
    if not model:
        block = config.get('model') or {}
        model = block.get('default') if isinstance(block, dict) else block
    # A concrete default must belong to a known class; never guess its strength.
    level = str(model or '').removeprefix('class:')
    if not str(model or '').startswith('class:'):
        levels = [key for key, routes in policy.get('routes', {}).items()
                  if any(r['model'] == model for r in routes)]
        if len(levels) != 1:
            return None
        level = levels[0]
    walled = {**active_walls(now=now), **walls}
    providers = config.get('providers') or {}
    routes = eligible_routes(policy, level, class_requirements(config), eligible=lambda r: (
        r['provider'] not in walled and is_provider_enabled(providers.get(r['provider'], {}))))
    return (routes[0]['model'], routes[0]['provider']) if routes else None


def decision(conn, task, *, lane='ready', cfg=None, base=None, now=None):
    """Return (guard reason, alternate route); never writes or allocates quota."""
    from hermes_cli.kanban_model_tier import resolve_model_tier
    state = episode(conn, task.id)
    now = time.time() if now is None else now
    if not state:
        return None, None
    outstanding = _outstanding(conn, task.id, state, now)
    if not outstanding:
        return None, None
    state = max(outstanding, key=lambda s: s['until'])
    held = 'subscription_quota_reset' if state['hinted'] else 'rate_limit_cooldown'
    walls, unknown = {}, False
    for wall in outstanding:
        names = _providers(wall)
        unknown = unknown or not names
        for name in names:
            walls[name] = max(walls.get(name, 0), wall['until'])
    episode_walls = dict(walls)  # this episode's own walls: a pin moves only off a wall its own run hit
    try:
        base = base or resolve_model_tier(conn, task, lane=lane, cfg=cfg)
        target = _base_route(task, base)
        if target is not None and not unknown and target[1] not in walls:
            # A provider this episode never hit may still be walled (another card's wall, the live pool):
            # admitting it would send a card moved off codex, then walled on anthropic, back to codex.
            elsewhere = _walled_elsewhere(*target, now)
            if elsewhere is None:
                # The route admission will use is a known, free, different provider (e.g. a re-pinned card).
                return None, None
            walls[target[1]] = elsewhere
    except sqlite3.Error:
        raise
    except Exception:
        logger.warning('kanban quota: route resolve failed for %s; holding', task.id, exc_info=True)
        return held, None
    if lane == 'review' or base[2] in ('review_unpinned', 'review_override') or unknown:
        # An unrecorded wall may be any provider: no route can be proven to avoid it.
        return held, None
    try:
        if base[2] == 'pinned':
            from hermes_cli.kanban_model_tier import load_tier_config
            from hermes_cli.kanban_pin_reroute import pinned_route
            route = pinned_route(conn, task, lane=lane, now=now, current=episode_walls,
                                 cfg=load_tier_config() if cfg is None else cfg)
        else:
            route = alternate_route(conn, task, walls, base, now) or _strong_fallback(task, base, walls, cfg, now)
    except sqlite3.Error:
        raise
    except Exception:
        # Config/policy read failures fail closed (hold), never into a guessed route.
        logger.warning('kanban quota: alternate route probe failed for %s', task.id, exc_info=True)
        route = None
    if route:
        return None, route
    return held, None


def active(conn, task_id, now=None):
    """True while this task's quota episode still holds a deadline in the future."""
    state = episode(conn, task_id)
    if not state:
        return False
    now = time.time() if now is None else now
    return bool(_outstanding(conn, task_id, state, now))


def _notice_action(conn, task_id, state):
    from hermes_cli import kanban_db as kb
    task = kb.get_task(conn, task_id)
    route = decision(conn, task, lane=task.status)[1] if task is not None else None
    if route:
        return f'reroute to {route[0]} ({route[1]})'
    until = datetime.fromtimestamp(state['until'], ZoneInfo('America/Los_Angeles')).strftime('%Y-%m-%d %H:%M')
    return f'wait until {until} PT'


NOTICE_KIND = 'quota_episode_notice'


def notice_text(payload):
    """Chat line for a ``quota_episode_notice`` (gateway and TUI notifiers share it)."""
    payload = payload if isinstance(payload, dict) else {}
    count = payload.get('count') if type(payload.get('count')) is int else 3
    action = str(payload.get('action') or 'wait for the quota reset')[:200]
    return f' quota wall: {count} consecutive requeues; {action}. No failure budget consumed.'


def record_episode(conn, task_id, run_id):
    """Called inside the reaper transaction; event and notice are atomic/idempotent.

    Database errors propagate (the reaper's transaction owns them); a failure while choosing
    the notice's wording falls back to the bare wait, so it can never leave a card running."""
    from hermes_cli import kanban_db as kb
    state = episode(conn, task_id)
    if not state or state['run_id'] != run_id:
        return
    exists = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? AND kind='quota_episode'",
                          (task_id, run_id)).fetchone()
    if not exists:
        kb._append_event(conn, task_id, 'quota_episode', state, run_id=run_id)
    if state['count'] < 3:
        return
    notices = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?", (task_id, NOTICE_KIND))
    if any(kb._json_dict(row[0]).get('episode_id') == state['episode_id'] for row in notices):
        return
    try:
        action = _notice_action(conn, task_id, state)
    except sqlite3.Error:
        raise
    except Exception:
        logger.warning('kanban quota: notice wording failed for %s', task_id, exc_info=True)
        action = 'wait for the quota reset'
    kb._append_event(conn, task_id, NOTICE_KIND, {**state, 'action': action}, run_id=run_id)
    kb.add_comment(conn, task_id, 'kanban-dispatcher',
                   f'Quota wall: {state["count"]} consecutive requeues; {action}. No failure budget consumed.')
