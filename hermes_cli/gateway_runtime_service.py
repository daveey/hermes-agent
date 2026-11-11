"""Non-installing service-manager operations for an explicit canonical profile.

The legacy start helpers refresh units, regenerate plists, or fall back to
unmanaged processes. Client startup must not call those helpers.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
from xml.parsers.expat import ExpatError

from gateway.runtime_contract import RuntimeState
from hermes_cli._subprocess_compat import windows_hide_flags


class RuntimeStartError(RuntimeError):
    def __init__(self, reason: str, state: RuntimeState = "inaccessible", *, detail: str | None = None):
        # ``reason`` is the stable code callers match on; ``detail`` is what the user reads.
        super().__init__(reason)
        self.reason, self.state = reason, state
        # One line, bounded: Desktop prints the first line of ``detail`` after the reason code. It echoes
        # service definitions and child output that users paste into reports, so redact before truncating.
        if detail:
            from agent.redact import redact_sensitive_text
            detail = " ".join(redact_sensitive_text(detail, force=True, redact_url_credentials=True).split())[:500]
        self.detail = detail or None


def remaining(deadline: float) -> float:
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise TimeoutError
    return budget


def service_suffix(home: Path) -> str:
    """Host-service suffix for the REQUESTED home, never the initiating client's env.

    Same rule as ``hermes_cli.gateway._profile_suffix``: only the platform-native
    default home owns the bare unit name; ``<root>/profiles/<name>`` yields the
    profile name; any other root (Docker, a temp harness) yields a path hash so a
    custom root can never resolve to the production ``hermes-gateway`` unit.
    """
    from hermes_constants import account_default_hermes_home
    from hermes_cli.gateway import _profile_name_from_home
    default = account_default_hermes_home().resolve()
    home = home.resolve()
    if home == default:
        return ""
    root = default
    if not home.is_relative_to(default):
        root = home.parent.parent if home.parent.name == "profiles" else home
    if home != root:
        name = _profile_name_from_home(home, root)
        if name:
            return name
    return hashlib.sha256(str(home).encode()).hexdigest()[:8]


@dataclass(frozen=True)
class ExistingService:
    backend: str
    start_argv: tuple[str, ...]
    running: bool = False


def _run(argv: list[str] | tuple[str, ...], deadline: float, *, encoding: str | None = "utf-8"):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                          text=encoding is not None, encoding=encoding, errors="strict" if encoding else None,
                          creationflags=windows_hide_flags(), timeout=remaining(deadline), check=False)


def _verify_binding(verify, *args, **kwargs):
    try:
        return verify(*args, **kwargs)
    except (ValueError, TypeError, KeyError, AttributeError, ExpatError) as exc:
        reason = str(exc)
        if reason in {"profile_mismatch", "service_account_mismatch", "service_disabled"}:
            raise RuntimeStartError(reason) from None
        raise RuntimeStartError("service_identity_unverified", detail=_unverified_detail(exc)) from None


def _unverified_detail(exc: Exception) -> str:
    return getattr(exc, "check", None) or f"{type(exc).__name__} while reading the service definition: {exc}"


def _child_failure(argv: list[str] | tuple[str, ...], code: int, output: str) -> str:
    first = next((line.strip() for line in (output or "").splitlines() if line.strip()), "no output")
    return f"{' '.join(argv)} exited {code}: {first}"


def _exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _systemd(home: Path, deadline: float) -> ExistingService | None:
    from hermes_cli import gateway as gw
    from hermes_cli.service_manager import _s6_running
    from hermes_cli.gateway_runtime_service_identity import ForeignRoot, SYSTEMD_IDENTITY_PROPERTIES, verify_systemd
    if _s6_running():
        raise RuntimeStartError("external_supervisor")
    suffix = service_suffix(home)
    unit = f"hermes-gateway{'-' + suffix if suffix else ''}.service"
    paths = (Path.home() / ".config/systemd/user" / unit, Path("/etc/systemd/system") / unit)
    # On non-systemd hosts there is no manager to own transient units. Installed
    # definitions still count: a broken manager is not permission to bypass one.
    # Every Hermes entrypoint ensures the gateway, so a host with no installed unit must not
    # touch the service manager at all (a headless runner has no user bus to answer it).
    # With a unit installed anywhere, both scopes are asked: two claiming the name is a conflict.
    installed = (*paths, Path("/usr/lib/systemd/system") / unit, Path("/lib/systemd/system") / unit)
    if not any(_exists(p) for p in installed):
        return None
    found = []
    for system in (False, True):
        command = gw._systemctl_cmd(system)
        result = _run([*command, "show", unit, "--no-pager", "--all",
                       "--property=LoadState,ActiveState,SubState,UnitFileState," + ",".join(SYSTEMD_IDENTITY_PROPERTIES)], deadline)
        props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if not _exists(paths[int(system)]) and (result.returncode or props.get("LoadState") == "not-found"):
            # Nothing installed in this scope: an unreachable or empty manager there has no
            # unit that could serve or block this home.
            continue
        if result.returncode or props.get("LoadState") != "loaded":
            raise RuntimeStartError("service_manager_unavailable")
        if props.get("UnitFileState") in {"masked", "masked-runtime"}:
            raise RuntimeStartError("service_masked")
        state = props.get("ActiveState")
        if state == "deactivating":
            raise RuntimeStartError("service_draining", "draining")
        if state not in {"inactive", "active", "activating", "reloading", "failed"}:
            raise RuntimeStartError("service_state_unknown")
        if state == "failed":
            raise RuntimeStartError("service_failed")
        environment = _run([*command, "show-environment"], deadline)
        if environment.returncode:
            raise RuntimeStartError("service_identity_unverified", detail=_child_failure(
                [*command, "show-environment"], environment.returncode, environment.stderr or environment.stdout))
        try:
            verify_systemd(props, environment.stdout, home, system=system)
        except ForeignRoot:
            # A unit named like ours but pinned to another HOME root belongs to a
            # different install (a sandbox HOME beside a real one); it neither serves
            # nor blocks this home.
            continue
        except ValueError as exc:
            reason = str(exc)
            if reason in {"profile_mismatch", "service_account_mismatch"}:
                raise RuntimeStartError(reason) from None
            raise RuntimeStartError("service_identity_unverified", detail=_unverified_detail(exc)) from None
        found.append(ExistingService("systemd", tuple([*command, "--no-ask-password", "start", unit]),
                                     state != "inactive"))
    if len(found) > 1:
        raise RuntimeStartError("service_scope_conflict", "conflict")
    return found[0] if found else None


def _s6(home: Path, deadline: float) -> ExistingService | None:
    """Find the container gateway slot that owns ``home``.

    TUI clients call ``ensure_gateway_runtime`` inside the Docker image too.
    They must request that s6 starts the registered slot, rather than falling
    through the Linux systemd probe and treating s6 as inaccessible.
    """
    from hermes_cli.service_manager import S6ServiceManager
    from hermes_cli.gateway_runtime_multiplex import implied_host_root

    # with-contenv is also the service run script's authority. The initiating
    # client may have selected a named profile or a different install root.
    try:
        container_home = Path("/run/s6/container_environment/HERMES_HOME").read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeStartError("service_identity_unverified",
                                detail=f"cannot read the container's HERMES_HOME: {exc}") from None
    if not container_home:
        raise RuntimeStartError("service_identity_unverified", detail="the container's HERMES_HOME is empty")
    root = Path(container_home).resolve()
    home = home.resolve()
    if home != root and home.parent != root / "profiles":
        raise RuntimeStartError("profile_mismatch")
    # A normal named profile belongs to the root multiplexer. Returning None
    # lets ensure select its host, including a first boot with no saved verdict.
    if home != root and implied_host_root(home) == root:
        return None
    profile = "default" if home == root else home.name
    manager = S6ServiceManager()
    service_name = f"gateway-{profile}"
    if not (manager.scandir / service_name).is_dir():
        raise RuntimeStartError("service_manager_unavailable")
    return ExistingService("s6", (service_name,), manager.is_running(service_name))


def _launchd(home: Path, deadline: float) -> ExistingService | None:
    import pwd
    suffix = service_suffix(home)
    label = f"ai.hermes.gateway{'-' + suffix if suffix else ''}"
    account = pwd.getpwuid(os.getuid())  # windows-footgun: ok — native launchd only
    account_home = Path(account.pw_dir)
    plist = account_home / "Library/LaunchAgents" / f"{label}.plist"
    from hermes_cli.gateway_runtime_service_identity import verify_launchd_loaded, verify_launchd_plist, read_definition
    installed = _exists(plist)
    domains = [f"gui/{os.getuid()}", f"user/{os.getuid()}"]  # windows-footgun: ok — native launchd only
    found = []
    loaded = []
    for domain in domains:
        result = _run(["launchctl", "print", f"{domain}/{label}"], deadline)
        if result.returncode == 0:
            loaded.append(result.stdout)
            found.append(ExistingService("launchd", ("launchctl", "kickstart", f"{domain}/{label}"),
                                         "state = running" in result.stdout))
            continue
        # launchctl's native absent-service code; other errors retain uncertainty.
        if result.returncode != 113:
            raise RuntimeStartError("service_manager_unavailable")
    if len(found) > 1:
        raise RuntimeStartError("service_scope_conflict", "conflict")
    if found:
        _verify_binding(verify_launchd_loaded, loaded[0], home, uid=os.getuid(), username=account.pw_name)  # windows-footgun: ok — native launchd only
        return found[0]
    if installed:
        definition = _verify_binding(plistlib.loads, read_definition(plist))
        _verify_binding(verify_launchd_plist, definition, label, home)
        # Load ONLY the existing file, never bootout/rewrite or kickstart -k.
        return ExistingService("launchd", ("launchctl", "bootstrap", domains[0], str(plist)))
    return _custom_launchd_job(home.resolve(), account_home / "Library/LaunchAgents", domains[0], deadline)


# Trailing ``gateway run`` flags a supervised job may carry; anything else is not a plain gateway run.
_CUSTOM_JOB_RUN_FLAGS = frozenset({"--quiet", "--external-supervisor", "--replace"})


def _custom_job_runs_gateway_for(argv: list, home: Path) -> bool:
    """``argv`` ends in ``<hermes | -m hermes_cli.main> [--profile X] gateway run [flags]``, optionally behind
    a launcher wrapper; any ``--home`` the wrapper names must be this home too."""
    if not argv or not all(isinstance(arg, str) for arg in argv):
        return False
    for index, arg in enumerate(argv):
        if arg == "--home" and index + 1 < len(argv):
            if Path(argv[index + 1]).expanduser().resolve() != home:
                return False
    starts = [i for i in range(len(argv) - 1) if argv[i] == "gateway" and argv[i + 1] == "run"]
    if len(starts) != 1 or not set(argv[starts[0] + 2:]) <= _CUSTOM_JOB_RUN_FLAGS:
        return False
    before = argv[:starts[0]]
    if len(before) >= 2 and before[-2] in {"--profile", "-p"}:
        before = before[:-2]
    entry = before[-1] if before else ""
    return entry == "hermes_cli.main" or Path(entry).name in {"hermes", "hermes.exe"}


def custom_launchd_supervisor_for_home(home: Path, *, deadline: float) -> ExistingService | None:
    """[daveey] The loaded KeepAlive LaunchAgent (any label) that serves ``home``, else None.

    ``hermes update`` asks this before relaunching a MANUAL gateway: a home that such a job serves is
    that job's to restart (gateway-e2cd786685, second occurrence). macOS only; any probe failure
    answers None so the caller keeps its old behavior."""
    if sys.platform != "darwin":
        return None
    import pwd
    try:
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)  # windows-footgun: ok — native launchd only
        return _custom_launchd_job(Path(home).expanduser().resolve(), account_home / "Library/LaunchAgents",
                                   f"gui/{os.getuid()}", deadline)  # windows-footgun: ok — native launchd only
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _custom_launchd_job(home: Path, agents: Path, domain: str, deadline: float) -> ExistingService | None:
    """[daveey] A KeepAlive LaunchAgent that serves THIS home under a label Hermes did not install.

    Hosts that supervise a gateway themselves (the mini: ``com.daveey.babybear.hermes`` pins
    ``HERMES_HOME`` and runs ``gateway run --external-supervisor`` under a launcher) have no
    ``ai.hermes.gateway*`` job, so this probe used to answer "no service" and the client spawned an
    unmanaged gateway inside the job's restart gap. That spawn has no idle exit, so it held the home and
    every KeepAlive restart was refused forever (self-heal defect gateway-e2cd786685). Matched exactly:
    the plist's ``HERMES_HOME`` resolves to this home (a profile under it is another home), KeepAlive is
    on, the job is not Disabled, it runs ``gateway run``, and it is loaded in the gui domain. The result
    is a plain ``kickstart`` (start it if stopped), never ``-k``, ``bootstrap`` or a plist rewrite."""
    from hermes_cli.gateway_runtime_service_identity import read_definition
    try:
        plists = sorted(agents.glob("*.plist"))
    except OSError:
        return None
    for plist in plists:
        try:
            definition = plistlib.loads(read_definition(plist))
        except (OSError, ValueError, ExpatError, plistlib.InvalidFileException):
            continue  # an unreadable agent cannot be shown to serve this home
        if not isinstance(definition, dict) or definition.get("Disabled") or not definition.get("KeepAlive"):
            continue
        label, env = definition.get("Label"), definition.get("EnvironmentVariables")
        configured = env.get("HERMES_HOME") if isinstance(env, dict) else None
        if not isinstance(label, str) or not label or not isinstance(configured, str) or not configured:
            continue
        if not Path(configured).is_absolute() or Path(configured).expanduser().resolve() != home:
            continue
        if not _custom_job_runs_gateway_for(definition.get("ProgramArguments") or [], home):
            continue
        result = _run(["launchctl", "print", f"{domain}/{label}"], deadline)
        if result.returncode != 0:
            continue  # installed but not loaded: launchd is not supervising it now
        return ExistingService("launchd", ("launchctl", "kickstart", f"{domain}/{label}"),
                               "state = running" in result.stdout)
    return None


def _windows_task_name(home: Path) -> str:
    suffix = service_suffix(home)
    return f"Hermes_Gateway{'_' + suffix if suffix else ''}"


def refresh_ready_owner_launcher(home: Path, observed) -> None:
    """Repair a ready owner's logon launcher (``refresh_dead_launcher``). Ensure returns a ready owner
    before discovery, yet one can run on the new payload while its launcher still names the deleted
    interpreter: started by ``hermes gateway start``, or the old payload removed once it outlived it."""
    if observed.state != "ready" or sys.platform != "win32":
        return
    from hermes_cli.gateway_runtime import control_home_for
    from hermes_cli.gateway_windows import refresh_dead_launcher
    owner = control_home_for(home, observed.endpoint)
    refresh_dead_launcher(owner, _windows_task_name(owner))


def _windows(home: Path, deadline: float) -> ExistingService | None:
    from hermes_cli.gateway_windows import _decode_schtasks_output, refresh_dead_launcher
    name = _windows_task_name(home)
    # Before the task runs it, and for the next logon: a Store update deletes the launcher's interpreter.
    refresh_dead_launcher(home, name)

    def query(refusal: str, *argv: str) -> str:
        # schtasks/whoami write the console code page, and /XML declares UTF-16 over those bytes:
        # decode like the drift check does (#116193) instead of the locale codec or raw bytes (#139083).
        result = _run(list(argv), deadline, encoding=None)
        if result.returncode:
            output = _decode_schtasks_output(result.stderr or result.stdout or b"")
            raise RuntimeStartError(refusal, detail=_child_failure(argv, result.returncode, output))
        return _decode_schtasks_output(result.stdout or b"")

    rows = csv.reader(io.StringIO(query("service_manager_unavailable", "schtasks.exe", "/Query", "/FO", "CSV", "/NH")))
    # Orphaned TaskCache entries print localized one-field diagnostics among the rows (#142140). Only a
    # well-formed row can name our task, so skipping the rest can only fall through to the unmanaged path.
    if any(len(row) == 3 and row[0].lstrip("\\") == name for row in rows):
        from hermes_cli.gateway_runtime_service_identity import verify_windows_task
        unverified = "service_identity_unverified"
        definition = query(unverified, "schtasks.exe", "/Query", "/TN", name, "/XML")
        accounts = list(csv.reader(io.StringIO(query(unverified, "whoami.exe", "/USER", "/FO", "CSV", "/NH"))))
        if len(accounts) != 1 or len(accounts[0]) != 2:
            raise RuntimeStartError("service_identity_unverified", detail=(
                f"whoami.exe /USER printed {len(accounts)} row(s) of {[len(row) for row in accounts]} field(s), "
                "expected one account and SID"))
        _verify_binding(verify_windows_task, definition, home, *accounts[0])
        return ExistingService("windows", ("schtasks.exe", "/Run", "/TN", name))
    # A Startup-folder entry (the fallback when Scheduled Task install was declined/blocked) is login
    # persistence only, with no start verb or identity to verify. Starting a stopped owner is the
    # same detached direct spawn manual `hermes gateway start` uses beside it; the unmanaged path
    # pins the explicit home and refuses a job-bound (no-breakaway) child, and the runtime lock
    # plus host attach arbitrate against the entry firing again at the next login.
    return None


def _no_supervisor(home: Path, deadline: float) -> None:
    """Android/Termux has no service manager (main refuses `gateway install/start` there and says
    "Run manually: hermes gateway"): an unmanaged detached start is its only owner. Python 3.13+
    reports ``sys.platform == "android"``; older Termux interpreters report linux and reach the
    systemd probe, which finds no unit and lands on the same unmanaged start."""


def discover_existing_gateway_service(home: Path, *, deadline: float) -> ExistingService | None:
    from hermes_cli.service_manager import _s6_running

    backend = _s6 if _s6_running() else {"linux": _systemd, "darwin": _launchd, "win32": _windows,
                                         "android": _no_supervisor}.get(sys.platform)
    if backend is None:
        raise RuntimeStartError("unsupported_supervisor_platform")
    try:
        return backend(home, deadline)
    except subprocess.TimeoutExpired:
        raise TimeoutError from None
    except (OSError, ValueError) as exc:
        raise RuntimeStartError("service_manager_unavailable") from exc


def start_existing_gateway_service(service: ExistingService, *, deadline: float) -> None:
    """Request start once; only live control readiness can establish success."""
    if service.running:
        return
    if service.backend == "s6":
        from hermes_cli.service_manager import S6ServiceManager
        try:
            S6ServiceManager().start(service.start_argv[0])
        except subprocess.TimeoutExpired:
            raise TimeoutError from None
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeStartError("service_start_failed") from exc
        return
    try:
        result = _run(service.start_argv, deadline, encoding=None)
    except subprocess.TimeoutExpired:
        raise TimeoutError from None
    except OSError as exc:
        raise RuntimeStartError("service_start_failed") from exc
    if result.returncode:
        raise RuntimeStartError("service_start_failed")
