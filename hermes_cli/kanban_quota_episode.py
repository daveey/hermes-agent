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
# A wall this gateway recorded holds as recorded for this long; after it, only while this pool agrees.
LOCAL_WALL_TRUST_SECONDS = 30 * 60
# [daveey] An unconfirmed (auth-churn) refusal retries soon: tokens are re-read within minutes, and a
# short cap keeps its backoff below provider_walls.MIN_WALL_SECONDS, so it never becomes a wall.
AUTH_CHURN_MAX_BACKOFF_SECONDS = 600


def _backoff(ended_at, count, *, cap=None):
    """The doubling-backoff deadline after the *count*-th consecutive rate_limited run ending at *ended_at*;
    *cap* (an auth-churn refusal) bounds the delay below ``MAX_BACKOFF_SECONDS``."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_quota_receipt import valid_deadline
    cooldown = max(0, kb._resolve_rate_limit_cooldown_seconds())
    limit = MAX_BACKOFF_SECONDS if cap is None else min(cap, MAX_BACKOFF_SECONDS)
    delay = min(limit, cooldown * 2 ** min(max(count, 1) - 1, 20))
    ended = kb._to_epoch(ended_at)
    # An unreadable end stamp anchors backoff at now; an unrepresentable sum holds one full cap.
    return valid_deadline((ended if ended is not None else time.time()) + delay)


def _episode_runs(conn, task_id, run_id=None):
    """This replica's consecutive rate_limited runs ending at *run_id* (default: the latest), newest first."""
    runs = []
    bound = conn.execute("SELECT ended_at,id FROM task_runs WHERE task_id=? AND id=? AND ended_at IS NOT NULL",
                         (task_id, run_id)).fetchone() if run_id is not None else None
    if run_id is not None and bound is None:
        return runs
    from hermes_cli.kanban_quota_episode_release import released_run
    released = released_run(conn, task_id)  # a quota_episode_released event closes every run up to it
    for row in conn.execute(
        "SELECT id, outcome, ended_at FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL "
        "AND (? IS NULL OR (ended_at,id)<=(?,?)) ORDER BY ended_at DESC, id DESC",
        (task_id, run_id, bound['ended_at'] if bound else None, run_id)):
        if row['outcome'] != 'rate_limited' or (released is not None and row['id'] <= released):
            break
        runs.append(row)
    return runs


def episode(conn, task_id, *, run_id=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_quota_receipt import receipt_deadline, receipt_route, receipt_unconfirmed
    runs = _episode_runs(conn, task_id, run_id)
    if not runs:
        return None
    latest = runs[0]
    stored = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='quota_episode' "
        "ORDER BY id DESC LIMIT 1", (task_id, latest['id'])).fetchone()
    if stored:
        return kb._json_dict(stored[0])
    reset = receipt_deadline(conn, task_id, latest['id'])
    churn = receipt_unconfirmed(conn, task_id, latest['id'])
    backoff = _backoff(latest['ended_at'], len(runs), cap=AUTH_CHURN_MAX_BACKOFF_SECONDS if churn else None)
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
    from hermes_cli.kanban_provider_walls import receipt_wall_model
    return {'episode_id': runs[-1]['id'], 'run_id': latest['id'], 'count': len(runs),
            'until': until if until is not None else time.time() + MAX_BACKOFF_SECONDS,
            'backoff_until': backoff, 'hinted': reset is not None,
            'provider': actual or admitted, 'providers': providers,
            'model': route.get('model'), 'auth_churn': churn,
            'wall_model': receipt_wall_model(conn, task_id, latest['id'], actual or admitted)}


def _providers(state):
    found = state.get('providers')
    if isinstance(found, list):
        return [p for p in found if isinstance(p, str) and p]
    return [state['provider']] if state.get('provider') else []


def _disproved(wall, now, memo):
    """True for a hinted wall the live OAuth pool now contradicts: every provider it names serves its
    model (any model for a class route) and no later provider-wide wall stands. A transient auth bench
    once made ``quota_unavailable`` report a days-long false wall; the stored deadline must not outlive it.
    A file wall ending no later than this one may be its own mirror (``record_wall_for_run``), so only a
    later one is independent evidence. *memo* caches the pool reads for one ``_outstanding`` call."""
    from agent.model_routing import is_class_selector
    from hermes_cli import kanban_provider_walls as pw
    names = _providers(wall)
    if not wall.get('hinted') or not names:
        return False
    model = wall.get('wall_model') or wall.get('model')
    if not isinstance(model, str) or not model or is_class_selector(model) or len(names) > 1:
        model = None
    if 'walls' not in memo:
        memo['walls'] = pw.active_walls(now=now)
    for name in names:
        if (name, model) not in memo:
            memo[name, model] = pw.live_pool_serving(name, model)
    return all((pw.wall_until(memo['walls'], name, model) or 0) <= wall['until'] and memo[name, model] for name in names)


