"""Model quota walls must not turn a serving sibling into refusal_exhausted."""
from types import SimpleNamespace
import time

import pytest

from hermes_cli import kanban_provider_walls as pw
from hermes_cli import kanban_refusal_fallback as fb
from hermes_cli import kanban_ratio_routing as ratio
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli.test_kanban_quota_episode import board as board  # noqa: PLC0414 -- pytest fixture reexport

A, B, P = 'claude-fable-5-2', 'claude-sonnet-5-5', 'anthropic'


@pytest.fixture
def wallfile(tmp_path, monkeypatch):
    path = tmp_path / 'walls.json'
    monkeypatch.setattr(pw, 'walls_path', lambda: path)
    monkeypatch.setattr(pw, 'live_quota_wall', lambda *_: None)
    monkeypatch.setattr(pw, 'live_pool_serving', lambda *_: True)
    return path


@pytest.mark.parametrize('scoped', [True, False])
def test_legacy_quota_exit_scopes_only_explicit_model_evidence(board, wallfile, scoped):
    conn = kbc.connect()
    tid = kb.create_task(conn, title='quota wall regression', assignee='default')
    with kb.write_txn(conn):
        kb._append_event(conn, tid, 'model_tier', {'provider': P, 'model': A}, run_id=None)
    text = f'HTTP 429: Subscription quota unavailable for {P}/{A}; resets in ~30-60m' if scoped else 'HTTP 429: provider quota exhausted'
    log = board / 'quota.log'
    log.write_text(text)
    pw.record_wall_for_run(conn, tid, None, log)
    assert set(pw.active_walls()) == {f'{P}/{A}' if scoped else P}


@pytest.mark.parametrize('scoped', [True, False])
def test_refusal_class_peers_and_admission_preserve_serving_sibling(board, wallfile, monkeypatch, scoped):
    pw.record_wall(P, 3600, model=A if scoped else None)
    monkeypatch.setattr(ratio, '_pool_routes', lambda *_: [(P, [A, B])])
    conn = kbc.connect()
    tid = kb.create_task(conn, title='serving sibling admission', assignee='default')
    task = kb.get_task(conn, tid)
    # A prior safety refusal still excludes Codex; Anthropic B is the remaining eligible route.
    monkeypatch.setattr(fb, 'refused_providers', lambda *_: ['openai-codex'])
    monkeypatch.setattr(fb, '_class_model', lambda *_: 'class:standard')
    from hermes_cli import kanban_review_model as review
    monkeypatch.setattr(review, 'is_review_run', lambda *_: False)
    result = SimpleNamespace(skipped_ineligible=[])
    assert fb.class_peers(task, 'class:standard') == ([(B, P)] if scoped else [])
    assert fb.admission(conn, tid, result, dry_run=True) is (not scoped)
    assert result.skipped_ineligible == ([] if scoped else [(tid, 'refusal_exhausted')])


def test_scoped_deadlines_keep_provider_wide_wall_and_longer_reset(wallfile):
    now = time.time()
    pw.record_wall(P, 3600, model=A, now=now)
    pw.record_wall(P, 7200, now=now)
    walls = pw.active_walls(now=now)
    assert pw.wall_until(walls, P, A) == now + 7200
    assert pw.wall_until(walls, P, B) == now + 7200
    assert pw.drop_wall(P, model=A, not_after=now + 3600)
    assert pw.active_walls(now=now) == {P: now + 7200}


def test_ratio_and_retier_match_exact_model(wallfile):
    pw.record_wall(P, 3600, model=A)
    entry = {'ratio': 1, 'rate_tokens_per_s': 1}
    walls = pw.active_walls()
    assert ratio._eligible(P, A, entry, walls) is None
    assert ratio._eligible(P, B, entry, walls) == 1
    task = SimpleNamespace(model_override=B, provider_override=P)
    cfg = {'default_model': 'gpt-test', 'default_provider': 'openai-codex'}
    assert pw.retier_for_walls(task, cfg) is None
    task.model_override = A
    assert pw.retier_for_walls(task, cfg) == ('gpt-test', 'openai-codex', P)


@pytest.mark.parametrize('text', [
    'HTTP 429: provider quota exhausted',
    f'Subscription quota unavailable for other/{A}; resets at 9999999999',
    f'Subscription quota unavailable for {P}/{A}; Subscription quota unavailable for {P}/{B};',
])
def test_unknown_or_ambiguous_scope_stays_broad(text):
    assert pw.quota_model(text, P) is None


