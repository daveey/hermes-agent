"""Malformed producer contributions never corrupt a caller-owned row batch."""
from copy import deepcopy

import pytest

from hermes_cli import kanban_annotation_facts as annotations
from tests.hermes_cli import test_kanban_annotation_facts as helpers

setup = helpers.setup


@pytest.mark.parametrize('observations', [None, 'bad', {'source': 'kanban.card'}, [None],
                                         [{'source': 'turn.lease'}]])
def test_bad_second_observation_contribution_is_atomic(setup, observations):
    store, scope, consumer, *_ = setup
    helpers.publish(store, scope, 'first', 'kanban', {'kanban_active': 2})
    helpers.publish(store, scope, 'second', 'kanban', {'execution_observations': observations})
    rows = [{'id': 'first', 'kanban_active': 1}, {'id': 'second', 'kanban_active': 1,
             'execution_observations': [{'source': 'turn.lease', 'state': 'running'}]}]
    before = deepcopy(rows)
    with pytest.raises(annotations.AnnotationUnavailable):
        consumer.annotate('kanban', rows, helpers.forbidden)
    assert rows == before


@pytest.mark.parametrize('family', ['goal', 'cron', 'review'])
def test_scalar_family_cannot_replace_other_observations(setup, family):
    store, scope, consumer, *_ = setup
    helpers.publish(store, scope, 's', family, {'execution_observations': []})
    rows = [{'id': 's', 'execution_observations': [{'source': 'turn.lease', 'state': 'running'}]}]
    before = deepcopy(rows)
    with pytest.raises(annotations.AnnotationUnavailable):
        consumer.annotate(family, rows, helpers.forbidden)
    assert rows == before


def test_kanban_replica_run_retention_matches_legacy(setup):
    store, scope, consumer, *_ = setup
    old = {'source': 'kanban.replica_run', 'state': 'unknown'}
    new = {'source': 'kanban.replica_run', 'state': 'waiting'}
    helpers.publish(store, scope, 's', 'kanban', {'execution_observations': [new]})
    rows = [{'id': 's', 'execution_observations': [old]}]
    consumer.annotate('kanban', rows, helpers.forbidden)
    assert rows[0]['execution_observations'] == [old, new]
