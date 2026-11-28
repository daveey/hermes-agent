"""Quota exit hints must survive reaping and gate admission beyond the fixed cooldown."""
from pathlib import Path
from datetime import UTC, datetime

from hermes_cli import kanban_model_tier as tier
from hermes_cli.kanban_quota_episode import episode, record_episode
from hermes_cli.kanban_shared_blockers import task_route

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    kb.init_db()
    return home


def test_text_reset_hint_survives_reaper_and_blocks_after_cooldown(board, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="quota reset", assignee="a")
        claimed = kb.claim_task(conn, tid, claimer=kb._claimer_id())
        assert claimed is not None
        pid = 71001
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        kb._append_event(conn, tid, "run_route", {
            "model": "claude-opus-5-5", "provider": "anthropic",
            "source": "pin", "tier_reason": "pinned", "lane": "ready",
        }, run_id=claimed.current_run_id)
        # Exact-run receipt is authoritative output; no synthetic outcome row.
        kb._append_event(conn, tid, "worker_result", {
            "pid": pid, "claim_lock": claimed.claim_lock, "exit_code": 75,
            "last_output": "Anthropic rate-limited every one of 3 attempts — its usage limit resets in ~11h",
        }, run_id=claimed.current_run_id)
        conn.commit()
        dispatch._record_worker_exit(pid, 75 << 8)
        dispatch.detect_crashed_workers(conn)
        run = conn.execute("SELECT outcome FROM task_runs WHERE id=?", (claimed.current_run_id,)).fetchone()
        assert run["outcome"] == "rate_limited"
        assert kb.get_task(conn, tid).consecutive_failures == 0
        monkeypatch.setattr(kb.time, "time", lambda: now + 301)
        assert dispatch.check_respawn_guard(conn, tid) is not None
        monkeypatch.setattr(kb.time, "time", lambda: now + 11 * 3600 - 1)
        assert dispatch.check_respawn_guard(conn, tid) is not None
        monkeypatch.setattr(kb.time, "time", lambda: now + 11 * 3600 + 600)
        assert dispatch.check_respawn_guard(conn, tid) is None


def reap(conn, tid, *, text='', fields=None, provider='anthropic', lane='ready', before_reap=None, admit=None):
    claim = kb.claim_review_task if lane == 'review' else kb.claim_task
    task = claim(conn, tid, claimer=kb._claimer_id())
    assert task is not None
    pid = 71001
    conn.execute('UPDATE tasks SET worker_pid=? WHERE id=?', (pid, tid))
    if admit is not None:
        # The dispatcher's real admission writes the run_route receipt (class routes: provider unknown).
        tier.apply_model_tier(conn, task, lane=lane, cfg=admit)
    else:
        kb._append_event(conn, tid, 'run_route', {'model': 'claude-sonnet-5-5', 'provider': provider,
                         'source': 'profile_default', 'tier_reason': 'default', 'lane': lane},
                         run_id=task.current_run_id)
    kb._append_event(conn, tid, 'worker_result', {'pid': pid, 'claim_lock': task.claim_lock,
                     'exit_code': 75, 'last_output': text, **(fields or {})}, run_id=task.current_run_id)
    conn.commit()
    dispatch._record_worker_exit(pid, 75 << 8)
    if before_reap:
        before_reap()
    dispatch.detect_crashed_workers(conn)
    assert kb.get_task(conn, tid).status == lane
    assert kb.get_task(conn, tid).consecutive_failures == 0
    return task.current_run_id