@pytest.mark.parametrize('scoped', [True, False])
def test_exact_receipt_reaper_and_neutral_release_scoping(board, wallfile, monkeypatch, scoped):
    from tests.hermes_cli.test_kanban_quota_episode import reap
    from hermes_cli import kanban_quota_release as release
    from hermes_cli import kanban_quota_episode as quota
    monkeypatch.setattr(pw, 'live_pool_serving', lambda *_: False)
    conn = kbc.connect()
    tid = kb.create_task(conn, title='exact quota receipt', assignee='default')
    text = f'Subscription quota unavailable for {P}/{A}; resets in ~2h' if scoped else 'HTTP 429: provider quota; resets in ~2h'
    run = reap(conn, tid, text=text, fields={'route_provider': P, 'route_model': A})
    # The reaper's legacy mirror and the durable neutral-effect writer must agree.
    expected = f'{P}/{A}' if scoped else P
    assert set(pw.active_walls()) == {expected}
    assert pw.receipt_wall_model(conn, tid, run, P) == (A if scoped else None)
    wallfile.unlink()
    state = quota.episode(conn, tid)
    release.finish({**state, 'write_wall': True, 'wall_model': pw.receipt_wall_model(conn, tid, run, P)})
    assert set(pw.active_walls()) == {expected}


def test_pruning_requires_the_walled_model_to_serve(wallfile, monkeypatch):
    pw.record_wall(P, 86400, model=A)
    monkeypatch.setattr(pw, 'live_pool_serving', lambda provider, model=None: model == B)
    assert pw.prune_disproved_walls() == []
    monkeypatch.setattr(pw, 'live_pool_serving', lambda provider, model=None: model == A)
    assert pw.prune_disproved_walls() == [f'{P}/{A}']


def test_class_wall_routes_match_model_not_provider(wallfile, monkeypatch):
    from tests.hermes_cli.test_kanban_quota_episode import routing
    from hermes_cli.kanban_class_walls import class_wall_routes
    config, _ = routing(monkeypatch)
    template = config['model_routing']['routes']['standard'][0]
    config['model_routing']['routes']['standard'] = [dict(template, provider=P, model=m) for m in (A, B)]
    pw.record_wall(P, 3600, model=A)
    routes, known = class_wall_routes(SimpleNamespace(assignee='a'), 'class:standard', now=time.time())
    assert [(r['model'], r['provider']) for r in routes] == [(B, P)]
    assert set(known) == {P}


def test_releasing_scoped_episode_preserves_independent_broad_wall(wallfile):
    from hermes_cli.kanban_quota_episode_release import _drop_walls
    now = time.time()
    pw.record_wall(P, 7200, model=A, now=now)
    pw.record_wall(P, 3600, now=now)
    _drop_walls([{'provider': P, 'wall_model': A, 'until': now + 7200}])
    assert pw.active_walls(now=now) == {P: now + 3600}


def test_owned_model_episode_can_admit_serving_sibling(board, wallfile, monkeypatch):
    from tests.hermes_cli.test_kanban_quota_episode import reap, routing
    from hermes_cli import kanban_quota_episode as quota
    config, _ = routing(monkeypatch)
    template = config['model_routing']['routes']['standard'][0]
    config['model_routing']['routes']['standard'] = [dict(template, provider=P, model=m) for m in (A, B)]
    monkeypatch.setattr(pw, 'live_pool_serving', lambda provider, model=None: model == B)
    conn = kbc.connect()
    tid = kb.create_task(conn, title='owned model quota', assignee='a')
    reap(conn, tid, text=f'Subscription quota unavailable for {P}/{A}; resets in ~2h',
         fields={'route_provider': P, 'route_model': A})
    task = kb.get_task(conn, tid)
    assert quota.episode(conn, tid)['wall_model'] == A
    assert set(pw.active_walls()) == {f'{P}/{A}'}
    assert quota.decision(conn, task, base=(B, P, 'default')) == (None, None)
    assert quota.decision(conn, task, base=('class:standard', 'auto', 'default')) == (None, (B, P))


@pytest.mark.parametrize('state', ['walled', 'unknown'])
def test_release_probes_every_scoped_model_before_releasing_provider(state):
    from hermes_cli import kanban_quota_probe as probe
    seen = []
    def verdict(provider, model):
        seen.append((provider, model))
        return probe.Verdict(state if model == A else 'headroom', model)
    found = probe.verdicts([{'provider': P, 'wall_model': m} for m in (A, B)], B, verdict)
    assert set(seen) == {(P, A), (P, B)}
    assert found[P]['state'] == state