def _unhinted(conn, task_id, wall):
    """*wall* with its disproved hint rejected: it keeps only the doubling backoff ('retry after 5s' x N
    must stay paced). Episodes stored before ``backoff_until`` recompute it from their latest run, read
    through this replica's run id (a synced payload names the recording gateway's); a run this replica
    lacks anchors at the wall's recording time, never at now, which would renew the backoff each read."""
    from hermes_cli.kanban_quota_episode_release import _local_run
    backoff = wall.get('backoff_until')
    if not isinstance(backoff, (int, float)):
        row = conn.execute('SELECT ended_at FROM task_runs WHERE task_id=? AND id=?',
                           (task_id, _local_run(conn, task_id, wall))).fetchone()
        count = wall.get('count') if isinstance(wall.get('count'), int) else 1
        backoff = _backoff(row['ended_at'] if row else _wall_origin(conn, task_id, wall)[0], count)
    until = min(wall['until'], backoff) if backoff is not None else wall['until']
    return {**wall, 'until': until, 'hinted': False}


def _outstanding(conn, task_id, state, now):
    """Every wall of this episode still before its deadline (earlier providers included).

    The episode starts at this replica's first run of it: a stored *state* names the recording gateway's
    run ids, which compared against local event rows also took in walls of earlier (released) episodes."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_quota_episode_release import _local_run
    out = [state] if state['until'] > now else []
    latest = _local_run(conn, task_id, state)
    runs = _episode_runs(conn, task_id, latest)
    for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='quota_episode' AND run_id>=?",
        (task_id, runs[-1]['id'] if runs else latest)):
        prior = kb._json_dict(row[0])
        if isinstance(prior.get('until'), (int, float)) and prior['until'] > now:
            out.append(prior)
    memo = {}
    out = [_unhinted(conn, task_id, wall) if _disproved(wall, now, memo) else wall for wall in out]
    return [wall for wall in out if wall['until'] > now]


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
    known = pw.wall_until(pw.active_walls(now=now), provider, model)
    return known if known is not None else pw.live_quota_wall(provider, model)


def _strong_fallback(task, base, walls, cfg, now):
    """Main's strong-tier fallback for an unpinned default card, judged against this episode's walls too.

    Used when no class alternate exists (routing off, or the default model is in no class): without it the
    card waited out the whole wall although the strong tier had room."""
    from copy import copy
    from hermes_cli.kanban_model_tier import load_tier_config
    from hermes_cli.kanban_provider_walls import retier_for_walls
    from hermes_cli.kanban_model_neutral import routing_view
    task = routing_view(task)
    if task.model_override or task.provider_override or base[2] != 'default' or not base[0]:
        return None
    from hermes_cli.kanban_class_walls import class_wall_routes
    config = load_tier_config() if cfg is None else cfg
    strong = str(config.get('strong_model') or '')
    if strong == 'class:carbon':
        return None
    if strong.startswith('class:'):
        routes, _known = class_wall_routes(task, strong, now=now, extra_walls=walls)
        return (routes[0]['model'], routes[0]['provider']) if routes else None
    probe = copy(task)
    probe.model_override, probe.provider_override = base[:2]
    moved = retier_for_walls(probe, config, now=now, extra_walls=walls)
    return moved[:2] if moved else None


def hold_until(conn, task_id, now=None):
    """Latest deadline this task's quota episode still holds (every provider it hit), or None."""
    state = episode(conn, task_id)
    if not state:
        return None
    now = time.time() if now is None else now
    outstanding = _outstanding(conn, task_id, state, now)
    return max(s['until'] for s in outstanding) if outstanding else None


def release_at(conn, task, now=None, *, lane=None):
    """When the guard stops holding *task*: the earliest known wall reset at which :func:`decision` admits it, else
    :func:`hold_until`. None when no episode holds. Read-only; advisory (the dispatcher re-decides every tick). [daveey]

    Hub 2026-11-14 (t_01a1d95da6382c64): unpinned default cards walled on codex until 11-20 waited only for
    anthropic, the class's other route, whose session wall reset at 09:20Z; they rerouted minutes later. Every
    deadline shown (pickup skip, stuck warning, chat wait) read 11-20, so the hold looked like a missing fallback."""
    from hermes_cli.kanban_provider_walls import active_walls
    now = time.time() if now is None else now
    until = hold_until(conn, task.id, now)
    if until is None:
        return None
    state = episode(conn, task.id) or {}
    resets = {float(v) for v in active_walls(now=now).values() if isinstance(v, (int, float))}
    resets |= {float(s['until']) for s in _outstanding(conn, task.id, state, now) if state}
    lane = lane or getattr(task, 'status', None) or 'ready'
    for reset in sorted(r for r in resets if now < r < until):
        if decision(conn, task, lane=lane, now=reset + 1)[0] is None:
            return reset
    return until