@pytest.mark.parametrize('hint', ['relative', 'timestamp', 'epoch', 'typed', 'retry_after'])
@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_deadline_fixed_to_receipt_and_typed_precedence(board, monkeypatch, hint, lane):
    clock = [1_800_000_000]
    monkeypatch.setattr(kb.time, 'time', lambda: clock[0])
    now = clock[0]
    reset = now + 7200
    text = ('resets at ' + datetime.fromtimestamp(reset, UTC).isoformat()
            if hint == 'timestamp' else 'usage limit resets in ~2h')
    if hint == 'retry_after':
        text = 'quota exhausted; retry after 7200s'
    if hint == 'epoch':
        # The live 11-01 hub receipt shape when the typed field is absent.
        text = f'SubscriptionQuotaUnavailable: Subscription quota unavailable for openai-codex/gpt-6-astra; resets at {reset}.0'
    fields = {'failure_reason': 'rate_limit', 'failure_resets_at': now + 3600} if hint == 'typed' else {}
    expected = now + 3600 if hint == 'typed' else reset
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='deadline', assignee='a')
        if lane == 'review':
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
            conn.commit()
        run_id = reap(conn, tid, text=text, fields=fields, lane=lane,
                      before_reap=lambda: clock.__setitem__(0, now + 100))
        assert episode(conn, tid)['until'] == expected
        for tick in [now + 301, expected - 1]:
            clock[0] = tick
            assert dispatch.check_respawn_guard(conn, tid, lane=lane) == 'subscription_quota_reset'
        # A later unrelated receipt cannot change custody or re-anchor the persisted deadline.
        kb._append_event(conn, tid, 'worker_result', {'exit_code': 75, 'last_output': 'resets in ~99h'}, run_id=run_id + 100)
        clock[0] = expected
        assert dispatch.check_respawn_guard(conn, tid, lane=lane) is None
        assert episode(conn, tid)['until'] == expected


@pytest.mark.parametrize('outcome', ['completed', 'crashed'])
def test_backoff_cap_notice_once_and_nonwall_resets(board, monkeypatch, outcome):
    clock = [1_800_000_000]
    monkeypatch.setattr(kb.time, 'time', lambda: clock[0])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='backoff', assignee='a', max_retries=100)  # above auto-replan threshold
        for count, delay in enumerate([300, 600, 1200, 2400, 4800, 7200, 7200], 1):
            start = clock[0]
            run_id = reap(conn, tid)
            state = episode(conn, tid)
            assert state['count'] == count and state['until'] == start + delay
            clock[0] += delay - 1
            assert dispatch.check_respawn_guard(conn, tid) == 'rate_limit_cooldown'
            clock[0] += 1
            assert dispatch.check_respawn_guard(conn, tid) is None
            with kb.write_txn(conn):
                record_episode(conn, tid, run_id)
        notices = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='quota_episode_notice'", (tid,)).fetchall()
        assert len(notices) == 1
        assert 'PT' in kb._json_dict(notices[0][0])['action']
        comments = conn.execute("SELECT body FROM task_comments WHERE task_id=? AND author='kanban-dispatcher'", (tid,)).fetchall()
        assert sum('Quota wall:' in row[0] for row in comments) == 1
        # An intervening normal attempt terminates the episode even if it wasn't successful.
        kb.claim_task(conn, tid, claimer=kb._claimer_id())
        with kb.write_txn(conn):
            kb._end_run(conn, tid, outcome=outcome, status=outcome)
            conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
        assert episode(conn, tid) is None
        for count in range(3):
            reap(conn, tid)
            assert episode(conn, tid)['count'] == count + 1
            clock[0] = episode(conn, tid)['until']
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='quota_episode_notice'", (tid,)).fetchone()[0] == 2


