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

from hermes_cli import kanban_lockguard as _kanban_lockguard
from hermes_cli import kanban_ro_batch as _kanban_ro_batch
import json
import os
import re
import shlex
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

ENV = "HERMES_ORIGIN_SESSION"

_NAME = r"[A-Za-z0-9_.-]{1,128}"
_REF_FORMS = (
    re.compile(rf"^@session:(?P<profile>{_NAME})@(?P<gw>{_NAME})/(?P<session>{_NAME})$"),
    re.compile(rf"^(?P<gw>{_NAME})/(?P<profile>{_NAME})/(?P<session>{_NAME})$"),
    re.compile(rf"^(?P<gw>{_NAME}):(?P<session>{_NAME})$"),
)
# This-gateway form, as session links and cross_session write it: ``@session:<profile>/<id>``.
_LOCAL_FORM = re.compile(rf"^@session:(?P<profile>{_NAME})/(?P<session>{_NAME})$")


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
    """``@session:<profile>@<gw>/<id>``, ``<gw>/<profile>/<id>``, ``<gw>:<id>`` (profile default) or the
    this-gateway ``@session:<profile>/<id>``."""
    raw = str(text or "").strip()
    lm = _LOCAL_FORM.match(raw)
    if lm:
        return Origin(gateway_name() or "local", lm["profile"], lm["session"])
    for form in _REF_FORMS:
        m = form.match(raw)
        if m:
            d = m.groupdict()
            return Origin(d["gw"], d.get("profile") or "default", d["session"])
    raise OriginError("origin_ref_invalid",
                      f"origin session {raw!r} is not @session:<profile>@<gateway>/<id>, "
                      "<gateway>/<profile>/<id>, <gateway>:<id> or @session:<profile>/<id>")


def ambient_origin_ref() -> str:
    return (os.environ.get(ENV) or "").strip()


def _hub_db() -> Optional[Path]:
    base = os.environ.get("HERMES_HUB_STATE") or str(Path.home() / ".local" / "state" / "hermes-hub")
    p = Path(base) / "hub.db"
    return p if p.is_file() else None


def _ro(path: Path) -> sqlite3.Connection:
    """A read-only connection to ``path``; inside ``kanban_ro_batch.batch()`` a borrow of the pass's shared one."""
    return _kanban_ro_batch.open_ro(path, _open_ro)


def _open_ro(path: Path) -> sqlite3.Connection:
    try:
        conn = _kanban_lockguard.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn
    except sqlite3.OperationalError:
        conn = _kanban_lockguard.connect(f"file:{path}?mode=rw", uri=True, timeout=0.5)
        conn.execute("PRAGMA query_only = ON")
        return conn


_NAMES_MAX_AGE_S = 5.0
_NAMES_RACY_NS = 100_000_000
_names_memo: Optional[tuple[tuple, float, str, frozenset]] = None
_NAME_MAX_AGE_S = 600.0  # ``gateway_name``'s own memo: see its docstring
_name_memo: Optional[tuple[tuple, float, str]] = None


