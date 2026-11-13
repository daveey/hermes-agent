"""A portable create never lands worker-first where no pool worker could claim it. [daveey]

11-13 14:03Z and 14:23Z: two MacBook cards (t_01a1d5386cb55abf, t_01a1d54acc8c8e54) sat ready 42 and 22 min with
140 and 49 reservations across worker-2..4 and were never claimed. Each body's "Existing work checked" record said
"`hermes-hub tasks` (all boards) had no open card ...": the coordination scan read that past search as a home
coordination action, so ``queue_decision`` returned None (``required_labels`` None). ``creation_decision`` then fell
back to worker-first-v1 from the MacBook's stale ``worker_pools`` list, and every reserved worker's
``claim_refusal`` refused the same text. Three fixes, one test group each: the record is evidence, not work; a
coordination-refused card is never enrolled; ``creation_decision`` on a ``worker_queue`` gateway never falls back to
worker-first (the hub's admission judge may still route a card it denies worker-first via ``pool_route``).
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_placement_coordination as coordination
from hermes_cli import kanban_worker_placement as wp

GATEWAY = "macbook"
POOLS = {"worker_pools": {GATEWAY: ["worker-2", "worker-3", "worker-4"]}}
QUEUE = {"worker_queue": {GATEWAY: "pool:worker"}}
REQUIRES = ["terminal", "file", "git"]

# t_01a1d5386cb55abf as created (its "Existing work checked" line was edited at 14:45Z), trimmed to the sections
# the scan reads.
TITLE = ("Board applies a non-holder's run handoff and `completed` event while rejecting the same write's task row "
         "(live_claim), so rejected completions land half-applied")
BODY = """## Problem (observed)
When a gateway that does not hold a card's live Board lease writes a completion, the Board answers `live_claim` and
rejects the `tasks` lifecycle columns. It still applies the `task_runs` update for that card's current run.

## Existing work checked
On 11-13 at 06:05 PST, `hermes-hub tasks` (all boards) had no open card on Board-side fencing of run rows or events.
The cards above guard client entry paths only.