def routing(monkeypatch, *, blocked=False, profile=False):
    def route(provider, model, **kw):
        # Real allocations require tools+streaming (agent.model_routing_agent.class_requirements).
        return dict(provider=provider, model=model, billing='subscription', weight=1,
                    capabilities={'tools': True, 'streaming': True}, eligible=True, funding_verified=True, **kw)
    config = {'model': {'default': 'class:standard', 'provider': 'auto'},
              'model_routing': {'enabled': True, 'routes': {'standard': [
                  route('anthropic', 'claude-sonnet-5-5'),
                  route('openai-codex', 'gpt-test')],
                  'deep': [route('anthropic', 'claude-opus-5-5')]}},
              'providers': {'openai-codex': {'enabled': not blocked}}}
    cfg = {} if profile else {'default_model': 'class:standard', 'strong_model': 'class:deep',
                              'default_model_keep_patterns': ['security']}
    monkeypatch.setattr(tier, 'load_tier_config', lambda: cfg)
    monkeypatch.setattr('hermes_cli.kanban_model_assignment._assignee_config', lambda _: config)
    # Isolated enabled class routes for the host-level routability probe too (the home's own config is empty).
    monkeypatch.setattr('hermes_cli.config_effective.load_user_config_effective', lambda *a, **k: config)
    return config, cfg


@pytest.mark.parametrize('entry', ['default', 'profile', 'pinned', 'provider_pin', 'review', 'pattern', 'retry', 'disabled', 'ineligible'])
def test_shared_admission_preview_and_owner_route(board, monkeypatch, entry):
    now = 1_800_000_000
    monkeypatch.setattr(kb.time, 'time', lambda: now)
    config, cfg = routing(monkeypatch, blocked=entry == 'disabled', profile=entry == 'profile')
    if entry == 'ineligible':
        config['model_routing']['routes']['standard'][1]['funding_verified'] = False
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='security work' if entry == 'pattern' else 'quota', assignee='a')
        if entry == 'pinned':
            # Historical raw selector seed (disposable DB): dynamically ignored, author row retained.
            conn.execute("UPDATE tasks SET model_override='claude-opus-5-5', provider_override='anthropic' WHERE id=?", (tid,))
        elif entry == 'provider_pin':
            conn.execute("UPDATE tasks SET provider_override='anthropic' WHERE id=?", (tid,))
        elif entry == 'retry':
            conn.execute("INSERT INTO task_runs(task_id,started_at,ended_at,status,outcome) VALUES (?,?,?,'crashed','crashed')", (tid, now - 20, now - 10))
        lane = 'review' if entry == 'review' else 'ready'
        if lane == 'review':
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()
        reap(conn, tid, text='resets in ~11h', lane=lane)
        # Legacy selectors and missing model attribution cannot constrain review dispatch.
        expected_reroute = entry in {'default', 'profile', 'pinned', 'provider_pin', 'review'}
        assert (dispatch.check_respawn_guard(conn, tid, lane=lane) is None) == expected_reroute
        before = conn.total_changes
        preview = task_route(conn, tid)
        assert conn.total_changes == before
        task = kb.get_task(conn, tid)
        original = (task.model_override, task.provider_override)
        if expected_reroute:
            claim = kb.claim_review_task if lane == 'review' else kb.claim_task
            task = claim(conn, tid, claimer=kb._claimer_id())
        tier.apply_model_tier(conn, task, lane=lane, cfg=cfg)
        assert tier.effective_route(task)[:2] == preview[:2]
        if expected_reroute:
            assert preview[:2] == ('gpt-test', 'openai-codex')
            assert tier.owner_model_pin(conn, task) == ('gpt-test', 'openai-codex')
        elif entry in {'review', 'pattern', 'retry'}:
            assert preview[:2] == ('class:deep', None)
            assert tier.owner_model_pin(conn, task) == ('class:deep', 'auto')
        persisted = kb.get_task(conn, tid)
        assert (persisted.model_override, persisted.provider_override) == original


