"""Raw local parent testimony; never dependency satisfaction or release. [daveey]"""
from hermes_cli.kanban_actionability import NONTERMINAL


def snapshot(conn, parent_ids):
    result = []
    for pid in parent_ids:
        row = conn.execute('SELECT status,containment_episode FROM tasks WHERE id=?', (pid,)).fetchone()
        result.append({'id': pid, 'status': row['status'] if row else None,
                       'containment_episode': row['containment_episode'] if row else None})
    return result


def check(recorded, canonical):
    from hermes_cli.kanban_actionability_materialize import StaleIntent
    if not canonical and recorded is None:
        return  # pre-amendment parent-free source
    if not isinstance(recorded, list) or len(recorded) != len(canonical):
        raise ValueError('original raw parent testimony unavailable')
    for old, current in zip(recorded, canonical):
        if (not isinstance(old, dict) or set(old) != {'id', 'status', 'containment_episode'}
                or old['id'] != current['id']
                or old['status'] is not None and old['status'] not in (*NONTERMINAL, 'done', 'archived')
                or old['containment_episode'] is not None and (
                    not isinstance(old['containment_episode'], str) or not old['containment_episode'].strip())
                or old['status'] is None and old['containment_episode'] is not None):
            raise ValueError('original raw parent testimony malformed')
        if old['status'] is None:
            if current['status'] in ('done', 'archived') or current['containment_episode']:
                raise StaleIntent('unknown original parent is now terminal or contained')
            raise ValueError('original raw parent status unavailable')
        if old != current:
            raise StaleIntent('original raw parent status or containment changed')
