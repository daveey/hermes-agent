"""Replica rows that cannot reach this gateway keep no cards in the serve's remote caches. [daveey]

On the mini the hub replica held 18.6k task rows of 11 gateways (87 MB JSON). The sweep projection, the sweep memo
and the observation flights retained a decoded projection for every row, though only rows linked to this gateway
(``hermes_cli.kanban_replica_relevance.is_bystander`` false) can yield a card, notice or observation it reads.
"""
import contextlib
import gc
import json
import os
import sqlite3
import tracemalloc
from array import array

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_kanban_remote_arrival import _hub
from tests.tui_gateway.test_kanban_remote_notices import CHAT, _card, _dead_pid, _ledger_row
from tui_gateway import kanban_remote_notices as remote
from tui_gateway import kanban_remote_observations as obs
from tui_gateway import remote_sweep_projection as projection
from tui_gateway.kanban_remote_notices import sweep_remote_notices

NOW = 1_800_000_000.0
BYSTANDERS = 2000
RELEVANT = 20
BODY = 20_000
MARK = "BYSTANDER-PAYLOAD"


def _bystander(i, status="done", at=NOW - 60):
    card = _card(f"t_by_{i}", f"other_{i}", status=status, at=at, summary=f"{MARK} summary {i}")
    card["task"]["sessions"][0]["gw"] = "elsewhere"  # linked to another gateway's chat only
    card["task"]["body"] = f"{MARK} {i:06d} " + "x" * BODY
    return card


def _relevant(i):
    card = _card(f"t_rel_{i}", CHAT, status="done", at=NOW - 60, summary=f"relevant {i}")
    card["task"]["body"] = f"relevant {i:06d} " + "y" * BODY
    return card


def _opener(db):
    @contextlib.contextmanager
    def opener(profile, sid):
        yield (db, db.get_compression_tip(sid) or sid) if sid == CHAT else (None, None)

    return opener


def _roots():
    return {"_rows": projection._rows, "_sweep_memo": remote._sweep_memo, "_retained": obs._retained,
            "_memo": obs._memo, "_known": obs._known}


_OPAQUE = (type, type(json), type(_card), type(len), type(remote._lock), type(gc.collect))


def _reachable(root):
    """Every object reachable from ``root`` through data (no classes, modules, functions or locks)."""
    seen, stack, out = set(), [root], []
    while stack:
        item = stack.pop()
        if id(item) in seen or isinstance(item, _OPAQUE):
            continue
        seen.add(id(item))
        out.append(item)
        if isinstance(item, (str, bytes, int, float, bool)) or item is None:
            continue
        stack.extend(gc.get_referents(item))
        for name in getattr(type(item), "__slots__", ()):
            with contextlib.suppress(AttributeError):
                stack.append(getattr(item, name))
    return out


def _retained_state(tmp_path, cards):
    """Bytes the remote modules retain after a snapshot, an observe and a notices sweep of ``cards``."""
    tmp_path.mkdir()
    replica = _hub(tmp_path, cards)
    ledger = tmp_path / "remote-notices.db"
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")
        remote._reset_for_tests()
        gc.collect()
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            remote._sweep_snapshot(replica, "macbook")
            obs.observe(replica, ledger, NOW)
            delivered = sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=_opener(db),
                                             ledger=ledger, now=NOW)
            gc.collect()
            retained = tracemalloc.get_traced_memory()[0] - before
        finally:
            tracemalloc.stop()
    return retained, delivered


def test_bystander_rows_retain_no_cards_or_payload_text(tmp_path):
    relevant = [_relevant(i) for i in range(RELEVANT)]
    base, base_delivered = _retained_state(tmp_path / "relevant", relevant)
    full, delivered = _retained_state(tmp_path / "fleet", relevant + [_bystander(i) for i in range(BYSTANDERS)])
    per_row = (full - base) / BYSTANDERS
    assert per_row < 300, f"{per_row:.0f} bytes retained per bystander row"
    assert len(delivered) == len(base_delivered) == RELEVANT  # the relevant cards still notify their chat
    assert len(projection._rows) == RELEVANT + BYSTANDERS
    for name, root in _roots().items():
        for item in _reachable(root):
            if isinstance(item, str):
                assert MARK not in item, f"bystander payload text reachable from {name}"
            if isinstance(item, dict):
                assert not str(item.get("id", "")).startswith("t_by_"), f"bystander card reachable from {name}"
            if isinstance(item, (list, tuple)):
                assert not any(isinstance(x, str) and x.startswith("default/default/t_by_") for x in item[1:2]), \
                    f"bystander card reachable from {name}"