@pytest.mark.parametrize('pinned', [False, True])
def test_walled_concrete_default_model_reroutes_to_class_peer(board, monkeypatch, all_assignees_spawnable, pinned):
    """Hub 11-01: kanban.default_model is the walled codex model itself; the next route comes from its class."""
    clock = [1_800_000_000]
    monkeypatch.setattr(kb.time, 'time', lambda: clock[0])
    config, _ = routing(monkeypatch)
    config['model_routing']['routes'] = {'standard': [
        dict(config['model_routing']['routes']['standard'][0], model='claude-opus-5-5'),
        dict(config['model_routing']['routes']['standard'][1], model='gpt-6-astra')]}
    # A concrete default is ignored (neutral class:standard); only the legacy pin row is a raw seed.
    cfg = {'default_model': 'gpt-6-astra', 'default_provider': 'openai-codex',
           'strong_model': 'class:deep'}
    monkeypatch.setattr(tier, 'load_tier_config', lambda: cfg)
    reset = clock[0] + 55 * 3600
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='hub default card', assignee='a')
        if pinned:
            conn.execute("UPDATE tasks SET model_override='gpt-6-astra', provider_override='openai-codex' WHERE id=?", (tid,))
            conn.commit()
        reap(conn, tid, admit=cfg, text=f'SubscriptionQuotaUnavailable: Subscription quota unavailable for '
             f'openai-codex/gpt-6-astra; resets at {reset}.0',
             fields={'failure_reason': 'rate_limit', 'failure_retryable': True, 'failure_resets_at': float(reset),
                     'route_provider': 'openai-codex', 'route_model': 'gpt-6-astra'})
        clock[0] += 301
        seen = []
        dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: seen.append((task.model_override, task.provider_override)))
        # A legacy pin row is ignored: both cards move within the class on the episode's wall.
        assert seen == [('claude-opus-5-5', 'anthropic')]
        route = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='run_route' "
                             "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0]
        assert '"provider": "anthropic"' in route
        assert 'quota_reroute' in route


def test_new_unpinned_card_created_after_a_known_wall_never_routes_to_it(board, monkeypatch, all_assignees_spawnable):
    """Hub 11-01 16:00 PST (t_01a1998e7c300793): a brand-new unpinned card, created after the Codex wall
    was already known, was admitted on the walled default (tier_reason: default) because no
    provider_walls.json existed and the live pool probe missed. The first reaped wall must persist the
    provider wall from its exact receipt, so every later default card skips the walled provider even
    when the live pool probe sees nothing."""
    from hermes_cli import kanban_provider_walls as walls
    clock = [1_800_000_000]
    monkeypatch.setattr(kb.time, 'time', lambda: clock[0])
    monkeypatch.setattr(walls.time, 'time', lambda: clock[0])
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    routing(monkeypatch)
    cfg = {'default_model': 'class:standard', 'strong_model': 'class:deep'}
    monkeypatch.setattr(tier, 'load_tier_config', lambda: cfg)
    reset = clock[0] + 55 * 3600
    with kbc.connect() as conn:
        first = kb.create_task(conn, title='walled first', assignee='a')
        reap(conn, first, admit=cfg, text=f'Provider-wide quota unavailable for openai-codex; resets at {reset}.0',
             fields={'failure_reason': 'rate_limit', 'failure_retryable': True, 'failure_resets_at': float(reset),
                     'route_provider': 'openai-codex', 'route_model': 'gpt-6-astra'})
        assert walls.active_walls(now=clock[0]).get('openai-codex') == pytest.approx(reset, abs=1)
        # Take the first card out of this tick so only the new one can be dispatched (its own episode
        # now reroutes it too; test_kanban_quota_episode_walls covers that).
        kb.archive_task(conn, first)
        clock[0] += 60
        new = kb.create_task(conn, title='brand-new default card', assignee='a')
        conn.commit()
        seen = []
        dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: seen.append((task.id, task.provider_override)))
        # Neutral admission stays a provider-unknown class (the worker allocates): the dispatcher never
        # concretely pins the walled provider, and its receipt names no provider at all.
        assert seen == [(new, 'anthropic')]
        route = kb._json_dict(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='run_route' "
                                           "ORDER BY id DESC LIMIT 1", (new,)).fetchone()[0])
        assert route['provider'] == 'anthropic' and route['model'] == 'claude-sonnet-5-5'


