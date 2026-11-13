"""Hub candidate admission never changes non-hub or structural decisions."""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard
import pytest

from hermes_cli import kanban_hub_admission as judge
from hermes_cli import kanban_placement as placement


@pytest.fixture
def gate(monkeypatch):
    calls = []
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKER_TASK", raising=False)
    def fake(fields, *, hub_reason=None):
        calls.append((fields, hub_reason))
        return judge.Verdict(admit=fields.get("title") == "Chrome", need="hub_chrome",
                             evidence="Chrome", why="test verdict")
    monkeypatch.setattr(judge, "admit", fake)
    return calls


def decide(fields, gateway="hub"):
    return placement.decide(fields, self_gw=gateway,
                            cfg={"worker_pools": {"hub": ["worker-1"]}},
                            live=lambda _: None, carries=True)


@pytest.mark.parametrize("title,admitted", [("Chrome", True), ("Read ~/.hermes/logs on the hub", False)])
def test_hub_auto_candidate(gate, title, admitted):
    result = decide({"title": title, "placement": "auto"})
    assert result["hub_admission"]["admit"] is admitted
    assert result.get("placement_policy") == (None if admitted else "worker-first-v1")
    if not admitted:
        assert "hub:write" in result["requires"]
    assert len(gate) == 1


@pytest.mark.parametrize("pin", ["local", "here", " Local "])
@pytest.mark.parametrize("title,where", [("Chrome", "local"), ("Read ~/.hermes/logs on the hub", "refuse")])
def test_hub_stay_here_pin_is_explicit(gate, pin, title, where):
    """Spec owner 11-12: placement='local'/'here' on the hub is an explicit pin; a deny refuses."""
    result = decide({"title": title, "placement": pin})
    assert result["where"] == where
    assert "placement_policy" not in result
    assert len(gate) == 1


@pytest.mark.parametrize("labels", [[], ["linux"], "linux"])
@pytest.mark.parametrize("title,where", [("Chrome", "local"), ("Read logs", "refuse")])
def test_hub_only_lease_labels_are_judged(gate, monkeypatch, labels, title, where):
    """runs_on=[] and labels no other machine satisfies are hub pins on the hub."""
    from hermes_cli import kanban_worker_node as wn
    monkeypatch.setattr(wn, "registry", lambda: {"machines": {"hub": {"labels": ["linux"]},
                                                                "worker-1": {"labels": ["gpu"]}}})
    assert decide({"title": title, "runs_on": labels})["where"] == where
    assert len(gate) == 1


def test_lease_labels_a_worker_satisfies_make_no_call(gate, monkeypatch):
    from hermes_cli import kanban_worker_node as wn
    monkeypatch.setattr(wn, "registry", lambda: {"machines": {"hub": {"labels": ["linux"]},
                                                                "worker-1": {"labels": ["linux"]}}})
    decide({"title": "Read logs", "runs_on": ["linux"]})
    assert gate == []


@pytest.mark.parametrize("runs_on", ["machine:hub", [" machine:hub"], "machine:hub,linux",
                                     ["linux", "machine:hub "]])
@pytest.mark.parametrize("gateway", ["hub", "mac"])
def test_machine_hub_shapes_are_judged(gate, runs_on, gateway):
    """Review t_01a1ceabf1ca19c8: string and padded runs_on shapes named machine:hub with zero calls."""
    result = decide({"title": "Read logs", "runs_on": runs_on}, gateway)
    assert result["where"] == "refuse"
    assert len(gate) == 1


@pytest.mark.parametrize("fields", [{"workspace_kind": "dir"}, {"board": "other"},
                                     {"requires": ["host_repo"]}, {"needs": ["ssh:somewhere"]},
                                     {"assignee": "other"}])