def _strong_class(base):
    """True for an unpinned strong-tier class route (``retry_after_*``/``pattern:*``/``review`` on
    ``class:<x>``/``auto``): the worker would allocate any route of that class, so a wall on one provider
    leaves the class's other providers open to it. [daveey]"""
    from agent.model_routing import is_class_selector
    model, provider, reason = base
    return (isinstance(model, str) and is_class_selector(model) and str(provider or 'auto').strip().lower() == 'auto'
            and (reason == 'review' or str(reason).startswith(('retry_after_', 'pattern:'))))


def alternate_route(conn, task, walls, base, now):
    """Non-allocating probe using the worker's billing/capability eligibility.

    A strong-tier class route (``_strong_class``) also requires the route's provider to serve on this
    gateway's live pool: the strong tier is chosen for its strength, so it moves only to proven room."""
    from agent.model_routing import eligible_routes, validate_policy, ModelRoutingError
    from agent.model_routing_agent import class_requirements
    from hermes_cli.config import is_provider_enabled
    from hermes_cli.kanban_model_assignment import _assignee_config
    from hermes_cli.kanban_provider_walls import active_walls, live_pool_serving, wall_until
    model, _provider, reason = base
    strong = _strong_class(base)
    if ((reason not in {'default', 'off', 'profile_out_of_scope', 'class'} and not strong) or not walls):
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
    if level == 'carbon':
        return None
    if not str(model or '').startswith('class:'):
        levels = [key for key, routes in policy.get('routes', {}).items()
                  if key != 'carbon' and any(r['model'] == model for r in routes)]
        if len(levels) != 1:
            return None
        level = levels[0]
    walled = {**active_walls(now=now), **walls}
    providers = config.get('providers') or {}
    routes = eligible_routes(policy, level, class_requirements(config), eligible=lambda r: (
        not wall_until(walled, r['provider'], r['model']) and is_provider_enabled(providers.get(r['provider'], {}))))
    if strong:
        routes = [r for r in routes if live_pool_serving(r['provider'], r['model'])]
    return (routes[0]['model'], routes[0]['provider']) if routes else None