def _decodes(monkeypatch):
    decoded = []
    original = json.loads

    def loads(text, *args, **kwargs):
        value = original(text, *args, **kwargs)
        if isinstance(value, dict) and "status" in value:
            decoded.append(value.get("id"))
        return value

    monkeypatch.setattr(json, "loads", loads)
    return decoded


def test_second_snapshot_decodes_only_the_changed_row_and_rebuilds_no_states(tmp_path, monkeypatch):
    cards = [_relevant(i) for i in range(3)] + [_bystander(i) for i in range(200)]
    replica = _hub(tmp_path, cards)
    remote._reset_for_tests()
    first_cards, first_states = remote._sweep_snapshot(replica, "macbook")
    before = dict(projection._rows)
    changed = _bystander(7, status="blocked", at=NOW)
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE replica SET data=?, seq=seq+1000 WHERE key=?", (json.dumps(changed["task"]),
                                                                              changed["key"]))
    decoded = _decodes(monkeypatch)
    cards_now, states = remote._sweep_snapshot(replica, "macbook")
    assert set(decoded) == {"t_by_7"}  # one payload decoded (its cards and its state each parse it, as on main)
    assert cards_now == first_cards
    assert states is first_states  # one mapping updated in place, not a dict rebuilt over every row
    assert states[f"hub/{changed['key']}"][0] == f"blocked@{int(NOW)}"
    unchanged = [ref for ref in before if ref != f"hub/{changed['key']}"]
    assert all(projection._rows[ref] is before[ref] for ref in unchanged)  # no per-row object for unchanged rows
    assert sum(type(value) is str for value in projection._rows.values()) == 200  # bystanders keep "seq\0state"


def test_dead_claim_on_a_now_terminal_bystander_settles_as_on_main(tmp_path):
    claimed_at = NOW - 3600
    state = f"blocked@{int(claimed_at)}"
    card = _card("t_claimed", CHAT, status="done", at=NOW - 60)
    # The card finished, and cleanup moved its link to another gateway: a terminal bystander here, whose
    # session list still names the claimed chat under profile "work" (the swept row's profile comes from it).
    card["task"]["sessions"] = [{"gw": "elsewhere", "profile": "work", "session": CHAT, "rel": "shepherd"}]
    replica = _hub(tmp_path, [card, _relevant(0)])
    ledger = tmp_path / "remote-notices.db"
    ref = f"hub/{card['key']}"
    notice_key = f"remote:hub:{card['key']}:{state}:default:{CHAT}"
    remote._reset_for_tests()
    conn = remote._ledger(ledger)
    with conn:
        conn.execute("INSERT INTO notices (session, ref, state, at, delivered, holder_pid, holder_start, notice_key, "
                     "legacy_seen) VALUES (?,?,?,?,0,?,NULL,?,1)",
                     (CHAT, ref, state, claimed_at + 5, _dead_pid(), notice_key))
    conn.close()
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")
        sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=_opener(db), ledger=ledger, now=NOW)
    row = _ledger_row(ledger, CHAT, ref)
    assert (row["delivered"], row["holder_pid"], row["holder_start"], row.get("settled")) == (
        remote.SETTLED, None, None, "stale")
    with sqlite3.connect(ledger) as c:
        swept = c.execute("SELECT replica, profile, session, notice_id, reason FROM swept WHERE reason='stale'")
        assert swept.fetchall() == [(str(replica), "work", CHAT, notice_key, "stale")]
    # The settlement above is main's (it passes there); here the row was held compact and its sessions read back.
    assert type(projection._rows.get(ref)) is str


