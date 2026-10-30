"""A KeepAlive launchd job that serves the home under a label Hermes did not install.

The mini supervises ``~/.bb-hermes`` as ``com.daveey.babybear.hermes`` (``~/.hermes`` is a symlink
to it). Discovery only knew ``ai.hermes.gateway[-suffix]``, so during the job's restart gap a client
saw "no service", spawned an unmanaged gateway (no idle exit), and every KeepAlive restart was then
refused by that owner for good (self-heal defect gateway-e2cd786685). The launchctl boundary is a
stub: nothing here touches a real service manager.
"""
import plistlib
import pwd
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

LABEL = "com.example.babybear.hermes"


def _agent(tmp_path, home, **overrides):
    argv = [str(tmp_path / ".hermes/bin/hermes-gateway-launcher"), "--home", str(home), "--",
            str(tmp_path / "code/hermes-agent/.venv/bin/hermes"), "gateway", "run", "--external-supervisor"]
    definition = dict(Label=LABEL, KeepAlive=True, RunAtLoad=True, ProgramArguments=argv,
                      EnvironmentVariables=dict(HERMES_HOME=str(home), HOME=str(tmp_path)))
    definition.update(overrides)
    agents = tmp_path / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{LABEL}.plist").write_bytes(plistlib.dumps(definition))
    # An unrelated agent of the same home that is not a gateway must never be picked.
    (agents / "com.example.babybear.brain.plist").write_bytes(plistlib.dumps(dict(
        Label="com.example.babybear.brain", KeepAlive=True, ProgramArguments=["/usr/bin/python3", "brain.py"],
        EnvironmentVariables=dict(HERMES_HOME=str(home)))))
    return definition


def _mini_home(tmp_path):
    """``~/.hermes`` -> ``~/.bb-hermes``: the client asks for the symlink, the plist pins the target."""
    real = tmp_path / ".bb-hermes"
    real.mkdir(mode=0o700)
    link = tmp_path / ".hermes"
    link.symlink_to(real)
    return link, real


def _launchctl(monkeypatch, service, *, loaded=(LABEL,), state="not running"):
    calls = []

    def run(argv, *_a, **_k):
        calls.append(list(argv))
        assert argv[0] == "launchctl"
        if argv[1] == "print":
            label = argv[2].rsplit("/", 1)[1]
            if label in loaded and argv[2].startswith("gui/"):
                return SimpleNamespace(returncode=0, stdout=f"{argv[2]} = {{\n\tstate = {state}\n}}\n")
            return SimpleNamespace(returncode=113, stdout="")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(service, "_run", run)
    return calls


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path), pw_name="owner"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def test_custom_label_keepalive_job_for_this_home_is_the_service(account, monkeypatch):
    from hermes_cli import gateway_runtime_service as service
    link, real = _mini_home(account)
    _agent(account, real)
    calls = _launchctl(monkeypatch, service)

    found = service._launchd(link, time.monotonic() + 5)

    assert found is not None, "a KeepAlive job serving this home under another label is the service"
    assert found.backend == "launchd"
    # Plain kickstart (start if stopped), never -k: a running owner is left alone.
    assert found.start_argv[:2] == ("launchctl", "kickstart") and found.start_argv[-1].endswith("/" + LABEL)
    assert "-k" not in found.start_argv and found.running is False
    service.start_existing_gateway_service(found, deadline=time.monotonic() + 5)
    assert calls[-1][:2] == ["launchctl", "kickstart"]


def test_running_custom_job_is_awaited_not_restarted(account, monkeypatch):
    from hermes_cli import gateway_runtime_service as service
    link, real = _mini_home(account)
    _agent(account, real)
    calls = _launchctl(monkeypatch, service, state="running")
    found = service._launchd(link, time.monotonic() + 5)
    assert found is not None and found.running is True
    service.start_existing_gateway_service(found, deadline=time.monotonic() + 5)
    assert not any(c[1] == "kickstart" for c in calls)


@pytest.mark.parametrize("case", ["other_home", "no_keepalive", "disabled", "not_a_gateway", "not_loaded",
                                  "launcher_other_home", "profile_home"])
