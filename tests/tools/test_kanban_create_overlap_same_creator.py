"""A card's own earlier cards are compared at create time, not exempt as fan-out. [daveey]

Incident (2026-11-13, hub default board): three times in one day a dispatcher card created a second card for
work its own earlier card already covered, and each duplicate ran a worker until it noticed:
- t_01a1d44c7b488318 -> t_01a1d44d9e68cca4 (75 s) and t_01a1d4914914925a -> t_01a1d4917584d038 (12 s): the
  creator archive-and-recreated a card to drop a placement marker, the archive was refused because the first
  card was already claimed, and the recreate (now with a key) went ahead.
- t_01a1d462ff5c8698 -> t_01a1d4e145f389a5 (2 h 18 m): a review-round nit filed while the round-1 child that
  owned ``_answer_interrupted_calls`` was still open; the duplicate opened PR #2237 before noticing.
``_find`` skipped every card the same worker had created, and the first creates had no key, so nothing matched.

The replay fixture holds those 3 pairs and 14 real legitimate same-creator fan-outs from the same 7 days
(enumerated legs, implement + exact-head review, one review's distinct follow-ups, cross-repo pairs), plus the
background cards that name the same code identifiers, so the rarity filter counts what the board held.

A fourth duplicate (macbook 11-13, t_01a1d6505a3d1591 -> t_01a1d6546c5bcdd9) got past the key-family exemption:
both follow-ups were keyed ``followup:<creator>:...``, the first by a run a core update reclaimed. Four keyed
fan-outs filed in one run (same family) are in the fixture so that exemption still admits a batch.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_overlap as ko

FIXTURE = Path(__file__).parent / "fixtures" / "kanban_overlap_same_creator_replay.json"
CASES = json.loads(FIXTURE.read_text())["cases"]
GATEWAYS = {"hub", "hermes-hub"}


def _card(c: dict, *, creator, local=True, status="running") -> dict:
    return {"id": c["id"], "board": "default", "gateway": "hub", "status": status, "title": c["title"],
            "body": c["body"], "assignee": c.get("assignee"), "created_at": c.get("created_at"),
            "session_id": None, "creator_task_id": creator, "local": local,
            "key_family": ko.key_family(c.get("key")),
            "has_key": c.get("has_key", False), "placed_as": None, "awaits": None, "chats": []}


def _replay(case: dict):
    a = _card(case["a"], creator=case.get("a_creator", case["creator"]))
    background = [_card({"id": f"t_bg{i:012x}", "title": "background", "body": body}, creator=None, local=False)
                  for i, body in enumerate(case["background"])]
    b = case["b"]
    # The pre-fix ``_find`` takes neither keyword; passing them only when accepted keeps the replay runnable on
    # the old code, where it shows the bug (every own card skipped) instead of a TypeError.
    extra = {k: v for k, v in (("assignee", b["assignee"]), ("run_started", case["run_started"]))
             if k in inspect.signature(ko._find).parameters}
    matches, _ = ko._find(title=b["title"], body=b["body"], target_board="default", session_id=None,
                          self_tid=case["creator"], parents=case["b_parents"], family=ko.key_family(case["b_key"]),
                          self_gw="hub", cards=[a, *background], gateways=set(GATEWAYS), **extra)
    return next((m for m in matches if m["id"] == a["id"]), None)


def _cid(case):
    return f"{case['kind']}:{case['a']['id']}->{case['b']['id']}"


def test_fixture_covers_the_incident_and_ten_fan_outs():
    kinds = [c["kind"] for c in CASES]
    assert kinds.count("duplicate") == 4 and kinds.count("fan-out") >= 10
    # Same-key-family fan-outs filed in one run (rollout legs, per-plugin batch, two review models) stay admitted.
    keyed = [c for c in CASES if c["kind"] == "fan-out"
             and ko.key_family(c["a"].get("key")) == ko.key_family(c["b_key"])]
    assert len(keyed) >= 4


def _incident():
    return next(c for c in CASES if c["b"]["id"] == "t_01a1d6546c5bcdd9")


def test_same_key_family_from_an_earlier_run_is_compared():
    """macbook 11-13: the creator keyed both follow-ups ``followup:<creator>:...``. The first was filed by a run a
    core update reclaimed; the next run filed it again. The key-family exemption admitted it (task
    t_01a1d65c3b4d419f); only a card from this same run is exempt by family."""
    case = _incident()
    assert ko.key_family(case["a"]["key"]) == ko.key_family(case["b_key"]) == "followup"
    assert case["a"]["created_at"] < case["run_started"] <= case["b"]["created_at"]
    match = _replay(case)
    assert match is not None and match["why"].startswith("titles share")
    assert ko.verdict([match], [], "")["code"] == ko.EVENT  # another run: a refusal, never a silent reuse
    same_run = {**case, "run_started": case["a"]["created_at"]}
    assert _replay(same_run) is None, "a keyed batch filed in one run stays exempt by family"
    unknown = {**case, "run_started": None}
    assert _replay(unknown) is None, "an unknown run start keeps the exemption"


def test_another_creators_same_family_card_from_before_this_run_stays_exempt():
    """Only the creator's OWN keyed cards from an earlier run lose the key-family exemption. Another creator's
    same-family card from before this run stays skipped, or any keyed batch of another creator would become a
    refusal source (post-merge check of PR #2605, mutant D2)."""
    case = _incident()
    assert _replay(case) is not None, "the creator's own earlier-run card is compared"
    other = {**case, "a_creator": "t_0000000000000000"}
    assert other["a_creator"] != case["creator"]
    assert _replay(other) is None, "another creator's same-family card stays exempt"


def test_an_own_same_family_card_with_no_created_at_stays_exempt():
    """An own same-family card whose created_at is unknown is not provably from an earlier run, so it keeps the
    exemption, as :func:`kanban_overlap_own.earlier_run` documents (post-merge check of PR #2605, mutant D5)."""
    case = _incident()
    no_time = {**case, "a": {**case["a"], "created_at": None}}
    assert _replay(no_time) is None, "an own card with no created_at stays exempt by family"
    assert not ko._own.earlier_run({"created_at": None}, case["run_started"])
    assert ko._own.earlier_run({"created_at": case["a"]["created_at"]}, case["run_started"])


@pytest.mark.parametrize("case", [c for c in CASES if c["kind"] == "duplicate"], ids=_cid)
def test_recorded_same_creator_duplicate_is_refused(case):
    match = _replay(case)
    assert match is not None, "the creator's own open card must be compared, not exempt as fan-out"


def test_same_run_reissues_deduplicate_and_the_later_round_is_refused():
    verdicts = {c["b"]["id"]: ko.verdict([m], [], "") for c in CASES if c["kind"] == "duplicate"
                for m in [_replay(c)]}
    assert verdicts["t_01a1d44d9e68cca4"] == {**verdicts["t_01a1d44d9e68cca4"], "deduplicated": True,
                                              "task_id": "t_01a1d44c7b488318"}
    assert verdicts["t_01a1d4917584d038"]["task_id"] == "t_01a1d4914914925a"
    later = verdicts["t_01a1d4e145f389a5"]  # another run, two hours on: a refusal, not a silent reuse
    assert later["ok"] is False and later["code"] == ko.EVENT
    assert "_answer_interrupted_calls" in later["matches"][0]["why"]


@pytest.mark.parametrize("case", [c for c in CASES if c["kind"] == "fan-out"], ids=_cid)
def test_recorded_legitimate_fan_out_is_admitted(case):
    assert _replay(case) is None


@pytest.mark.parametrize("case", [c for c in CASES if c["kind"] == "reissue"], ids=_cid)
def test_reissue_that_only_adds_a_round_or_sha_is_refused(case):
    """PR #2329 review: a title that only adds "(round 2)" or a sha is the same work, not an enumerated leg."""
    match = _replay(case)
    assert match is not None, "an added round number or sha must not read as an enumeration marker"


@pytest.mark.parametrize("mine, theirs", [
    ("Fix the overlap guard (PR #2329 review nits)", "Fix the overlap guard (PR #2329 review nits) round 2"),
    ("Fix the overlap guard (PR #2329 review nits)", "Fix the overlap guard (PR #2329 review nits) at 9baf8d8abc"),
    ("Fix the overlap guard for PR #2329", "Fix the overlap guard for PR #2329, attempt 3 after 2 crashes"),
])
def test_an_added_number_is_not_an_enumeration(mine, theirs):
    from hermes_cli import kanban_overlap_own as own
    card = {"title": theirs, "assignee": "default"}
    assert own.own_distinct(mine, "default", card) is None


@pytest.mark.parametrize("mine, theirs", [
    ("Fix guard (attempt 2)", "Fix guard (attempt 3)"),
    ("Fix guard (round 1)", "Fix guard (round 2)"),
    ("Fix guard, retry 1", "Fix guard, retry 2"),
    ("Fix guard v2", "Fix guard v3"),
    ("Fix guard (2nd attempt)", "Fix guard (3rd attempt)"),
    ("Fix guard (round 1)", "Round 2: fix guard"),
    # PR #2481 review 5896093750: a dotted version and a try/take counter are re-issue counters too.
    ("Fix guard v2.1", "Fix guard v2.2"),
    ("Fix guard (2nd try)", "Fix guard (3rd try)"),
    ("Fix guard, take 2", "Fix guard, take 3"),
    ("Fix guard, try 1", "Fix guard, try 2"),
])
def test_a_bumped_reissue_counter_is_not_an_enumeration(mine, theirs):
    """PR #2429 review: a counter after attempt/round/retry/v... is a re-issue of the same work, not a leg."""
    from hermes_cli import kanban_overlap_own as own
    card = {"title": theirs, "assignee": "default"}
    assert own.own_distinct(mine, "default", card) is None


@pytest.mark.parametrize("mine, theirs", [
    ("Sleep probe w1-01 (holds one slot)", "Sleep probe w1-02 (holds one slot)"),
    ("Victim scan on worker-2: live probes", "Victim scan on worker-3: live probes"),
    ("Energy filter 1/3, write the tool", "Energy filter 3/3, read the results"),
    ("Two-worker fixture A (idle hold)", "Two-worker fixture B (hostname only)"),
    ("Review PR #2110 at head 5d2e512cbb", "Review PR #2110 at head 9baf8d8abc"),
    ("w1-01 sleep probe", "sleep probe w1-02"),  # PR #2429 review: a moved index is still an index
    ("Pass 1 of 3: parser", "Pass 2 of 3: parser"),  # PR #2481 review: a staged pass is a leg
    ("Fixture A: idle hold", "idle hold, fixture B"),
    # PR #2481 review 5896093750: a counter beside a real index hides only itself, not the index.
    ("Fix guard (attempt 2, w1-01)", "Fix guard (attempt 3, w1-02)"),
    ("Fix guard (2nd try, worker-2)", "Fix guard (3rd try, worker-3)"),
    ("Take 1 of 3: parser", "Take 2 of 3: parser"),  # a staged "N of M" stays a leg
])
def test_an_index_in_the_same_place_is_an_enumeration(mine, theirs):
    from hermes_cli import kanban_overlap_own as own
    card = {"title": theirs, "assignee": "default"}
    assert (own.own_distinct(mine, "default", card) or "").startswith("enumerated")


def test_distinct_from_still_admits_an_own_duplicate():
    case = next(c for c in CASES if c["b"]["id"] == "t_01a1d4e145f389a5")
    match = _replay(case)
    assert ko.covered([match], [case["a"]["id"]], "round-2 nit is separate")


def test_shared_identifiers_many_cards_name_are_not_a_fix_site():
    case = next(c for c in CASES if c["b"]["id"] == "t_01a1d4e145f389a5")
    crowded = {**case, "background": case["background"] + ["_answer_interrupted_calls interrupted_result "
                                                           "with_own_result"] * 3}
    assert _replay(crowded) is None


# -- through kanban_create, as a dispatcher worker -------------------------------------------------------------

@pytest.fixture
def worker(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_capability_registry as reg
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    monkeypatch.setattr(reg, "self_gateway", lambda: "hub")
    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: True)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    from tests.tools.test_kanban_create_overlap import register_chat
    chat = register_chat(h)
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title="Review PR #2110 at exact head", assignee="default", session_id=chat)
        assert kb.claim_task(conn, creator) is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", creator)
    return creator


