"""Origin binding for cards created on behalf of a session on ANOTHER gateway. [daveey]

A chat on gateway A often hands a whole job to an orchestrator session on gateway B (``hermes chat -q`` over
SSH, a mailbox task, ...), which then creates the real Kanban cards on B. Those cards carry B's orchestrator as
``session_id``, so A's chat lost them: its task list was empty and its sidebar row painted done while B ran the
work for hours.

The origin is declared once, when the orchestrator starts: ``hermes chat --origin-session <ref>`` (or
``HERMES_ORIGIN_SESSION=<ref>``). Every card it then creates through the supported paths (the ``kanban_create``
tool, ``hermes kanban create``) gets a ``platform='hub'`` notify subscription ``<gw>/<profile>/<session>`` for
that origin. hermes-hub publishes it as a ``rel: shepherd`` session link, children inherit it
(``_inherit_notify_subs``), and gateway A's ``kanban_session_activity`` reads it from its replica for the task
list, the sidebar counts and the archive guard alike. The card's own ``session_id`` (B's creator) is never
rewritten.

Binding is not handoff: the result says ``readback: pending`` until gateway A has seen the link
(``hermes-hub links <session> --expect <gw>/<board>/<id>`` on A exits 0 only then).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

ENV = "HERMES_ORIGIN_SESSION"

_NAME = r"[A-Za-z0-9_.-]{1,128}"
_REF_FORMS = (
    re.compile(rf"^@session:(?P<profile>{_NAME})@(?P<gw>{_NAME})/(?P<session>{_NAME})$"),
    re.compile(rf"^(?P<gw>{_NAME})/(?P<profile>{_NAME})/(?P<session>{_NAME})$"),
    re.compile(rf"^(?P<gw>{_NAME}):(?P<session>{_NAME})$"),
)


class OriginError(ValueError):
    """A typed setup failure: ``code`` is machine-readable, ``str()`` says what to do."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Origin:
    gateway: str
    profile: str
    session: str

    @property
    def chat_id(self) -> str:
        return f"{self.gateway}/{self.profile}/{self.session}"

    @property
    def ref(self) -> str:
        return f"@session:{self.profile}@{self.gateway}/{self.session}"


def parse_origin(text: str) -> Origin:
    """``@session:<profile>@<gw>/<id>``, ``<gw>/<profile>/<id>`` or ``<gw>:<id>`` (profile default)."""
    raw = str(text or "").strip()
    for form in _REF_FORMS:
        m = form.match(raw)
        if m:
            d = m.groupdict()
            return Origin(d["gw"], d.get("profile") or "default", d["session"])
    raise OriginError("origin_ref_invalid",
                      f"origin session {raw!r} is not @session:<profile>@<gateway>/<id>, "
                      "<gateway>/<profile>/<id> or <gateway>:<id>")


def ambient_origin_ref() -> str:
    return (os.environ.get(ENV) or "").strip()


def _hub_db() -> Optional[Path]:
    base = os.environ.get("HERMES_HUB_STATE") or str(Path.home() / ".local" / "state" / "hermes-hub")
    p = Path(base) / "hub.db"
    return p if p.is_file() else None


def _ro(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn
    except sqlite3.OperationalError:
        conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=0.5)
        conn.execute("PRAGMA query_only = ON")
        return conn


def gateway_names() -> tuple[str, set[str]]:
    """``(this gateway, every gateway hermes-hub knows)`` from the local hub.db; ("", set()) without one."""
    me, known = "", set()
    path = _hub_db()
    if path is not None:
        try:
            conn = _ro(path)
            try:
                row = conn.execute("SELECT v FROM meta WHERE k = 'gateway'").fetchone()
                me = str(row[0]) if row and row[0] else ""
                known = {str(r[0]) for r in conn.execute("SELECT DISTINCT gw FROM replica") if r[0]}
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    if not me:
        try:
            cfg = Path(os.environ.get("HERMES_MAIL_CONFIG") or Path.home() / ".config" / "hermes-mail" / "config.json")
            me = str(json.loads(cfg.read_text()).get("gateway") or "")
        except (OSError, ValueError, AttributeError):
            me = ""
    if me:
        known.add(me)
    return me, known


def resolve(explicit: Optional[str] = None) -> Optional[Origin]:
    """The origin to bind new cards to (explicit arg, else ``$HERMES_ORIGIN_SESSION``), validated; None if neither
    is set or it names this gateway (a same-gateway chat already owns its cards through ``session_id``/tui subs).
    Raises ``OriginError`` when the ref is malformed or names a gateway hermes-hub does not know: such a binding
    could never be read back on the origin, which is exactly the silent failure this exists to stop."""
    raw = (explicit or "").strip() or ambient_origin_ref()
    if not raw:
        return None
    origin = parse_origin(raw)
    me, known = gateway_names()
    if not me:
        raise OriginError("hub_not_installed",
                          "this gateway has no hermes-hub client (no hub.db / hermes-mail config), so a card created "
                          f"here can never show in {origin.ref}; install hermes-hub or create the cards on that gateway")
    if origin.gateway == me:
        return None
    if origin.gateway not in known:
        raise OriginError("origin_gateway_unknown",
                          f"gateway {origin.gateway!r} is not a hermes-hub gateway (known: {', '.join(sorted(known))}); "
                          "use the hermes-hub gateway name in the origin ref")
    return origin


def bind(conn: Any, task_id: str, origin: Origin, *, board: Optional[str] = None) -> dict[str, Any]:
    """Subscribe ``origin`` to ``task_id`` (platform 'hub'); idempotent. Returns the binding receipt."""
    from hermes_cli import kanban_db_notify as kbn

    kbn.add_notify_sub(conn, task_id=task_id, platform="hub", chat_id=origin.chat_id)
    me, _ = gateway_names()
    target = f"{me}/{board or 'default'}/{task_id}"
    return {"origin": origin.ref, "chat_id": origin.chat_id, "task": target, "readback": "pending",
            "verify_on_origin": f"hermes-hub links {origin.session} --expect {target}"}


def preflight(tool_names: Iterable[str]) -> None:
    """A session started with an origin must be able to create bound cards through the real tool. Raises
    ``OriginError('kanban_tool_unavailable')`` instead of letting the agent fall back to private handlers."""
    if not ambient_origin_ref():
        return
    resolve()
    if "kanban_create" not in set(tool_names):
        raise OriginError("kanban_tool_unavailable",
                          f"{ENV} is set but the kanban_create tool is not available in this session; enable the "
                          "'kanban' toolset (hermes tools enable kanban) and start the session again")