@pytest.mark.parametrize('reroute', [False, True])
def test_real_dispatch_waits_or_uses_preview_route(board, monkeypatch, all_assignees_spawnable, reroute):
    clock = [1_800_000_000]
    monkeypatch.setattr(kb.time, 'time', lambda: clock[0])
    routing(monkeypatch, blocked=not reroute)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='dispatch quota', assignee='a')
        reap(conn, tid, text='resets in ~11h')
        clock[0] += 301
        seen = []
        def spawn(task, workspace):
            seen.append((task.model_override, task.provider_override))
        dispatch.dispatch_once(conn, spawn_fn=spawn)
        if reroute:
            assert seen == [('gpt-test', 'openai-codex')]
        else:
            assert seen == []
            assert kb.get_task(conn, tid).status == 'ready'
            clock[0] += 11 * 3600 + 600
            dispatch.dispatch_once(conn, spawn_fn=spawn)
            assert len(seen) == 1


def test_reroute_notice_once_and_no_return_to_previous_walled_provider(board, monkeypatch):
    clock = [1_800_000_000]
    monkeypatch.setattr(kb.time, 'time', lambda: clock[0])
    routing(monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='reroute notice', assignee='a')
        for _ in range(3):
            run_id = reap(conn, tid, text='resets in ~11h')
        notices = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='quota_episode_notice'", (tid,)).fetchall()
        assert len(notices) == 1
        assert 'reroute to gpt-test (openai-codex)' == kb._json_dict(notices[0][0])['action']
        with kb.write_txn(conn):
            record_episode(conn, tid, run_id)
        assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=? AND body LIKE 'Quota wall:%'", (tid,)).fetchone()[0] == 1
        # The alternative hits a shorter wall. Its reset must not expose the original provider.
        reap(conn, tid, provider='openai-codex')
        clock[0] = episode(conn, tid)['until']
        assert dispatch.check_respawn_guard(conn, tid) is None
        assert task_route(conn, tid)[:2] == ('gpt-test', 'openai-codex')


@pytest.mark.parametrize('wrong', ['run', 'task', 'claim', 'exit'])
def test_unowned_receipt_cannot_supply_reset(board, monkeypatch, wrong):
    from hermes_cli.kanban_quota_receipt import receipt_deadline
    now = 1_800_000_000
    monkeypatch.setattr(kb.time, 'time', lambda: now)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='custody', assignee='a')
        task = kb.claim_task(conn, tid, claimer=kb._claimer_id())
        kb._append_event(conn, 'other' if wrong == 'task' else tid, 'worker_result', {
            'exit_code': 1 if wrong == 'exit' else 75,
            'claim_lock': 'other' if wrong == 'claim' else task.claim_lock,
            'last_output': 'resets in ~11h'}, run_id=task.current_run_id + (1 if wrong == 'run' else 0))
        assert receipt_deadline(conn, tid, task.current_run_id) is None



# --- Provider custody for class routes: the runtime, not the admission receipt, names the wall ---

def _route_receipt(provider, model='claude-sonnet-5-5'):
    from types import SimpleNamespace
    from hermes_cli.kanban_quota_receipt import route_fields
    return route_fields(None, SimpleNamespace(provider=provider, model=model))


@pytest.mark.parametrize('custody', ['runtime', 'typed_refusal', 'none'])
def test_class_admission_reroutes_only_on_recorded_runtime_provider(board, monkeypatch, custody):
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    from hermes_cli.kanban_quota_receipt import route_fields
    now = 1_800_000_000
    monkeypatch.setattr(kb.time, 'time', lambda: now)
    _, cfg = routing(monkeypatch, profile=True)
    fields = {'runtime': _route_receipt('anthropic'),
              'typed_refusal': route_fields(SubscriptionQuotaUnavailable('anthropic', 'claude-sonnet-5-5', now + 3600)),
              'none': {}}[custody]
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='class wall', assignee='a')
        run_id = reap(conn, tid, text='resets in ~11h', fields=fields, admit=cfg)
        admitted = kb._json_dict(conn.execute(
            "SELECT payload FROM task_events WHERE run_id=? AND kind='run_route'", (run_id,)).fetchone()[0])
        assert admitted['provider'] is None  # real admission of a class route cannot know it
        guard = dispatch.check_respawn_guard(conn, tid)
        if custody == 'none':
            # Unknown wall: never guess that some other provider is safe.
            assert guard == 'subscription_quota_reset'
            assert task_route(conn, tid)[:2] != ('gpt-test', 'openai-codex')
        else:
            assert episode(conn, tid)['providers'] == ['anthropic']
            assert guard is None
            assert task_route(conn, tid)[:2] == ('gpt-test', 'openai-codex')


