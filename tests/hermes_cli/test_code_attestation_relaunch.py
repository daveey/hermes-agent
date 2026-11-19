"""Route ``managed-stub-relaunch``: the ``venv_sync.relaunch_command`` child of the managed stub.

The MacBook gateway always runs as this child, so before this route existed ``_detect_route``
refused it outright (``relaunched-launch``; the old ``test_relaunch_prefix_script_refuses`` pinned
that). The route is now supported with the direct route's invariant: every byte of Python that
runs before the audit hook is identified byte-exact, and nothing the process says about itself
(flags, env, ``sys.orig_argv``) is evidence by itself. Every case below is a REAL subprocess
launched through the real stub/bootstrap/main (fixtures shared with test_code_attestation_route),
except the one-shot hook attribution unit and the gateway gate, which consumes recorded ledgers
from those real launches.
"""
import json
from pathlib import Path
import sys
import types

import pytest

from tests.gateway import test_quiet_control as _quiet
from tests.hermes_cli import test_code_attestation_route as _route
from tests.hermes_cli.test_code_attestation_route import (
    ARGS, assert_supported, current_script, refused, run, run_stub)
from tests.gateway.test_quiet_control import CleanLedger, attest, ok, start_params, write_window

pytestmark = pytest.mark.live_system_guard_bypass

# Shared fixtures (real committed core copy, the test interpreter, the quiet-control env).
core = _route.core
python = _route.python
env = _quiet.env

RELAUNCH = "managed-stub-relaunch"


def relaunch_command(interpreter, root, script, *, options=("-I",), args=ARGS):
    """Exactly what the parent stub's bootstrap execs: ``venv_sync.relaunch_command``."""
    from hermes_cli import venv_sync
    original = [str(interpreter), *options, "-c", script, *args]
    return venv_sync.relaunch_command(Path(interpreter), root, ["-c", *args], original, None)


def _source_at(command):
    """Index of the inline source in a relaunch command: right after ``-c`` (the program's argv follows it)."""
    return command.index("-c", 1) + 1


def _with_source(command, source):
    at = _source_at(command)
    return [*command[:at], source, *command[at + 1:]]


def spaced_python(tmp_path):
    """The test venv reached through a directory whose name has a space (the Mac's interpreter is
    ``.../Hermes Python.app/Contents/MacOS/python3.14``); the venv itself is unchanged."""
    link = tmp_path / "Hermes Python.app"
    link.symlink_to(Path(sys.prefix), target_is_directory=True)
    interpreter = link / "bin" / Path(sys.executable).name
    assert " " in str(interpreter) and interpreter.exists()
    return interpreter


def launcher(probe):
    found = [s for s in probe["seed"] if s["kind"] == "trusted_launcher_script"]
    assert len(found) == 1
    return found[0]


# ---- accept -------------------------------------------------------------------------------

def test_relaunch_of_the_current_stub_is_attested_through_a_spaced_interpreter(tmp_path, core):
    from hermes_cli import code_attestation
    interpreter = spaced_python(tmp_path)
    command = relaunch_command(interpreter, core, current_script(core))
    # The Mac's live shape: the stub's own -I plus the relaunch's -I, one -c source, then the program's argv
    # (it names what the process runs in ps and `hermes doctor`; the prefix assigns the same sys.argv).
    assert command[0] == str(interpreter) and command[1:4] == ["-I", "-I", "-c"] and command[5:] == list(ARGS)
    probe = run(tmp_path, command)
    assert_supported(probe, core, code_attestation.CURRENT_TEMPLATE_HEAD_DIGEST, route=RELAUNCH)
    record = launcher(probe)
    assert record["route"] == RELAUNCH and record["relaunch_options"] == 2
    assert record["orig_argv0"] == str(interpreter)
    # The embedded stub script ran through the prefix's exec() AFTER the hook: attributed to the
    # trusted launcher (no unrooted-exec:<string>), and only that one code object.
    assert probe["report"]["launcher"]["script_exec_observed"] is True
    prefix = {s["module"] for s in probe["seed"] if s["kind"] == "relaunch_prefix_stdlib"}
    assert prefix == {"runpy", "importlib.util", "pathlib"}


