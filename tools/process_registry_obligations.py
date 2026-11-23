"""[daveey] Completion custody for gateway-watched background jobs across a gateway restart.

A messaging-gateway job's completion has one consumer, the process watcher task the gateway runs for it
(``gateway/run_process_watcher.py``; the post-turn queue drain discards process completions). The
watcher polls every few seconds. ``_move_to_finished`` writes the job's result receipt, removes it from
``_running`` and drops its checkpoint entry before that poll. A gateway restart in that gap left
nothing to re-adopt: no running entry, and recovery reads a receipt as "observed and delivered"
(``result_recorded``). The completion was lost even though the receipt held the exit and output.

The receipt now also carries the owed delivery. In the same atomic write that records the result
(before the checkpoint entry goes), a gateway-watched ``notify_on_complete`` job's receipt gets a
``delivery_obligation`` block with its watcher route. The obligation is settled (the block removed)
only when one of these happens:

- the watcher's completion is admitted, or found already admitted (the durable completion admission
  keyed by producer identity ``(completion, proc id, started_at)``);
- the watcher ends on an intended suppression (the result was consumed, the notice is not surfaced,
  or there is no text);
- the agent consumes the output through ``wait`` / ``read_log`` / ``kill``.

A gateway start (``recover_from_checkpoint(owner="gateway")`` only) re-arms a watcher for each owed
receipt of its own profile whose writer is gone. That watcher delivers the real exit and output once,
through the normal admission path, so a replay after a lost acknowledgement resolves to the existing
admission instead of a second turn. Receipts without the block stay retrieval-only, never replayed.
Recovery is bounded by ``OBLIGATION_MAX_AGE_SECONDS`` and ``OBLIGATION_MAX_RECOVERIES``.

``python -m tools.process_registry_obligations reopen proc_<id> ...`` marks the obligation on one
existing receipt by exact id. It refuses when a completion admission for that identity already exists.

A Desktop chat's notify job (``owner: tui``) finished in the Desktop backend that hosts the chat carries the same
block. Its delivery is in that backend's memory until the notice turn starts; a SIGKILL/OOM of the backend
(no orderly exit journal) lost it. ``tui_gateway/completion_obligation.py`` settles it (turn start, intended
suppression, the exit journal's custody transfer), and Desktop recovery (``recover_from_checkpoint(owner="tui")``)
re-arms an owed one whose writer is gone, once per start, through the re-adopt sink. Each owner adopts only its
own (``owner``; a block without one is the gateway's). A tui obligation whose chat is never reopened (deleted or
archived) is re-armed and wakes that chat on each Desktop start until ``OBLIGATION_MAX_RECOVERIES`` or
``OBLIGATION_MAX_AGE_SECONDS`` drops it: bounded by design, like the gateway's.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("tools.process_registry")

OBLIGATION_KEY = "delivery_obligation"
# Like the async-delegation replay cap: an owed notice older than this is dropped with a logged reason.
OBLIGATION_MAX_AGE_SECONDS = 48 * 3600
# Gateway or Desktop starts that may re-arm one obligation (a notice refused at every start must not loop forever).
OBLIGATION_MAX_RECOVERIES = 8
_ROUTE_FIELDS = ("watcher_platform", "watcher_chat_id", "watcher_user_id", "watcher_user_name",
                 "watcher_thread_id", "watcher_message_id", "watcher_interval", "parent_session_id",
                 "completion_output_chars", "handoff_note")
_ID_RE = re.compile(r"proc_[\w]+")


def receipt_path(session_id: str) -> Optional[Path]:
    """This profile's receipt path for an exact process id (None for anything that is not one)."""
    if not isinstance(session_id, str) or not _ID_RE.fullmatch(session_id):
        return None
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "logs" / "process-results" / f"{session_id}.json"


def _read(path: Path) -> Optional[dict]:
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("id") == path.stem else None


