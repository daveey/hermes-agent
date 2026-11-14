"""Incremental DependencySelector / TaskTokens deltas equal a fresh full scan; ledger current cache. [daveey]"""
import json
import random

import pytest

from hermes_cli import kanban_annotation_ledger as L
from hermes_cli import kanban_annotation_semantic as semantic
from hermes_cli.kanban_annotation_dependencies import DependencySelector
from hermes_cli.kanban_replica_rows import Rows, pack

ME, PROF = "gw1", "prof"


def rows_at(gen, tasks, base_gen=None, changed=frozenset(), removed=frozenset(), previous=None):
    by_key = {}
    for k, t in tasks.items():
        row = (*k, pack(json.dumps(t)), gen, gen)
        if previous is not None and k not in changed and k in previous.by_key:
            row = previous.by_key[k]  # the real delta reader keeps unchanged row objects
        by_key[k] = row
    return Rows(path="r", ident=(1, 2), gen=gen, base_gen=base_gen, changed=frozenset(changed),
                removed=frozenset(removed), by_key=by_key, version_column="version", meta={}, sums=(), last_seq=gen)


def random_task(rng, key, linked):
    t = {"id": key[1].split("/")[-1], "title": rng.choice("abc"), "activity_at": rng.random()}
    if linked:
        t["sessions"] = [{"session": "s1", "gw": ME if rng.random() < 0.8 else "gwX", "profile": PROF,
                          "rel": "shepherd"}]
    return t


@pytest.mark.parametrize("seed", range(12))
def test_incremental_selection_equals_full_scan(seed):
    rng = random.Random(seed)
    universe = [(rng.choice(["gw1", "gw2"]), f"{rng.choice(['p', 'q'])}/b{rng.randrange(3)}/t{i % 15}")
                for i in range(40)] + [("gw1", f"b0/t{i}") for i in range(5)]  # legacy twins share ids
    universe = list(dict.fromkeys(universe))
    tasks = {k: random_task(rng, k, rng.random() < 0.3) for k in universe[:25]}
    toks, sel = semantic.TaskTokens(), DependencySelector()
    prev = rows_at(1, tasks)
    owner = ()
    sel.select(prev, toks.advance(prev), ME, PROF, owner_keys=owner)
    for gen in range(2, 60):
        changed, removed = set(), set()
        for _ in range(rng.randrange(1, 4)):
            k = rng.choice(universe)
            op = rng.random()
            if op < 0.2 and k in tasks:
                del tasks[k]
                removed.add(k)
                changed.discard(k)
            elif op < 0.5:  # liveness-only
                if k in tasks:
                    tasks[k] = dict(tasks[k], activity_at=rng.random())
                    changed.add(k)
            else:
                tasks[k] = random_task(rng, k, rng.random() < 0.4)
                changed.add(k)
                removed.discard(k)
        if rng.random() < 0.1:
            owner = (rng.choice(universe),)
        cur = rows_at(gen, tasks, base_gen=prev.gen, changed=changed, removed=removed, previous=prev)
        mapping = toks.advance(cur)
        got = sel.select(cur, mapping, ME, PROF, owner_keys=owner, changed=toks.changed, base=toks.base)
        want = DependencySelector().select(cur, mapping, ME, PROF, owner_keys=owner)
        assert got == want, gen
        prev = cur
    assert sel.full_scans == 1  # every later generation was served incrementally


