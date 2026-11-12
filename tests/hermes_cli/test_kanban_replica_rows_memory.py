"""What a live remote-links read keeps, and how much it allocates on the way, is bounded by the row text. [daveey]

hermes-hub's replica held ~13.5k task rows (~60 MB of JSON, mostly bodies, comments and run logs) of which 54 linked a
session of the mini. Each gateway, dashboard and Desktop serve kept the whole decoded replica plus its text (~300 MB)
and built a 256 MB string (UCS-4: one emoji anywhere widens it) on every full read, which put 1-2 GB heaps on the
mini (t_01a1cf67a93c05b6). Here 2,000 rows with 20 KB bodies, 1 in 100 linking this origin, must keep under
75% of their text (the old code kept over twice the text) (zlib-packed rows, slim records) with a peak well under the text itself, decode only the linked
records whole, and still answer exactly what the explicit full read answers.
"""
import gc
import json
import random
import sqlite3
import tracemalloc

from hermes_cli import kanban_replica_projection as projection
from hermes_cli import kanban_replica_rows as replica_rows
from hermes_cli import kanban_session_activity as activity
from tests.hermes_cli import test_kanban_replica_read_memo as _memo_tests
from tests.hermes_cli.test_kanban_remote_terminal_release import observed
from tests.hermes_cli.test_kanban_replica_incremental import World, _ok, _scopes
from tests.hermes_cli.test_kanban_replica_read_memo import card

hub = _memo_tests.hub
N, BODY, LINKED_EVERY = 2000, 20_000, 100
_WORDS = [''.join(random.Random(i).choices('abcdefghijklmnopqrstuvwxyz', k=3 + i % 7)) for i in range(3000)]


def _body(i):
    rng = random.Random(i)
    return ' '.join(rng.choices(_WORDS, k=BODY // 5))[:BODY - 2] + ('😀' if i == 7 else ' .')  # one emoji: the old packed string went UCS-4


def _task(i):
    t = card(f't_{i}', body=_body(i))
    if i % LINKED_EVERY:
        t['sessions'] = [{'gw': 'elsewhere', 'profile': 'default', 'session': 's9', 'rel': 'shepherd'}]
    return t


def _fill(path):
    rows = [('owner', f'work/build/t_{i}', i + 1, json.dumps(_task(i))) for i in range(N)]
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO replica VALUES (?,'tasks',?,1,?,?,1000000)", rows)
    _scopes(path, [_ok(1000.0)], seq=N + 50)  # the owner scope is fresh: a done card is released
    return sum(len(r[3].encode()) for r in rows)


def test_a_live_read_keeps_packed_slim_rows_and_peaks_below_the_text(hub):
    path, _, _, clock = hub
    text_bytes = _fill(path)
    w = World(path, clock)
    w.seq = N + 100
    gc.collect()
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        base = tracemalloc.get_traced_memory()[0]
        live = activity._read_remote_links(path)
        gc.collect()
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    retained, transient = current - base, peak - current
    assert len(live) == N // LINKED_EVERY
    assert retained < 0.75 * text_bytes, (retained, text_bytes)
    # Above what it keeps, a read allocates at most a page at a time: the old whole-replica string alone was 1x
    # the text, 4x here (the emoji row made it UCS-4).
    assert transient < 0.4 * text_bytes, (transient, text_bytes)
    (state,) = projection._states.values()
    whole = [st for st in state.stages.values() if not st.slim]
    assert len(whole) == N // LINKED_EVERY  # only the linked records are kept whole
    assert all(set(st.t) <= set(projection.SLIM_FIELDS) for st in state.stages.values() if st.slim)
    (view,) = replica_rows._views.values()
    assert all(isinstance(row[2], bytes) for row in view.by_key.values())
    assert sum(len(row[2]) for row in view.by_key.values()) < 0.6 * text_bytes  # zlib level 1: ~53% here
    assert not any(gc.is_tracked(row[2]) for row in view.by_key.values())
    full = activity._read_remote_links(path, now=clock['now'])
    assert live == full


def test_a_full_read_pages_the_rows(hub):
    path, _, statements, clock = hub
    _fill(path)
    size = replica_rows.page_rows((N, 0, 0, 0, 0, N * BODY))
    assert size == (2 << 20) // BODY  # ~2 MB of text a page: 104 rows of 20 KB
    live = activity._read_remote_links(path)
    pages = [s for s in statements if 'max(rowid)' in s]
    assert len(pages) == -(-N // size)  # the short last page ends the read
    assert live == activity._read_remote_links(path, now=clock['now'])
    assert len(replica_rows._views[str(path)].by_key) == N


def test_a_slim_row_a_legacy_alias_links_decodes_whole_again(hub):
    """An unlinked qualified row kept slim gets this origin's link from its frozen ``board/id`` twin: the card is
    built from the whole record, decoded again from the packed text."""
    path, _, _, clock = hub
    _fill(path)
    w = World(path, clock)
    w.seq = N + 100
    w.same()
    same_card = {'created_at': 500, 'created_by': 'u'}
    w.put('work/build/t_5', json.dumps(observed({**_task(5), 'status': 'done', **same_card})))  # released
    w.put('build/t_5', json.dumps({**card('t_5'), 'profile': None, **same_card}))
    w.tick(1005.0)
    projection.stats['rehydrated'] = 0
    rows = w.same()
    t5 = [r for r in rows if r['id'] == 't_5']
    assert t5 and t5[0]['legacy_key'] == 'owner/build/t_5'
    assert projection.stats['rehydrated'] >= 1