def _rewrite(path: Path, record: dict) -> None:
    """Atomic rewrite that keeps the receipt's mtime (retention and pruning order are by completion)."""
    from utils import atomic_json_write
    try:
        stat = path.stat()
    except OSError:
        stat = None
    atomic_json_write(path, record, mode=0o600)
    if stat is not None:
        with suppress(OSError):
            os.utime(path, (stat.st_atime, stat.st_mtime))


def _writer() -> dict:
    from tools.process_registry import ProcessRegistry
    pid = os.getpid()
    return {"writer_pid": pid, "writer_start_time": ProcessRegistry._safe_host_start_time(pid)}


def owes_gateway_delivery(session) -> bool:
    """Whether a finished session's completion is owed to a messaging-gateway watcher."""
    from tools.process_registry_readopt import OWNER_GATEWAY, checkpoint_entry_owner
    return bool(session.notify_on_complete and int(session.watcher_interval or 0) > 0
                and not getattr(session, "_completion_via_readopt_sink", False)
                and checkpoint_entry_owner({"session_key": session.session_key,
                                            "watcher_platform": session.watcher_platform}) == OWNER_GATEWAY)


def owes_tui_delivery(session) -> bool:
    """Whether a finished session's completion is owed to a Desktop chat this Desktop backend hosts. A CLI
    delivers in-process and never leaves one behind. A managed worker of the Desktop backend does, on purpose
    (it receives a nonsecret completion destination, and its per-turn queue dies with it): the Desktop keeper
    re-arms that obligation once the worker is gone."""
    from tools.process_registry_readopt import OWNER_TUI, checkpoint_entry_owner
    if not session.notify_on_complete or checkpoint_entry_owner(
            {"session_key": session.session_key, "watcher_platform": session.watcher_platform}) != OWNER_TUI:
        return False
    from hermes_cli.process_identity import is_desktop_owned_backend
    from agent import runtime_session_store
    return bool(is_desktop_owned_backend() or (
        runtime_session_store._worker_process and getattr(runtime_session_store, '_desktop_completion_owner', False)))


def obligation_owner(owed: dict) -> str:
    from tools.process_registry_readopt import OWNER_GATEWAY
    return str(owed.get("owner") or OWNER_GATEWAY)


def obligation_for(session, *, owed_since: Optional[float] = None) -> dict:
    from agent.redact import redact_sensitive_text
    owed = {f: getattr(session, f) for f in _ROUTE_FIELDS}
    # The route fields are the checkpoint's own (persisted there verbatim); the subagent's free-text
    # handoff note is not, so it gets the receipt command's redaction before it reaches disk.
    if owed.get("handoff_note"):
        owed["handoff_note"] = redact_sensitive_text(str(owed["handoff_note"]), force=True)
    return {**owed, "owed_since": owed_since or time.time(), "recoveries": 0, **_writer()}


def settle(session_id: str, how: str) -> bool:
    """Remove the owed-delivery block from this profile's receipt; True when one was removed."""
    path = receipt_path(session_id)
    if path is None or not path.exists():
        return False
    record = _read(path)
    if not record or OBLIGATION_KEY not in record:
        return False
    record.pop(OBLIGATION_KEY, None)
    try:
        _rewrite(path, record)
    except OSError:
        logger.warning("Could not settle the owed completion of %s (%s)", session_id, how, exc_info=True)
        return False
    logger.info("Completion of %s settled (%s)", session_id, how)
    return True


def owed_still(session_id: str) -> Optional[dict]:
    """The obligation the current receipt still carries (a receipt rewrite keeps, never re-creates, it)."""
    path = receipt_path(session_id)
    record = _read(path) if path is not None and path.exists() else None
    owed = record.get(OBLIGATION_KEY) if record else None
    return owed if isinstance(owed, dict) else None


def settle_quietly(session_id: str, how: str) -> bool:
    """``settle`` for the watcher: a failed acknowledgement keeps custody (the next start retries)."""
    try:
        return settle(session_id, how)
    except Exception:  # health: allow BLE001 -- a failed ack keeps custody; the next start retries
        logger.warning("Settling completion %s failed", session_id, exc_info=True)
        return False


