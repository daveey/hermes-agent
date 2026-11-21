"""``runs_on`` never rides the hermes-hub courier: a card that names machine labels stays on its board. [daveey]

2026-11-11 04:31 UTC: a MacBook chat ran ``kanban_create(placement="hub", runs_on=["machine:worker-1"])``. The courier
(hermes-hub ``create-task``, whose ``CREATE_OPTIONS`` has no ``runs_on``) created t_01a1c8de2f9e874c on the hub with
``runs_on`` NULL, so it ran in the hub's own queue instead of on worker-1. ``runs_on`` was in neither
``CARRIED_OPTIONS`` nor ``UNCARRIED_OPTIONS``, so nothing refused it. The labels work from the creating board (the
Board lease reads them), so the card stays local; with an explicit pin to another gateway the create is refused.
The decide-level cases sit in ``test_kanban_placement.py`` (``test_options_no_client_carries_stay_local``).
"""
import json

from hermes_cli import kanban_placement as kp
from tests.hermes_cli import test_kanban_placement as helpers

mac = helpers.mac


def test_runs_on_card_pinned_to_the_hub_is_refused_not_sent_without_its_labels(mac):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "Regenerate the plugin index", "placement": "hub",
                                        "runs_on": ["machine:worker-1"]}))
    assert not out.get("ok") and out["code"] == "placement_unavailable" and "runs_on" in out["error"], out
    assert mac["calls"] == [] and helpers._local_count() == 0


def test_runs_on_card_with_a_new_client_is_still_refused(mac):
    """A client that carries ``--option`` still has no runs_on option: same refusal, no courier call."""
    from tools import kanban_tools as kt
    mac["result"]["help"] = helpers.NEW_HELP
    out = json.loads(kt._handle_create({"title": "Regenerate the plugin index", "placement": "hub",
                                        "runs_on": ["machine:worker-1"]}))
    assert out.get("code") == "placement_unavailable" and "runs_on" in out["error"], out
    assert mac["calls"] == [] and helpers._local_count() == 0


def test_runs_on_is_never_among_the_options_sent_to_the_target():
    assert "runs_on" in kp.UNCARRIED_OPTIONS and "runs_on" not in kp.CARRIED_OPTIONS
    assert "runs_on" not in kp.carried_options({"title": "t", "runs_on": ["machine:worker-1"]})


def test_mandatory_model_pin_is_refused_not_silently_relaxed(mac):
    from tools import kanban_tools as kt
    mac["result"]["help"] = helpers.NEW_HELP
    out = json.loads(kt._handle_create({"title": "Regenerate the plugin index", "placement": "hub",
                                        "model": "example-model", "pin_mandatory": True}))
    assert "Kanban is model-neutral" in out["error"], out
    assert mac["calls"] == [] and helpers._local_count() == 0


def test_runs_on_card_is_not_pool_portable():
    """A worker pool never re-places a card whose creator named its machines."""
    assert kp.portable_reason({"title": "Fix the flaky test", "runs_on": ["machine:worker-1"]},
                              {"assignees": ["default"]}) == "uncarried option"


# kanban_create parameters the courier handles outside the two option lists, each reviewed:
HANDLED_ELSEWHERE = {
    "title", "body", "assignee",  # create-task's own arguments
    "origin_session",             # sent as the request's origin binding
    "placement",                  # the routing input itself
    "board", "parents", "workspace_kind",  # decide() keeps the card local on these
    "human_action_now",           # only meaningful with initial_status, which is uncarried
    "hub_reason", "force_hub",    # consumed by hub admission at the origin, before routing [daveey]
}


def test_every_create_parameter_is_classified_for_the_courier():
    """runs_on was in neither list, so it was dropped. A new kanban_create parameter must be put in one."""
    from tools.kanban_tools_schemas import KANBAN_CREATE_SCHEMA
    params = set(KANBAN_CREATE_SCHEMA["parameters"]["properties"])
    carried, uncarried = set(kp.CARRIED_OPTIONS), set(kp.UNCARRIED_OPTIONS)
    assert not carried & uncarried
    unclassified = params - carried - uncarried - HANDLED_ELSEWHERE
    assert not unclassified, (f"kanban_create parameters {sorted(unclassified)} are in neither CARRIED_OPTIONS nor "
                              "UNCARRIED_OPTIONS: a courier create would drop them silently")
