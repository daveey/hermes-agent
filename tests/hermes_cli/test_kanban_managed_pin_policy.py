"""Kanban pins must pass the managed subscription policy at create time, not at worker startup. [daveey]

On the hub (``model_routing.enforce_subscription_only: true``) two cards pinned to the retired
``claude-sonnet-4-5`` were accepted by ``kanban_create``; each worker then died at startup with
"Managed subscription policy rejects unregistered or ambiguous concrete pin", exited 1 like an
ordinary crash, and was retried until triage. The create-time check only caught vendor/provider
family mismatches. These tests pin the three layers of the fix: create refuses, dispatch blocks a
pin already in the DB without spawning, and a worker that still hits the policy exits terminal.
"""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

def _managed_yaml(enabled=True):
    lines = ['model_routing:', '  enabled: true', '  policy_version: 1', '  allow_metered: false',
             f'  enforce_subscription_only: {"true" if enabled else "false"}', '  routes:']
    for level, provider, model in (('deep', 'anthropic', 'claude-opus-5-5'),
                                   ('standard', 'anthropic', 'claude-sonnet-5-5'),
                                   ('standard', 'openai-codex', 'gpt-6.1-sol')):
        if f'    {level}:' not in lines:
            lines.append(f'    {level}:')
        lines += [f'      - provider: {provider}', f'        model: {model}', '        billing: subscription',
                  '        weight: 1', '        eligible: true', '        funding_verified: true',
                  '        capabilities: {tools: true, streaming: true}']
    return '\n'.join(lines) + '\n'


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    (home / 'config.yaml').write_text('kanban:\n  orchestrator_profile: supervisor\n' + _managed_yaml())

    def configure(enforce=True):
        for name in ('worker', 'supervisor'):
            profile = home / 'profiles' / name
            profile.mkdir(parents=True, exist_ok=True)
            (profile / 'config.yaml').write_text(
                'model:\n  default: claude-sonnet-5-5\n  provider: anthropic\n'
                'platform_toolsets:\n  cli: [terminal, file, kanban]\nterminal:\n  backend: local\n'
                + _managed_yaml(enforce))

    configure()
    kb.init_db()
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        yield conn, configure


def _count(conn):
    return conn.execute('SELECT count(*) FROM tasks').fetchone()[0]


@pytest.mark.parametrize('provider', [None, 'anthropic'])
def test_create_refuses_retired_pin_under_managed_policy(board, provider):
    conn, _ = board
    with pytest.raises(ValueError, match='model-neutral') as info:
        kb.create_task(conn, title='Check live', assignee='worker',
                       model_override='claude-sonnet-4-5', provider_override=provider)
    message = str(info.value)
    assert 'class/tier' in message
    assert _count(conn) == 0


def test_create_refuses_registered_pin(board):
    conn, _ = board
    with pytest.raises(ValueError, match='model-neutral'):
        kb.create_task(conn, title='Review', assignee='worker',
                       model_override='claude-sonnet-5-5', provider_override='anthropic')
    assert _count(conn) == 0


def test_create_refuses_registered_model_on_unregistered_provider(board):
    conn, _ = board
    with pytest.raises(ValueError, match='model-neutral'):
        kb.create_task(conn, title='Review', assignee='worker',
                       model_override='claude-sonnet-5-5', provider_override='openrouter')


def test_class_selector_and_unpinned_cards_are_untouched(board):
    conn, _ = board
    kb.create_task(conn, title='Unpinned', assignee='worker')
    kb.create_task(conn, title='Class', assignee='worker', model_override='class:deep')
    assert _count(conn) == 2


def test_policy_off_still_rejects_concrete_restrictions(board):
    conn, configure = board
    configure(enforce=False)
    with pytest.raises(ValueError, match='model-neutral'):
        kb.create_task(conn, title='Legacy', assignee='worker',
                       model_override='claude-sonnet-4-5', provider_override='anthropic')
    assert _count(conn) == 0


def test_set_model_override_refuses_retired_pin(board):
    conn, _ = board
    tid = kb.create_task(conn, title='Card', assignee='worker')
    with pytest.raises(ValueError, match='model-neutral'):
        kb.set_model_override(conn, tid, 'claude-sonnet-4-5', provider='anthropic')
    assert kb.get_task(conn, tid).model_override is None


