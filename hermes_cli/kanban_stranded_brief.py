"""Plain-language decision brief for the stranded-park backstop. [daveey]"""
from __future__ import annotations

import json


def _data(raw):
    try:
        value = json.loads(raw or '{}')
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _text(value, limit=300):
    return ' '.join(str(value or '').split())[:limit]


def reason(entry):
    data = entry['payload'] if entry else {}
    disposition = data.get('disposition')
    detail = disposition.get('summary') if isinstance(disposition, dict) else None
    why = detail or data.get('summary') or data.get('reason') or data.get('error')
    if isinstance(why, str) and why not in ('decision_pending', 'scope_stopped', 'capability_unavailable'):
        return _text(why.replace('_', ' '))
    return {'spend_budget_triaged': 'The work reached its budget for further attempts.',
            'decision_retained': 'The decision owner kept the work stopped; further work has not been authorized.',
            'safety_refused': 'The provider refused the work; a budget decision cannot authorize another attempt.'}.get(
                entry['kind'] if entry else '', 'The work is stopped and has no active owner to finish it.')


def context(conn, row, entry):
    """Use the real source, not the generated wrapper's routing instructions, as the work summary."""
    wrapper = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='administrative_decision' "
                           "ORDER BY id DESC LIMIT 1", (row['id'],)).fetchone()
    source = None
    if wrapper and row.get('created_by') == 'kanban-spend-budget':
        source_id = _data(wrapper[0]).get('root_task')
        found = conn.execute('SELECT * FROM tasks WHERE id=?', (source_id,)).fetchone()
        source = dict(found) if found else None
    work = source or row
    first = next((line.strip().lstrip('# ').strip() for line in str(work.get('body') or '').splitlines()
                  if line.strip()), '')
    summary = _text(first or work.get('title') or 'Unfinished work')
    return summary, reason(entry), source is not None


def request(conn, row, entry, age, evidence):
    from hermes_cli.kanban_spend_decision import execution_restricted, pending
    from hermes_cli.kanban_human_request import validate_human_request
    summary, why, administrative = context(conn, row, entry)
    tid = row['id']
    held = pending(conn, tid)
    restricted = execution_restricted(conn, tid)
    # The stopped card an Unblock releases (kanban_release): this card, or the one this decision card was filed for.
    from hermes_cli import kanban_release
    stopped, runs = kanban_release.held(conn, tid), kanban_release.default_runs()
    if stopped:
        resume = (f'Release {"the stopped work" if administrative else "this card"} to finish its acceptance: {runs} '
                  'more runs under a goal-level scope; it can be stopped again. Safety holds still apply.')
        drop = ('Dismiss this decision notice; source work stays stopped with no further spend; reopen to reconsider.'
                if administrative else
                'Archive this card and leave its work unfinished; no further spend; reopen to reconsider.')
        recommendation = 'B - avoid authorizing more work without a bounded proposal.'
    elif administrative:
        resume = ('Return the decision to an agent to record your direction; source work stays stopped, '
                  'with no further spend until separately authorized.')
        drop = 'Dismiss this decision notice; source work stays stopped with no further spend; reopen to reconsider.'
        recommendation = 'B - keep the existing stop until there is an explicit reason to authorize more work.'
    elif held or restricted:
        resume = ('Ask the owner to record a bounded scope and budget decision; no automatic restart or spend. '
                  'Existing safety and scope restrictions still apply.')
        drop = 'Archive this card and leave its work unfinished; no further spend; reopen to reconsider.'
        recommendation = 'B - avoid authorizing more work without a bounded proposal.'
    else:
        resume = 'Return the unchanged work to an agent; uses more execution budget; it can be stopped again.'
        drop = 'Archive this card and leave its work unfinished; no further spend; reopen to reconsider.'
        recommendation = 'B - leave work stopped rather than restart it without a current owner or plan.'
    # Preserve the existing Unblock/Archive transition contract. Explicit choices currently turn
    # even an archive-valued answer into an unblock; describe the two real controls in the brief.
    release = (f"Unblock = release it to finish with {runs} more runs under a goal-level scope "
               f"(`hermes kanban release {stopped}`); safety holds still apply" if stopped else
               f"Unblock = ask the owner to record a bounded decision; the button cannot release this hold. "
               f"For an ordinary budget stop use `hermes kanban promote {tid} --reason '<what changed>'`; "
               "scope renewal and safety gates still apply" if held or restricted else
               "Unblock = return this decision to an agent (not permission to run the source work)" if administrative else
               "Unblock = it goes back to work as it is")
    return validate_human_request({
        'action': f"Decide how to handle: {summary}. {release}; archive = {drop}",
        'why_human': (f"Tried: checking for an active owner and a usable request after {age // 3600}h stopped. "
                      "Only you can: choose whether to dismiss this work or give its owner new direction."),
        'interaction': 'approval',
        'scope': (f"Situation: {summary} {why}\nOptions:\nA) Unblock: {resume}\nB) Archive: {drop}\n"
                  f"Recommend: {recommendation}\nIf you do nothing, the work stays stopped with no further spend."),
        'evidence': evidence,
    }, kind=row.get('block_kind'))