def test_route_fields_prefer_typed_refusal_and_never_record_auto():
    from types import SimpleNamespace
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    from hermes_cli.kanban_quota_receipt import route_fields
    refusal = RuntimeError('agent init failed')
    refusal.__cause__ = SubscriptionQuotaUnavailable('openai-codex', 'gpt-test', 1.0)
    assert route_fields(refusal, SimpleNamespace(provider='anthropic', model='m')) == {
        'route_provider': 'openai-codex', 'route_model': 'gpt-test'}
    assert route_fields(None, SimpleNamespace(provider='auto', model='class:standard')) == {}
    assert route_fields(None, None) == {}


@pytest.mark.parametrize('caps', [{'tools': False, 'streaming': True}, {'tools': True, 'streaming': True, 'context_tokens': 1000}])
def test_reroute_applies_worker_capability_requirements(board, monkeypatch, caps):
    now = 1_800_000_000
    monkeypatch.setattr(kb.time, 'time', lambda: now)
    config, _cfg = routing(monkeypatch, profile=True)
    config['model_routing']['routes']['standard'][1]['capabilities'] = caps
    config['model']['context_length'] = 200_000
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='capability', assignee='a')
        reap(conn, tid, text='resets in ~11h')
        # The only other route cannot serve the worker's tools/context floor: hold, don't pin it.
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'


@pytest.mark.parametrize('repin', [('gpt-test', 'openai-codex'), ('claude-opus-5-5', 'anthropic')])
def test_changed_legacy_pin_never_changes_the_runs_own_provider_judgement(board, monkeypatch, repin):
    now = 1_800_000_000
    monkeypatch.setattr(kb.time, 'time', lambda: now)
    _, cfg = routing(monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='pinned', assignee='a')
        # Historical raw selector seed (disposable DB): ignored; the episode is judged by the run's receipt.
        conn.execute("UPDATE tasks SET model_override='claude-sonnet-5-5', provider_override='anthropic', "
                     "model_pin_mandatory=1 WHERE id=?", (tid,))
        conn.commit()
        reap(conn, tid, text='resets in ~11h')
        before = dispatch.check_respawn_guard(conn, tid)
        conn.execute('UPDATE tasks SET model_override=?, provider_override=? WHERE id=?', (*repin, tid))
        conn.commit()
        assert dispatch.check_respawn_guard(conn, tid) == before  # a changed historical row moves nothing
        task = kb.claim_task(conn, tid, claimer=kb._claimer_id()) if before is None else kb.get_task(conn, tid)
        tier.apply_model_tier(conn, task, lane='ready', cfg=cfg)
        # The author row is never rewritten, and never becomes the run's route.
        persisted = kb.get_task(conn, tid)
        assert (persisted.model_override, persisted.provider_override) == repin


# --- Runtime-emitted reset phrasings: compound and minute windows are not cut short ---

@pytest.mark.parametrize('text, seconds', [
    ('usage limit resets in 2h 30m', 9000), ('usage limit resets in 2h30m', 9000),
    ('its usage limit resets in ~45 min. Send /retry', 2700),
    ('resets in ~2h; elsewhere resets in ~11h', 11 * 3600)])