def test_kanban_create_tool_returns_typed_refusal(board, monkeypatch):
    from tools import kanban_tools as tools
    conn, _ = board
    parent = kb.create_task(conn, title='Parent', assignee='worker')
    monkeypatch.setenv('HERMES_KANBAN_TASK', parent)
    monkeypatch.setattr(tools, '_persisted_identity', lambda: 'worker')
    result = json.loads(tools._handle_create({'title': 'Child', 'model': 'claude-sonnet-4-5'}))
    assert 'model-neutral' in result['error']
    assert _count(conn) == 1


def test_kanban_create_tool_model_description_names_the_registry():
    from tools.kanban_tools_schemas import KANBAN_CREATE_SCHEMA
    description = KANBAN_CREATE_SCHEMA['parameters']['properties']['model']['description']
    assert 'class' in description.lower() and 'concrete' in description.lower()


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_dispatch_ignores_legacy_pin_without_rewriting_author_evidence(board, lane):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    conn, _ = board
    tid = kb.create_task(conn, title='Legacy route', assignee='worker', session_id='chat')
    conn.execute('UPDATE tasks SET model_override=?, status=? WHERE id=?', ('claude-sonnet-4-5', lane, tid))
    conn.commit()
    spawned = []
    dispatch_once(conn, spawn_fn=lambda item, *args: spawned.append(item.model_override), max_spawn=1)
    assert spawned == (['class:standard'] if lane == 'ready' else ['claude-opus-5-5'])
    assert kb.get_task(conn, tid).model_override == 'claude-sonnet-4-5'
    event = [e.payload for e in kb.list_events(conn, tid) if e.kind == 'model_tier'][-1]
    if lane == 'review':
        assert event['implementer_models'] == ['claude-sonnet-4-5']


def test_dispatch_dry_run_ignores_bad_pin_without_writing(board):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    conn, _ = board
    tid = kb.create_task(conn, title='Legacy route', assignee='worker')
    conn.execute('UPDATE tasks SET model_override=? WHERE id=?', ('claude-sonnet-4-5', tid))
    conn.commit()
    before = list(conn.iterdump())
    result = dispatch_once(conn, dry_run=True)
    assert (tid, 'model_pin_not_in_routing_registry') not in result.respawn_guarded
    assert list(conn.iterdump()) == before


def test_worker_startup_policy_rejection_is_terminal_not_a_crash(monkeypatch, tmp_path):
    """A worker that still reaches the policy check must exit EX_CONFIG (blocked after one spawn),
    not exit 1 (retried as a transient crash until triage)."""
    from agent.model_routing import ModelRoutingError
    from agent.model_routing_managed import ManagedPinRejectedError
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

    home = tmp_path / 'hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr('cli._cprint', lambda *a, **k: None, raising=False)

    def shell():
        s = CLIAgentSetupMixin.__new__(CLIAgentSetupMixin)
        s.model, s.requested_provider = 'claude-sonnet-4-5', None
        s._explicit_api_key = s._explicit_base_url = None
        s._fallback_model, s.tool_progress_mode = [], 'off'
        return s

    def _reject(**kw):
        raise ManagedPinRejectedError('claude-sonnet-4-5', None, ['claude-sonnet-5-5'])

    monkeypatch.setattr('hermes_cli.runtime_provider.resolve_runtime_provider', _reject)
    rejected = shell()
    assert rejected._ensure_runtime_credentials() is False
    assert rejected._credentials_terminal is True

    def _other(**kw):  # other routing errors (e.g. credentials for a valid pin) stay retryable
        raise ModelRoutingError('Class routing credentials unavailable; legacy fallback is prohibited')

    monkeypatch.setattr('hermes_cli.runtime_provider.resolve_runtime_provider', _other)
    other = shell()
    assert other._ensure_runtime_credentials() is False
    assert other._credentials_terminal is False


