"""Keyed remote notice discovery (``collect(..., keys=...)``): a fact wake reads only the woken replica rows. [daveey]

The whole-scope path (``_chat_index`` -> published copy / ``_index`` + ``observe``) reads every task row of hub.db;
on a keyed change-feed wake that is prohibited. These pin that the keyed path reads only the woken rows (batched,
coalesced, read-only), routes them exactly as the canonical projection does, keeps durable claim/ACK, never
establishes an observation baseline, and leaves the whole-scope path unchanged.
"""
import hashlib
import json
import re
import sqlite3

import pytest

from tests.tui_gateway.test_kanban_remote_notices import CHAT, _card, _hub as _base_hub
from tui_gateway import kanban_remote_notices as krn
from tui_gateway import kanban_remote_notices_keyed as keyed

T0 = 1_800_000_000.0
OTHER = "20261022_204322_4d8ce1"


def _hub(tmp_path, cards, *, ready=True):
    path = _base_hub(tmp_path, cards)
    if ready:
        with sqlite3.connect(path) as conn:
            conn.executemany("INSERT INTO meta VALUES (?,?)", [("cursor", "100"), ("head", "100")])
    return path


def _arrive(replica, card, gw="hub", seq=50):
    with sqlite3.connect(replica) as conn:
        conn.execute("INSERT INTO replica VALUES (?,'tasks',?,?,?)", (gw, card["key"], json.dumps(card["task"]), seq))


def _baseline(replica, ledger):
    krn._reset_for_tests()
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=T0) == []


def _forbid_whole_scope(monkeypatch):
    from tui_gateway import kanban_remote_index, kanban_remote_observations, remote_index_projection

    def boom(*_a, **_k):
        raise AssertionError("whole-scope read on a keyed wake")

    for owner, name in ((krn, "_chat_index"), (krn, "_index"), (krn, "_task_rows"), (krn, "_canonical"),
                        (kanban_remote_index, "lookup"), (kanban_remote_index, "compute"),
                        (kanban_remote_index, "replica_key"), (kanban_remote_index, "ensure_holder"),
                        (kanban_remote_observations, "observe"), (kanban_remote_observations, "_persist"),
                        (remote_index_projection, "snapshot")):
        monkeypatch.setattr(owner, name, boom)


def _rows(ledger):
    with sqlite3.connect(ledger) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM notices ORDER BY session, ref, state")]


def test_keyed_wake_claims_only_woken_card_without_whole_scope_reads(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.db"
    replica = _hub(tmp_path, [_card("t_old", CHAT, status="done", at=T0 - 99_999)])
    _baseline(replica, ledger)
    before = {r["ref"] for r in _rows(ledger)}  # the seeded old card, settled silently by the full read
    woken = _card("t_woken", CHAT, status="done", at=T0 + 60, final="woken proof")
    quiet = _card("t_unwoken", CHAT, status="done", at=T0 + 60)
    _arrive(replica, woken, seq=51)
    _arrive(replica, quiet, seq=52)
    _forbid_whole_scope(monkeypatch)
    digest = hashlib.sha256(replica.read_bytes()).hexdigest()

    got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=T0 + 120, keys=[("hub", woken["key"])])
    assert [item["ref"] for item in got] == [f"hub/{woken['key']}"]
    assert "woken proof" in got[0]["text"] and got.quiet
    assert hashlib.sha256(replica.read_bytes()).hexdigest() == digest  # read-only
    assert {r["ref"] for r in _rows(ledger)} - before == {f"hub/{woken['key']}"}  # the unwoken card: untouched

    # Durable claim/ACK: the in-flight claim is not re-offered, and once acked never again.
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=T0 + 121,
                       keys=[f"hub/{woken['key']}"]) == []
    assert krn.ack(got[0], ledger=ledger)
    again = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=T0 + 122, keys=[("hub", woken["key"])])
    assert again == [] and again.quiet


