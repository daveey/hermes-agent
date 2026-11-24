"""Stored turn results of live sessions lose their transcript copy at the exact-retry horizon. [daveey]

Every settled admission stores its turn result in ``state_meta`` (``gateway.admission.result.v1.<id>``) so an
exact retry replays it byte for byte. 98% of a stored result is ``messages``, a copy of rows the transcript
already holds, and nothing dropped it while the session lived: only a deleted session's results were compacted.
On the MacBook on 2026-11-23 that was 395 MB in 6,466 rows, 300 MB of it settled more than a week earlier.

The promise is byte-exact replay within ``RETRY_HORIZON_S``. Past it, a terminal admission's result gets the same
compaction retirement applies (``_compact_result``): the final response, flags and usage stay, the transcript
copies go. ``session_admissions.settled_at`` is stamped by trigger when a row turns terminal; a row settled before
the column existed is stamped by the first pass here and ages from that pass.
"""
import time

from hermes_state_mutation_retirement import RETRY_HORIZON_S, _compact_result
from hermes_state_terminal import RESULT_PREFIX

# One prune pass rewrites at most this many results (each up to a few MB) inside its write transaction.
COMPACT_LIMIT = 1000

_DUE = (
    "SELECT a.admission_id FROM session_admissions a JOIN state_meta m ON m.key = ? || a.admission_id "
    "WHERE a.status = 'terminal' AND a.settled_at < ? AND json_valid(m.value) "
    "AND json_type(m.value, '$.result.messages') = 'array' "
    "AND json_array_length(m.value, '$.result.messages') > 0 ORDER BY a.seq LIMIT ?")


def compact_settled_results(conn, older_than=RETRY_HORIZON_S, limit=COMPACT_LIMIT, now=None):
    """Compact the stored results of admissions settled more than *older_than* seconds ago. Returns how many."""
    now = time.time() if now is None else now
    conn.execute("UPDATE session_admissions SET settled_at = ? WHERE status = 'terminal' AND settled_at IS NULL",
                 (now,))
    due = [row[0] for row in conn.execute(_DUE, (RESULT_PREFIX, now - older_than, limit)).fetchall()]
    for admission_id in due:
        _compact_result(conn, admission_id)
    return len(due)