def test_pin_policy_rejection_is_typed_and_keeps_its_message():
    from agent.model_routing import ModelRoutingError
    from agent.model_routing_managed import ManagedPinRejectedError, pin_policy
    routes = {'deep': [('anthropic', 'claude-opus-5-5')],
              'standard': [('anthropic', 'claude-sonnet-5-5'), ('openai-codex', 'gpt-6.1-sol')]}
    config = {'model_routing': {
        'enabled': True, 'policy_version': 1, 'allow_metered': False, 'enforce_subscription_only': True,
        'routes': {level: [{'provider': p, 'model': m, 'billing': 'subscription', 'weight': 1, 'eligible': True,
                            'funding_verified': True, 'capabilities': {'tools': True, 'streaming': True}}
                           for p, m in pairs] for level, pairs in routes.items()}}}
    with pytest.raises(ManagedPinRejectedError) as info:
        pin_policy('claude-sonnet-4-5', None, config)
    assert isinstance(info.value, ModelRoutingError)
    assert 'Managed subscription policy rejects unregistered or ambiguous concrete pin' in str(info.value)
    assert info.value.allowed == ['claude-opus-5-5', 'claude-sonnet-5-5', 'gpt-6.1-sol']


FABLE = 'claude-fable-5-2'


def _hand_off_for_review(conn, metadata, *, pin=None):
    """Implementer run spawned through dispatch (so its run_route receipt exists), then a review handoff."""
    from hermes_cli.kanban_db_dispatch import dispatch_once
    tid = kb.create_task(conn, title='Browser build', assignee='worker')
    if pin:
        conn.execute("UPDATE tasks SET model_override=?, provider_override='anthropic' WHERE id=?", (pin, tid))
        conn.commit()
    dispatch_once(conn, spawn_fn=lambda *args: None, max_spawn=1)
    assert kb.get_task(conn, tid).status == 'running'
    claimed = kb.get_task(conn, tid)
    kb._append_event(conn, tid, 'worker_result', {'route_model': pin or 'claude-sonnet-5-5',
                                               'route_provider': 'anthropic'}, run_id=claimed.current_run_id)
    assert kb.request_review(conn, tid, summary='Ready for independent review')
    conn.execute("UPDATE task_runs SET metadata=? WHERE task_id=? AND outcome='review_requested'",
                 (json.dumps(metadata or {}), tid))
    conn.commit()
    return tid


def test_refused_review_override_falls_back_to_registered_independent_reviewer(board):
    """t_01a1ce48c929f055 (2026-11-12): handoff metadata review_model=claude-fable-5-2, which only the hub
    registers. Pool-worker review spawns died with ManagedPinRejectedError until the card gave up."""
    from hermes_cli.kanban_db_dispatch import dispatch_once
    conn, _ = board
    tid = _hand_off_for_review(conn, {'review_model': FABLE, 'review_provider': 'anthropic'})
    seen = []
    dispatch_once(conn, spawn_fn=lambda item, *args: seen.append((item.model_override, item.provider_override)),
                  max_spawn=1)
    assert seen == [('claude-opus-5-5', 'anthropic')]
    tier = [e.payload for e in kb.list_events(conn, tid) if e.kind == 'model_tier'][-1]
    assert tier['reason'] == 'review_unpinned' and tier['lane'] == 'review'
    assert tier['model'] == 'claude-opus-5-5'
    assert 'requested' not in tier
    assert tier['implementer_models'] == ['claude-sonnet-5-5']
    assert kb.get_task(conn, tid).consecutive_failures == 0


def test_refused_review_override_may_use_the_registered_model_the_author_ran(board):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    conn, _ = board
    # The implementer ran on the only registered deep model; model diversity is retired, so it may review too.
    tid = _hand_off_for_review(conn, {'review_model': FABLE}, pin='claude-opus-5-5')
    seen = []
    dispatch_once(conn, spawn_fn=lambda item, *args: seen.append(item.model_override), max_spawn=1)
    assert seen == ['claude-opus-5-5']
    event = [e.payload for e in kb.list_events(conn, tid) if e.kind == 'model_tier'][-1]
    assert event['reason'] == 'review_override_refused' and event['requested']['model'] == FABLE
    assert kb.get_task(conn, tid).consecutive_failures == 0


def _disable_anthropic(profile='worker'):
    """Unavailable routing fixture: the registry's only deep route (anthropic) is no longer enabled on the host."""
    import os
    path = Path(os.environ['HERMES_HOME']) / 'profiles' / profile / 'config.yaml'
    path.write_text(path.read_text() + 'providers:\n  anthropic:\n    enabled: false\n')