def test_legacy_relative_text_uses_full_window(board, monkeypatch, text, seconds):
    from hermes_cli.kanban_quota_receipt import receipt_deadline
    now = 1_800_000_000
    monkeypatch.setattr(kb.time, 'time', lambda: now)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='text', assignee='a')
        run_id = reap(conn, tid, text=text)
        assert receipt_deadline(conn, tid, run_id) == now + seconds


# --- Faults: malformed deadlines and optional notice/routing failures never strand the reaper ---

@pytest.mark.parametrize('fields, text', [
    ({'failure_reason': 'rate_limit', 'failure_resets_at': 1e300}, ''),
    ({}, 'usage limit resets in 99999999999999999999h'),
    ({}, 'quota exhausted; retry after 99999999999999999999s')])
def test_unrepresentable_deadline_is_rejected_and_reaper_finishes(board, monkeypatch, fields, text):
    now = 1_800_000_000
    monkeypatch.setattr(kb.time, 'time', lambda: now)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='huge', assignee='a')
        for count in range(3):  # third reap renders the PT notice from the deadline
            reap(conn, tid, text=text, fields=fields)
        state = episode(conn, tid)
        assert state['hinted'] is False and state['until'] == now + 1200
        assert kb.get_task(conn, tid).status == 'ready'
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='quota_episode_notice'", (tid,)).fetchone()[0] == 1


def test_overflowing_retry_after_never_strands_the_sweep(board, monkeypatch):
    """A 400-digit 'retry after' overflows float(); the bystander dead worker must still be reaped."""
    now = 1_800_000_000
    monkeypatch.setattr(kb.time, 'time', lambda: now)
    with kbc.connect() as conn:
        bad = kb.create_task(conn, title='huge int', assignee='a')
        reap(conn, bad, text='quota exhausted; retry after ' + '9' * 400 + 's')
        state = episode(conn, bad)
        assert state['hinted'] is False and state['until'] == now + 300
        # Episode bookkeeping failing for one card leaves the others' reclaim intact.
        import hermes_cli.kanban_quota_episode as qe
        monkeypatch.setattr(qe, 'record_episode', lambda *a: (_ for _ in ()).throw(RuntimeError('boom')))
        other = kb.create_task(conn, title='bystander', assignee='a')
        reap(conn, other, text='resets in ~11h')
        assert kb.get_task(conn, other).status == 'ready'


def test_short_hints_never_undercut_doubling_backoff(board, monkeypatch):
    clock = [1_800_000_000]
    monkeypatch.setattr(kb.time, 'time', lambda: clock[0])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='short hint', assignee='a')
        for delay in [300, 600, 1200, 2400]:
            start = clock[0]
            reap(conn, tid, text='429 rate limited; retry after 5s')
            assert episode(conn, tid)['until'] == start + delay
            clock[0] += 10
            assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
            clock[0] = start + delay


def test_notice_routing_failure_falls_back_and_reaper_finishes(board, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(kb.time, 'time', lambda: now)
    routing(monkeypatch)
    def broken(_assignee):
        raise RuntimeError('profile config unreadable')
    monkeypatch.setattr('hermes_cli.kanban_model_assignment._assignee_config', broken)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='broken config', assignee='a')
        for _ in range(3):
            reap(conn, tid, text='resets in ~11h')
        notice = kb._json_dict(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='quota_episode_notice'", (tid,)).fetchone()[0])
        assert notice['action'].startswith('wait until') and notice['action'].endswith('PT')
        # Admission fails closed on the same config error: held, no exception out of the guard.
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'


def test_database_integrity_error_in_episode_record_propagates(board, monkeypatch):
    import sqlite3
    from hermes_cli.kanban_quota_episode import record_episode
    now = 1_800_000_000
    monkeypatch.setattr(kb.time, 'time', lambda: now)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='integrity', assignee='a')
        run_id = reap(conn, tid, text='resets in ~11h')
        conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='quota_episode'", (tid,))
        conn.commit()
        real = kb._append_event
        def failing(c, task_id, kind, *a, **k):
            if kind == 'quota_episode':
                raise sqlite3.IntegrityError('constraint failed')
            return real(c, task_id, kind, *a, **k)
        monkeypatch.setattr(kb, '_append_event', failing)
        with pytest.raises(sqlite3.IntegrityError):
            with kb.write_txn(conn):
                record_episode(conn, tid, run_id)