@pytest.mark.parametrize("change", ["unchanged", "claimed_state", "recipient_profile"])
def test_compact_late_load_rejects_a_new_sequence_before_settlement(tmp_path, change):
    claimed_at = NOW - 3600
    state = f"blocked@{int(claimed_at)}"
    card = _bystander(9)
    card["task"]["sessions"] = [{"gw": "elsewhere", "profile": "work", "session": CHAT, "rel": "shepherd"}]
    replica = _hub(tmp_path, [card])
    ledger = tmp_path / "remote-notices.db"
    ref = f"hub/{card['key']}"
    notice_key = f"remote:hub:{card['key']}:{state}:work:{CHAT}"
    remote._reset_for_tests()
    _, states = remote._sweep_snapshot(replica, "macbook")
    snapshot_state, compact = states[ref]
    assert isinstance(compact, projection.CompactTask)
    assert snapshot_state != state
    assert compact.load(replica) == card["task"]
    with contextlib.closing(remote._ledger(ledger)) as conn, conn:
        remote._swept(conn)
        conn.execute("INSERT INTO notices (session, ref, state, at, delivered, holder_pid, holder_start, notice_key, "
                     "legacy_seen) VALUES (?,?,?,?,0,?,NULL,?,1)",
                     (CHAT, ref, state, claimed_at + 5, _dead_pid(), notice_key))
    before = _ledger_row(ledger, CHAT, ref)
    if change != "unchanged":
        if change == "claimed_state":
            replacement = _card("t_by_9", CHAT, status="blocked", at=claimed_at)["task"]
            replacement["sessions"] = card["task"]["sessions"]
        else:
            replacement = dict(card["task"], sessions=[{
                "gw": "elsewhere", "profile": "different", "session": CHAT, "rel": "shepherd"}])
        with sqlite3.connect(replica) as conn:
            conn.execute("UPDATE replica SET seq=seq+1000, data=? WHERE gw='hub' AND key=?",
                         (json.dumps(replacement), card["key"]))
            seq, payload = conn.execute("SELECT seq, data FROM replica WHERE key=?", (card["key"],)).fetchone()
        assert seq != compact.seq and json.loads(payload) == replacement
    # Do not refresh the snapshot: settlement must reject the changed row at its late-load boundary.
    settled = remote._settle_stale_claims(states, replica, ledger)
    after = _ledger_row(ledger, CHAT, ref)
    with sqlite3.connect(ledger) as conn:
        swept = conn.execute("SELECT profile, session, notice_id, reason FROM swept").fetchall()
    if change == "unchanged":
        assert settled == 1
        assert (after["delivered"], after["holder_pid"], after["settled"]) == (remote.SETTLED, None, "stale")
        assert swept == [("work", CHAT, notice_key, "stale")]
    else:
        assert after == before, "changed-sequence late load must leave the current claim untouched"
        assert settled == 0 and swept == []
        assert compact.load(replica) is None


def test_moved_compact_row_does_not_abort_later_stale_claims(tmp_path):
    claimed_at = NOW - 3600
    state = f"blocked@{int(claimed_at)}"
    cards = [_bystander(i) for i in range(2)]
    replica = _hub(tmp_path, cards)
    ledger = tmp_path / "remote-notices.db"
    refs = [f"hub/{card['key']}" for card in cards]
    remote._reset_for_tests()
    _, states = remote._sweep_snapshot(replica, "macbook")
    assert all(isinstance(states[ref][1], projection.CompactTask) for ref in refs)
    conn = remote._ledger(ledger)
    try:
        with conn:
            for ref in refs:
                conn.execute(
                    "INSERT INTO notices (session, ref, state, at, delivered, holder_pid, holder_start, "
                    "notice_key, legacy_seen) VALUES (?,?,?,?,0,?,NULL,?,1)",
                    (CHAT, ref, state, claimed_at + 5, _dead_pid(), f"stale:{ref}:{state}"))
        # Verify the actual batch order: the missing row must precede the settleable claim.
        assert [row[0] for row in conn.execute(
            "SELECT ref FROM notices WHERE delivered=0 AND holder_pid IS NOT NULL")] == refs
    finally:
        conn.close()
    before = dict(_ledger_row(ledger, CHAT, refs[0]))
    # The snapshot still names this compact row, but its lazy load now finds nothing.
    with sqlite3.connect(replica) as conn:
        conn.execute("DELETE FROM replica WHERE key=?", (cards[0]["key"],))
    assert states[refs[0]][1].load(replica) is None
    assert states[refs[1]][1].load(replica) is not None

    settled = remote._settle_stale_claims(states, replica, ledger)

    assert dict(_ledger_row(ledger, CHAT, refs[0])) == before
    later = _ledger_row(ledger, CHAT, refs[1])
    assert (later["delivered"], later["holder_pid"], later["holder_start"], later["settled"]) == (
        remote.SETTLED, None, None, "stale")
    assert settled == 1
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("SELECT notice_id, reason FROM swept").fetchall() == [
            (f"stale:{refs[1]}:{state}", "stale")]


