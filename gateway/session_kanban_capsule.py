"""Native owner capsule requirement: derived, frozen, and fail-closed without live transport. [daveey]

A Kanban card needs a capsule when its claimed task needs ``host_repo`` (workspace kind or declared
requires) while its assignee's actual resolved terminal backend is not ``local``. The owner derives this
itself from the board row and the profile policy, never from caller params, saved policy or config.

``build_kanban_policy`` freezes ``capsule_requirement`` into ``kanban_json`` (audit/recovery shape), but
every admission and launch re-derives it from the board (``require_capsule_transport``): an older saved
policy without the field, or a replayed request with a forged ``False``, cannot weaken it.

Transport: a required card may only proceed once a run-bound, host-owned, authenticated observation
transport is live for exactly ``(db, task_id, run_id, claim_lock)`` of this attempt: the dispatcher host's
rendezvous socket (``gateway.session_kanban_rendezvous``). Admission probes it (``_live_transport_ready``,
never consuming); the launch (``attach_capsule_transport`` inside ``placing_spawn``) authenticates and
consumes it for that one spawn. No rendezvous, or any check failing, refuses with
``capsule_observation_missing``. There is deliberately no boolean, config key, env var or caller param
that can make it ready.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

from hermes_state_runtime import RuntimeStoreError

MISSING = 'capsule_observation_missing'
NOT_REQUIRED = {'required': False}
REQUIRED = {'required': True, 'requires': 'host_repo', 'backend': 'remote'}


def capsule_requirement(task) -> dict:
    """Derived requirement for a claimed task row: REQUIRED or NOT_REQUIRED (unreadable backend → REQUIRED)."""
    from hermes_cli.kanban_capsule_admission import _requires_host_repo
    from hermes_cli import kanban_capabilities
    if not _requires_host_repo(task):
        return dict(NOT_REQUIRED)
    # Same seam native create/claim admission reads for the host_repo grant: a capsule is required exactly
    # when the assignee's own policy does not grant host_repo (non-local backend).
    try:
        granted = 'host_repo' in kanban_capabilities._profile_capabilities(task.assignee, ['host_repo'])
    except Exception as exc:  # an unreadable policy cannot prove the grant: require
        logging.getLogger(__name__).warning(
            'capsule requirement: %s policy unreadable (%s: %s); requiring a capsule',
            task.assignee, type(exc).__name__, exc, exc_info=True)
        granted = False
    return dict(NOT_REQUIRED) if granted else dict(REQUIRED)


def _rederive(context: dict) -> dict:
    from hermes_cli import kanban_db as kb
    db, task_id = context.get('db'), context.get('task_id')
    if not (isinstance(db, str) and db and isinstance(task_id, str) and task_id):
        return dict(REQUIRED)
    try:
        with closing(sqlite3.connect(Path(db).resolve(strict=True).as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            task = kb.get_task(conn, task_id)
    except (OSError, ValueError, sqlite3.Error):
        return dict(REQUIRED)
    if task is None:
        return dict(REQUIRED)
    return capsule_requirement(task)


def _live_transport_ready(context: dict) -> bool:
    """Authenticated, run-bound owner transport readiness (admission probe; does not consume the run)."""
    from gateway.session_kanban_rendezvous import probe
    return probe(context)


def _required_context(policy):
    """The frozen Kanban context when the card needs a capsule, else None (non-Kanban / ordinary)."""
    raw = getattr(policy, 'kanban_json', None)
    if raw is None:
        return None
    try:
        context = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeStoreError(MISSING) from exc
    if not isinstance(context, dict):
        raise RuntimeStoreError(MISSING)
    frozen = context.get('capsule')
    # The board row re-derives every time; a frozen value can only add a requirement, never remove one.
    required = _rederive(context)['required'] or (frozen is not None and frozen != NOT_REQUIRED)
    return context if required else None


def require_capsule_transport(policy) -> None:
    """Raise ``capsule_observation_missing`` for a capsule-required Kanban policy without live transport.

    Non-Kanban policies and ordinary cards (local backend, or no host_repo need) return unchanged.
    """
    context = _required_context(policy)
    if context is not None and not _live_transport_ready(context):
        raise RuntimeStoreError(MISSING)


def check_capsule_transport(policy) -> bool | None:
    """Launch preflight: ``capsule_observation_missing`` (cause: the precise rendezvous refusal) unless a required
    card's rendezvous is live for this attempt. Never consumes; ordinary cards return unchanged."""
    context = _required_context(policy)
    if context is None:
        return
    from gateway.session_kanban_rendezvous import RendezvousError, check
    try:
        check(context)
    except RendezvousError as exc:
        raise RuntimeStoreError(MISSING) from exc
    return True


def attach_capsule_transport(policy):
    """Launch-time: None for ordinary cards; for a required card the authenticated, consumed rendezvous.

    Raises ``capsule_observation_missing`` (cause: the precise rendezvous refusal) when none is established.
    """
    context = _required_context(policy)
    if context is None:
        return None
    from gateway.session_kanban_rendezvous import RendezvousError, attach
    try:
        return attach(context)
    except RendezvousError as exc:
        raise RuntimeStoreError(MISSING) from exc


def capsule_worker_context(context, text=None, span_key='context_workspace'):
    """The owner-built worker context (``text``, default ``context['context']``), with its host
    ``Workspace:`` header line replaced by the capsule's when this worker process is capsule-bound (the only
    workspace its tools can reach). Ordinary cards: as is.

    The line is the one at the owner-computed header span (``context[span_key]``, by header structure); a
    missing or non-matching span fails closed, so a title/body holding ``## Body`` or a forged ``Workspace:``
    line never leaves the host path in the prompt. The rest of the text (task body, approved plan) is never
    touched. [daveey]"""
    text = context['context'] if text is None else text
    from tools import capsule_workspace
    if not capsule_workspace.bound() or not isinstance(text, str):
        return text
    span = context.get(span_key)
    ok = (isinstance(span, (list, tuple)) and len(span) == 2 and type(span[0]) is int and isinstance(span[1], str)
          and span[0] > 0 and span[1].startswith('Workspace: ') and '\n' not in span[1]
          and span[1].endswith(' @ ' + str(context.get('workspace'))))
    if ok:
        at, line = span
        ok = text[at - 1:at] == '\n' and text[at:at + len(line)] == line and text[at + len(line):at + len(line) + 1] in ('\n', '')
    if not ok:
        raise RuntimeError('capsule_context_header_drift')
    return (text[:at] + f'Workspace: {capsule_workspace.CAPSULE_CWD} (task capsule: a private git checkout; '
            'the host-side path is not visible in this sandbox)' + text[at + len(line):])


def resume_workspace_span(context, resume_base):
    """The header span inside ``context['resume_context']`` (a fixed note prepended to ``resume_base``, whose
    header is the one ``context_workspace`` locates), or None when either is missing. [daveey]"""
    span, resumed = context.get('context_workspace'), context.get('resume_context')
    if not (isinstance(span, list) and isinstance(resumed, str) and isinstance(resume_base, str)
            and resumed.endswith(resume_base)):
        return None
    return [span[0] + len(resumed) - len(resume_base), span[1]]


def capsule_prompt_context(context, history):
    """The worker's first-turn card context (:func:`~gateway.session_kanban_resume.worker_prompt_context`),
    with the capsule Workspace rewrite at the matching span when bound. Unbound: byte-identical. [daveey]"""
    from gateway.session_kanban_resume import worker_prompt_context
    text = worker_prompt_context(context, history)
    key = 'context_workspace' if text is context['context'] else 'resume_context_workspace'
    return capsule_worker_context(context, text, key)
