"""Raw parent testimony must not become effective lifecycle clearance. [daveey]"""
import pytest

from hermes_cli import kanban_actionability_parents as parents
from hermes_cli.kanban_actionability_materialize import StaleIntent


def test_known_parent_completion_stales():
    with pytest.raises(StaleIntent):
        parents.check([{'id': 'p', 'status': 'ready', 'containment_episode': None}],
                      [{'id': 'p', 'status': 'done', 'containment_episode': None}])


def test_mixed_satisfied_and_live_parents_are_not_invalid():
    snapshot = [{'id': 'a', 'status': 'done', 'containment_episode': None},
                {'id': 'b', 'status': 'ready', 'containment_episode': None}]
    parents.check(snapshot, snapshot)


@pytest.mark.parametrize('status,episode,exception', [('ready', None, ValueError), ('done', None, StaleIntent),
                                                   ('triage', 'hold', StaleIntent)])
def test_unknown_never_filled_from_canonical(status, episode, exception):
    with pytest.raises(exception):
        parents.check([{'id': 'p', 'status': None, 'containment_episode': None}],
                      [{'id': 'p', 'status': status, 'containment_episode': episode}])


@pytest.mark.parametrize('bad', [None, {}, [], [{'id': 'p', 'status': 'ready'}],
                                [{'id': 'p', 'status': 'bogus', 'containment_episode': None}],
                                [{'id': 'p', 'status': None, 'containment_episode': 'hold'}]])
def test_missing_or_malformed_testimony_stays_unavailable(bad):
    with pytest.raises(ValueError):
        parents.check(bad, [{'id': 'p', 'status': 'ready', 'containment_episode': None}])


def test_parent_free_compatibility():
    parents.check(None, [])


def test_containment_identity_change_is_stale():
    with pytest.raises(StaleIntent):
        parents.check([{'id': 'p', 'status': 'triage', 'containment_episode': 'old'}],
                      [{'id': 'p', 'status': 'triage', 'containment_episode': 'new'}])