def test_keyed_and_whole_scope_paths_offer_the_same_notice(tmp_path):
    results = []
    for name in ("keyed", "full"):
        base = tmp_path / name
        base.mkdir()
        ledger = base / "ledger.db"
        replica = _hub(base, [])
        _baseline(replica, ledger)
        card = _card("t_same", CHAT, status="blocked", at=T0 + 30)
        _arrive(replica, card)
        keys = [("hub", card["key"])] if name == "keyed" else None
        got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=T0 + 60, lineage=(OTHER,), keys=keys)
        assert len(got) == 1 and got.quiet
        results.append({k: got[0][k] for k in ("text", "session", "ref", "state", "event_key", "owner_at")})
        with sqlite3.connect(ledger) as conn:
            results[-1]["first_seen"] = conn.execute("SELECT first_seen FROM observations WHERE ref LIKE '%t_same'"
                                                     ).fetchone()[0]
    assert results[0] == results[1]


def test_keyed_read_without_baseline_never_seeds_it(tmp_path):
    """No observation baseline yet: owner-fresh cards are claimed (as an unready replica does), old ones are left
    for the full read, nothing is written to ``observations`` and the result is incomplete."""
    ledger = tmp_path / "ledger.db"
    fresh = _card("t_fresh", CHAT, status="done", at=T0 - 60)
    old = _card("t_old", CHAT, status="done", at=T0 - krn.FRESH_S - 600)
    replica = _hub(tmp_path, [fresh, old])
    krn._reset_for_tests()
    got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=T0,
                      keys=[("hub", fresh["key"]), ("hub", old["key"])])
    assert [item["ref"] for item in got] == [f"hub/{fresh['key']}"]
    assert not got.quiet and got.next_deadline is not None
    assert {r["ref"] for r in _rows(ledger)} == {f"hub/{fresh['key']}"}  # the old card is not settled stale
    with sqlite3.connect(ledger) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='observation_sources'").fetchone()

    # The whole-scope reconciler then seeds the baseline as before: the old card settles silently, once.
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=T0 + 1) == []
    assert {r["ref"]: r["delivered"] for r in _rows(ledger)}[f"hub/{old['key']}"] == krn.SETTLED


def test_unready_replica_is_incomplete_and_writes_no_observation(tmp_path):
    ledger = tmp_path / "ledger.db"
    replica = _hub(tmp_path, [])
    _baseline(replica, ledger)
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE meta SET v='99' WHERE k='cursor'")
    card = _card("t_partial", CHAT, status="done", at=T0 - krn.FRESH_S - 60)
    _arrive(replica, card)
    got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=T0, keys=[("hub", card["key"])])
    assert got == [] and not got.quiet
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations WHERE ref LIKE '%t_partial'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0] == 0


def test_keyed_first_observation_is_the_one_the_full_read_keeps(tmp_path):
    """A state observed on a keyed wake keeps that first_seen: a later full read gives it no second window."""
    ledger = tmp_path / "ledger.db"
    replica = _hub(tmp_path, [])
    _baseline(replica, ledger)
    card = _card("t_seen", CHAT, status="done", at=T0 + 60)
    _arrive(replica, card)
    got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=T0 + 120, keys=[("hub", card["key"])])
    assert len(got) == 1
    assert krn.release(got[0], ledger=ledger)  # busy chat
    krn._reset_for_tests()
    late = T0 + 120 + krn.FRESH_S + 300
    full = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=late)
    assert len(full) == 1 and full[0]["text"].startswith("Late notices")
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("SELECT first_seen FROM observations WHERE ref=?",
                            (f"hub/{card['key']}",)).fetchone()[0] == T0 + 120


