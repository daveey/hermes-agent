"""An owner-controlled device is not an external producer. [daveey]"""
import pytest

from hermes_cli import kanban_awaiting as awaiting
from hermes_cli import kanban_wait_info as waits
from tests.hermes_cli import test_kanban_awaiting as helpers

kanban_home = helpers.kanban_home


PHONE = "python3 /workspace/probe_phone_available.py"
PARK = {"wake_check": PHONE, "awaiting": {"kind": "external", "ref": "paired-phone",
        "why": "Wait for natural paired-phone visibility"}}


def test_legacy_phone_visibility_park_exposes_owner_step_without_mutation():
    info = waits.describe("scheduled", park=PARK, since=100, now=200)
    assert info["wait_state"] == "human"
    assert info["owner_step"]
    assert "phone" in info["owner_step"].lower()
    assert not info["agent_owned"]
    assert waits.describe("scheduled", park=PARK, since=100, now=10000)["owner_step"] == info["owner_step"]
    assert "owner_step" not in PARK


def test_replica_uses_same_phone_visibility_classification():
    row = {"status": "scheduled", **PARK}
    assert waits.describe_replica(row)["wait_state"] == "human"


@pytest.mark.parametrize("declared", [None, PARK["awaiting"], {"kind": "clock", "why": "device window"}])
def test_admission_refuses_phone_probe_before_reading_or_writing_board(declared):
    with pytest.raises(awaiting.AwaitingError, match="human_wait.*human_request"):
        awaiting.resolve(None, "t_abcdef1234567890", awaiting=declared,
                         reason="Wait for natural paired-phone visibility", wake_check=PHONE)


@pytest.mark.parametrize("probe", ["python3 probe_ci_available.py", "python3 probe_phone_captures.py",
                                  "echo probe_phone_available.py", "printf '%s' probe_phone_available.py",
                                  "python3 -c 'probe_phone_available.py'", "curl -o probe_phone_available.py",
                                  "curl https://example.com/phone_available.py", "", "'", None])
def test_unrelated_or_passive_producers_are_not_owner_steps(probe):
    info = waits.describe("scheduled", park={"wake_check": probe, "awaiting": {
        "kind": "external", "ref": "capture service", "why": "passive logs"}})
    assert info["wait_state"] == "waiting"
    assert "owner_step" not in info


@pytest.mark.parametrize("op", ["schedule", "continue", "block"])
def test_real_lifecycle_refuses_owner_probe_atomically(kanban_home, monkeypatch, op):
    with helpers.kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        helpers._as_worker(monkeypatch, conn, tid)
        args = {"reason": "Wait for natural paired-phone visibility; no repeated ask.",
                "wake_check": PHONE, "awaiting": PARK["awaiting"]}
        if op == "continue":
            args["checkpoint"] = args.pop("reason")
            args["resume_after_seconds"] = 900
        elif op == "block":
            args["kind"] = "time"
        result = helpers._tool(op, args)
        assert "human_wait" in result.get("error", ""), result
        assert helpers.kb.get_task(conn, tid).status == "running"
        assert helpers.kwc.get_state(conn, tid) is None
        assert not [e for e in helpers.kb.list_events(conn, tid) if e.kind == "scheduled"]


@pytest.mark.parametrize("probe", [PHONE, "/usr/bin/python3 -S /workspace/probe_phone_available.py",
                                  "python3.14 -I -B '/some path/probe_paired_iphone_visibility.py'",
                                  "/workspace/probe_device_available.py"])
def test_known_legacy_probe_invocations(probe):
    assert waits.owner_device_step(probe)


def test_system_clock_requeue_is_not_a_new_device_ask(monkeypatch):
    from hermes_cli import kanban_comment_origin
    monkeypatch.setattr(kanban_comment_origin, "check_park", lambda *args: None)
    declared = {"kind": "clock", "why": "system retry backoff"}
    assert awaiting.resolve(None, "t_abcdef1234567890", awaiting=declared,
                            reason="internal requeue", wake_check=PHONE, system=True) == declared


def test_validated_human_ask_takes_precedence():
    info = waits.describe("scheduled", human=True, park=PARK)
    assert info["wait_state"] == "human"
    assert "owner_step" not in info


def test_other_statuses_do_not_resurrect_old_device_wait():
    for status in ("running", "done", "ready", "review"):
        assert waits.describe(status, park=PARK) is None