def _tool_create(**args):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_create({"assignee": "default", "requires": [], "placement": "local", **args}))


NIT = "PR #2110 follow-up: quit-waiter review nits (stale doc line, missing pruned-partial test)"


def test_worker_reissue_in_the_same_run_returns_the_first_card(worker):
    first = _tool_create(title=NIT, body="Fix the two nits from review 5889.")
    assert first["ok"] and first["task_id"]
    again = _tool_create(title=NIT, body="Fix the two nits from review 5889.",
                         idempotency_key="followup:pr2110:review-nits")
    assert again["ok"] and again["deduplicated"] and again["task_id"] == first["task_id"]
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        assert [t.title for t in kb.list_tasks(conn)].count(NIT) == 1
        events = [(r[0], json.loads(r[1])) for r in conn.execute(
            "SELECT task_id, payload FROM task_events WHERE kind = ?", (ko.EVENT,))]
    assert [e["outcome"] for _tid, e in events] == ["deduplicated"]


def test_worker_enumerated_legs_and_distinct_override_still_create(worker):
    for leg in ("w1-01", "w1-02"):
        assert _tool_create(title=f"Sleep probe {leg}: measure wake latency on worker pool")["ok"]
    first = _tool_create(title=NIT)
    second = _tool_create(title=NIT + " (round 2)", distinct_from=[first["task_id"]],
                          distinct_reason="round-2 nits are separate")
    assert second["ok"] and second.get("created", True) and second["task_id"] != first["task_id"]