## Decided fix
1. **Board service** (the `/v2/boards/<id>/ops` handler): fence by the same rule that rejects the task row.
"""


def _args(body=BODY, title=TITLE):
    return {"title": title, "body": body, "requires": list(REQUIRES), "assignee": "default"}


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(kp, "config", lambda: dict(POOLS))


# --- the record is evidence, not work ----------------------------------------------------------------------------

def test_the_original_card_body_needs_no_coordination_label():
    # RED before: None ("needs home coordination action" from the `hermes-hub tasks` in the record).
    assert coordination.required_labels(_args(), POOLS) == set()


@pytest.mark.parametrize("record", [
    "## Existing work checked\nOn 11-13 `hermes-hub tasks` (all boards) had no open card.\n\n## Do\n",
    "### Existing work checked (11-13)\nRan `hermes-hub tasks` and `cross_task comment`: nothing open.\n# Plan\n",
    "**Existing work checked:** `hermes-hub tasks` (all boards) had no open card.\n\n## Do\n",
])
def test_an_existing_work_record_is_stripped_up_to_the_next_heading_or_paragraph(record):
    text = kp.without_evidence(record + "Summarise the CI failures.\n")
    assert "hermes-hub" not in text and "Summarise the CI failures." in text


@pytest.mark.parametrize("body", [
    "## Existing work checked\nNone open.\n\nThen open Chrome on the MacBook and sign in.\n",
    "## Existing work checked\n\nNone open.\n\nThen open Chrome on the MacBook and sign in.\n",
    "## Existing work checked\nNone open.\n\n- Then open Chrome on the MacBook and sign in.\n",
])
def test_heading_less_text_after_the_record_paragraph_is_still_scanned(body):
    # RED before: the heading section ran to the next heading, so the trailing step vanished (fail-open).
    text = kp.without_evidence(body)
    assert "None open." not in text and "open Chrome on the MacBook" in text
    assert kp.portable_reason(_args(body, title="sign in")) == "local marker"  # the host marker still binds


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
@pytest.mark.parametrize("body", [
    "## Existing work checked\nNone open.\n\nThen open Chrome on the MacBook and sign in.\n",
    "## Existing work checked\n\nNone open.\n\nThen open Chrome on the MacBook and sign in.\n",
    "**Existing work checked:** None open.\n\nThen open Chrome on the MacBook and sign in.\n",
])
def test_crlf_blank_lines_still_end_the_record_paragraph(body, newline):
    # RED before: a CRLF blank line is "\r", which the blank-line test never matched, so the record ran on to the
    # next heading and hid the trailing step's host marker (fail-open, PR #2383 review 5894587994).
    body = body.replace("\n", newline)
    text = kp.without_evidence(body)
    assert "None open." not in text and "open Chrome on the MacBook" in text
    assert kp.portable_reason(_args(body, title="sign in")) == "local marker"


@pytest.mark.parametrize("record", [
    "**Existing work checked:** searched board for X\r\n",
    "## Existing work checked\r\nSearched board for X\r\n",
])
def test_a_crlf_record_list_is_stripped_whole(record):
    # RED under CR-only normalisation ("\r" -> "\n" before "\r\n"): every CRLF became a blank line, so the record
    # paragraph ended after its first line and the list items leaked back into the scanned text.
    body = ("Fix the thing.\r\n\r\n" + record + "- t_01a1d5f0c0ffee02 is different\r\n- PR #12 closed\r\n\r\n"
            "Next para stays.\r\n")
    text = kp.without_evidence(body)
    assert "t_01a1d5f0c0ffee02" not in text and "PR #12" not in text and "\r" not in text
    assert "Fix the thing." in text and "Next para stays." in text


def test_the_same_action_outside_the_record_still_stays_home():
    body = BODY.replace("## Decided fix\n", "## Decided fix\nRun `hermes-hub tasks` to list every board.\n")
    assert coordination.required_labels(_args(body), POOLS) is None
    assert "hermes-hub tasks" in kp.without_evidence(body)  # a later section is never swallowed


# --- a coordination-refused card is never enrolled --------------------------------------------------------------

REFUSED = BODY.replace("## Decided fix\n", "## Decided fix\nRun `hermes-hub tasks` to list every board.\n")


def test_a_coordination_refused_card_is_not_enrolled_worker_first():
    # RED before: {"placement_policy": "worker-first-v1", ...} on a pool gateway without a worker queue.
    assert wp.creation_decision(_args(REFUSED), POOLS, GATEWAY, "auto", None) is None


@pytest.mark.parametrize("body", [
    REFUSED,
    "Read each request with `hermes-hub outcomes --id <id>`.",
    "Run `hermes kanban boards create scratch` on the target.",
    "Then `cross_task comment` the owner card with the result.",
])
def test_no_create_gets_worker_first_when_every_pool_member_would_refuse_its_claim(body):
    """Detection for the class: whatever spelling the scan refuses, a card ``claim_refusal`` refuses on every pool
    member never gets ``placement_policy=worker-first-v1``."""
    args = _args(body, title="placement probe")
    card = {**args, "placement_policy": wp.POLICY, "requires": REQUIRES}
    assert all(coordination.claim_refusal(card, m) for m in POOLS["worker_pools"][GATEWAY])
    out = wp.creation_decision(args, POOLS, GATEWAY, "auto", None) or {}
    assert out.get("placement_policy") != wp.POLICY, out


def test_a_claimable_card_still_enrolls_on_a_pool_gateway_without_a_queue():
    out = wp.creation_decision(_args(), POOLS, GATEWAY, "auto", None)
    assert out is not None and out["placement_policy"] == wp.POLICY, out


# --- creation_decision on a worker_queue gateway never falls back to worker-first -------------------------------

def test_a_queue_gateway_keeps_a_queue_miss_home_instead_of_worker_first():
    cfg = {**POOLS, **QUEUE}
    # RED before: worker-first-v1 when no live machine carries the label, or when the queue is off (non-primary).
    assert wp.creation_decision(_args(), cfg, GATEWAY, "auto", None, machine_ok=lambda _labels: False) is None
    assert wp.creation_decision(_args(), cfg, GATEWAY, "auto", None, queue=False) is None
    out = wp.creation_decision(_args(), cfg, GATEWAY, "auto", None, machine_ok=lambda _labels: True)
    assert out == {"where": "local", "runs_on": ["pool:worker"], "why": wp.QUEUED_WHY.format(label="pool:worker")}