@pytest.mark.parametrize("title,where", [("Chrome", "local"), ("Read ~/.hermes/logs on the hub", "refuse")])
def test_structural_keep_local_is_judged(gate, fields, title, where):
    """Spec owner 11-12: a structural keep-local on the hub is not exempt; it cannot go to the
    pool, so a deny refuses the create."""
    result = decide({"title": title, **fields})
    assert result["where"] == where
    assert "placement_policy" not in result
    assert len(gate) == 1


@pytest.mark.parametrize("fields", [{"workspace_kind": "dir"}, {"board": "other"}])
def test_structural_refusal_zero_calls(gate, fields):
    """A structural refusal (a foreign hub pin that cannot carry the card) makes no model call."""
    result = decide({"title": "Chrome", "placement": "hub", **fields}, "mac")
    assert result["where"] == "refuse"
    assert gate == []


@pytest.mark.parametrize("pin", ["hub", "hermes-hub"])
@pytest.mark.parametrize("title,where", [("Chrome", "hub"), ("Read logs", "refuse")])
def test_explicit_foreign_hub(gate, pin, title, where):
    assert decide({"title": title, "placement": pin}, "mac")["where"] == where
    assert len(gate) == 1


@pytest.mark.parametrize("fields,gateway", [({"placement": "local"}, "mac"),
    ({"runs_on": ["machine:worker-1"]}, "hub"), ({"title": "code"}, "mac"),
    ({"title": "Read ~/.hermes/logs on the hub", "placement": "local"}, "mac")])
def test_nonhub_zero_calls(gate, fields, gateway):
    decide(fields, gateway)
    assert gate == []