def test_woken_keys_are_coalesced_and_read_in_batches_of_at_most_500(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.db"
    replica = _hub(tmp_path, [])
    _baseline(replica, ledger)
    target = _card("t_target", CHAT, status="done", at=T0 + 10)
    _arrive(replica, target)
    statements = []
    real_ro = krn._ro

    def traced(path):
        conn = real_ro(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(krn, "_ro", traced)
    keys = [("hub", f"default/default/t_{i:05d}") for i in range(1203)] + [("hub", target["key"])] * 3
    keys += [("macbook", "default/default/t_own")]  # this gateway's own row: never read
    got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=T0 + 20, keys=keys)
    assert [item["ref"] for item in got] == [f"hub/{target['key']}"]
    reads = [s for s in statements if "FROM replica" in s and " IN (" in s]
    sizes = [len(m.group(1).split(",")) for s in reads if (m := re.search(r"IN \(([^)]*)\)", s))]
    assert len(sizes) == len(reads)
    assert sizes == [500, 500, 204]  # 1204 distinct woken keys, each once
    assert all("t_own" not in s for s in statements)
    assert not [s for s in statements if "json_group_array" in s]  # no whole-table scan


def test_legacy_row_waits_for_reconciliation_without_suffix_scan(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.db"
    replica = _hub(tmp_path, [])
    _baseline(replica, ledger)
    current = _card("t_moved", CHAT, status="done", at=T0 + 10)
    legacy = {"key": "default/t_moved", "task": _card("t_moved", CHAT, status="blocked", at=T0 + 5)["task"]}
    _arrive(replica, legacy, seq=60)
    _arrive(replica, current, seq=61)
    statements = []
    real_ro = krn._ro
    def traced(path):
        conn = real_ro(path)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(krn, '_ro', traced)
    only_legacy = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=T0 + 20,
                              keys=[("hub", legacy["key"])])
    assert only_legacy == [] and not only_legacy.quiet
    assert not any('substr(key' in sql for sql in statements)
    both = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=T0 + 20,
                       keys=[("hub", legacy["key"]), ("hub", current["key"])])
    assert [item["ref"] for item in both] == [f"hub/{current['key']}"]


def test_keyed_wake_leaves_legacy_delivered_rows_to_the_full_reconciler(tmp_path):
    """``_reconcile_legacy`` settles every legacy row whose card is not in its input; a keyed read holds only the
    woken cards, so it must not run there (else a legacy row of an unwoken card would be retired for good)."""
    ledger = tmp_path / "ledger.db"
    replica = _hub(tmp_path, [])
    _baseline(replica, ledger)
    other = _card("t_legacy", CHAT, status="done", at=T0 + 5)
    woken = _card("t_woken", CHAT, status="done", at=T0 + 10)
    _arrive(replica, other, seq=70)
    _arrive(replica, woken, seq=71)
    with sqlite3.connect(ledger) as conn:
        conn.execute("INSERT INTO notices (session, ref, state, at, delivered) VALUES (?,?,?,?,1)",
                     (CHAT, f"hub/{other['key']}", f"done@{int(T0 + 5)}", T0 + 5))
    got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=T0 + 20,
                      keys=[("hub", woken["key"])], transcript_has=lambda key, tid: False)
    assert [item["ref"] for item in got] == [f"hub/{woken['key']}"]
    legacy_row = {r["ref"]: r for r in _rows(ledger)}[f"hub/{other['key']}"]
    assert legacy_row["legacy_seen"] == 0 and legacy_row["notice_key"] is None


def test_lineage_and_released_links_route_like_the_projection(tmp_path):
    ledger = tmp_path / "ledger.db"
    replica = _hub(tmp_path, [])
    _baseline(replica, ledger)
    for_old_id = _card("t_lineage", OTHER, status="done", at=T0 + 10)
    released = _card("t_released", CHAT, status="done", at=T0 + 10)
    released["task"]["sessions"][0]["released"] = True
    elsewhere = _card("t_elsewhere", "someone_else", status="done", at=T0 + 10)
    for i, card in enumerate((for_old_id, released, elsewhere)):
        _arrive(replica, card, seq=80 + i)
    keys = [("hub", c["key"]) for c in (for_old_id, released, elsewhere)]
    full_twin = krn.collect(CHAT, "default", hub_db=replica, ledger=tmp_path / "twin.db", now=T0 + 20,
                            lineage=(OTHER,))
    got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=T0 + 20, lineage=(OTHER,), keys=keys)
    assert [i["ref"] for i in got] == [f"hub/{for_old_id['key']}"]
    assert [i["ref"] for i in got] == [i["ref"] for i in full_twin]