def _stat_sig(path: Path) -> Optional[tuple[int, int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_ino, st.st_mtime_ns, st.st_size


def _mail_config() -> Path:
    return Path(os.environ.get("HERMES_MAIL_CONFIG") or Path.home() / ".config" / "hermes-mail" / "config.json")


def gateway_names() -> tuple[str, set[str]]:
    """``(this gateway, every gateway hermes-hub knows)`` from the local hub.db; ("", set()) without one.

    The answer is reused while its inputs (hub.db, its WAL, the mail config) keep their identity, size and mtime,
    for at most ``_NAMES_MAX_AGE_S``: links reconcile, creator backfill and provenance checks ask once per row,
    and each uncached answer opens hub.db (4% of hub gateway CPU, 2026-11-12).

    The local replica removes tombstoned rows on pull. Names with surviving rows remain addressable:
    placement aliases can point in either direction during rollout and do not prove which partition is live.
    Once the old partition's tombstones arrive, its name disappears naturally. [daveey]"""
    global _names_memo
    path, cfg = _hub_db(), _mail_config()
    key = _names_key(path, cfg)
    memo, now = _names_memo, time.monotonic()
    if memo is not None and memo[0] == key and now - memo[1] < _NAMES_MAX_AGE_S:
        return memo[2], set(memo[3])
    failed: list[sqlite3.Error] = []
    me, known = _read_gateway_names(path, cfg, failed)
    # A file written within the timestamp granularity could change again under the same signature: not kept.
    # Nor is an answer from a hub.db read that failed (locked, mid-replace): the next caller reads again.
    racy = time.time_ns() - _NAMES_RACY_NS
    _names_memo = None if failed or any(isinstance(sig, tuple) and sig[1] >= racy for sig in key) else (
        key, now, me, frozenset(known))
    return me, known


def gateway_name() -> str:
    """This gateway's own name: exactly ``gateway_names()[0]``, without the known-gateway set.

    A still-valid ``gateway_names`` memo answers first. Otherwise the name is remembered for ``_NAME_MAX_AGE_S``
    (10 min), keyed on hub.db's identity (path, device, inode) and the mail config's signature, never on hub.db's
    WAL or mtime: the hub writes those continuously, so a memo keyed on them would almost never be kept, and every
    card in a task list asked again (an open + close of hub.db each, 70% of ``session_tasks`` CPU on the hub,
    2026-11-20). Re-enrolling replaces hub.db (a new inode), which busts the memo, as does any change to the mail
    config. A miss reads only the hub.db ``meta`` row (a primary-key lookup) and the mail config, never the
    ``replica`` scan. ``gateway_names`` (the known set, which does change) keeps its short memo.

    A gateway CAN be renamed in place (``hermes-mail rename-self``: same key, same hub.db file). The rename
    writes the mail config's ``gateway`` and hub.db ``meta.gateway``; hermes-hub also copies the config name
    into ``meta.gateway`` on every sync, so the two agree except while a rename is being written. The config
    write changes the memo key, and a name read while the two disagree is not kept (the next caller reads
    again), so the new name is returned from the first call after the rename completes, with no restart. Only
    a ``meta.gateway`` edit that leaves the mail config untouched (nothing supported does that, and the next
    hermes-hub sync reverts it) can lag, for at most ``_NAME_MAX_AGE_S``. An empty name is never kept either:
    hermes-hub writes ``meta.gateway`` only after its first sync, and an empty answer must not stick for
    10 min. [daveey]"""
    global _name_memo
    path, cfg = _hub_db(), _mail_config()
    memo, now = _names_memo, time.monotonic()
    if memo is not None and now - memo[1] < _NAMES_MAX_AGE_S and memo[0] == _names_key(path, cfg):
        return memo[2]
    key = _name_key(path, cfg)
    own = _name_memo
    if own is not None and own[0] == key and now - own[1] < _NAME_MAX_AGE_S:
        return own[2]
    failed: list[sqlite3.Error] = []
    me = _read_gateway_names(path, cfg, failed, names=False)[0]
    # A failed hub.db read (locked, mid-replace), an empty name (hub not synced yet), a mail config written
    # within the timestamp granularity, or a name the mail config does not agree with (a rename is half
    # written) is not kept: the next caller reads again.
    cfg_sig = key[-1]
    racy = isinstance(cfg_sig, tuple) and cfg_sig[1] >= time.time_ns() - _NAMES_RACY_NS
    renaming = _mail_gateway(cfg) not in ("", me)
    _name_memo = None if failed or racy or renaming or not me else (key, now, me)
    return me


def _mail_gateway(cfg: Path) -> str:
    """The gateway name the hermes-mail config carries; "" when it has none or cannot be read."""
    try:
        return str(json.loads(cfg.read_text(encoding="utf-8-sig")).get("gateway") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _name_key(path: Optional[Path], cfg: Path) -> tuple:
    """Identity of ``gateway_name``'s inputs: hub.db's file identity (not its contents) and the mail config."""
    ident = None
    if path is not None:
        try:
            st = path.stat()
            ident = (st.st_dev, st.st_ino)
        except OSError:
            ident = None
    return str(path), ident, str(cfg), _stat_sig(cfg)


def _names_key(path: Optional[Path], cfg: Path) -> tuple:
    """Identity of the gateway-name inputs: hub.db, its WAL and the mail config."""
    return (str(path), _stat_sig(path) if path else None, _stat_sig(Path(f"{path}-wal")) if path else None,
            str(cfg), _stat_sig(cfg))


def _read_gateway_names(path: Optional[Path], cfg: Path,
                        failed: Optional[list[sqlite3.Error]] = None, *, names: bool = True) -> tuple[str, set[str]]:
    me, known = "", set()
    if path is not None:
        try:
            conn = _ro(path)
            try:
                row = conn.execute("SELECT v FROM meta WHERE k = 'gateway'").fetchone()
                me = str(row[0]) if row and row[0] else ""
                if names:
                    known = {str(r[0]) for r in conn.execute("SELECT DISTINCT gw FROM replica") if r[0]}
            finally:
                conn.close()
        except sqlite3.Error as exc:
            if failed is not None:
                failed.append(exc)
    if not me:
        me = _mail_gateway(cfg)
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
    if _LOCAL_FORM.match(raw):  # names this very gateway: its chat already owns its cards
        return None
    origin = parse_origin(raw)
    me, known = gateway_names()
    if not me:
        raise OriginError("hub_not_installed",
                          "this gateway has no hermes-hub client (no hub.db / hermes-mail config), so a card created "
                          f"here can never show in {origin.ref}; install hermes-hub or create the cards on that gateway")
    if origin.gateway not in known:  # a ref written before its gateway was renamed still names that gateway
        from hermes_cli import kanban_gateway_alias
        origin = Origin(kanban_gateway_alias.current(origin.gateway, known), origin.profile, origin.session)
    if origin.gateway == me:
        return None
    if origin.gateway not in known:
        raise OriginError("origin_gateway_unknown",
                          f"gateway {origin.gateway!r} is not a hermes-hub gateway (known: {', '.join(sorted(known))}); "
                          "use the hermes-hub gateway name in the origin ref")
    return origin


def _replica_task_owners(task_id: str, board: str) -> list[tuple[str, str]]:
    """``(gateway, profile)`` of every hermes-hub replica task record keyed ``<profile>/<board>/<task_id>``."""
    path = _hub_db()
    if path is None:
        return []
    try:
        conn = _ro(path)
        try:
            rows = conn.execute("SELECT gw, key FROM replica WHERE coll = 'tasks' AND key LIKE ? ESCAPE '\\'",
                                ("%/" + re.sub(r"([%_\\])", r"\\\1", f"{board}/{task_id}"),)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    owners = []
    for gw, key in rows:
        parts = str(key or "").split("/")
        if len(parts) == 3 and parts[1:] == [board, task_id] and gw:
            owners.append((str(gw), parts[0]))
    return sorted(set(owners))


def _request_id(task_id: str, session: str) -> str:
    return f"adopt:{task_id}:{session}"[:128]


# One name in a hermes-hub route (gateway, profile, board, task, session), as hermes-hub validates it; it is
# also what keeps the command shell-safe unquoted and never parsed as a flag (no leading '-').
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


def _safe(value: Any, placeholder: str) -> str:
    text = str(value or "").strip()
    return text if _SAFE_NAME.match(text) else placeholder


def _turn_session_id(env: Mapping[str, str], explicit_env: bool) -> str:
    """The chat this turn runs for. A gateway or Desktop backend serves several chats in one process, and
    ``os.environ["HERMES_SESSION_ID"]`` holds whichever chat wrote it last: the current turn's id is the session
    ContextVar (``get_session_env`` falls back to the env only when none is bound). A hint built from the env
    told chat 370c8b to subscribe chat 5fb607 to its cards (agent-884fdd96f8). An explicit ``env`` (tests, a
    caller replaying a child's env) is used as given. [daveey]"""
    if explicit_env:
        return str(env.get("HERMES_SESSION_ID") or "")
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return str(env.get("HERMES_SESSION_ID") or "")
    return str(get_session_env("HERMES_SESSION_ID", "") or "")


def adopt_next_step(task_id: str, board: str, *, env: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """The supported way to attach an existing card to a chat, for a ``kanban_origin_adopt`` call that has no
    courier context. That tool only runs inside a courier-delivered adoption; a chat (or a worker) gets the
    exact ``hermes-hub adopt-task`` command instead, filled from args, the hub replica and the session env.

    Returns ``{next_step, run_on, complete, message}`` plus ``delegate`` for a worker whose origin chat is on
    another gateway: a ``hermes-hub create-task`` that gets the command run there. Unknown or unsafe parts stay
    ``<placeholders>`` and ``complete`` is False: such a command must be filled in before it is run.
    """
    explicit_env = env is not None
    env = os.environ if env is None else env
    me, _ = gateway_names()
    me = _safe(me, "")
    task_id = _safe(task_id, "<task-id>")
    board = _safe(board, "<board>")
    try:
        from hermes_cli.profiles import current_profile_name
        my_profile = _safe(current_profile_name("default"), "default")
    except (ImportError, OSError, ValueError):
        my_profile = "default"
    worker = bool((env.get("HERMES_KANBAN_TASK") or "").strip())
    # Execution placement is not card custody: a pool worker may run a hub-home card.
    # Only a unique replica owner may populate an adoption destination.
    owners = _replica_task_owners(task_id, board)
    unknown_owner = len(owners) != 1
    owner_gw, owner_profile = ((_safe(owners[0][0], "<owner-gateway>"), _safe(owners[0][1], "<owner-profile>"))
                               if len(owners) == 1 else ("<owner-gateway>", "<owner-profile>"))
    if worker:
        # The adoption request must be queued FROM the origin chat's gateway.
        try:
            origin = parse_origin(ambient_origin_ref()) if ambient_origin_ref() else None
        except OriginError:
            origin = None
        run_on = _safe(origin and origin.gateway, "<origin-gateway>")
        session = _safe(origin and origin.session, "<origin-session-id>")
        origin_profile = _safe(origin and origin.profile, "<origin-profile>")
    else:
        run_on = me or "<this-gateway>"
        session = _safe(_turn_session_id(env, explicit_env), "<origin-session-id>")
        # "custom" = a HERMES_HOME outside ~/.hermes: no profile name to pass; the CLI resolves its own.
        origin_profile = my_profile if my_profile != "custom" else ""
    if me and owner_gw == me and owner_profile == my_profile and (not worker or run_on == me):
        # Local provenance and tui subscriptions are profile-local. Cross-profile
        # adoption keeps both identities through the existing courier route.
        step = f"hermes kanban --board {board} origin-adopt {task_id} --local"
        who = ("A worker is fenced from that on its own board: report to the chat with cross_session instead, or "
               "have the chat run it:" if worker else "Bind this chat to it by running in a terminal here:")
        return {"next_step": step, "run_on": me, "complete": "<" not in step,
                "message": f"{task_id} and the chat are both on this gateway ({me}); kanban_origin_adopt is only for "
                           f"a courier-delivered adoption. {who} {step}"}
    step = (f"hermes-hub adopt-task {owner_gw} {owner_profile}/{board}/{task_id} --origin-session {session} "
            + (f"--origin-profile {origin_profile} " if origin_profile else "")
            + f"--id {_request_id(task_id, session)}")
    out: dict[str, Any] = {"next_step": step, "run_on": run_on, "complete": "<" not in step}
    if worker:
        title = f"Attach {owner_gw} card {task_id} to chat {session}"
        body = (f"Run on this gateway: {step}  Then confirm with: hermes-hub links {session} "
                f"--expect {owner_gw}/{owner_profile}/{board}/{task_id}")
        out["delegate"] = (f"hermes-hub create-task {run_on} {shlex.quote(title)} --body {shlex.quote(body)} "
                           "--assignee default --origin-session ''")
        out["message"] = (
            "kanban_origin_adopt only runs inside a courier-delivered adoption; a worker cannot attach its card to "
            f"a chat with it. The origin chat's gateway ({run_on}) must queue the request: {step} . From this "
            f"worker, get it run there with: {out['delegate']} (do not ask a human to call kanban_origin_adopt).")
    else:
        out["message"] = (
            "kanban_origin_adopt only runs inside a courier-delivered adoption, never from a chat. Attach the card "
            f"to this chat by running in a terminal on this gateway ({run_on}): {step}"
            + (f" ({task_id} on board {board!r} is not in this gateway's hermes-hub replica, so fill in the owner "
               "gateway and profile: `hermes-hub tasks --all` lists them.)" if unknown_owner else ""))
    if not out["complete"]:
        out["message"] += " Replace every <placeholder> before running it."
    return out


def bind(conn: Any, task_id: str, origin: Origin, *, board: Optional[str] = None) -> dict[str, Any]:
    """Subscribe ``origin`` to ``task_id`` (platform 'hub'); idempotent. Returns the binding receipt."""
    from hermes_cli import kanban_db_notify as kbn

    # ``reason: origin`` marks the binding as owning: children inherit it (``_inherit_notify_subs``),
    # unlike the hermes-hub ``shepherd`` subs that only watch this card. [daveey]
    kbn.add_notify_sub(conn, task_id=task_id, platform="hub", chat_id=origin.chat_id,
                       delivery_metadata={"reason": "origin"})
    me, _ = gateway_names()
    target = f"{me}/{board or 'default'}/{task_id}"
    return {"origin": origin.ref, "chat_id": origin.chat_id, "task": target, "readback": "pending",
            "verify_on_origin": f"hermes-hub links {origin.session} --expect {target}",
            "verify_visible": (f"hermes -p {shlex.quote(origin.profile)} kanban chat-tasks "
                               f"{shlex.quote(origin.session)} --expect {shlex.quote(target)}")}


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