def test_force_operator_audit(gate, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", "operator-session")
    result = decide({"title": "Read logs", "placement": "hub", "force_hub": True})
    assert result["hub_admission"]["forced"] is True
    assert result["hub_admission"]["session"] == "operator-session"
    assert gate == []


@pytest.mark.parametrize("variable", ["HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK"])
def test_force_worker_refused(gate, monkeypatch, variable):
    monkeypatch.setenv(variable, "t_worker")
    result = decide({"placement": "hub", "force_hub": True})
    assert result["where"] == "refuse"
    assert gate == []


def test_machine_hub_pin_denied(gate):
    result = decide({"title": "Read logs", "runs_on": ["machine:hub"]})
    assert result["where"] == "refuse"
    assert len(gate) == 1


def test_machine_hub_pin_admitted(gate):
    result = decide({"title": "Chrome", "runs_on": ["machine:hub"]})
    assert result["where"] == "local"
    assert result["hub_admission"]["admit"] is True


def test_judge_exception_cannot_fall_back_to_hub(gate, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("judge unavailable")
    monkeypatch.setattr(judge, "admit", broken)
    result = decide({"placement": "hub"})
    assert result["where"] == "refuse"
    assert result["hub_admission"]["why"] == "admission unavailable"


def test_denied_without_pool_refuses(gate):
    result = placement.decide({"title": "Read logs"}, self_gw="hub", cfg={})
    assert result["where"] == "refuse"
    assert result["hub_admission"]["admit"] is False


def test_tool_keeps_verdict_and_effective_contract(gate):
    from tools.kanban_tools_placement import _local_placement
    fields = {"title": "Read ~/.hermes/logs on the hub", "placement": "auto"}
    decision = decide(fields)
    result = _local_placement(placement, fields, decision, machine_ok=None, note_local_pin=True)
    assert result == decision
    assert len(gate) == 1


QUEUE_CFG = {"worker_queue": {"hub": "pool:worker"}}


@pytest.mark.parametrize("cfg", [QUEUE_CFG, {**QUEUE_CFG, "worker_pools": {"hub": ["worker-1"]}}])
def test_denied_follows_worker_queue(gate, cfg):
    """Operator 11-12 (PR #1247): a denied card joins the one worker queue, never machine:worker-N;
    hub:write rides along as capability:hub-write so only a hub-write worker claims it."""
    result = placement.decide({"title": "Read ~/.hermes/logs on the hub"}, self_gw="hub", cfg=cfg,
                              live=lambda _: None, carries=True, machine_ok=lambda label: label == "pool:worker")
    assert result["where"] == "local"
    assert result["runs_on"][0] == "pool:worker"
    assert "capability:hub-write" in result["runs_on"]
    assert not any(v.startswith("machine:") for v in result["runs_on"])
    assert "placement_policy" not in result
    assert "hub:write" in result["requires"]
    assert result["hub_admission"]["admit"] is False
    assert len(gate) == 1


def test_denied_without_live_queue_label_is_worker_first(gate):
    cfg = {**QUEUE_CFG, "worker_pools": {"hub": ["worker-1"]}}
    result = placement.decide({"title": "Read ~/.hermes/logs on the hub"}, self_gw="hub", cfg=cfg,
                              live=lambda _: None, carries=True, machine_ok=lambda _label: False)
    assert result.get("placement_policy") == "worker-first-v1"
    assert "runs_on" not in result


def test_admitted_ignores_worker_queue(gate):
    result = placement.decide({"title": "Chrome"}, self_gw="hub", cfg=QUEUE_CFG, live=lambda _: None,
                              carries=True, machine_ok=lambda _label: True)
    assert result["hub_admission"]["admit"] is True
    assert not result.get("runs_on")


def test_origin_and_courier_denies_never_queue(gate, monkeypatch):
    from hermes_cli import kanban_hub_admission_placement as hap
    monkeypatch.setattr(placement, "_machine_picks_up", lambda _label: True)
    audit = {"admit": False}
    assert "runs_on" not in hap.pool_route({"title": "x"}, QUEUE_CFG, "hub", audit, ["hub:write"], queue=False)
    assert hap.pool_route({"title": "x"}, QUEUE_CFG, "hub", audit, ["hub:write"])["runs_on"][0] == "pool:worker"


@pytest.mark.parametrize("assignee", [None, ""])
def test_omitted_assignee_remedy_names_default(gate, assignee):
    fields = {"title": "Portable work", "placement": "local"}
    if assignee is not None:
        fields["assignee"] = assignee
    result = placement.decide(fields, self_gw="hub", cfg={"assignees": ["bb-tuner"]})
    assert result["where"] == "refuse"
    assert "assignee 'default'" in result["remedy"]
    assert "assignee='bb-tuner'" in result["remedy"]


def test_explicit_labels_remedy_also_names_non_pool_assignee(gate):
    result = placement.decide(
        {"title": "Portable work", "placement": "local", "assignee": "mac-ops", "runs_on": []},
        self_gw="hub", cfg={"assignees": ["default"]},
    )
    assert result["where"] == "refuse"
    assert "drop runs_on" in result["remedy"]
    assert "assignee 'mac-ops'" in result["remedy"]
    assert "assignee='default'" in result["remedy"]


def test_assignee_remedy_access_is_optional_and_uses_hub_contract():
    from hermes_cli import kanban_hub_admission_placement as hap
    text = hap.hub_stay_remedy(hap.ASSIGNEE_KEEP, None, assignee="mac-ops", pool_profiles={"default"})
    assert "only if the work needs it" in text
    assert "requires=['hub:write']" in text
    assert "capability:hub-write" not in text


def test_assignee_keep_remedy_tracks_structural_reason():
    """The remedy keys on structural_reason's exact text; a reworded reason must not silently drop it."""
    from hermes_cli import kanban_hub_admission_placement as hap
    cfg = {"worker_pools": {"hub": ["worker-1"]}, "assignees": ["default", "bb-tuner"]}
    assert placement.structural_reason({"assignee": "mac-ops"}, cfg) == hap.ASSIGNEE_KEEP
    text = hap.hub_stay_remedy(hap.ASSIGNEE_KEEP, None, assignee="mac-ops", pool_profiles={"default", "bb-tuner"})
    assert "'mac-ops'" in text and "pool profiles: bb-tuner, default" in text and "assignee='default'" in text


# hermes-hub's claim-stall incident template as it shipped on 11-13 (hermes_hub.py ``alert_claim_stalls``), and
# the same step spelled through hub-admin. t_01a1d267a7e60bc2 (the first) was enrolled worker-first and every
# pool worker's claim gate refused it ("needs home coordination action"), so it sat ready for 40 minutes.
CLAIM_STALL_STEPS = (
    "Requests (exact original ids; keep them, do not mint replacements):\n"
    "- t_x-macbook-comment-v1  coll=tasks key=default/default/t_y action=comment claims=0 age=4304s\n\n"
    "What to do:\n"
    "1. Read each request's exact state with {read} (`hermes-hub requests` is a bounded recent window and can "
    "omit old ids).\n"
    "2. Try to read macbook (tailnet SSH, cross_task). If it cannot be reached, park this card with a wake_check.\n"
)
CLAIM_STALL_BARE = CLAIM_STALL_STEPS.format(read="`hermes-hub outcomes --id <id>`")
CLAIM_STALL_HUB_ADMIN = CLAIM_STALL_STEPS.format(read="`hub-admin 'hermes-hub outcomes --id <id>'`").replace(
    "(`hermes-hub requests` is", "(`hub-admin 'hermes-hub requests'` is")
HUB_CFG = {**QUEUE_CFG, "worker_pools": {"hub": ["worker-1"]}}


def _claim_stall(body):
    return {"title": "Hub requests to macbook unclaimed: claim-stall incident (1 request)", "body": body,
            "assignee": "default"}


@pytest.mark.parametrize("queue_live", [True, False])
def test_claim_stall_template_no_worker_can_claim_stays_home(gate, queue_live):
    """A denied card whose text the pickup claim gate refuses on every worker is not enrolled anywhere."""
    from hermes_cli import kanban_placement_coordination as coord
    result = placement.decide(_claim_stall(CLAIM_STALL_BARE), self_gw="hub", cfg=HUB_CFG, live=lambda _: None,
                              carries=True, machine_ok=lambda label: queue_live and label == "pool:worker")
    assert result["where"] == "local"
    assert "runs_on" not in result and "placement_policy" not in result
    assert "requires" not in result  # the deny route's hub:write is a worker label; home keeps the declaration
    assert result["hub_admission"]["admit"] is False
    assert result["hub_admission"]["home"] == coord.ACTION_REASON
    # Exactly what the claim gate says for the row the old route produced.
    row = {"placement_policy": "worker-first-v1", "requires": '["hub:write"]', **_claim_stall(CLAIM_STALL_BARE)}
    assert coord.claim_refusal(row, "worker-1") == coord.ACTION_REASON


def test_claim_stall_template_via_hub_admin_queues_for_hub_write_workers(gate, monkeypatch):
    """The hub-admin spelling runs on the hub with its write access: hub-write workers can claim it."""
    import time
    from hermes_cli import kanban_placement_coordination as coord
    from hermes_cli import kanban_worker_node as wn
    result = placement.decide(_claim_stall(CLAIM_STALL_HUB_ADMIN), self_gw="hub", cfg=HUB_CFG,
                              live=lambda _: None, carries=True, machine_ok=lambda label: label == "pool:worker")
    assert result["runs_on"][0] == "pool:worker"
    assert "capability:hub-write" in result["runs_on"]
    assert "capability:coordination-read" not in result["runs_on"]
    assert "hub:write" in result["requires"]
    entry = {"state": "active", "pickup": "explicit", "last_seen": time.time() * 1000,
             "labels": ["capability:hub-write"]}
    monkeypatch.setattr(wn, "registry", lambda: {"machines": {"worker-1": entry}})
    row = {"placement_policy": "worker-first-v1", "requires": '["hub:write"]', **_claim_stall(CLAIM_STALL_HUB_ADMIN)}
    assert coord.claim_refusal(row, "worker-1") is None
    entry["labels"] = ["capability:coordination-read"]
    assert coord.claim_refusal(row, "worker-1") == coord.REASON
