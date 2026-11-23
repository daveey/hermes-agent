"""Canonical selector-aware annotation projections for native startup. [daveey]

SessionFact remains the default consumer contract. Native startup explicitly
selects variant ViewFacts; these do not mislabel scalar session rows as complete
annotations. Only families with real capture contracts register here.
"""
from hermes_cli.fact_contract import FactKey
from hermes_cli.fact_materializers import Projection


def selector_for(scope, row, *, display=False):
    """One identity for lookup AND demand; no paths or prior stamps from callers."""
    if type(display) is not bool:
        raise ValueError('annotation display must be boolean')
    profile = row.get('profile', scope.profile_id)
    if profile != scope.profile_id:
        raise ValueError('annotation profile mismatch')
    identity = {'id': row['id'], 'profile': profile}
    if not isinstance(identity['id'], str) or not identity['id']:
        raise ValueError('annotation session id required')
    if '_lineage_root_id' in row:
        root = row['_lineage_root_id']
        if root is not None and not isinstance(root, str):
            raise ValueError('invalid lineage root')
        identity['_lineage_root_id'] = root
    if '_lineage_ids' in row:
        lineage = row['_lineage_ids']
        if lineage is not None and (not isinstance(lineage, (tuple, list))
                                   or any(not isinstance(item, str) for item in lineage)):
            raise ValueError('invalid lineage ids')
        identity['_lineage_ids'] = None if lineage is None else list(lineage)
    return {'row': identity, 'display': display}


def _membership_projection(family, field, project, argument):
    def dependencies(scope, selector, snapshot):
        # Also validates identities arriving through direct owner demands.
        if selector != selector_for(scope, selector['row'], display=selector['display']):
            raise ValueError('invalid annotation selector')
        return {FactKey(scope, 'AnnotationInput', family)}

    def render(selector, facts, now):
        captured = next(iter(facts.values())).value
        row = dict(selector['row'])
        project([row], captured[argument])
        return {field: row[field]} if field in row else {}

    return Projection('annotations.' + family, ('AnnotationInput',), dependencies, render)


def projections():
    """Register captured goal/cron families, never silently empty other families."""
    from hermes_cli.goal_session_activity import project_goal_activity
    from hermes_cli.cron_session_activity import project_cron_activity
    return (
        _membership_projection('goal', 'goal_active', project_goal_activity, 'active'),
        _membership_projection('cron', 'cron_active', project_cron_activity, 'owners'),
    )