def test_bystander_that_gains_a_legacy_alias_is_refetched_like_a_cold_read(tmp_path, monkeypatch):
    target = _bystander(5)
    replica = _hub(tmp_path, [_relevant(0), _relevant(1), target] + [_bystander(i) for i in range(6, 40)])
    remote._reset_for_tests()
    remote._sweep_snapshot(replica, "macbook")
    ref = f"hub/{target['key']}"
    assert type(projection._rows[ref]) is str
    legacy = _card("t_by_5", CHAT, status="blocked", at=NOW - 7200)  # the pre-profile key, linked here
    with sqlite3.connect(replica) as conn:
        seq = conn.execute("SELECT max(seq) FROM replica").fetchone()[0] + 1
        conn.execute("INSERT INTO replica VALUES (?,?,?,?,?)",
                     ("hub", "tasks", legacy["key"].split("/", 1)[1], json.dumps(legacy["task"]), seq))
    decoded = _decodes(monkeypatch)
    warm_cards, warm_states = remote._sweep_snapshot(replica, "macbook")
    assert set(decoded) == {"t_by_5"}  # the qualified row is read again although its seq did not move
    assert type(projection._rows[ref]) is not str
    monkeypatch.undo()
    warm = (warm_cards, dict(warm_states))
    remote._reset_for_tests()
    cold_cards, cold_states = remote._sweep_snapshot(replica, "macbook")
    assert warm == (cold_cards, dict(cold_states))
    assert warm[1][ref] == (f"done@{int(NOW - 60)}", {"sessions": target["task"]["sessions"]})


def test_observation_lane_keeps_bystanders_in_fixed_width_columns(tmp_path):
    replica = _hub(tmp_path, [_relevant(0)] + [_bystander(i) for i in range(50)])
    ledger = tmp_path / "remote-notices.db"
    remote._reset_for_tests()
    fresh = obs.observe(replica, ledger, NOW)
    assert {key for (_gw, key, _state) in fresh} == {_relevant(0)["key"]}
    (flight,) = obs._retained.values()
    assert len(flight.decoded) == 1 and len(flight.lane.table) == 50
    assert all(isinstance(column, array) for column in (flight.lane.table.hashes, flight.lane.table.seqs))
    with sqlite3.connect(ledger) as conn:  # bystander terminal states still reach the ledger, as on main
        refs = {ref for (ref,) in conn.execute("SELECT ref FROM observations")}
    assert refs == {f"hub/{_relevant(0)['key']}"} | {f"hub/{_bystander(i)['key']}" for i in range(50)}


def test_replaced_replica_reclassifies_compact_bystander_with_unchanged_seq(tmp_path):
    card = _bystander(5)
    replica = _hub(tmp_path, [card])
    ledger = tmp_path / "remote-notices.db"
    remote._reset_for_tests()
    assert obs.observe(replica, ledger, NOW) == {}
    (flight,) = obs._retained.values()
    assert flight.decoded == {} and len(flight.lane.table) == 1
    assert flight.lane.table.trusted
    # An unchanged poll is the control: this really is a compact, irrelevant row.
    assert obs.observe(replica, ledger, NOW + 1) == {}

    stage = tmp_path / "stage"
    stage.mkdir()
    linked = _card("t_by_5", CHAT, status="blocked", at=NOW + 5)
    replacement = _hub(stage, [linked])
    with sqlite3.connect(replica) as old, sqlite3.connect(replacement) as new:
        assert old.execute("SELECT * FROM meta").fetchall() == new.execute("SELECT * FROM meta").fetchall()
        identity_sql = "SELECT gw, coll, key, seq FROM replica"
        assert old.execute(identity_sql).fetchall() == new.execute(identity_sql).fetchall()
    before = replica.stat()
    os.replace(replacement, replica)
    assert (replica.stat().st_dev, replica.stat().st_ino) != (before.st_dev, before.st_ino)

    fresh = obs.observe(replica, ledger, NOW + 10)
    ident = ("hub", linked["key"], f"blocked@{int(NOW + 5)}")
    assert fresh == {ident: NOW + 10}
    assert fresh.first_seen == {ident: NOW + 10}
    assert len(flight.decoded) == 1 and len(flight.lane.table) == 0
    with sqlite3.connect(ledger) as conn:
        rows = conn.execute("SELECT source, ref, state, first_seen FROM observations ORDER BY state").fetchall()
    assert rows == [
        (str(replica.resolve()), f"hub/{card['key']}", f"blocked@{int(NOW + 5)}", NOW + 10),
        (str(replica.resolve()), f"hub/{card['key']}", f"done@{int(NOW - 60)}", NOW),
    ]
    assert obs.observe(replica, ledger, NOW + 20) == fresh


