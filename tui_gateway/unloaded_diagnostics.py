"""Bounded, stage-local unloaded receipts; no delivery or scheduling decisions.

All names below are static. Metrics are overlays, never additive parent CPU.
The shared accounting registry reports capacity overflow as ``_overflow``.
"""
from contextvars import ContextVar
from functools import wraps

from gateway.notification_retry_provenance import REASONS, SUBPASSES, origin
from hermes_cli import loop_accounting as accounting


LABELS = {
    'instrumentation': ('unknown', 'overflow'),
    'retry.site': ('enumeration_failed', 'set_cursor_failed', 'top_gt_cursor', 'board_error', 'remote_error', 'unknown'),
    'host.exit': ('complete', 'exception', 'unknown'),
    'host.due': ('fact', 'retry', 'due', 'maintenance', 'unknown'),
    'host.fact': ('health', 'TaskFact', 'SourceState', 'SessionFact', 'ChatWaitingFact', 'reset', 'unknown'),
    'host.primary': ('fact', 'maintenance', 'unknown') + tuple(
        category + '.' + subpass + '.' + reason
        for category in ('retry', 'due') for subpass in SUBPASSES for reason in REASONS),
    'remote_notices.fallback': ('woken', 'self_holder', 'dead_holder', 'held_with_report', 'no_token',
                                'probe_error', 'other_pending', 'admission_bypassed', 'unknown'),
    'remote_notices.pending_detail': ('identity_unknown', 'chat_reactivated', 'snapshot_changed',
                                      'no_ledger', 'unsettled', 'unknown'),
    'remote.call': ('memo_hit', 'seed_only', 'seed_failed', 'refreshed', 'exception', 'unknown'),
    'remote.cert': ('not_attempted', 'ack_failed', 'inputs_unavailable', 'not_all_acked', 'prints_none',
                    'after_poll_none', 'cursor_moved', 'certified', 'exception', 'unknown'),
    'remote.health': ('not_reached', 'no_generation', 'complete_true', 'complete_false', 'exception', 'unknown'),
    'remote.chat.trigger': ('snapshot_diff', 'health_forced', 'unchanged_skip', 'unknown'),
    'remote.chat.result': ('unchanged_skip', 'db_none', 'refresh_failed', 'exception', 'acked_refreshed',
                           'acked_intentionally_skipped', 'unknown'),
    'remote.chat.flag': ('health_pending', 'unknown'),
    'remote.cert.flag': ('prints_none', 'after_poll_none', 'cursor_moved', 'unknown'),
}
MISS = ('inputs_error', 'initial_poll_failed', 'health_pending', 'no_memo', 'inputs_changed',
        'ledger_changed', 'boards_not_quiet', 'unknown')
for _miss in MISS:
    LABELS['remote.miss.' + _miss] = ('true', 'false', 'not_evaluated', 'unknown')


def emit(family, label):
    """Unknown labels cannot grow the shared registry or expose caller identities."""
    if family not in LABELS:
        family, label = 'instrumentation', 'unknown'
    if label not in LABELS[family]:
        label = 'unknown'
    accounting.record('unloaded.diag.' + family + '.' + label, outer=False)


class RemoteCall:
    """One lexical remote invocation; nested unreached stages keep their own units."""
    def __init__(self):
        self.call = 'unknown'
        self.cert = 'not_attempted'
        self.health = 'not_reached'
        self.miss = {}

    def test(self, reason, value):
        self.miss[reason] = bool(value)
        return value

    def check(self, reason, predicate):
        self.miss[reason] = 'unknown'
        return self.test(reason, predicate())

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        if kind is not None:
            self.call = 'exception'
            # Health acknowledgment follows the finalized certification stage;
            # its failure cannot undo a memo assignment or conflate their units.
            if self.health == 'not_reached':
                self.cert = 'exception'
        emit('remote.call', self.call)
        emit('remote.cert', self.cert)
        emit('remote.health', self.health)
        for reason in MISS:
            value = self.miss.get(reason)
            label = 'not_evaluated' if value is None else ('unknown' if value == 'unknown' else ('true' if value else 'false'))
            emit('remote.miss.' + reason, label)


