"""Core mirror passes have one owner despite per-pass SHAs and ordinals. [daveey]"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_overlap as ko
from tests.tools import test_kanban_create_overlap as helpers

home = helpers.home

NEW = ("Mirror core backlog into hermes-mods core/ (72+ unpublished fork commits, "
       "through 98274ef72a9 and anything newer)")
OPEN_TITLE = ("Mirror core commits 1626+ into hermes-mods core/ "
              "(79929d70df and everything newer; starts 11-02 09:00 PST)")
HISTORICAL = (
    "Mirror core 48913b6339 (checkpoint workspace-vanished WARNING + kanban scratch-removal INFO) "
    "into hermes-mods core/",
    "hermes-mods core/ mirror: 17 recent daveey/hermes main commits have no patch",
)


MISSED = (
    "Mirror merged post-turn secret scope repair PR678 into public core patches",
    "Mirror trailing core commits after ordinal1539 (PR540/555/556 and landed outcome fixes) [replacement, Claude-routed]",
    "Mirror trailing core commits after ordinal1539 (PR540/555/556 and landed outcome fixes)",
    "Mirror core through e572a3832043: composable metadata-only ask classification",
    "Mirror fork backlog through composer Escape origin fix d9e0c56cb2",
    "Mirror contiguous core backlog through offline DNS wait PR429",
    "Mirror fork backlog after frozen 1368 snapshot (next ordinals 1369+)",
    "Mirror contiguous core range after patch1253 including PR309 cancellation lock fix",
    "Reconcile public core mirror through frozen-decision reset fence PR296",
    "Reconcile public core mirror past 1202, including frozen-decision reset fence PR296",
    "Publish outstanding fork core mirror through auxiliary OAuth rotation cb1aa70f614",
    "Mirror PR160 review admission lifecycle patch and documentation to hermes-mods",
    "Mirror daveey/hermes commits after hermes-mods 0688",
    "Mirror remaining unmirrored daveey/hermes commits after hermes-mods 0670",
    "Mirror unexported Hermes core range after 0248 through continuation guard",
)
WIDE_OWNER = "Mirror core commits 1703+ into hermes-mods core/"


@pytest.mark.parametrize("title", MISSED)
@pytest.mark.parametrize("remote", [False, True])
def test_historical_mirror_variants(home, tmp_path, title, remote):
    if remote:
        tid = "t_12345678"
        helpers._fake_replica(tmp_path / "hub-state" / "hub.db", [
            ("macbook", {"id": tid, "board": "default", "title": WIDE_OWNER, "status": "ready"})])
    else:
        tid = owner(WIDE_OWNER)
    result = check(title)
    assert result and result["code"] == "create_overlap"
    assert [m["id"] for m in result["matches"]] == [tid]
    assert "same singleton job: hermes-mods core mirror" in result["matches"][0]["why"]


@pytest.mark.parametrize("envelope", [False, True])
@pytest.mark.parametrize("remote", [False, True])
def test_capability_owner_serves_waiter_but_not_other_mirror(home, tmp_path, envelope, remote):
    a = "t_12345678" if remote else owner(WIDE_OWNER)
    if remote:
        helpers._fake_replica(tmp_path / "hub-state" / "hub.db", [
            ("macbook", {"id": a, "board": "default", "title": WIDE_OWNER, "status": "ready"})])
    title = f"Capability owner: ssh:hub for macbook/{a} ({WIDE_OWNER})"
    args: dict = {"body": f"Capability owner card for waiting card macbook/{a}"}
    if envelope:
        args.update(distinct_from=[a], distinct_reason="Execution of waiting card, not a second pass")
    assert check(title, **args) is None
    b = owner("Mirror fork backlog through independent snapshot", key="other:b")
    result = check(title, **args)
    assert result and [m["id"] for m in result["matches"]] == [b]
    assert "singleton" in result["matches"][0]["why"]


@pytest.mark.parametrize("title", [
    "BB-499: Brain freshness: brain:hub_mirror",
    "Validate and independently review libtirpc mirror repair PR63 (exact derivation + Nix CI)",
    "hermes-sync breaks when an agent checks out a fork branch in ~/code/hermes (mirror must stay on `state`)",
    "Complete native Bluesky mirrors for original threads and media; durable retry",
    "Check whether core PR#48 (goal-disposition fallback) needs a numbered-patch-mirror entry",
    "kanban_create lets a second hermes-mods core-mirror card through when titles carry different SHAs (11-02 pair scored 0.40)",
    "kanban_overlap: widen the core-mirror singleton identity (15 real core-mirror cards in 14 days lack 'hermes-mods' or 'core', incl. PR678 card 11-02)",
    "hermes-mods main red: …",
    "Merge upstream 557254faa2 into fork main",
])
def test_real_non_mirror_identities(title):
    assert ko.singleton_job(title) is None


def owner(title=OPEN_TITLE, status="ready", session="other", key="core-mirror:a"):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=title, assignee="default", session_id=session, idempotency_key=key)
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        conn.commit()
    return tid


def check(title=NEW, **args):
    with kbc.connect_closing() as conn:
        refusal, _ = ko.check(conn, args, courier=False, title=title, session_id="new", self_tid=None, parents=[])
    return json.loads(refusal) if refusal else None


@pytest.mark.parametrize("pair", [(NEW, OPEN_TITLE), HISTORICAL])
@pytest.mark.parametrize("remote", [False, True])
def test_exact_mirror_pairs_refused(home, tmp_path, pair, remote):
    new, old = pair
    if remote:
        tid = "t_remote"
        helpers._fake_replica(tmp_path / "hub-state" / "hub.db", [
            ("macbook", {"id": tid, "board": "default", "title": old, "status": "scheduled"})])
    else:
        tid = owner(old)
    result = check(new)
    assert result and result["code"] == "create_overlap"
    assert [m["id"] for m in result["matches"]] == [tid]
    assert "singleton" in result["matches"][0]["why"]


@pytest.mark.parametrize("session", ["new", "other"])
def test_mirror_same_family_and_same_chat_still_refused(home, session):
    tid = owner(session=session)
    result = check(idempotency_key="core-mirror:b")
    assert result and [m["id"] for m in result["matches"]] == [tid]
    assert check(idempotency_key="core-mirror:a") is None  # exact retry, not a second card


@pytest.mark.parametrize("status", ["done", "archived"])
@pytest.mark.parametrize("remote", [False, True])
def test_closed_mirror_not_an_owner(home, tmp_path, status, remote):
    if remote:
        helpers._fake_replica(tmp_path / "hub-state" / "hub.db", [
            ("macbook", {"id": "t_closed", "board": "default", "title": OPEN_TITLE, "status": status})])
    else:
        owner(status=status)
    assert check() is None


@pytest.mark.parametrize("title", [
    "hermes-mods main red: test_no_bare_ro_open flags plugins/coach/origin_authority.py:80",
    "Merge upstream 557254faa2 into fork main",
    "Validate and independently review libtirpc mirror repair PR63 (exact derivation + Nix CI)",
])
def test_non_mirror_titles_do_not_match_singleton(home, title):
    owner(title)
    assert check() is None


@pytest.mark.parametrize("exemption", ["parent", "self", "wait", "target"])
def test_singleton_is_not_a_heuristic_fanout_or_target_match(home, exemption):
    tid = owner(OPEN_TITLE + (" on mini" if exemption == "target" else ""))
    matches = ko.find(title=NEW + (" on macbook" if exemption == "target" else ""),
                      body=f"Wait on {tid}" if exemption == "wait" else "",
                      target_board="default", session_id="new",
                      self_tid=tid if exemption == "self" else None,
                      parents=[tid] if exemption == "parent" else [])
    assert [m["id"] for m in matches] == [tid]


def test_singleton_does_not_change_word_score():
    reasons, score = ko._reasons({"title": OPEN_TITLE, "board": "default"}, ko.words(NEW), NEW, "", "default")
    assert reasons == ["same singleton job: hermes-mods core mirror"]
    assert score == 0.4


def test_mirror_override_is_recorded(home):
    tid = owner()
    args = {"idempotency_key": "core-mirror:b", "distinct_from": [tid]}
    assert check(**args)["code"] == "create_overlap"
    args["distinct_reason"] = "Isolated fixture mirror, not the production append-only series"
    with kbc.connect_closing() as conn:
        refusal, after = ko.check(conn, args, courier=False, title=NEW, session_id="new", self_tid=None, parents=[])
        assert refusal is None
        new = kb.create_task(conn, title=NEW, assignee="default")
        after(conn, new)
        events = [e.payload for e in kb.list_events(conn, new) if e.kind == ko.EVENT]
    assert len(events) == 1
    assert events[0]["outcome"] == "override" and events[0]["reason"] == args["distinct_reason"]
    assert events[0]["matches"][0]["id"] == tid
