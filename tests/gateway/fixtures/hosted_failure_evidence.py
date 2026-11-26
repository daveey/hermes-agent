"""Failure-only, bounded diagnostics for the disposable hosted-room daemon test."""
from contextlib import closing
import json
import sqlite3

from hermes_cli.debug_redaction import redact_debug_support_text


TAIL_BYTES = 4096


def _tail(path):
    try:
        with path.open('rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - TAIL_BYTES))
            return stream.read(TAIL_BYTES).decode('utf-8', errors='replace')
    except OSError as exc:
        return f'unavailable: {type(exc).__name__}'


def follower_failure_evidence(home, target, snapshot, request_count):
    """Read only this fixture's task metadata and log tails, never config or prompts.

    The observation is after the failed deadline, not an atomic historical snapshot.
    Failed settlement and a still-running follower must remain distinguishable.
    """
    try:
        with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True, timeout=.1)) as db:
            rows = db.execute(
                'SELECT task_id, status, execution_generation, settlement_status, '
                'substr(result_json, 1, 4096), started_at, terminal_at '
                'FROM hosted_room_driver_tasks WHERE room_id=? ORDER BY source_event_seq, task_id LIMIT 10',
                ('unknown',),
            ).fetchall()
        tasks = []
        for task_id, status, generation, settlement, result, started, terminal in rows:
            try:
                parsed = json.loads(result or '{}')
            except ValueError:
                parsed = {}
            tasks.append(dict(task_id=task_id, status=status, execution_generation=generation,
                              settlement_status=settlement,
                              error=parsed.get('error') if isinstance(parsed, dict) else None,
                              started_at=started, terminal_at=terminal))
    except sqlite3.Error as exc:
        tasks = f'unavailable: {type(exc).__name__}'
    evidence = dict(snapshot=snapshot, request_count=request_count, tasks=tasks,
                    logs={f'{label}/{name}': _tail(profile / name)
                          for label, profile in [('host', home), ('member', target)]
                          for name in ('restart.log', 'logs/gateway.log')})
    def scrub(value):
        if isinstance(value, str):
            return redact_debug_support_text(value, max_chars=4096)
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value
    return json.dumps(scrub(evidence), default=str)[:24000]


def assert_follower_deadline(within_deadline, home, target, snapshot, request_count):
    # The assert message is lazy: passing polls do no diagnostic I/O at all.
    assert within_deadline, ('the follower never settled after discard: '
                             + follower_failure_evidence(home, target, snapshot, request_count))