def test_launch_handoff_env_is_traced_data_not_evidence(tmp_path, core, python):
    """``venv_sync.exec_launch`` hands the exec'd child its verdict in the environment; the child's
    ``prepare_launch`` pops it only after bootstrap installed attestation, so a POSIX relaunch
    always installs with it set. It selects a currency verdict, never code: accepted, reported."""
    from hermes_cli import code_attestation
    command = relaunch_command(python, core, current_script(core))
    probe = run(tmp_path, command, extra_env={code_attestation.LAUNCH_HANDOFF_ENV: f"current:1:{core}"})
    assert_supported(probe, core, code_attestation.CURRENT_TEMPLATE_HEAD_DIGEST, route=RELAUNCH)
    assert launcher(probe)["launch_handoff_env"] is True


def test_handoff_env_on_a_direct_launch_still_refuses(tmp_path, core, python):
    from hermes_cli import code_attestation
    probe = run_stub(tmp_path, python, current_script(core),
                     extra_env={code_attestation.LAUNCH_HANDOFF_ENV: f"current:1:{core}"})
    refused(probe, "relaunched-launch")


# ---- near misses: each a real subprocess ----------------------------------------------------

def _prefix(core):
    from hermes_cli import _launch_template
    return _launch_template.relaunch_prefix(str(core), ["-c", *ARGS])


def test_module_body_refuses(tmp_path, core, python):
    source = _prefix(core) + "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
    refused(run(tmp_path, [str(python), "-I", "-I", "-c", source, *ARGS]), "unsupported-route:relaunch-body")


def test_run_path_body_refuses(tmp_path, core, python):
    stub = tmp_path / "stub.py"
    stub.write_text(current_script(core))
    source = _prefix(core) + f"runpy.run_path({str(stub)!r}, run_name='__main__')"
    refused(run(tmp_path, [str(python), "-I", "-I", "-c", source, *ARGS]), "unsupported-route:relaunch-body")


def test_other_interpreter_option_refuses(tmp_path, core, python):
    command = relaunch_command(python, core, current_script(core), options=("-X", "utf8", "-I"))
    assert command[1:5] == ["-X", "utf8", "-I", "-I"]
    refused(run(tmp_path, command), "unsupported-route:relaunch-options")


def test_argv_mismatch_refuses(tmp_path, core, python):
    command = relaunch_command(python, core, current_script(core)) + ["--extra"]
    refused(run(tmp_path, command), "unsupported-route:relaunch-argv")


def test_trailing_argv_other_than_sys_argv_refuses(tmp_path, core, python):
    """After the source only sys.argv's exact tail is admitted: dropped or reordered tokens refuse."""
    command = relaunch_command(python, core, current_script(core))
    at = command.index("-c", 1) + 2
    refused(run(tmp_path, command[:at]), "unsupported-route:relaunch-argv")
    second = tmp_path / "second"
    second.mkdir()
    refused(run(second, [*command[:at], *reversed(command[at:])]), "unsupported-route:relaunch-argv")


def test_prefix_differing_in_one_byte_refuses(tmp_path, core, python):
    command = relaunch_command(python, core, current_script(core))
    # Same length, same behaviour: only the bytes differ.
    changed = command[_source_at(command)].replace("_iu, pathlib as _pl;", "_iu,pathlib as _pl ;", 1)
    assert changed != command[_source_at(command)] and len(changed) == len(command[_source_at(command)])
    refused(run(tmp_path, _with_source(command, changed)), "unsupported-route:relaunch-source")


def test_extra_statements_refuse(tmp_path, core, python):
    command = relaunch_command(python, core, current_script(core))
    prefix = _prefix(core)
    body = command[_source_at(command)][len(prefix):]
    before = _with_source(command, prefix + "pass; " + body)
    refused(run(tmp_path, before), "unsupported-route:relaunch-body")
    second = tmp_path / "second"
    second.mkdir()
    refused(run(second, _with_source(command, command[_source_at(command)] + "; pass")), "unsupported-route:relaunch-body")


def test_tampered_embedded_script_refuses(tmp_path, core, python):
    script = current_script(core)
    changed = script.replace("sys.exit(main())\n", "sys.exit(main() or 0)\n")
    assert changed != script
    refused(run(tmp_path, relaunch_command(python, core, changed)), "unsupported-route:stub-tail")


