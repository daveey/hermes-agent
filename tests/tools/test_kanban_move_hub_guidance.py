"""kanban_move never steers a worker to move a routine hub job back to the hub (t_01a1d0e68acaba94).

t_01a1ccba2bf93bf1 and t_01a1cad1b3da962a were moved to machine:hub for hub reads and routine hub writes that a
capability:hub-write worker does with `ssh hermes-hub` and `hub-admin`; the tool description only said to move
when the work needs "a specific host's files or logs". These tests pin the guidance's intent, not its wording.
"""
import re

from tools.kanban_tools_schemas import KANBAN_MOVE_SCHEMA


def _hub_guidance():
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(KANBAN_MOVE_SCHEMA["description"].split()))
    return [s.lower() for s in sentences if "hub-admin" in s]


def test_move_guidance_routes_hub_reads_and_writes_through_worker_access():
    guidance = _hub_guidance()
    assert guidance, "kanban_move must say how a worker reaches the hub without moving there"
    text = " ".join(guidance)
    assert "ssh hermes-hub" in text  # hub reads
    assert "hub:write" in text  # the access a hub-admin worker declares for writes


def test_move_guidance_discourages_a_hub_move_and_gates_it_on_admission():
    text = " ".join(_hub_guidance())
    assert re.search(r"\b(never|do not|don't)\b[^.;]*\bhub\b", text), "a hub move must be discouraged"
    assert "machine:hub" not in text or "admission" in text, "a machine:hub move must name hub admission"