def test_custom_job_must_be_an_exact_keepalive_gateway_of_this_home(case, account, monkeypatch):
    from hermes_cli import gateway_runtime_service as service
    link, real = _mini_home(account)
    other = account / ".other-hermes"
    other.mkdir()
    overrides = {
        "other_home": {"EnvironmentVariables": dict(HERMES_HOME=str(other))},
        "no_keepalive": {"KeepAlive": False},
        "disabled": {"Disabled": True},
        "not_a_gateway": {"ProgramArguments": [str(account / "code/hermes-agent/.venv/bin/hermes"), "dashboard"]},
        "launcher_other_home": {"ProgramArguments": [str(account / ".hermes/bin/hermes-gateway-launcher"), "--home",
                                                     str(other), "--", "hermes", "gateway", "run"]},
    }.get(case, {})
    if case == "profile_home":
        # A profile under the home is a different home: the root's job does not serve it by this rule.
        link = real / "profiles" / "worker"
        link.mkdir(parents=True)
    _agent(account, real, **overrides)
    _launchctl(monkeypatch, service, loaded=() if case == "not_loaded" else (LABEL,))
    assert service._launchd(link, time.monotonic() + 5) is None


def test_ensure_starts_the_custom_supervisor_and_never_spawns(account, monkeypatch):
    """The live failure: an absent owner inside the job's restart gap. Before the fix ensure spawned."""
    from hermes_cli import gateway_runtime as runtime, gateway_runtime_service as service, gateway_runtime_start as start
    link, real = _mini_home(account)
    _agent(account, real)
    calls = _launchctl(monkeypatch, service)
    spawned = []
    monkeypatch.setattr(service.sys, "platform", "darwin")  # os-marker: ok — launchctl and the plist dir are stubbed; the logic is host-independent
    monkeypatch.setattr("hermes_cli.service_manager._s6_running", lambda: False)
    monkeypatch.setattr(runtime, "discover_gateway_endpoint", lambda *a, **k: runtime.GatewayDiscovery("absent"))
    monkeypatch.setattr(start, "spawn_unmanaged_gateway", lambda target, **k: spawned.append(Path(target)))

    runtime.ensure_gateway_runtime(link, timeout=0.3)

    assert spawned == [], "a supervised home must never get an unmanaged gateway"
    assert any(c[:2] == ["launchctl", "kickstart"] and c[-1].endswith("/" + LABEL) for c in calls)


def test_detached_replacement_hands_the_home_to_the_custom_supervisor(account, monkeypatch, capsys):
    """`gateway restart --detach` with no canonical service used to spawn unmanaged right away."""
    from hermes_cli import gateway_runtime_service as service, gateway_runtime_start as start
    link, real = _mini_home(account)
    _agent(account, real)
    calls = _launchctl(monkeypatch, service)
    monkeypatch.setattr(service.sys, "platform", "darwin")  # os-marker: ok — launchctl and the plist dir are stubbed; the logic is host-independent
    monkeypatch.setattr("hermes_cli.service_manager._s6_running", lambda: False)
    monkeypatch.setattr(start, "spawn_unmanaged_gateway", lambda *a, **k: pytest.fail("spawned beside a supervisor"))

    assert start.start_detached_replacement(link) == 0
    assert any(c[:2] == ["launchctl", "kickstart"] and c[-1].endswith("/" + LABEL) for c in calls)
    assert LABEL in capsys.readouterr().out


def test_detached_replacement_without_any_service_still_spawns(account, monkeypatch):
    from hermes_cli import gateway_runtime_service as service, gateway_runtime_start as start
    link, _real = _mini_home(account)
    _launchctl(monkeypatch, service, loaded=())
    monkeypatch.setattr(service.sys, "platform", "darwin")  # os-marker: ok — launchctl and the plist dir are stubbed; the logic is host-independent
    monkeypatch.setattr("hermes_cli.service_manager._s6_running", lambda: False)
    spawned = []
    monkeypatch.setattr(start, "spawn_unmanaged_gateway", lambda target, **k: spawned.append(Path(target)))
    assert start.start_detached_replacement(link) == 0
    assert spawned == [link]