def test_refused_review_override_without_registered_reviewer_never_spawns(board):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    conn, _ = board
    tid = _hand_off_for_review(conn, {'review_model': FABLE}, pin='claude-opus-5-5')
    _disable_anthropic()
    seen = []
    dispatch_once(conn, spawn_fn=lambda *args: seen.append(args), max_spawn=1)
    assert seen == []
    task = kb.get_task(conn, tid)
    assert task.status == 'triage' and task.claim_lock is None
    failed = [e.payload for e in kb.list_events(conn, tid) if e.kind == 'gave_up'][-1]
    assert failed['infrastructure'] is True
    assert FABLE in failed['error'] and 'model_pin_not_in_routing_registry' in failed['error']


def test_registered_review_override_is_kept(board):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    conn, _ = board
    tid = _hand_off_for_review(conn, {'review_model': 'claude-opus-5-5'})
    seen = []
    dispatch_once(conn, spawn_fn=lambda item, *args: seen.append(item.model_override), max_spawn=1)
    assert seen == ['claude-opus-5-5']
    assert [e.payload for e in kb.list_events(conn, tid) if e.kind == 'model_tier'][-1]['reason'] == 'review_unpinned'


def _receipts(conn, tid, *, route=None, ran=None):
    """Rewrite the handoff run's run_route receipt to *route* and/or book a worker_result that ran *ran*."""
    run = conn.execute("SELECT id FROM task_runs WHERE task_id=? AND outcome='review_requested' "
                       "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0]
    if route:
        conn.execute("DELETE FROM task_events WHERE task_id=? AND run_id=? AND kind='worker_result'", (tid, run))
        conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND run_id=? AND kind='run_route'",
                     (json.dumps({'model': route[0], 'provider': route[1], 'lane': 'ready',
                                  'source': 'class' if route[0].startswith('class:') else 'pin'}), tid, run))
    if ran:
        kb._append_event(conn, tid, 'worker_result', {'exit_code': 0, 'route_provider': ran[1],
                                                      'route_model': ran[0]}, run_id=run)
    conn.commit()


def _second_deep_route():
    """Register gpt-6.1-sol as a second deep route on the worker profile."""
    import os
    path = Path(os.environ['HERMES_HOME']) / 'profiles' / 'worker' / 'config.yaml'
    extra = ('      - provider: openai-codex\n        model: gpt-6.1-sol\n        billing: subscription\n'
             '        weight: 1\n        eligible: true\n        funding_verified: true\n'
             '        capabilities: {tools: true, streaming: true}\n')
    text = path.read_text()
    path.write_text(text.replace('    standard:\n', extra + '    standard:\n', 1))


def test_class_routed_implementer_receipts_do_not_gate_review_admission(board):
    """A class-routed implementer run (run_route ``class:deep``) with a worker_result naming what ran no longer
    matters to a refused override: the registered deep route reviews regardless of attribution."""
    from hermes_cli.kanban_db_dispatch import dispatch_once
    conn, _ = board
    tid = _hand_off_for_review(conn, {'review_model': FABLE})
    _receipts(conn, tid, route=('class:deep', None), ran=('claude-sonnet-5-5', 'anthropic'))
    seen = []
    dispatch_once(conn, spawn_fn=lambda item, *args: seen.append(item.model_override), max_spawn=1)
    assert seen == ['claude-opus-5-5']
    assert [e.payload for e in kb.list_events(conn, tid) if e.kind == 'model_tier'][-1]['reason'] == (
        'review_unpinned')