class ProcessObligationMixin:
    """Registry side: decide, settle and re-arm owed completions, a messaging gateway's (``owner: gateway``) and a
    Desktop chat's (``owner: tui``). ``_tui_owed`` holds the ids this process wrote or re-armed a tui obligation for,
    so a Desktop notice turn settles only those and never reads the receipt of a job that owes nothing."""

    # Supplied by ProcessRegistry / ProcessCheckpointMixin / ProcessReadoptMixin.
    _lock: Any
    _running: dict
    _finished: dict
    _completion_consumed: set
    pending_watchers: list
    _owned_here: Any
    _writer_alive: Any
    _recovered_watcher: Any
    _deliver_readopted_event: Any

    def _completion_obligation(self, session) -> Optional[dict]:
        """Called under ``self._lock`` by the receipt writer: the owed-delivery block, or None."""
        from tools.process_registry_readopt import OWNER_GATEWAY, OWNER_TUI
        if session.id in self._completion_consumed:
            return None
        if owes_gateway_delivery(session):
            return {**obligation_for(session), "owner": OWNER_GATEWAY}
        if not owes_tui_delivery(session):
            return None
        self._tui_owed_ids().add(session.id)
        return {**obligation_for(session), "owner": OWNER_TUI}

    def _tui_owed_ids(self) -> set:
        return vars(self).setdefault("_tui_owed", set())

    def _consume_completion(self, session_id: str, *, session) -> None:
        """The agent has the output (wait/log/kill): suppress the autonomous notice, here and across a restart.
        Under ``self._lock`` so it cannot interleave with ``_move_to_finished``'s receipt write."""
        with self._lock:
            self._completion_consumed.add(session_id)
            from tools.process_registry_results import record_consumed
            record_consumed(session)
            settle_quietly(session_id, "consumed via wait/log")

    def settle_completion(self, session_id: str, how: str) -> bool:
        """The watcher's acknowledgement, under ``self._lock`` like every other receipt rewrite, so a
        concurrent kill-path rewrite cannot read the obligation before it and write it back after."""
        with self._lock:
            return settle_quietly(session_id, how)

    def settle_tui_completion(self, session_id: str, how: str) -> bool:
        """``settle_completion`` for a Desktop notice: only an id in ``_tui_owed`` (this process wrote or re-armed
        its obligation) can owe one here, so any other id is answered from memory, with no receipt read. The id
        stays while the receipt still owes it, so a failed write is retried by the next settle."""
        with self._lock:
            owed = self._tui_owed_ids()
            if session_id not in owed:
                return False
            settled = settle_quietly(session_id, how)
            if settled or owed_still(session_id) is None:
                owed.discard(session_id)
            return settled

    def _rewrite_killed_receipt(self, session, consume_output: bool) -> None:
        """A kill that lost the finish race rewrites the reader's receipt as killed. An explicit kill
        (``consume_output``) hands the output to its caller, so it settles the obligation; any other kill
        keeps the one the receipt carries (never re-creates it). Read and write under ``self._lock``."""
        from tools import process_registry as registry_module  # its save name (tests patch it there)
        with self._lock:
            if consume_output:
                from tools.process_registry_results import record_consumed
                record_consumed(session)
                settle_quietly(session.id, "consumed via kill")
            registry_module.save_completed_result(session, None if consume_output else owed_still(session.id))

    def _recover_owed_completions_locked(self, owner: Optional[str]) -> int:
        """Under the checkpoint file lock: re-arm each owed receipt of this profile that *owner* holds (a gateway's
        through a recovered watcher, a Desktop chat's through the re-adopt sink)."""
        from tools.process_registry_readopt import OWNER_GATEWAY, OWNER_TUI, owner_accepts
        from tools.process_registry_profiles import scope_profile
        if owner not in (OWNER_GATEWAY, OWNER_TUI):
            return 0  # a managed worker or CLI holds no other process's owed completion
        from hermes_constants import get_hermes_home
        directory = get_hermes_home() / "logs" / "process-results"
        try:
            paths = sorted(directory.glob("proc_*.json"))
        except OSError:
            return 0
        scope, now, armed = scope_profile(), time.time(), 0
        for path in paths:
            record = _read(path) or {}
            owed = record.get(OBLIGATION_KEY)
            if not isinstance(owed, dict) or obligation_owner(owed) != owner:
                continue
            sid = record["id"]
            entry = {"session_key": record.get("session_key"), "watcher_platform": owed.get("watcher_platform")}
            if not owner_accepts(owner, entry) or not self._owned_here(record.get("session_key"), scope):
                continue
            with self._lock:
                tracked = sid in self._running or sid in self._finished
            if tracked or (owner == OWNER_TUI and owed.get("writer_pid") == os.getpid()):
                continue  # this process still holds it (a Desktop backend parks it in memory for the chat)
            if owed.get("writer_pid") != os.getpid() and self._writer_alive(owed):
                continue  # a live gateway still has this watcher
            reason, tries = "", 0
            try:
                age, tries = now - float(owed.get("owed_since") or 0), int(owed.get("recoveries") or 0)
                if not math.isfinite(age) or tries < 0:
                    raise ValueError("invalid recovery budget")
                if tries >= OBLIGATION_MAX_RECOVERIES:
                    reason = f"{tries} {owner} starts did not deliver it"
                elif age > OBLIGATION_MAX_AGE_SECONDS:
                    reason = f"owed for {int(age)}s"
            except (TypeError, ValueError, OverflowError):
                reason = "unreadable recovery budget"
            if reason:
                record.pop(OBLIGATION_KEY, None)
                with suppress(OSError):
                    _rewrite(path, record)
                logger.warning("Dropped the owed completion of %s (session %s): %s", sid,
                               record.get("session_key") or "-", reason)
                continue
            # Failed reconstruction spends a start too; otherwise malformed sessions never hit the cap.
            record[OBLIGATION_KEY] = {**owed, "recoveries": tries + 1, **_writer()}
            try:
                _rewrite(path, record)
            except OSError:
                logger.warning("Could not claim the owed completion of %s", sid, exc_info=True)
                continue
            try:
                session = self._owed_session(record, owed)
            except (KeyError, TypeError, ValueError, OverflowError):
                logger.warning("Skipping the unreadable owed completion %s", sid, exc_info=True)
                continue
            with self._lock:
                self._finished[sid] = session
                if owner == OWNER_TUI:
                    self._tui_owed_ids().add(sid)
            if owner == OWNER_TUI:  # parks for the chat (woken when it is not loaded here)
                from tools.process_registry_awaiting import completion_event, publishable
                self._deliver_readopted_event({**publishable(completion_event(session)), "park_if_unowned": True})
            else:
                self.pending_watchers.append(self._recovered_watcher(session))
            armed += 1
            logger.info("Re-armed the owed completion of %s (session %s): it was owed when the last %s stopped "
                        "(exit %s)", sid, session.session_key or "-", owner, session.exit_code)
        return armed

    @staticmethod
    def _owed_session(record: dict, owed: dict):
        from tools.process_registry import ProcessSession
        from tools.process_registry_results import _RESULT_FIELDS
        fields: dict[str, Any] = {key: record[key] for key in _RESULT_FIELDS}
        fields.update({f: owed[f] for f in _ROUTE_FIELDS if f in owed and owed[f] is not None})
        fields["notify_on_complete"] = True
        session = ProcessSession(**fields, exited=True, output_buffer=str(record.get("output") or ""))
        session.exited_at = time.time()
        session._finish_claimed = True
        session._completion_event.set()
        return session