def test_failed_bystander_write_retries_unchanged_replica(tmp_path):
    replica = _hub(tmp_path, [])
    ledger = tmp_path / "remote-notices.db"
    remote._reset_for_tests()
    assert obs.observe(replica, ledger, NOW) == {}  # establish the source before these old states arrive
    source = str(replica.resolve())
    cards = [_bystander(i, at=NOW - 8 * 3600) for i in range(2)]
    with sqlite3.connect(replica) as conn:
        conn.executemany("INSERT INTO replica VALUES ('hub','tasks',?,?,?)",
                         [(card["key"], json.dumps(card["task"]), i + 101) for i, card in enumerate(cards)])
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("SELECT source FROM observation_sources").fetchall() == [(source,)]
        # Fail a real INSERT, after the first row was inserted, so the whole batch must roll back.
        conn.execute("CREATE TRIGGER fail_bystander BEFORE INSERT ON observations "
                     "WHEN (SELECT count(*) FROM observations) = 1 "
                     "BEGIN SELECT RAISE(ABORT, 'bystander ledger failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="bystander ledger failure"):
        obs.observe(replica, ledger, NOW + 10)
    (flight,) = obs._retained.values()
    assert len(flight.lane.table) == 2 and flight.decoded == {}  # not relevant rows that naturally retry
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("SELECT * FROM observations").fetchall() == []
        conn.execute("DROP TRIGGER fail_bystander")
    # Do not reset the caches, replace either file, or change any replica row before retrying.
    assert obs.observe(replica, ledger, NOW + 20) == {}
    expected = sorted((source, f"hub/{card['key']}", f"done@{int(NOW - 8 * 3600)}", NOW + 20)
                      for card in cards)
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("SELECT source, ref, state, first_seen FROM observations ORDER BY ref").fetchall() == expected
    assert obs.observe(replica, ledger, NOW + 30) == {}
    remote._reset_for_tests()  # cold reconstruction must retain the successful retry's first_seen, too
    assert obs.observe(replica, ledger, NOW + 40) == {}
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("SELECT source, ref, state, first_seen FROM observations ORDER BY ref").fetchall() == expected


def test_warm_observation_transactions_prune_historical_known_states(tmp_path):
    target, stable = _relevant(0), _relevant(1)
    replica = _hub(tmp_path, [target, stable])
    ledger = tmp_path / "remote-notices.db"
    source = str(replica.resolve())
    slot = (str(ledger), source)
    remote._reset_for_tests()
    first = obs.observe(replica, ledger, NOW)
    history = {(f"{gw}/{key}", state): obs.first_seen_of(first, (gw, key, state)) for gw, key, state in first}
    assert obs._known[slot][1] == history
    ledger_id = obs._file_id(ledger)

    # Keep every cache warm and the ledger inode stable across committed replica changes.
    for step, status in enumerate(("blocked", "done", "blocked"), 1):
        target = _card("t_rel_0", CHAT, status=status, at=NOW + step)
        with sqlite3.connect(replica) as conn:
            conn.execute("UPDATE replica SET data=?, seq=seq+1000 WHERE key=?",
                         (json.dumps(target["task"]), target["key"]))
        transactions = obs.stats["transactions"]
        fresh = obs.observe(replica, ledger, NOW + step + 10)
        current = {(f"{gw}/{key}", state): obs.first_seen_of(fresh, (gw, key, state)) for gw, key, state in fresh}
        assert len(current) == 2
        assert obs.stats["transactions"] == transactions + 1
        assert obs._file_id(ledger) == ledger_id
        assert obs._known[slot][1] == current  # historical states must not accumulate in memory
        history.update(current)

    # Relevance alone can take _persist's no-missing/no-extra fast path. Change the terminal state too,
    # so the newly compact bystander forces persistence and pruning, not an unconditional deletion claim.
    target = _card("t_rel_0", CHAT, status="done", at=NOW + 100)
    target["task"]["sessions"][0]["gw"] = "elsewhere"
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE replica SET data=?, seq=seq+1000 WHERE key=?",
                     (json.dumps(target["task"]), target["key"]))
    transactions = obs.stats["transactions"]
    fresh = obs.observe(replica, ledger, NOW + 110)
    assert obs.stats["transactions"] == transactions + 1
    assert {key for _gw, key, _state in fresh} == {stable["key"]}
    assert obs._known[slot][1] == {
        (f"{gw}/{key}", state): obs.first_seen_of(fresh, (gw, key, state)) for gw, key, state in fresh
    }
    history[(f"hub/{target['key']}", f"done@{int(NOW + 100)}")] = NOW + 110
    with sqlite3.connect(ledger) as conn:
        persisted = {(ref, state): at for ref, state, at in conn.execute(
            "SELECT ref, state, first_seen FROM observations WHERE source=?", (source,))}
    assert persisted == history  # pruning the warm cache must preserve insert-only ledger history