def test_unrelated_change_reuses_result_object_without_scan():
    linked = {"session": "s1", "gw": ME, "profile": PROF, "rel": "shepherd"}
    tasks = {("gw1", f"p/b/t{i}"): {"id": f"t{i}", "title": "x"} for i in range(200)}
    tasks[("gw1", "p/b/t0")]["sessions"] = [linked]
    toks, sel = semantic.TaskTokens(), DependencySelector()
    r1 = rows_at(1, tasks)
    first = sel.select(r1, toks.advance(r1), ME, PROF)
    tasks[("gw1", "p/b/t7")] = {"id": "t7", "title": "changed"}
    r2 = rows_at(2, tasks, base_gen=1, changed={("gw1", "p/b/t7")}, previous=r1)
    m2 = toks.advance(r2)
    decodes = sel.decodes
    again = sel.select(r2, m2, ME, PROF, changed=toks.changed, base=toks.base)
    assert again is first
    assert sel.decodes == decodes + 1 and sel.full_scans == 1
    tasks[("gw1", "p/b/t0")] = {"id": "t0", "title": "substantive", "sessions": [linked]}
    r3 = rows_at(3, tasks, base_gen=2, changed={("gw1", "p/b/t0")}, previous=r2)
    m3 = toks.advance(r3)
    moved = sel.select(r3, m3, ME, PROF, changed=toks.changed, base=toks.base)
    assert moved != first and moved == DependencySelector().select(r3, m3, ME, PROF)


def test_wrong_base_falls_back_to_full_scan():
    tasks = {("gw1", "p/b/t1"): {"id": "t1", "title": "x"}}
    toks, sel = semantic.TaskTokens(), DependencySelector()
    r1 = rows_at(1, tasks)
    sel.select(r1, toks.advance(r1), ME, PROF)
    tasks[("gw1", "p/b/t1")] = {"id": "t1", "title": "y"}
    r2 = rows_at(2, tasks, base_gen=1, changed={("gw1", "p/b/t1")}, previous=r1)
    m2 = toks.advance(r2)
    sel.select(r2, m2, ME, PROF, changed=toks.changed, base={})  # not the mapping it last saw
    assert sel.full_scans == 2


def test_failed_incremental_call_leaves_state_intact():
    linked = [{"session": "s1", "gw": ME, "profile": PROF, "rel": "shepherd"}]
    tasks = {("gw1", "p/b/t1"): {"id": "t1", "title": "x", "sessions": linked}}
    toks, sel = semantic.TaskTokens(), DependencySelector()
    r1 = rows_at(1, tasks)
    first = sel.select(r1, toks.advance(r1), ME, PROF)
    tasks[("gw1", "p/b/t1")] = {"id": "t1", "title": "y", "sessions": linked}
    r2 = rows_at(2, tasks, base_gen=1, changed={("gw1", "p/b/t1")}, previous=r1)
    m2 = toks.advance(r2)
    bad = dict(m2)
    bad[("gw1", "p/b/t1")] = ""  # an invalid token for a changed key: phase 1 raises before any mutation
    with pytest.raises(ValueError):
        sel.select(r2, bad, ME, PROF, changed=toks.changed, base=toks.base)
    assert sel._result is first and sel._tokens is not m2 and sel._tokens is toks.base
    assert sel.select(r2, m2, ME, PROF, changed=toks.changed, base=toks.base) != first
    assert sel.full_scans == 1


def test_current_cache_reuses_verdict_only_for_identical_row_and_ledger():
    row_task = {"id": "t1", "x": 1}
    obligation = {"binding_id": "b1", "task_json": json.dumps(row_task), "observation_version": 3,
                  "binding": {"owner": {"gateway": "g"}, "task_record_key": "p/b/t1"}}
    ledger = L.LedgerSnapshot("gw", "p", (obligation,), (), (), (), "tok")
    row = ("g", "p/b/t1", pack(json.dumps(row_task)), 3, 3)

    class R:
        by_key = {("g", "p/b/t1"): row}

    cache: dict = {}
    assert L.current_matches(ledger, R, cache) == (("b1", True),)
    assert cache["b1"][0] is row
    R.by_key = {("g", "p/b/t1"): ("g", "p/b/t1", pack(json.dumps({"id": "t1", "x": 2})), 3, 3)}
    assert L.current_matches(ledger, R, cache) == (("b1", False),)  # new row object: recomputed
    stale = dict(obligation, task_json=json.dumps({"id": "t1", "x": 2}))
    assert L.current_matches(L.LedgerSnapshot("gw", "p", (stale,), (), (), (), "t2"), R, cache) == (("b1", True),)