def test_empty_keys_read_nothing_and_are_quiet(tmp_path, monkeypatch):
    replica = _hub(tmp_path, [])
    monkeypatch.setattr(krn, "_ro", lambda *_a: pytest.fail("opened the replica for an empty wake"))
    got = krn.collect(CHAT, "default", hub_db=replica, ledger=tmp_path / "l.db", now=T0, keys=[])
    assert got == [] and got.quiet


def test_observation_lookup_uses_existing_index_and_refuses_missing_index(tmp_path):
    ledger = tmp_path / 'ledger.db'
    replica = _hub(tmp_path, [])
    _baseline(replica, ledger)
    card = _card('t_index', CHAT, status='done', at=T0 + 10)
    _arrive(replica, card)
    with sqlite3.connect(ledger) as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT first_seen, ref FROM observations WHERE source=? AND state=? "
            "AND substr(ref, instr(ref, '/') + 1)=?", ('source', 'state', card['key'])).fetchall()
        assert any('USING INDEX observations_card_state' in row[3] for row in plan)
        conn.execute('DROP INDEX observations_card_state')
    got = krn.collect(CHAT, 'default', hub_db=replica, ledger=ledger, now=T0 + 20,
                      keys=[('hub', card['key'])])
    assert len(got) == 1 and not got.quiet
    with sqlite3.connect(ledger) as conn:
        assert conn.execute('SELECT COUNT(*) FROM observations').fetchone()[0] == 0


def test_owner_batch_reads_each_key_once_across_chats_and_fences_scope(tmp_path, monkeypatch):
    ledger = tmp_path / 'ledger.db'
    replica = _hub(tmp_path, [])
    _baseline(replica, ledger)
    card = _card('t_shared', CHAT, status='done', at=T0 + 10)
    card['task']['sessions'].append(dict(card['task']['sessions'][0], session=OTHER))
    _arrive(replica, card)
    calls = []
    read = keyed.read_rows
    def traced(path, keys):
        calls.append(keys)
        return read(path, keys)
    monkeypatch.setattr(keyed, 'read_rows', traced)
    batch = keyed.WakeBatch(replica, ledger, T0 + 20, 'default', [('hub', card['key'])] * 3)
    assert calls == [[('hub', card['key'])]]
    _forbid_whole_scope(monkeypatch)
    monkeypatch.setattr(keyed, 'read_rows', lambda *_a: pytest.fail('per-chat replica read'))
    first = krn.collect(CHAT, 'default', hub_db=replica, ledger=ledger, keys=batch)
    second = krn.collect(OTHER, 'default', hub_db=replica, ledger=ledger, keys=batch)
    assert len(first) == len(second) == 1
    assert first[0]['session'] == CHAT and second[0]['session'] == OTHER
    for profile, target_ledger, now in (('other', ledger, T0 + 20),
                                        ('default', tmp_path / 'foreign.db', T0 + 20),
                                        ('default', ledger, T0 + 21)):
        refused = krn.collect(CHAT, profile, hub_db=replica, ledger=target_ledger, now=now, keys=batch)
        assert refused == [] and not refused.quiet
    monkeypatch.setattr(keyed.time, 'monotonic', lambda: batch.expires)
    expired = krn.collect(CHAT, 'default', hub_db=replica, ledger=ledger, keys=batch)
    assert expired == [] and not expired.quiet


def test_coalesce_accepts_pairs_and_refs():
    assert keyed.coalesce([("hub", "a/b/c"), "hub/a/b/c", ("", "x"), "nogw", ("mini", "k")]) == [
        ("hub", "a/b/c"), ("mini", "k")]