def test_relaunch_for_another_root_refuses(tmp_path, core, python):
    alias = tmp_path / "alias-root"
    alias.symlink_to(core, target_is_directory=True)
    command = relaunch_command(python, alias, current_script(core))
    refused(run(tmp_path, command), "unsupported-route:relaunch-source")


def test_non_canonical_exec_literal_refuses(tmp_path, core, python):
    """Same script, same prefix, an equivalent but non-canonical literal: not the producer's bytes."""
    command = relaunch_command(python, core, current_script(core))
    prefix = _prefix(core)
    assert command[_source_at(command)].startswith(prefix + "exec(") and command[_source_at(command)].endswith(")")
    loose = prefix + "exec( " + command[_source_at(command)][len(prefix) + len("exec("):-1] + " )"
    refused(run(tmp_path, _with_source(command, loose)), "unsupported-route:relaunch-source")


def test_rewritten_orig_argv_is_not_evidence(tmp_path, core, python):
    """A different -c program that rewrites ``sys.orig_argv`` to the genuine relaunch before the
    bootstrap: every argv check passes, the executing frame's code does not."""
    genuine = relaunch_command(python, core, current_script(core))
    lie = f"import sys; sys.orig_argv[:] = {genuine!r}; " + genuine[_source_at(genuine)]
    refused(run(tmp_path, [str(python), "-I", "-I", "-c", lie]), "unsupported-route:frame-code")


# ---- hook attribution of the embedded script (in-process unit) -----------------------------

def _bare_ledger(expected_frame_code, script_code):
    import threading
    from hermes_cli.code_attestation import Attestation
    ledger = object.__new__(Attestation)  # no hook installed into the test process
    ledger._lock = threading.RLock()
    ledger._unknown = set()
    ledger._generation = 0
    ledger._launcher = {}
    ledger._launcher_exec = (expected_frame_code, script_code)
    return ledger


def test_only_the_embedded_script_code_from_the_prefix_frame_is_the_launcher():
    frame = sys._getframe()
    script = compile("value = 1\n", "<string>", "exec")
    ledger = _bare_ledger(frame.f_code, script)
    assert ledger._launcher_event(compile("value = 1\n", "<string>", "exec"), frame) is True
    assert ledger._unknown == set() and ledger._launcher["script_exec_observed"] is True
    # One-shot: a second exec from the same frame is observed as any other string exec.
    assert ledger._launcher_event(script, frame) is False

    tampered = _bare_ledger(frame.f_code, script)
    assert tampered._launcher_event(compile("value = 2\n", "<string>", "exec"), frame) is True
    assert tampered._unknown == {"relaunch-exec-mismatch"}

    other = _bare_ledger(compile("pass", "<string>", "exec"), script)
    assert other._launcher_event(script, frame) is False and other._unknown == set()


# ---- the gateway gate consumes the ledger ---------------------------------------------------

class Recorded(CleanLedger):
    """A ledger as recorded from a real launch's probe (unknowns and route, nothing invented)."""

    def __init__(self, probe):
        super().__init__(probe["unknowns"], probe["report"]["route"])


@pytest.fixture(scope="module")
def recorded(tmp_path_factory, core):
    interpreter = Path(sys.executable)
    script = current_script(core)
    out = {}
    for name, command in (
            ("direct", [str(interpreter), "-I", "-c", script, *ARGS]),
            ("relaunch", relaunch_command(interpreter, core, script)),
            ("near_miss", relaunch_command(interpreter, core, script) + ["--extra"])):
        out[name] = Recorded(run(tmp_path_factory.mktemp(name), command))
    assert out["direct"]._route == "managed-stub" and out["relaunch"]._route == RELAUNCH
    return out


def _use(env, ledger):
    env.runner._quiet_code = ledger
    attest(env)


def test_quiet_start_admits_the_relaunch_route_where_it_admits_the_direct_route(env, recorded):
    _use(env, recorded["near_miss"])
    refused_start = ok(env, "quiet-observe-start", start_params(env))
    assert refused_start["valid"] is False and "observer_token" not in refused_start
    assert "unknown:code:unsupported-route:relaunch-argv" in refused_start["reasons"]
    for name in ("direct", "relaunch"):
        _use(env, recorded[name])
        started = ok(env, "quiet-observe-start", start_params(env))
        assert started.get("observer_token"), (name, started)