@pytest.mark.parametrize("mine, theirs", [
    ("Fix guard at 9baf8d8abc follow-up", "Fix guard cleanup follow-up"),
    ("Fix guard cleanup follow-up", "Fix guard at 9baf8d8abc follow-up"),
])
def test_a_replaced_span_with_a_marker_on_one_side_only_is_not_an_enumeration(mine, theirs):
    """Only a marker on both sides of a replaced span is an index: a sha that replaced a word is a re-issue."""
    from hermes_cli import kanban_overlap_own as own
    card = {"title": theirs, "assignee": "default"}
    assert own.enumerated(mine, theirs) is None
    assert own.own_distinct(mine, "default", card) is None


@pytest.mark.parametrize("mine, theirs", [
    ("w1-01 Fix guard w1-02 tests", "Fix guard tests w1-01"),
    ("Fix guard tests w1-01", "w1-01 Fix guard w1-02 tests"),
])
def test_a_moved_marker_beside_a_one_sided_marker_is_not_an_enumeration(mine, theirs):
    """A moved marker counts only when both titles carry a different one of its shape: ``w1-01`` moved and
    ``w1-02`` exists on one side only, so this is a re-issue of the same work, not a leg."""
    from hermes_cli import kanban_overlap_own as own
    card = {"title": theirs, "assignee": "default"}
    assert own.enumerated(mine, theirs) is None
    assert own.own_distinct(mine, "default", card) is None


@pytest.mark.parametrize("mine, theirs", [
    # A dotted counter after a re-issue word is one counter, not a number plus a "1"/"2" index.
    ("Fix guard, attempt 2.1", "Fix guard, attempt 2.2"),
    ("Fix guard round 1.1", "Fix guard round 1.2"),
    # "tries" is a try word like "try"/"take".
    ("Fix guard, tries 2", "Fix guard, tries 3"),
])
def test_a_dotted_or_tries_reissue_counter_is_not_an_enumeration(mine, theirs):
    """Post-merge check of PR #2535: the dotted cases pin the optional ``.N`` tail of ``_COUNT``; the
    ``tries`` case pins that alternative of ``_TRY_WORD``."""
    from hermes_cli import kanban_overlap_own as own
    card = {"title": theirs, "assignee": "default"}
    assert own.enumerated(mine, theirs) is None
    assert own.own_distinct(mine, "default", card) is None
