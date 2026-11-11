"""Best-effort local HTTP comment provenance, not human authentication.

Only a resolved raw loopback TCP peer is inspected. Remote/unresolvable callers retain the
legacy human default; explicit agent courier labels are handled before this heuristic.

Known limits (each records the legacy ``human``, never a false ``agent`` for a person):
- a caller dialing this host's LAN/tailnet address instead of loopback is not inspected;
- a peer owned by another user, a non-dumpable process (Linux ``sshd-session`` carrying a
  Desktop SSH-target ``ssh -L`` tunnel) or a socket already closed is unresolvable;
- macOS returns an empty environment for Apple-protected binaries (``/usr/bin/curl``), so
  a marker set only on such a process is invisible; a readable marked ancestor still counts;
- an agent-started local proxy/relay presents its own (possibly clean) ancestry.
A GUI launched from an agent terminal inherits its markers and records ``agent``. Hermes's
own dashboard/webapp auto-open drops them (``top_level_env.open_url_for_person``); other
in-process ``webbrowser.open`` callers (OAuth/device-flow sign-ins) can still cold-start a
marked browser. macOS denies the global socket inventory, so each bare comment scans
same-user processes there.
"""
from __future__ import annotations

import ipaddress
import sys

from hermes_cli import kanban_comment_writer as ancestry


def _endpoint(value):
    if not value or len(value) != 2:
        return None
    try:
        address = ipaddress.ip_address(value[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address, int(value[1])
    except (ValueError, TypeError):
        return None


def _socket_owners(psutil, peer, server) -> set[int]:
    def matches(connection):
        return _endpoint(connection.laddr) == peer and _endpoint(connection.raddr) == server

    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.Error, OSError):
        connections = []
    matching = [c for c in connections if matches(c)]
    owners = {c.pid for c in matching if c.pid}
    if (owners and all(c.pid for c in matching)) or sys.platform != "darwin":
        # Linux attributes every socket it can read in the global inventory; a per-process
        # pass can't read more (other users', non-dumpable sshd-session) and costs a scan.
        return owners
    # macOS may deny the global inventory but expose same-user process sockets. An
    # inaccessible unrelated process is not evidence about this connection's ancestry.
    try:
        for proc in psutil.process_iter():
            try:
                if any(matches(c) for c in proc.net_connections(kind="tcp")):
                    owners.add(proc.pid)
            except (psutil.Error, OSError):
                continue
    except (psutil.Error, OSError):
        pass  # No resolved owner keeps the explicitly required legacy default.
    return owners


def http_comment_writer(scope: dict) -> tuple[str, None]:
    """Lower bare comments from a resolved agent (or unreadable ancestry) to agent."""
    # Windows cannot complete the existing ancestry walk for Explorer-launched GUI
    # processes (Explorer's parent commonly exits). Keep the pre-existing default
    # there rather than relabel every person's drawer note; explicit agents still
    # bypass this helper. The socket heuristic is supported on Linux and Darwin.
    if sys.platform not in ("linux", "darwin"):
        return "human", None
    raw = scope.get("hermes.raw_transport")
    if raw is None:
        return "human", None
    peer, server = map(_endpoint, raw)
    if peer is None or server is None or not peer[0].is_loopback:
        return "human", None
    try:
        import psutil
    except ImportError:
        return "human", None
    for pid in _socket_owners(psutil, peer, server):
        try:
            if ancestry.agent_ancestor(pid) is not None:
                return "agent", None
        except ancestry.Inconclusive:
            return "agent", None
    return "human", None