def test_missing_code_ledger_is_pending_coverage(env):
    env.runner._quiet_code = None
    attest(env)
    result = ok(env, "quiet-observe-start", start_params(env))
    assert result["phase"] == "coverage_pending" and "code_attestation_missing" in result["reasons"]


def test_snapshot_reads_latched_code_unknowns_without_an_inventory(env):
    class Latched(CleanLedger):
        def unknowns(self):
            raise AssertionError("snapshots must not inventory modules")
    attest(env)
    token = ok(env, "quiet-observe-start", start_params(env))["observer_token"]
    env.runner._quiet_code = Latched(["unrooted-exec:<string>"])
    from tests.gateway.test_quiet_control import follow_params
    snap = ok(env, "quiet-observe-snapshot", follow_params(env, token))
    assert snap["valid"] is False and "unknown:code:unrooted-exec:<string>" in snap["reasons"]


# ---- --check: no lease, no cooldown, no epoch move, agrees with start -----------------------

def _tree(root):
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(Path(root).rglob("*")) if p.is_file()}


def _check(env, capsys):
    from hermes_cli import kanban_quiet

    def query(home, verb, *, params=None, report_unsupported=False, timeout=None):
        raw = json.dumps({"protocol": 1, "verb": verb, "id": 1, "params": params}).encode()
        response = json.loads(env.server.handle_request_line(raw, "uid:1000"))
        return response.get("result") if response.get("ok") else None

    code = kanban_quiet.check(env.root, query=query)
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    return code, json.loads(lines[0])


def test_check_takes_no_lease_writes_nothing_and_does_not_move_the_epoch(env, recorded, capsys):
    from gateway.control_dispatch import verb_is_work_bearing
    assert not verb_is_work_bearing("quiet-observe-check")  # never reserved in the registry
    _use(env, recorded["relaunch"])
    before = (_tree(env.root), env.registry.snapshot().epoch, env.registry.membership_generation())
    code, doc = _check(env, capsys)
    assert code == 0 and doc["admitted"] is True and doc["reasons"] == []
    assert doc["route"] == RELAUNCH and doc["registry_unknowns"] == []
    after = (_tree(env.root), env.registry.snapshot().epoch, env.registry.membership_generation())
    assert after == before
    started = ok(env, "quiet-observe-start", start_params(env))
    assert started.get("observer_token")  # start then agrees


def test_check_reports_the_route_refusal_and_start_agrees(env, recorded, capsys):
    _use(env, recorded["near_miss"])
    code, doc = _check(env, capsys)
    assert code == 3 and doc["admitted"] is False and doc["route"] is None
    assert "unsupported-route:relaunch-argv" in "".join(doc["registry_unknowns"])
    started = ok(env, "quiet-observe-start", start_params(env))
    assert "observer_token" not in started and set(started["reasons"]) <= set(doc["reasons"])


def test_check_and_start_agree_on_a_stale_update_window(env, recorded, capsys):
    _use(env, recorded["relaunch"])
    write_window(env.root, "idle", age=700)
    code, doc = _check(env, capsys)
    assert code == 3 and "update_window_stale" in doc["reasons"]
    started = ok(env, "quiet-observe-start", start_params(env))
    assert started["reasons"] == ["update_window_stale"]


def test_check_without_a_gateway_is_refused(tmp_path, capsys):
    from hermes_cli import kanban_quiet
    code = kanban_quiet.check(tmp_path, query=lambda *a, **k: None)
    doc = json.loads(capsys.readouterr().out)
    assert code == 3 and doc["reasons"] == ["gateway_unavailable"]


def test_check_cli_rejects_lease_arguments():
    from hermes_cli import kanban_quiet
    args = types.SimpleNamespace(check=True, max_s=5, reason=None, observer_argv=None)
    assert kanban_quiet.cmd_quiet_hold(args) == kanban_quiet.EXIT_USAGE


# ---- quiet-hold asks quiet-observe-check BEFORE the lease (no 2 h cooldown on a doomed start) --