@pytest.mark.parametrize('lane', ['review', 'ready'])
def test_review_owned_preclaim_does_not_resolve_generic_class(board, monkeypatch, lane):
    """Review lanes and skill-classified ready retries retain review-owned refusal accounting."""
    from hermes_cli.kanban_class_walls import unroutable_class
    from hermes_cli.kanban_dispatch_skip import pinned_quota_hold
    from hermes_cli import kanban_model_tier as tier
    from hermes_cli.kanban_review_model import is_review_run
    conn, _ = board
    tid = _hand_off_for_review(conn, {'review_model': FABLE})
    _receipts(conn, tid, route=('class:deep', None))
    if lane == 'ready':
        conn.execute("UPDATE tasks SET status='ready', skills=? WHERE id=?", ('["sdlc-review"]', tid))
        conn.commit()
    assert is_review_run(conn, kb.get_task(conn, tid), lane)
    before = list(conn.iterdump())

    def generic_resolution_forbidden(*args, **kwargs):
        pytest.fail('generic preclaim must not resolve a review route')

    monkeypatch.setattr(tier, 'resolve_model_tier', generic_resolution_forbidden)
    assert unroutable_class(conn, tid, lane=lane) is None
    assert pinned_quota_hold(conn, tid, lane=lane) is None
    assert list(conn.iterdump()) == before  # no claim, run, receipt or author changes


@pytest.mark.parametrize('lane', ['review', 'ready'])
def test_repeated_identical_review_refusal_moves_to_a_visible_block(board, monkeypatch, lane):
    """With no eligible class route, the third identical refusal stops deferring silently."""
    from hermes_cli.kanban_db_dispatch import dispatch_once
    DEFERRAL_LIMIT = 3  # hermes_cli.kanban_review_model.DEFERRAL_LIMIT
    monkeypatch.setenv('HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS', '0')
    conn, _ = board
    tid = _hand_off_for_review(conn, {'review_model': 'class:deep'})
    _receipts(conn, tid, route=('class:deep', None))
    if lane == 'ready':
        conn.execute("UPDATE tasks SET status='ready', skills=? WHERE id=?", ('["sdlc-review"]', tid))
        conn.commit()
    _disable_anthropic()
    seen = []
    runs_before = [tuple(r) for r in conn.execute('SELECT * FROM task_runs WHERE task_id=?', (tid,))]
    claims_before = [e.id for e in kb.list_events(conn, tid) if e.kind == 'claimed']
    for attempt in range(1, DEFERRAL_LIMIT + 1):
        dispatch_once(conn, spawn_fn=lambda *args: seen.append(args), max_spawn=1)
        task = kb.get_task(conn, tid)
        assert [tuple(r) for r in conn.execute('SELECT * FROM task_runs WHERE task_id=?', (tid,))] == runs_before
        assert [e.id for e in kb.list_events(conn, tid) if e.kind == 'claimed'] == claims_before
        assert task.claim_lock is None
        if attempt < DEFERRAL_LIMIT:
            assert (task.status, task.consecutive_failures) == (lane, 0), attempt
    assert seen == []
    assert task.status == 'triage'
    gave_up = [e.payload for e in kb.list_events(conn, tid) if e.kind == 'gave_up'][-1]
    # The breaker owns reason_code (worker_failures_exhausted); the admission refusal rides beside it.
    assert gave_up['admission_refusal'] == 'review_model_unavailable' and gave_up['sticky'] is True
    assert 'class:deep' in gave_up['error'] and 'no_eligible_routes' in gave_up['error']


@pytest.mark.parametrize('override', [None, FABLE])
@pytest.mark.parametrize('receipt', ['run_route', 'worker_result'])
def test_rerouted_pin_reviewer_is_a_registered_deep_route_whatever_ran(board, override, receipt):
    """The pin and rerouted author are attribution only: a registered deep route reviews, a refused override says so."""
    from hermes_cli.kanban_db_dispatch import dispatch_once
    conn, _ = board
    _second_deep_route()
    tid = _hand_off_for_review(conn, {'review_model': override} if override else None, pin='claude-opus-5-5')
    ran = ('gpt-6.1-sol', 'openai-codex')
    _receipts(conn, tid, **({'route': ran} if receipt == 'run_route' else {'ran': ran}))
    seen = []
    dispatch_once(conn, spawn_fn=lambda item, *args: seen.append(item.model_override), max_spawn=1)
    event = [e.payload for e in kb.list_events(conn, tid) if e.kind == 'model_tier'][-1]
    assert seen == [event['model']] and seen[0] in ('claude-opus-5-5', 'gpt-6.1-sol')
    assert event['reason'] == ('review_unpinned' if override is None else 'review_override_refused')
    assert kb.get_task(conn, tid).consecutive_failures == 0