def _admitted(record: dict) -> Optional[str]:
    """The admission id of a completion admission for this receipt's producer identity, if any."""
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    identity = json.dumps(["completion", record["id"], record.get("started_at")], separators=(",", ":"))
    # This profile's own state.db (the receipt's home), never a process-wide default path.
    db = SessionDB(get_hermes_home() / "state.db", read_only=True)
    try:
        with db._read_ctx() as conn:
            rows = conn.execute("SELECT admission_id, payload_json FROM session_admissions "
                                "WHERE instr(payload_json, ?) > 0", (record["id"],)).fetchall()
    finally:
        db.close()
    for admission_id, payload_json in rows:
        with suppress(ValueError, TypeError):
            payload = json.loads(payload_json)
            descriptor = payload.get("local_automation_v1") or payload.get("native_text_v1", {}).get("automation", {})
            if identity in (descriptor.get("identities") or [descriptor.get("identity")]):
                return admission_id
    return None


def reopen(session_id: str, *, platform: str, chat_id: str, thread_id: str = "", user_id: str = "",
           parent_session_id: str = "") -> dict:
    """Mark one existing receipt (exact id, this profile) as owed to its gateway route. Refuses when the
    receipt is missing, is not ``notify_on_complete``, is not gateway-routed, is already owed, or a completion
    admission for its producer identity exists. Delivery happens at the next gateway start, once."""
    path = receipt_path(session_id)
    if path is None:
        return {"ok": False, "error": "not an exact process id (proc_<hex>)"}
    record = _read(path) if path.exists() else None
    if record is None:
        return {"ok": False, "error": f"no readable receipt at {path}"}
    if not record.get("notify_on_complete"):
        return {"ok": False, "error": "the job did not ask for a completion notice"}
    if OBLIGATION_KEY in record:
        return {"ok": False, "error": "already owed; the next gateway start delivers it"}
    from tools.process_registry_readopt import OWNER_GATEWAY, checkpoint_entry_owner
    if not platform or not chat_id or checkpoint_entry_owner(
            {"session_key": record.get("session_key"), "watcher_platform": platform}) != OWNER_GATEWAY:
        return {"ok": False, "error": "a gateway route (--platform, --chat-id) and a gateway session key are required"}
    try:
        admission = _admitted(record)
    except Exception as exc:  # health: allow BLE001 -- an unverifiable admission state refuses the reopen
        return {"ok": False, "error": f"cannot read this profile's state.db to rule out an admission: {exc}"}
    if admission is not None:
        return {"ok": False, "error": f"already admitted as {admission}; nothing to recover"}
    record[OBLIGATION_KEY] = {
        "watcher_platform": platform, "watcher_chat_id": chat_id, "watcher_user_id": user_id,
        "watcher_user_name": "", "watcher_thread_id": thread_id, "watcher_message_id": "",
        "watcher_interval": 5, "parent_session_id": parent_session_id or record.get("parent_session_id") or "",
        "owed_since": time.time(), "recoveries": 0, "writer_pid": 0, "writer_start_time": None,
        "reopened": True}
    _rewrite(path, record)
    logger.info("Reopened the owed completion of %s for %s:%s", session_id, platform, chat_id)
    return {"ok": True, "receipt": str(path), "session_key": record.get("session_key"),
            "exit_code": record.get("exit_code"), "started_at": record.get("started_at")}


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="python -m tools.process_registry_obligations")
    sub = parser.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("show", help="print one receipt's custody state (read-only)")
    show.add_argument("session_id")
    op = sub.add_parser("reopen", help="owe one receipt's completion to its gateway route again")
    op.add_argument("session_id")
    op.add_argument("--platform", required=True)
    op.add_argument("--chat-id", required=True)
    op.add_argument("--thread-id", default="")
    op.add_argument("--user-id", default="")
    op.add_argument("--parent-session-id", default="")
    args = parser.parse_args(argv)
    if args.cmd == "show":
        path = receipt_path(args.session_id)
        record = _read(path) if path is not None and path.exists() else None
        if record is None:
            print(json.dumps({"ok": False, "error": "no readable receipt"}))
            return 1
        print(json.dumps({"ok": True, "receipt": str(path), "owed": record.get(OBLIGATION_KEY),
                          "admission": _admitted(record), "exit_code": record.get("exit_code"),
                          "session_key": record.get("session_key")}, default=str))
        return 0
    result = reopen(args.session_id, platform=args.platform, chat_id=args.chat_id, thread_id=args.thread_id,
                    user_id=args.user_id, parent_session_id=args.parent_session_id)
    print(json.dumps(result, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
