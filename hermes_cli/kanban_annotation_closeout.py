"""Conservative display cue after physical turn-hook completion. [daveey]

Never call waiting sources or their inputs on the hook's execution path: arbitrary
plugin reads can block the callback or consume its timeout. With waiting sources
registered, every executed post_llm_call callback may have changed their state.
Module identity cannot prove otherwise (plugins can register imported helpers).
Ordinary turns therefore also cue; their cost belongs in holder/replay accounting.
"""
from __future__ import annotations

import contextlib
import logging

from hermes_cli import loop_accounting

log = logging.getLogger(__name__)


@contextlib.contextmanager
def observe():
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli import kanban_session_activity as ksa

    if not host.enabled():
        yield
        return
    try:
        scope = ksa._origin_scope()
    except (Exception, SystemExit):  # health: allow BLE001 -- observer cannot prevent the real turn-end callback on scope errors
        log.warning('annotation closeout scope unavailable', exc_info=True)
        yield
        return
    had_sources = bool(ksa._waiting_sources)
    try:
        yield
    finally:
        # Preserve source removal/addition during callback execution. Do not cue
        # at the host timeout: a late worker can still commit after that point.
        if had_sources or ksa._waiting_sources:
            try:
                with loop_accounting.pass_('annotation.own_closeout_cue') as acct:
                    host.kick(scope)
                    acct.change()
            except (Exception, SystemExit):  # health: allow BLE001 -- cue failure must not replace a callback result or original exception
                log.warning('annotation closeout cue unavailable', exc_info=True)
