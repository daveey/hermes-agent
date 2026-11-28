"""Bounded dispatch briefs preserve complete current instructions or require expansion."""
import time


def render(conn, task, *, limit=10_000, recovery_tail=True):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_context as ctx
    from hermes_cli.kanban_containment_authority import render_context
    from hermes_cli.kanban_continuation import render_checkpoint_context
    from hermes_cli.kanban_wake_check import render_wake_context
    from hermes_cli.kanban_shared_blockers import render_blocker_context

    now = int(time.time())
    lines = []
    ctx._ctx_header(lines, task, conn=conn)
    # Header's legacy body cap is not appropriate for a complete-or-expand brief.
    if task.body and task.body.strip():
        index = lines.index('## Body')
        lines[index + 1] = task.body.strip()
    header = lines[:lines.index('## Body')] if '## Body' in lines else list(lines)
    comments = kb.list_comments(conn, task.id)
    governing = []
    ctx._ctx_standing(governing, task, comments)
    ctx._ctx_release(governing, conn, task.id, now)
    for text in (render_context(conn, task.id), render_wake_context(conn, task.id),
                 render_blocker_context(conn, task.id)):
        if text:
            governing.append(text)
    lines.extend(governing)
    checkpoint = render_checkpoint_context(conn, task.id)
    if checkpoint:
        lines.append(checkpoint)
    from hermes_cli.kanban_worker_recovery import build_recovery_context
    recovery = build_recovery_context(conn, task.id) if recovery_tail else ''
    if recovery:
        lines.append(recovery)
    parents = kb.parent_ids(conn, task.id)
    if parents:
        lines.append(f'Prerequisites: {", ".join(parents)}. Read their full handoffs with verbose=true before acting.')
    newest = comments[-5:]
    agent_ids = kb.agent_comment_ids(conn, task.id, [c.id for c in newest])
    if newest:
        lines.append('## Comment thread (newest 5)')
    for comment in newest:
        author = (comment.author or '').replace('`', '')
        tag = ' (agent chat)' if comment.id in agent_ids else ''
        lines += [f'comment from worker `{author}`{tag} at {ctx._ctx_stamp(comment.created_at, now)}:',
                  comment.body or '', '']
    pointer = f'Full body, checkpoints, runs, parent handoffs and history: kanban_show(task_id="{task.id}", verbose=true).'
    text = '\n'.join(lines + [pointer]) + '\n'
    if len(text) <= limit:
        return text
    # Never hand a worker a silently shortened acceptance criterion or permission boundary.
    notice = ('\n## Full context required before execution\n'
              'The complete current body/checkpoint/instructions/newest five comments exceed this brief budget. '
              'Do not execute from this summary. Read the verbose card and governing instructions first.\n')
    safety = '\n'.join(governing)
    prefix = '\n'.join(header) + '\n' + safety
    if len(prefix + notice + pointer) + 2 > limit:
        prefix = f'Task {task.id}\n' + safety
    if len(prefix + notice + pointer) + 2 > limit:
        prefix = f'Task {task.id}\nGoverning instructions also require the full read.\n'
    return prefix + notice + pointer + '\n'


def print_cli(conn, task, args):
    """The worker CLI show shares the tool/dispatch brief; interactive/full reads stay intact."""
    import json
    import os
    worker = os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_KANBAN_WORKER_TASK')
    if not worker or getattr(args, 'verbose', False):
        return False
    text = render(conn, task)
    print(json.dumps({'worker_context': text}) if getattr(args, 'json', False) else text)
    return True