class Chat:
    """Only instantiate for a chat actually reached by the projection union loop."""
    def __init__(self):
        self.trigger = self.result = 'unknown'

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        if kind is not None:
            self.result = 'exception'
        emit('remote.chat.trigger', self.trigger)
        emit('remote.chat.result', self.result)


_fallback = ContextVar('unloaded_fallback_diagnostic', default=None)


class Fallback:
    """Pass-local observation without changing the frozen Inputs public contract."""
    def __enter__(self):
        self.reason = 'unknown'
        self.detail = 'unknown'
        self.token = _fallback.set(self)
        return self

    def __exit__(self, kind, exc, tb):
        _fallback.reset(self.token)


def fallback_reason(reason, detail='unknown'):
    observation = _fallback.get()
    if observation is not None:
        observation.reason = reason
        observation.detail = detail


def holder_pending(remote, row):
    """Preserve the original short-circuited liveness decision and its first cause."""
    if remote._holder_is_me(row[3]):
        fallback_reason('self_holder')
        return True
    if not remote._holder_alive(row[3], row[4]):
        fallback_reason('dead_holder')
        return True
    return False


_admission = ContextVar('unloaded_host_admission', default=None)


class Admission:
    def __init__(self, now, sources=(), deadlines=(), maintenance=False):
        self.facts = tuple(source if source in LABELS['host.fact'] else 'unknown' for source in sources)
        self.flags = set()
        if sources:
            self.flags.add('fact' if any(source != 'unknown' for source in self.facts) else 'unknown')
        candidates = [(category, deadline, *origin(subpass, reason))
                      for category, deadline, subpass, reason in deadlines if deadline <= now]
        self.flags.update(entry[0] for entry in candidates)
        if maintenance:
            self.flags.add('maintenance')
        self.primary = 'unknown'
        if 'fact' in self.flags:
            self.primary = 'fact'
        else:
            for category in ('retry', 'due'):
                entries = sorted(entry[1:] for entry in candidates if entry[0] == category)
                if entries:
                    self.primary = category + '.' + '.'.join(entries[0][1:])
                    break
            else:
                if maintenance:
                    self.primary = 'maintenance'

    def __enter__(self):
        self.token = _admission.set(self)
        return self

    def __exit__(self, kind, exc, tb):
        _admission.reset(self.token)


def host_accounted(fn):
    """Lexical contexts encompass actual host work, early returns and BaseException.

    Primary CPU is a partition overlay enclosing the same host work. Parent and
    partition instrumentation overhead differs; neither is additive with nested work.
    """
    @wraps(fn)
    def wrapped(*args, **kwargs):
        admission = _admission.get()
        primary = admission.primary if admission is not None else 'unknown'
        name = 'unloaded.diag.host.primary.' + primary
        with accounting.pass_('unloaded.host_pass'), accounting.pass_(name):
            if admission is not None:
                for flag in admission.flags:
                    emit('host.due', flag)
                for source in admission.facts:
                    emit('host.fact', source)
            outcome = 'unknown'
            try:
                result = fn(*args, **kwargs)
                outcome = 'complete'
                return result
            except BaseException:
                outcome = 'exception'
                raise
            finally:
                emit('host.exit', outcome)
    return wrapped


def local_retry(result, reason, delay=1.0):
    emit('retry.site', reason)
    result.retry(delay, subpass='card_change_pass', reason=reason)


def retry_report(report, subpass='unknown', reason='unknown'):
    from gateway.kanban_notification_schedule import PassReport
    if isinstance(report, PassReport):
        report.retry(subpass, reason)
    else:
        report.retry()


def due_report(report, deadline, subpass='unknown', reason='unknown', *, cause='due'):
    from gateway.kanban_notification_schedule import PassReport
    if isinstance(report, PassReport):
        report.due_at(deadline, subpass, reason, cause=cause)
    else:
        report.due_at(deadline)
