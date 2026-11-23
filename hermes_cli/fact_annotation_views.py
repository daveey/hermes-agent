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


def _captured_projection(family, project):
    from hermes_cli.kanban_annotation_facts import FIELDS, _thaw

    def dependencies(scope, selector, snapshot):
        if selector != selector_for(scope, selector['row'], display=selector['display']):
            raise ValueError('invalid annotation selector')
        return {FactKey(scope, 'AnnotationInput', family)}

    def render(selector, facts, now):
        captured = next(iter(facts.values())).value
        row = dict(selector['row'])
        project(row, captured, now)
        fields = FIELDS[family] | {'execution_observations'}
        return _thaw({key: value for key, value in row.items() if key in fields})

    return Projection('annotations.' + family, ('AnnotationInput',), dependencies, render)


def _kanban(row, captured, now):
    from hermes_cli.kanban_activity_identity import row_ids
    from hermes_cli.kanban_activity_projection import project_kanban_activity
    from hermes_cli.kanban_external_activity import stamp_replica_counts
    from hermes_cli.kanban_annotation_facts import _thaw
    parts = list(captured['parts'])
    if len(parts) > 11:
        ids = set(row_ids(row))
        parts[11] = _thaw({sid: values for sid, values in parts[11].items() if sid in ids})
    project_kanban_activity([row], parts=parts, observed_at=captured['observed_at'],
                            external=captured['external'], now=now)
    stamp_replica_counts([row], captured['parts'][8], row_ids)


def _background(row, captured, now):
    from hermes_cli.process_session_activity import project_background_activity
    project_background_activity([row], **captured, now=now)


def _turn(row, captured, now):
    from hermes_cli.turn_session_activity import project_turn_activity
    from hermes_cli.turn_process_execution_observations import turn_from_digest

    def project(lease, **kwargs):
        return turn_from_digest(lease, digest=lease['holder_digest'], **kwargs)

    project_turn_activity([row], **captured, now=now, lease_project=project)


def _subagent(row, captured, now):
    from hermes_cli.subagent_session_activity import project_subagent_activity
    project_subagent_activity([row], **captured, now=now)


def _review(row, captured, now):
    from hermes_cli.review_session_activity import _row_ids, project_review_row
    # Latest assistant message across exactly this lineage, not a union of PRs
    # from older messages. Message IDs preserve the canonical SQLite ordering.
    ids = _row_ids(row)
    if any(sid not in captured['messages'] for sid in ids):
        raise ValueError('review lineage not captured')
    latest = max((captured['messages'][sid] for sid in ids),
                 key=lambda item: item[0], default=(-1, ()))
    info = {tuple(key): value for key, value in captured['reviews']}
    if any(info.get(tuple(key)) is None for key in latest[1]):
        raise ValueError('review provider pending or unavailable')
    project_review_row(row, reviews=[(tuple(key), info[tuple(key)]) for key in latest[1]], now=now)


def projections():
    """All seven families consume detached captures, never legacy I/O readers."""
    from hermes_cli.goal_session_activity import project_goal_activity
    from hermes_cli.cron_session_activity import project_cron_activity
    return (
        _membership_projection('goal', 'goal_active', project_goal_activity, 'active'),
        _membership_projection('cron', 'cron_active', project_cron_activity, 'owners'),
        *(_captured_projection(family, project) for family, project in (
            ('kanban', _kanban), ('background', _background), ('turn', _turn),
            ('subagent', _subagent), ('review', _review))),
    )
