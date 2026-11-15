"""Quota guard bucket-derived names must reach the canonical recovery check."""
import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_shared_blocker_checks as checks
from hermes_cli import kanban_shared_blockers as blockers
from tests.hermes_cli import test_kanban_shared_blockers as helpers

kanban_home = helpers.kanban_home


@pytest.mark.parametrize('login', ['daveey', 'octo', 'octo-cat'])
@pytest.mark.parametrize('suffix', ['', '-reserve', '-quota', '-quota-reserve'])
def test_core_bucket_identity(login, suffix):
    assert blockers.normalize_blocker_id(f'github-{login}-core{suffix}', cfg={}) == 'github-rest-quota'


@pytest.mark.parametrize('slug', [
    'github-daveey-graphql-reserve', 'github-octo-graphql-quota',
    'github-git-ops-outage', 'github-api-rate-limit',
    'github-actions-billing', 'github-actions-billing-outage',
    'bb-tuner-github-token-stale',
])
def test_unrelated_walls_remain_distinct(slug):
    assert blockers.normalize_blocker_id(slug, cfg={}) == slug


def test_bucket_runs_coalesce_and_check_clears(kanban_home):
    with kbc.connect_closing() as conn:
        for slug in ['github-daveey-core-reserve', 'github-octo-core-quota']:
            helpers._legacy_blocked_behind(conn, slug, blocker_id=slug)
        states = blockers.blocker_states(conn)
        assert set(states) == {'github-rest-quota'}
        assert states['github-rest-quota'].is_open
        assert len(states['github-rest-quota'].behind) == 2
        cfg = {'kanban': {'shared_blocker_checks': {'github-rest-quota': 'exit 0'}}}
        assert checks.run_due_checks(conn, cfg=cfg) == ['github-rest-quota']
        checks.apply_check_clears(conn, states)
        assert not states['github-rest-quota'].is_open
        assert states['github-rest-quota'].cleared_via == 'check'