# --- Long backoff holds are quota episodes, not generic guard-hold fix cards ---

def test_long_cooldown_hold_files_no_generic_fix_card(board, monkeypatch, all_assignees_spawnable):
    clock = [1_800_000_000]
    monkeypatch.setattr(kb.time, 'time', lambda: clock[0])
    monkeypatch.setattr(dispatch.time, 'time', lambda: clock[0])
    from hermes_cli import kanban_db_dispatch_guard_notice as notice
    monkeypatch.setattr(notice.time, 'time', lambda: clock[0])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='long backoff', assignee='a')
        for _ in range(5):  # 300,600,1200,2400 -> 4800 s backoff on the fifth
            reap(conn, tid)
            clock[0] = episode(conn, tid)['until']
        reap(conn, tid)
        assert episode(conn, tid)['until'] - clock[0] == 7200
        spawn = lambda task, ws: None
        for _ in range(3):
            dispatch.dispatch_once(conn, spawn_fn=spawn)
            clock[0] += 40 * 60
        assert episode(conn, tid)['until'] > clock[0] - 40 * 60
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE title LIKE 'Fix respawn-guard hold%'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=? AND body LIKE '[respawn-guard]%'", (tid,)).fetchone()[0] == 0
        assert kb.get_task(conn, tid).status == 'ready'


# --- The third-requeue notice reaches subscribed chats through the existing notifiers ---

def _third_requeue_notice(conn, tid):
    for _ in range(3):
        reap(conn, tid, text='resets in ~11h')


def test_quota_notice_delivered_once_by_gateway_notifier(board, monkeypatch):
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '0')  # explicit sleep-driven legacy transport proof
    import asyncio
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from hermes_cli import kanban_db_notify as kbn
    now = 1_800_000_000
    monkeypatch.setattr(kb.time, 'time', lambda: now)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='notify quota', assignee='a')
        kbn.add_notify_sub(conn, task_id=tid, platform='telegram', chat_id='chat-1')
        _third_requeue_notice(conn, tid)

    class Adapter:
        sent = []
        async def send(self, chat_id, text, metadata=None):
            self.sent.append(text)

    adapter = Adapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    real_sleep = asyncio.sleep
    async def tick(runner):
        async def fake_sleep(delay):
            if delay == 5:
                return
            runner._running = False
            await real_sleep(0)
        monkeypatch.setattr(asyncio, 'sleep', fake_sleep)
        await runner._kanban_notifier_watcher(interval=1)
    asyncio.run(tick(runner))
    quota = [t for t in adapter.sent if 'quota wall' in t]
    assert len(quota) == 1 and tid in quota[0] and 'PT' in quota[0]
    runner._running = True
    asyncio.run(tick(runner))
    assert len([t for t in adapter.sent if 'quota wall' in t]) == 1


def test_quota_notice_delivered_once_to_desktop_chat(board, monkeypatch):
    from hermes_cli import kanban_db_notify as kbn
    from gateway.warning_notifications import DiagnosticText
    from tui_gateway.server import _collect_kanban_notifications
    now = 1_800_000_000
    monkeypatch.setattr(kb.time, 'time', lambda: now)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='notify desktop', assignee='a')
        kbn.add_notify_sub(conn, task_id=tid, platform='tui', chat_id='desk-1')
        _third_requeue_notice(conn, tid)
    texts = [t for t in _collect_kanban_notifications({'session_key': 'desk-1'}) if 'quota wall' in t]
    assert len(texts) == 1 and tid in texts[0] and isinstance(texts[0], DiagnosticText)
    assert [t for t in _collect_kanban_notifications({'session_key': 'desk-1'}) if 'quota wall' in t] == []
