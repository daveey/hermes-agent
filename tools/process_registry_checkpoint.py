"""Running-process checkpoint persistence and PID-safe recovery."""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from agent.redact import redact_sensitive_text

logger = logging.getLogger("tools.process_registry")


class ProcessCheckpointMixin:
    def _detached_host_fate(self, pid: Optional[int], expected_start: Optional[int]) -> str:
        """Subclass supplies the live PID decision. See ProcessRegistry."""
        raise NotImplementedError

    # ----- Checkpoint (crash recovery) -----

    def _write_checkpoint(self, extra_entries: Optional[list[dict[str, Any]]] = None):
        """Write running process metadata to the checkpoint file atomically."""
        try:
            with self._checkpoint_file_lock():
                self._write_checkpoint_locked(extra_entries)
        except Exception as e:
            logger.debug("Failed to write checkpoint file: %s", e, exc_info=True)

    @staticmethod
    def _checkpoint_file_lock():
        """Cross-process advisory lock around the read-merge-write of processes.json:
        every Hermes process on the host (Desktop backend, messaging gateway, CLI,
        kanban workers) shares one checkpoint file. Best-effort; no-op without fcntl."""
        import contextlib
        from tools.process_registry import _checkpoint_path

        try:
            import fcntl
        except ImportError:  # Windows
            return contextlib.nullcontext()

        @contextlib.contextmanager
        def _lock():
            path = _checkpoint_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = str(path) + ".lock"
            with open(lock_path, "ab") as fh:  # flock-only; never read or written
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                except OSError:
                    pass
                try:
                    yield
                finally:
                    with contextlib.suppress(OSError):
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return _lock()

    def _writer_identity(self) -> dict[str, Any]:
        """Which process supervises the entries this registry writes. [daveey]"""
        pid = os.getpid()
        return {"writer_pid": pid, "writer_start_time": self._safe_host_start_time(pid)}

    def _writer_alive(self, entry: dict[str, Any]) -> bool:
        """True when the process that wrote *entry* still runs (so it still supervises
        it). Entries from before writer stamping count as orphaned."""
        pid = entry.get("writer_pid")
        if not isinstance(pid, int) or pid <= 0:
            return False
        if pid == os.getpid():
            return True
        return self._host_pid_is_ours(pid, entry.get("writer_start_time"))

    def _foreign_live_entries(self, own_ids: set) -> list[dict[str, Any]]:
        """Entries in the current file that another process wrote and whose child is
        still alive. Every process writes only its own ``_running`` sessions, so a
        plain overwrite erased the live children of every other Hermes process on the
        host: a Desktop backend's ``notify_on_complete`` job vanished from
        processes.json as soon as a kanban worker spawned something, and nothing
        could re-adopt it after the Desktop restarted. [daveey]"""
        from tools.process_registry import _checkpoint_path

        try:
            existing = json.loads(_checkpoint_path().read_text(encoding="utf-8-sig"))
        except Exception:
            return []
        if not isinstance(existing, list):
            return []
        kept = []
        for item in existing:
            if not isinstance(item, dict) or item.get("session_id") in own_ids:
                continue
            if item.get("pid_scope", "host") != "host" or not item.get("pid"):
                continue
            if self._detached_host_fate(item.get("pid"), item.get("host_start_time")) != "running":
                continue
            kept.append(item)
        return kept

    def _write_checkpoint_locked(self, extra_entries: Optional[list[dict[str, Any]]] = None):
        from tools.process_registry import _checkpoint_path, _CHECKPOINT_FIELDS

        writer = self._writer_identity()
        with self._lock:
            own_ids = set(self._running) | set(self._finished)
            entries = []
            for s in self._running.values():
                if s.exited:
                    continue
                # Backfill the start time so recovery can detect PID recycling
                # even for sessions spawned before this field existed.
                if s.host_start_time is None and s.pid_scope == "host" and s.pid:
                    s.host_start_time = self._safe_host_start_time(s.pid)
                entry = {"session_id": s.id, **{f: getattr(s, f) for f in _CHECKPOINT_FIELDS}}
                # Redact inline credentials before persisting (~/.hermes/processes.json).
                # Recovery uses command only for display (adoption re-validates the
                # PID, never re-runs it), so masking is lossless.
                # See #77484.
                entry["command"] = redact_sensitive_text(s.command, code_file=True)
                entry["owner_task_id"] = s.owner_task_id or s.task_id
                entry.update(writer)
                entries.append(entry)
            if extra_entries:
                tracked_ids = {item.get("session_id") for item in entries}
                entries.extend(item for item in extra_entries if item.get("session_id") not in tracked_ids)
        tracked_ids = {item.get("session_id") for item in entries}
        entries.extend(item for item in self._foreign_live_entries(own_ids | tracked_ids)
                       if item.get("session_id") not in tracked_ids)
        from utils import atomic_json_write
        atomic_json_write(_checkpoint_path(), entries)

    def reassert_checkpoint(self) -> bool:
        """Rewrite processes.json when it lacks one of this registry's live sessions; True if
        it rewrote. Another Hermes process still on pre-merge code (a kanban worker or CLI
        started before an update) overwrites the file with only its own entries, which drops
        this backend's watchers: the sidebar's restart view and the next backend's re-adoption
        both lose them, so the chat paints green and never wakes. [daveey]"""
        from tools.process_registry import _checkpoint_path

        with self._lock:
            own = {s.id for s in self._running.values() if not s.exited}
        if not own:
            return False
        try:
            with self._checkpoint_file_lock():
                try:
                    existing = json.loads(_checkpoint_path().read_text(encoding="utf-8-sig"))
                except Exception:
                    existing = []
                present = {e.get("session_id") for e in existing if isinstance(e, dict)} \
                    if isinstance(existing, list) else set()
                if own <= present:
                    return False
                self._write_checkpoint_locked()
                logger.info("Re-wrote process checkpoint: another writer dropped %d live entr%s",
                            len(own - present), "y" if len(own - present) == 1 else "ies")
                return True
        except Exception:
            logger.debug("Checkpoint reassert failed", exc_info=True)
            return False

    def recover_from_checkpoint(self, owner: Optional[str] = None) -> int:
        """On startup, probe PIDs from the checkpoint file; returns how many were
        recovered as detached sessions.

        ``owner`` limits adoption to one kind of entry (see
        ``tools.process_registry_readopt``): ``"gateway"`` (messaging session keys and
        legacy keyless entries) or ``"tui"`` (Desktop/TUI session ids). ``None`` adopts
        every orphan. The whole read-decide-write runs under the checkpoint file lock,
        so two backends starting together adopt an orphan once. [daveey]"""
        try:
            with self._checkpoint_file_lock():
                return self._recover_from_checkpoint_locked(owner)
        except Exception as e:
            logger.warning("Process checkpoint recovery failed: %s", e, exc_info=True)
            return 0

    def _recover_from_checkpoint_locked(self, owner: Optional[str]) -> int:
        from tools.process_registry import (
            ProcessSession, _CHECKPOINT_FIELDS, _checkpoint_path,
            _CHECKPOINT_DEFAULTS, _WATCHER_ROUTE_KEYS, _stop_systemd_unit,
        )
        from tools.process_registry_readopt import OWNER_TUI, owner_accepts

        checkpoint_path = _checkpoint_path()
        if not checkpoint_path.exists():
            return 0
        try:
            entries = json.loads(checkpoint_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return 0
        recovered = 0
        unresolved_scope_entries: list[dict[str, Any]] = []
        for entry in entries:
            pid, pid_scope = entry.get("pid"), entry.get("pid_scope", "host")
            if not pid:
                continue
            # The registry is process-global, so every profile's checkpoint carries every live
            # process; a multiplexer recovering several homes must adopt each session once.
            with self._lock:
                already_tracked = entry.get("session_id") in self._running
            if already_tracked:
                continue
            # Another live Hermes process (Desktop backend, kanban worker, CLI) still
            # supervises it: adopting would double-own the child and let OUR shutdown
            # kill it. Only orphans (writer gone) are recovered. [daveey]
            if entry.get("writer_pid") != os.getpid() and self._writer_alive(entry):
                continue
            if not owner_accepts(owner, entry):
                continue
            if pid_scope != "host":  # in-sandbox PIDs mean nothing once the env handle is gone
                logger.info(
                    "Skipping recovery for non-host process: %s (pid=%s, scope=%s)",
                    entry.get("command", "unknown")[:60], pid, pid_scope)
                continue
            # Alive and still ours: re-attach. A start-time probe that cannot
            # be read is not proof the PID was reused — dropping it would leave
            # a live child unsupervised, and marking it exited would invent a
            # completion. A positive mismatch means the number was recycled:
            # do not adopt it and do not signal it.
            fate = self._detached_host_fate(pid, entry.get("host_start_time"))
            if fate == "reused":
                logger.info(
                    "Not recovering session %s: pid %d is alive but its "
                    "start time no longer matches — PID was recycled onto "
                    "an unrelated process; refusing to adopt or signal it.",
                    entry.get("session_id", "?"), pid)
                continue
            if fate != "running":
                if owner == OWNER_TUI and entry.get("notify_on_complete"):
                    # Exited while no backend watched it: the chat still gets its wake.
                    self._queue_unknown_exit(self._session_from_entry(entry))
                systemd_unit = entry.get("systemd_unit", "")
                if systemd_unit and not _stop_systemd_unit(systemd_unit):
                    logger.warning(
                        "Could not reap persisted scope %s for dead wrapper pid %s; "
                        "retaining checkpoint entry for the next startup",
                        systemd_unit, pid)
                    unresolved_scope_entries.append(entry)
                continue
            # detached: can't read output, but can report status + kill
            session = self._session_from_entry(entry)
            # A TUI backend reports the eventual exit (status unknown) to the owning chat.
            session._report_lost_exit = owner == OWNER_TUI
            with self._lock:
                self._running[session.id] = session
            recovered += 1
            logger.info("Recovered detached process: %s (pid=%d)", session.command[:60], pid)
            # Re-enqueue watcher so gateway can resume notifications
            if session.watcher_interval > 0:
                self.pending_watchers.append({
                    "session_id": session.id,
                    "check_interval": session.watcher_interval,
                    "session_key": session.session_key,
                    **{key: getattr(session, f"watcher_{key}") for key in _WATCHER_ROUTE_KEYS},
                    "notify_on_complete": session.notify_on_complete,
                    "parent_session_id": session.parent_session_id,
                })
        self._write_checkpoint_locked(extra_entries=unresolved_scope_entries)
        return recovered

    @staticmethod
    def _session_from_entry(entry: dict[str, Any]):
        from tools.process_registry import ProcessSession, _CHECKPOINT_DEFAULTS, _CHECKPOINT_FIELDS

        fields = {f: entry.get(f, _CHECKPOINT_DEFAULTS[f]) for f in _CHECKPOINT_FIELDS}
        fields.update(
            command=entry.get("command", "unknown"),
            owner_task_id=entry.get("owner_task_id", "") or entry.get("task_id", ""),
            started_at=entry.get("started_at", time.time()))
        return ProcessSession(id=entry["session_id"], detached=True, **fields)
