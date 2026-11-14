"""Pure dependency selector: linked/ledger/retained seeds plus same (board, id) publishers."""
import json

import pytest

from hermes_cli import kanban_annotation_semantic as semantic
from hermes_cli.kanban_annotation_dependencies import DependencySelector
from hermes_cli.kanban_replica_rows import Rows, pack

ME, PROF = "gw1", "prof"
LINK = {"session": "s1", "gw": ME, "profile": PROF, "rel": "shepherd"}
A = ("gw1", "p/b/t_one")
TWIN = ("gw1", "b/t_one")  # legacy 2-part form
MOVED = ("gw2", "q/b/t_one")
OTHER = ("gw1", "p/b/t_other")
FOREIGN = ("gw9", "z/zb/t_zz")


def task(tid, **kw):
    return {"id": tid, "title": "x", **kw}


def build(gen, tasks, ident=(1, 2)):
    rows = {k: (*k, pack(json.dumps(t)), gen, gen) for k, t in tasks.items()}
    return Rows(path="r", ident=ident, gen=gen, base_gen=None, changed=frozenset(), removed=frozenset(),
                by_key=rows, version_column="version", meta={}, sums=(), last_seq=gen)


def run(sel, tasks, **kw):
    rows = build(kw.pop("gen", 1), tasks)
    toks = semantic.TaskTokens().advance(rows)
    return sel.select(rows, toks, kw.pop("gateway", ME), kw.pop("profile", PROF), **kw), toks


def keys(res):
    return [k for k, _ in res]


def base():
    return {A: task("t_one", sessions=[LINK]), OTHER: task("t_other"), FOREIGN: task("t_zz")}


@pytest.mark.parametrize("field", ["gw", "profile"])
def test_malformed_foreign_link_does_not_hide_valid_link(field):
    tasks = base()
    tasks[A]["sessions"] = [LINK, {**LINK, field: ["foreign"]}]
    tasks[FOREIGN]["sessions"] = [{**LINK, field: {"bad": "shape"}}]
    result, _ = run(DependencySelector(), tasks)
    assert keys(result) == [A]


def test_linked_status_selected_unrelated_not():
    res, toks = run(DependencySelector(), base())
    assert keys(res) == [A] and res[0][1] == toks[A]
    assert isinstance(res, tuple)


def test_unrelated_and_liveness_stable_and_no_decode():
    sel = DependencySelector()
    first, _ = run(sel, base())
    n = sel.decodes
    t = base()
    t[OTHER] = task("t_other", title="changed")
    t[FOREIGN] = task("t_zz", activity_at=5)
    t[A] = task("t_one", sessions=[LINK], activity_at=9, activity_kind="k")
    second, _ = run(sel, t, gen=2)
    assert second == first
    assert sel.decodes == n + 1  # only the substantively changed OTHER decoded; liveness-only rows did not


def test_substantive_change_changes_token():
    sel = DependencySelector()
    first, _ = run(sel, base())
    t = base()
    t[A] = task("t_one", sessions=[LINK], title="new")
    second, _ = run(sel, t, gen=2)
    assert second != first


def test_ledger_owner_without_sessions_pulls_publishers():
    res, _ = run(DependencySelector(), base(), owner_keys=[OTHER])
    assert keys(res) == sorted([A, OTHER])


def test_owner_key_absent_and_deleted_retained_key():
    sel = DependencySelector()
    t = base()
    first, _ = run(sel, t, retained_keys=[MOVED])
    assert (MOVED, None) in first
    t[MOVED] = task("t_one")
    mid, _ = run(sel, t, gen=2, retained_keys=[MOVED])
    assert (MOVED, mid[[k for k, _ in mid].index(MOVED)][1]) in mid and mid != first
    del t[MOVED]
    last, _ = run(sel, t, gen=3, retained_keys=[MOVED])
    assert last == first  # deletion returns to None token


def test_retained_present_only_no_links():
    res, _ = run(DependencySelector(), {OTHER: task("t_other")}, retained_keys=[("gw1", "p/b/gone")])
    assert keys(res) == [("gw1", "p/b/gone")] and res[0][1] is None


def test_inserted_twin_and_moved_publisher_enter():
    sel = DependencySelector()
    first, _ = run(sel, base())
    t = base()
    t[TWIN] = task("t_one")
    t[MOVED] = task("t_one", created_at=7)
    second, _ = run(sel, t, gen=2)
    assert keys(second) == sorted([A, TWIN, MOVED]) and second != first
    assert FOREIGN not in keys(second)


def test_deletion_and_changed_links_and_skipped_generation():
    sel = DependencySelector()
    t = base()
    t[TWIN] = task("t_one")
    first, _ = run(sel, t)
    assert TWIN in keys(first)
    t2 = base()
    t2[A] = task("t_one")  # link removed
    second, _ = run(sel, t2, gen=5)
    assert second == ()
    assert len(first) == 2  # prior result immutable


def test_cache_bounded_by_current_rows():
    sel = DependencySelector()
    run(sel, base())
    run(sel, {OTHER: task("t_other")}, gen=2)
    assert set(sel._facts) == {OTHER}


def test_scope_switch_no_redecode():
    sel = DependencySelector()
    run(sel, base())
    n = sel.decodes
    res, _ = run(sel, base(), gateway="gwX", profile="other")
    assert res == () and sel.decodes == n


def test_other_rel_and_scope_not_linked():
    t = {A: task("t_one", sessions=[{**LINK, "rel": "bogus"}, {**LINK, "profile": "zz"}])}
    res, _ = run(DependencySelector(), t)
    assert res == ()


@pytest.mark.parametrize("bad", [
    {("g", "onlyone"): task("onlyone")},
    {("g", "p/b/t1"): task("other")},
    {("g", "p//t1"): task("t1")},
])
def test_invalid_identity_fails_closed(bad):
    rows = build(1, bad)
    toks = {k: "tok" for k in rows.by_key}
    with pytest.raises(ValueError):
        DependencySelector().select(rows, toks, ME, PROF)


def test_malformed_data_and_inconsistent_tokens():
    rows = build(1, base())
    toks = dict(semantic.TaskTokens().advance(rows))
    sel = DependencySelector()
    with pytest.raises(ValueError):
        sel.select(rows, {**toks, ("x", "y/z"): "t"}, ME, PROF)
    del toks[OTHER]
    with pytest.raises(ValueError):
        sel.select(rows, toks, ME, PROF)
    bad = Rows(path="r", ident=(1, 2), gen=1, base_gen=None, changed=frozenset(), removed=frozenset(),
               by_key={A: (*A, pack("{nope"), 1, 1)}, version_column="v", meta={}, sums=(), last_seq=1)
    with pytest.raises(ValueError):
        sel.select(bad, {A: "tok"}, ME, PROF)
    with pytest.raises(ValueError):
        sel.select(rows, dict(semantic.TaskTokens().advance(rows)), ME, PROF, owner_keys=["bad"])


def test_failed_call_leaves_cache_intact():
    sel = DependencySelector()
    run(sel, base())
    before = dict(sel._facts)
    rows = build(2, {A: task("t_one"), ("g", "bad"): task("bad")})
    with pytest.raises(ValueError):
        sel.select(rows, {k: "t" for k in rows.by_key}, ME, PROF)
    assert sel._facts == before