def _serve(env, answer=None):
    """Route the production control-socket client to the fixture gateway (wire format as in
    ``_check``), or to a fixed ``answer`` when one is given (an older core, no gateway)."""
    from gateway import control_socket

    def query(home, verb, *, params=None, report_unsupported=False, timeout=None):
        if answer is not None:
            return answer()
        raw = json.dumps({"protocol": 1, "verb": verb, "id": 1, "params": params}).encode()
        response = json.loads(env.server.handle_request_line(raw, "uid:1000"))
        return response.get("result") if response.get("ok") else None

    env.monkeypatch.setattr(control_socket, "query_gateway_control", query)


@pytest.fixture
def hold_home(env, tmp_path):
    """A second HERMES_HOME for the hold: ``env`` already holds its own lease (and so a cooldown) in
    ``env.root``. The fixture gateway keeps serving and reads the lease and window from here."""
    from agent import claim_quiet as cq
    home = tmp_path / "hold-home"
    home.mkdir(mode=0o700)
    env.monkeypatch.setenv("HERMES_HOME", str(home))
    write_window(home, "idle")
    assert cq.inspect().state == "inactive"
    return home


def test_hold_refused_by_the_gateway_check_takes_no_lease_and_spends_no_cooldown(
        env, recorded, hold_home, tmp_path, capsys):
    from agent import claim_quiet as cq
    from hermes_cli import kanban_quiet
    _use(env, recorded["near_miss"])  # a launch route attestation does not support
    _serve(env)
    epoch = (env.registry.snapshot().epoch, env.registry.membership_generation())
    marker = tmp_path / "ran"
    code = kanban_quiet.hold(30, "measure", [sys.executable, "-c", f"open({str(marker)!r}, 'w')"])
    captured = capsys.readouterr()
    assert not (hold_home / "kanban" / "claim-quiet" / "last-started.json").exists(), "cooldown was committed"
    assert not marker.exists(), "the observer must never start"
    assert code == kanban_quiet.EXIT_CHECK_REFUSED, captured.err
    doc = json.loads(captured.out)
    assert doc["admitted"] is False and "unsupported-route:relaunch-argv" in "".join(doc["registry_unknowns"])
    assert "no cooldown spent" in captured.err
    assert cq.inspect().state == "inactive", "the lease was taken"
    assert (env.registry.snapshot().epoch, env.registry.membership_generation()) == epoch


def test_hold_admitted_by_the_gateway_check_acquires_and_the_gateway_starts(
        env, recorded, hold_home, tmp_path):
    import threading
    from agent import claim_quiet as cq
    from hermes_cli import kanban_quiet
    from tests.waiting import wait_until
    _use(env, recorded["relaunch"])
    _serve(env)
    go = tmp_path / "go"
    observer = ("import os, sys, threading\n"
                "while not os.path.exists(sys.argv[1]): threading.Event().wait(0.02)\n")
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("code", kanban_quiet.hold(
        60, "measure", [sys.executable, "-c", observer, str(go)])))
    worker.start()
    try:
        wait_until(lambda: cq.read_active() is not None, timeout=60, what="hold lease")
        held = type("Held", (), {"lease": cq.read_active()})
        started = ok(env, "quiet-observe-start", start_params(held))
        assert started.get("observer_token"), started
    finally:
        go.write_text("x")
        worker.join(60)
    assert not worker.is_alive() and result["code"] == kanban_quiet.EXIT_OK
    assert (hold_home / "kanban" / "claim-quiet" / "last-started.json").exists()
    assert cq.inspect().state == "inactive"


@pytest.mark.parametrize("answer", [lambda: {"unsupported": True}, lambda: None],
                         ids=["older-core", "no-gateway"])
def test_hold_proceeds_when_the_gateway_does_not_answer_the_check(env, hold_home, answer, capsys):
    from hermes_cli import kanban_quiet
    _serve(env, answer)
    assert kanban_quiet.hold(30, "measure", ["true"]) == kanban_quiet.EXIT_OK
    assert (hold_home / "kanban" / "claim-quiet" / "last-started.json").exists()
    assert "gateway check unavailable" in capsys.readouterr().err