def test_relevant_row_evicted_after_in_place_gateway_rename(tmp_path):
    target, stable = _relevant(0), _relevant(1)
    stable["task"]["sessions"].append(dict(stable["task"]["sessions"][0], gw="otherbox"))
    replica = _hub(tmp_path, [target, stable])
    ledger = tmp_path / "remote-notices.db"
    remote._reset_for_tests()
    first = obs.observe(replica, ledger, NOW)
    assert {key for _gw, key, _state in first} == {target["key"], stable["key"]}
    (flight,) = obs._retained.values()
    assert len(flight.decoded) == 2 and len(flight.lane.table) == 0
    inode = replica.stat().st_ino
    with sqlite3.connect(ledger) as conn:
        before = conn.execute("SELECT source, ref, state, first_seen FROM observations ORDER BY ref").fetchall()
    assert len(before) == 2
    with sqlite3.connect(replica) as conn:
        rows = conn.execute("SELECT * FROM replica ORDER BY key").fetchall()
        conn.execute("UPDATE meta SET v = 'otherbox' WHERE k = 'gateway'")
        conn.execute("UPDATE meta SET v = '101' WHERE k IN ('cursor', 'head')")
    assert replica.stat().st_ino == inode
    with sqlite3.connect(replica) as conn:
        assert conn.execute("SELECT * FROM replica ORDER BY key").fetchall() == rows

    fresh = obs.observe(replica, ledger, NOW + 5)
    expected = {ident: at for ident, at in first.items() if ident[1] == stable["key"]}
    assert fresh == expected, "renamed gateway must not return the now-bystander decoded row"
    assert fresh.first_seen == {ident: first.first_seen[ident] for ident in expected}
    assert next(iter(obs._retained.values())) is flight  # exercise warm eviction, not a cold reconstruction
    assert {ident[2] for ident in flight.decoded} == {stable["key"]}
    assert len(flight.lane.table) == 1 and flight.lane.me == "otherbox"
    assert obs.observe(replica, ledger, NOW + 10) == expected
    # Returning to the original name must restore relevance without inventing a new terminal observation.
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE meta SET v = 'macbook' WHERE k = 'gateway'")
        conn.execute("UPDATE meta SET v = '102' WHERE k IN ('cursor', 'head')")
    restored = obs.observe(replica, ledger, NOW + 15)
    assert restored == first and restored.first_seen == first.first_seen
    with sqlite3.connect(ledger) as conn:
        assert conn.execute(
            "SELECT source, ref, state, first_seen FROM observations ORDER BY ref").fetchall() == before


def test_bystander_linked_after_an_in_place_gateway_rename_is_observed(tmp_path):
    # The replica's meta.gateway changes in place: a row that was a bystander for the old name and is linked to the
    # new one must be re-classified at once (as main decodes every row), not stay compact until its seq moves.
    card = _card("t_b", "chat_x", status="done", at=NOW - 60)
    card["task"]["sessions"][0]["gw"] = "otherbox"
    replica = _hub(tmp_path, [card])
    ledger = tmp_path / "remote-notices.db"
    remote._reset_for_tests()
    assert obs.observe(replica, ledger, NOW) == {}
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE meta SET v = 'otherbox' WHERE k = 'gateway'")
        conn.execute("UPDATE meta SET v = '101' WHERE k IN ('cursor', 'head')")
    fresh = obs.observe(replica, ledger, NOW + 5)
    assert card["key"] in {key for (_gw, key, _state) in fresh}
    (flight,) = obs._retained.values()
    assert len(flight.lane.table) == 0 and flight.lane.me == "otherbox"