def _wall_origin(conn, task_id, wall):
    """``(recorded_at, local)`` for one stored wall: when it was recorded and whether this gateway ran it.

    Run ids in a synced payload are the recording gateway's; the event row's ``run_id`` is this replica's,
    and that run's ``claimed`` lock names the host (``kanban_host_identity``). No lock reads as local."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_host_identity import is_local_claim
    row = conn.execute(
        "SELECT run_id, created_at FROM task_events WHERE task_id=? AND kind='quota_episode' AND json_valid(payload) "
        "AND json_extract(payload, '$.run_id') IS ? AND json_extract(payload, '$.episode_id') IS ? "
        "ORDER BY id DESC LIMIT 1", (task_id, wall.get('run_id'), wall.get('episode_id'))).fetchone()
    if row is not None:
        run_id, recorded = row[0], kb._to_epoch(row[1])
    else:  # computed from this replica's own runs, not yet stored
        run_id = wall.get('run_id')
        ended = conn.execute("SELECT ended_at FROM task_runs WHERE task_id=? AND id=?", (task_id, run_id)).fetchone()
        recorded = kb._to_epoch(ended[0]) if ended else None
    locks = [kb._json_dict(raw).get('lock') for (raw,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='claimed'", (task_id, run_id))]
    locks = [lock for lock in locks if isinstance(lock, str) and lock]
    return recorded, (not locks or any(is_local_claim(lock) for lock in locks))


def _wall_models(task, wall, provider):
    """Concrete models this gateway would run *provider* on for the walled route (a class's routes), or []."""
    from agent.model_routing import is_class_selector
    from hermes_cli.kanban_model_assignment import _assignee_config
    model = wall.get('wall_model') or wall.get('model')
    if not isinstance(model, str) or not model:
        return []
    if not is_class_selector(model):
        return [model]
    policy = (_assignee_config(task.assignee) or {}).get('model_routing') or {}
    routes = (policy.get('routes') or {}).get(model.removeprefix('class:')) or []
    return [r['model'] for r in routes if isinstance(r, dict) and str(r.get('provider') or '').strip().lower() == provider
            and isinstance(r.get('model'), str) and r['model']]


def _served_here(conn, task, wall, now):
    """True when this gateway's own pool can serve every provider of a wall it did not just see itself.

    A wall another gateway recorded (pools are per gateway), or this gateway's own once older than
    ``LOCAL_WALL_TRUST_SECONDS``, holds only while this pool agrees. A wall this gateway just saw holds
    as recorded; an unknown provider or model, or a pool with no live entry, is never room."""
    from hermes_cli.kanban_provider_walls import live_quota_room
    names = _providers(wall)
    if not names:
        return False
    recorded, local = _wall_origin(conn, task.id, wall)
    if local and (recorded is None or now - recorded < LOCAL_WALL_TRUST_SECONDS):
        return False
    return all(any(live_quota_room(p, m) for m in _wall_models(task, wall, p)) for p in names)


def decision(conn, task, *, lane='ready', cfg=None, base=None, now=None):
    """Return (guard reason, alternate route); never writes or allocates quota."""
    from hermes_cli.kanban_provider_walls import wall_key, wall_until
    from hermes_cli.kanban_model_tier import resolve_model_tier
    from hermes_cli.kanban_model_neutral import routing_view
    task = routing_view(task)
    state = episode(conn, task.id)
    now = time.time() if now is None else now
    if not state:
        return None, None
    outstanding = _outstanding(conn, task.id, state, now)
    if not outstanding:
        return None, None
    state = max(outstanding, key=lambda s: s['until'])
    held = 'subscription_quota_reset' if state['hinted'] else 'rate_limit_cooldown'
    try:
        outstanding = [wall for wall in outstanding if not _served_here(conn, task, wall, now)]
    except sqlite3.Error:
        raise
    except Exception:
        logger.warning('kanban quota: local pool check failed for %s; keeping its walls', task.id, exc_info=True)
    # Every wall was another gateway's (or a stale one of ours) and this pool has room now. The base route
    # still goes through the walled-elsewhere check below; a run that hits the wall here records a new episode.
    lifted = not outstanding
    walls, unknown = {}, False
    for wall in outstanding:
        names = _providers(wall)
        unknown = unknown or not names
        for name in names:
            key = wall_key(name, wall.get('wall_model'))
            walls[key] = max(walls.get(key, 0), wall['until'])
    episode_walls = dict(walls)  # this episode's own walls: a pin moves only off a wall its own run hit
    try:
        base = base or resolve_model_tier(conn, task, lane=lane, cfg=cfg)
        target = _base_route(task, base)
        if target is None and lifted:
            # A class route: the worker allocates it against this gateway's live pool, which has room.
            return None, None
        if target is not None and not unknown and not wall_until(walls, target[1], target[0]):
            # A provider this episode never hit may still be walled (another card's wall, the live pool):
            # admitting it would send a card moved off codex, then walled on anthropic, back to codex.
            elsewhere = _walled_elsewhere(*target, now)
            if elsewhere is None:
                # The route admission will use is a known, free, different provider (e.g. a re-pinned card).
                return None, None
            walls[wall_key(target[1], target[0])] = elsewhere
    except sqlite3.Error:
        raise
    except Exception:
        logger.warning('kanban quota: route resolve failed for %s; holding', task.id, exc_info=True)
        return held, None
    if (lane == 'review' and not _strong_class(base)) or base[2] in (
            'review_unpinned', 'review_override', 'review_override_refused') or unknown:
        # An unrecorded wall may be any provider: no route can be proven to avoid it. A legacy unpinned
        # review on a class route (reason ``review``) chose no reviewer, so another route of its class may take it.
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
    until = (release_at(conn, task, lane=task.status) if task is not None else None) or state['until']
    until = datetime.fromtimestamp(until, ZoneInfo('America/Los_Angeles')).strftime('%Y-%m-%d %H:%M')
    return f'wait until {until} PT'


NOTICE_KIND = 'quota_episode_notice'


def notice_text(payload):
    """Chat line for a ``quota_episode_notice`` (gateway and TUI notifiers share it)."""
    payload = payload if isinstance(payload, dict) else {}
    count = payload.get('count') if type(payload.get('count')) is int else 3
    action = str(payload.get('action') or 'wait for the quota reset')[:200]
    return f' quota wall: {count} consecutive requeues; {action}. No failure budget consumed.'


def record_episode(conn, task_id, run_id, *, captured=False):
    """Called inside the reaper transaction; event and notice are atomic/idempotent.

    Database errors propagate (the reaper's transaction owns them); a failure while choosing
    the notice's wording falls back to the bare wait, so it can never leave a card running."""
    from hermes_cli import kanban_db as kb
    state = episode(conn, task_id, run_id=run_id if captured else None)
    if not state or state['run_id'] != run_id or state.get('auth_churn'):
        return  # an auth-churn (unconfirmed) refusal is a short retry, never a recorded quota episode
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
