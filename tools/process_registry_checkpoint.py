"""Running-process checkpoint persistence and PID-safe recovery."""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from agent.redact import redact_sensitive_text

logger = logging.getLogger("tools.process_registry")

# This thread's nesting depth inside the processes.json flock (see _checkpoint_file_lock).
_CHECKPOINT_LOCK_HELD = threading.local()
# How long another writer keeps an orphaned sandbox entry (its writer died) for the owner's recovery.
_SANDBOX_ORPHAN_KEEP_SECONDS = 7 * 24 * 3600


class ProcessCheckpointMixin:
    # Set by the multiplexed gateway: one registry serves every profile, so an ``agent:main:`` key names
    # the default profile rather than "whoever wrote it" (see tools.process_registry_profiles). [daveey]
    profile_scoped_checkpoints: bool = False
    _lock: Any
    _running: dict

    def _owned_here(self, session_key, scope=None) -> bool:
        from tools.process_registry_profiles import owned_here
        return owned_here(session_key, multiplexed=self.profile_scoped_checkpoints, scope=scope)

    def _tracked_or_held_locked(self, session_id: Any) -> bool:
        """Caller holds ``self._lock``: this registry already owns *session_id*, as a background job or as
        a held survivable foreground command (process_registry_survivable). [daveey]"""
        return session_id in self._running or session_id in vars(self).get("_foreground_holds", {})

    def _detached_host_fate(self, pid: Optional[int], expected_start: Optional[int]) -> str:
        """Subclass supplies the live PID decision. See ProcessRegistry."""
        raise NotImplementedError

    def _recover_owed_completions_locked(self, owner: Optional[str]) -> int:
        """ProcessObligationMixin re-arms owed completions, a gateway's and a Desktop chat's (``owner: tui``); a bare
        checkpoint owes none."""
        return 0

    # ----- Checkpoint (crash recovery) -----

    def _write_checkpoint(self, extra_entries: Optional[list[dict[str, Any]]] = None, *,
                          settled_id: Optional[str] = None):
        """Write running process metadata to the checkpoint file atomically. ``settled_id``: a job whose
        result was just recorded, dropped from every other profile's checkpoint too (after this one's
        lock is released), so no profile re-attaches it at a restart. [daveey]"""
        from tools.environments.sandbox_scope import hosted_scope

        # A hosted caller only changes its own agent's records, which are never persisted here, so
        # it has no host change to write: it neither takes the lock nor rewrites this host's file.
        if hosted_scope() is not None:
            return
        try:
            with self._checkpoint_file_lock():
                self._write_checkpoint_locked(extra_entries)
        except Exception as e:
            logger.debug("Failed to write checkpoint file: %s", e, exc_info=True)
        if settled_id:
            from tools.process_registry_profiles import drop_from_every_checkpoint
            try:
                drop_from_every_checkpoint([settled_id])
            except Exception:
                logger.debug("Dropping %s from other checkpoints failed", settled_id, exc_info=True)

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
            # [daveey] Reentrant per thread: recovery holds the lock across its read-decide-write
            # and adopts sessions through _track_started, which writes the checkpoint again. A
            # second flock on a fresh descriptor would wait on this thread's own lock forever.
            if getattr(_CHECKPOINT_LOCK_HELD, "depth", 0):
                _CHECKPOINT_LOCK_HELD.depth += 1
                try:
                    yield
                finally:
                    _CHECKPOINT_LOCK_HELD.depth -= 1
                return
            path = _checkpoint_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = str(path) + ".lock"
            with open(lock_path, "ab") as fh:  # flock-only; never read or written
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                except OSError:
                    pass
                _CHECKPOINT_LOCK_HELD.depth = 1
                try:
                    yield
                finally:
                    _CHECKPOINT_LOCK_HELD.depth = 0
                    with contextlib.suppress(OSError):
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return _lock()

    def _writer_identity(self) -> dict[str, Any]:
        """Which process supervises the entries this registry writes. [daveey]

        ``owner_pid`` (upstream): the gateway whose jobs these are. Every interpreter on the home
        writes this file; ``hermes update --force`` sweeps only what the gateway it killed owns
        (update_cmd_force_stop): a managed worker's jobs are its owner's."""
        from agent.runtime_session_store import is_worker_process
        pid = os.getpid()
        owner_pid = os.getppid() if is_worker_process() else pid  # windows-footgun: ok — getppid exists on Windows
        return {"writer_pid": pid, "writer_start_time": self._safe_host_start_time(pid), "owner_pid": owner_pid}

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
        now = time.time()
        for item in existing:
            if not isinstance(item, dict) or item.get("session_id") in own_ids:
                continue
            if item.get("pid_scope", "host") != "host" and item.get("pid"):
                # A sandbox PID cannot be probed from here. Keep the entry while its writer still
                # supervises it, and as an orphan awaiting its owner's restart recovery for up to
                # _SANDBOX_ORPHAN_KEEP_SECONDS: dropping it erased the only trace a restarted gateway
                # could re-attach (or report as lost). [daveey]
                started = item.get("started_at")
                fresh = isinstance(started, (int, float)) and now - started < _SANDBOX_ORPHAN_KEEP_SECONDS
                if self._writer_alive(item) or fresh:
                    kept.append(item)
                continue
            if item.get("pid_scope", "host") != "host" or not item.get("pid"):
                continue
            if self._detached_host_fate(item.get("pid"), item.get("host_start_time")) != "running":
                continue
            kept.append(item)
        return kept

    def _write_checkpoint_locked(self, extra_entries: Optional[list[dict[str, Any]]] = None,
                                 drop_ids: frozenset = frozenset()):
        """``drop_ids``: entries this writer just settled (e.g. a lost-watcher notice sent at recovery)
        that must leave the file even though no table of ours tracks them."""
        from tools.process_registry import _checkpoint_path, _CHECKPOINT_FIELDS

        from tools.process_registry_profiles import scope_profile

        writer = self._writer_identity()
        scope = scope_profile()
        with self._lock:
            # Survivable foreground commands the tool is waiting on are checkpointed too: a crash
            # mid-wait leaves them to the next owner (process_registry_survivable). [daveey]
            held = list(vars(self).get("_foreground_holds", {}).values())
            own_ids = set(self._running) | set(self._finished) | set(drop_ids) | {s.id for s in held}
            entries = []
            for s in (*self._running.values(), *held):
                # A hosted agent's process is not this host's to persist: its record (command,
                # cwd, owner) stays in memory, whoever triggers the write (scoped or not).
                if s.exited or s.sandbox_key:
                    continue
                # One registry serves every profile of a multiplexed gateway: a job goes only in its own
                # profile's checkpoint, or every other profile re-attaches it after it finished. [daveey]
                if not self._owned_here(s.session_key, scope):
                    continue
                # Backfill the start time so recovery can detect PID recycling
                # even for sessions spawned before this field existed.
                if s.host_start_time is None and s.pid_scope == "host" and s.pid:
                    s.host_start_time = self._safe_host_start_time(s.pid)
                # A recreated container has a new id: persist the handle that reaches the live one. [daveey]
                if s.env_ref is not None and s.pid_scope != "host":
                    from tools.environments.reattach import env_reattach_handle
                    s.sandbox_handle = env_reattach_handle(s.env_ref) or s.sandbox_handle
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
        path = _checkpoint_path()
        # Every interpreter of the profile (owner, each managed worker) rewrites this file from
        # its own table; a worker's output-log handoff is its only path to the next one, so a
        # writer keeps the handoffs it does not track. The caller holds _checkpoint_file_lock
        # (the same processes.json.lock upstream's _FileLock takes), so no second lock here.
        tracked_ids = {item.get("session_id") for item in entries}
        entries.extend(item for item in self._foreign_handoffs(path, own_ids | tracked_ids)
                       if item.get("session_id") not in tracked_ids)
        atomic_json_write(path, entries)

    def _foreign_handoffs(self, path, known) -> list[dict[str, Any]]:
        """On-disk worker handoffs this registry never tracked whose log still decides an outcome:
        the process is alive (same start time) or its recorded exit is waiting to be read."""
        from pathlib import Path
        from tools.process_registry_output_log import _exit_path, adoptable_log
        try:
            on_disk = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return []
        return [entry for entry in on_disk if isinstance(entry, dict) and entry.get("session_id") not in known
                and (log := adoptable_log(entry.get("output_log"))) and Path(log).exists()
                and (_exit_path(log).exists() or self._detached_host_fate(
                    entry.get("pid"), entry.get("host_start_time")) == "running")]


    def reassert_checkpoint(self) -> bool:
        """Rewrite processes.json when it lacks one of this registry's live sessions; True if
        it rewrote. Another Hermes process still on pre-merge code (a kanban worker or CLI
        started before an update) overwrites the file with only its own entries, which drops
        this backend's watchers: the sidebar's restart view and the next backend's re-adoption
        both lose them, so the chat paints green and never wakes. [daveey]"""
        from tools.process_registry import _checkpoint_path

        from tools.process_registry_profiles import scope_profile

        scope = scope_profile()
        with self._lock:
            own = {s.id for s in self._running.values() if not s.exited and self._owned_here(s.session_key, scope)}
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

    def recover_from_checkpoint(self, owner: Optional[str] = None, *,
                                session_keys: Optional[Iterable[str]] = None) -> int:
        """On startup, probe PIDs from the checkpoint file; returns how many were
        recovered as detached sessions.

        ``owner`` limits adoption to one kind of entry (see
        ``tools.process_registry_readopt``): ``"gateway"`` (messaging session keys and
        legacy keyless entries) or ``"tui"`` (Desktop/TUI session ids). ``None`` adopts
        every orphan. ``session_keys`` limits it further to those sessions' jobs and leaves
        every other entry untouched: a managed worker serves one turn of one session, and
        adopting another chat's orphan (a Desktop job whose backend had just rolled over)
        took its exit into a process with no consumer for it (proc_70810e2dae05). The whole
        read-decide-write runs under the checkpoint file lock, so two backends starting
        together adopt an orphan once. [daveey]"""
        keys = None if session_keys is None else frozenset(k for k in session_keys if k)
        try:
            with self._checkpoint_file_lock():
                recovered, foreign, settled, source = self._recover_from_checkpoint_locked(owner, keys)
                # Completions a stopped gateway still owed (exit settled before its watcher ticked). [daveey]
                try:
                    recovered += self._recover_owed_completions_locked(owner)
                except Exception:
                    logger.warning("Owed process completion recovery failed", exc_info=True)
        except Exception as e:
            logger.warning("Process checkpoint recovery failed: %s", e, exc_info=True)
            return 0
        # Outside the lock: these take other profiles' checkpoint locks, one at a time. [daveey]
        from tools.process_registry_profiles import drop_from_every_checkpoint, hand_to_owners
        try:
            hand_to_owners(foreign, source, multiplexed=self.profile_scoped_checkpoints)
            drop_from_every_checkpoint(settled)
        except Exception:
            logger.warning("Settling other profiles' process checkpoints failed", exc_info=True)
        return recovered

    def _recover_from_checkpoint_locked(self, owner: Optional[str], session_keys: Optional[frozenset] = None):
        """Returns (recovered count, entries another profile owns, settled ids, this checkpoint's path)."""
        from tools.process_registry import (
            ProcessSession, _CHECKPOINT_FIELDS, _checkpoint_path,
            _CHECKPOINT_DEFAULTS, _stop_systemd_unit,
        )
        from tools.process_registry_readopt import OWNER_TUI, UNKNOWN_EXIT_REASON, owner_accepts
        from tools.process_registry_output_log import adoptable_log
        from tools.process_registry_profiles import result_recorded, scope_profile

        # This host adopts what a crashed predecessor left running, so the jobs it starts must
        # survive its own crash for the next one: they stay out of the owner-death supervisor.
        self._jobs_outlive_host = True
        checkpoint_path = _checkpoint_path()
        if not checkpoint_path.exists():
            return 0, [], set(), checkpoint_path
        try:
            entries = json.loads(checkpoint_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return 0, [], set(), checkpoint_path
        recovered = 0
        unresolved_scope_entries: list[dict[str, Any]] = []
        settled_ids: set = set()  # sandbox entries answered with a lost-watcher notice: leave the file
        foreign: list[dict[str, Any]] = []  # another profile's jobs: handed to its checkpoint after the lock
        scope = scope_profile()
        for entry in entries:
            pid, pid_scope = entry.get("pid"), entry.get("pid_scope", "host")
            if not pid or (session_keys is not None and entry.get("session_key") not in session_keys):
                continue
            # The registry is process-global, so every profile's checkpoint carries every live
            # process; a multiplexer recovering several homes must adopt each session once.
            # A survivable foreground command the terminal tool still waits on is checkpointed too (so a
            # crash mid-wait leaves it to the next owner), but while it is held here it is ours: adopting it
            # made a second, background owner whose reader reported lost/-1 for a call that exited 0. [daveey]
            with self._lock:
                already_tracked = self._tracked_or_held_locked(entry.get("session_id"))
            if already_tracked:
                continue
            # Its exit was already observed (a result receipt exists in some profile): re-attaching would start
            # a watcher on a finished job's (maybe reused) PID. A notice it still owes rides that receipt
            # (process_registry_obligations). [daveey]
            if result_recorded(entry.get("session_id")):
                logger.info("Not re-attaching finished process %s of session %s: its exit is already recorded",
                            entry.get("session_id"), entry.get("session_key") or "-")
                settled_ids.add(entry.get("session_id"))
                continue
            # Only the profile that owns the job recovers it; this copy goes to the owner's file. [daveey]
            if not self._owned_here(entry.get("session_key"), scope):
                foreign.append(entry)
                continue
            # Another live Hermes process (Desktop backend, kanban worker, CLI) still
            # supervises it: adopting would double-own the child and let OUR shutdown
            # kill it. Only orphans (writer gone) are recovered. [daveey]
            if entry.get("writer_pid") != os.getpid() and self._writer_alive(entry):
                continue
            if not owner_accepts(owner, entry):
                continue
            if pid_scope != "host":
                # In-sandbox PIDs are only reachable through the sandbox: re-attach to it when it is still
                # up, else tell the owning session its watcher is gone. Never drop it silently. [daveey]
                if self._recover_sandbox_entry(entry, owner):
                    recovered += 1
                else:
                    settled_ids.add(entry.get("session_id"))
                continue
            # Alive and still ours: re-attach. A start-time probe that cannot
            # be read is not proof the PID was reused — dropping it would leave
            # a live child unsupervised, and marking it exited would invent a
            # completion. A positive mismatch means the number was recycled:
            # do not adopt it and do not signal it.
            fate = self._detached_host_fate(pid, entry.get("host_start_time"))
            # A worker spawn's output log and recorded exit outlive both the spawner and the PID,
            # so its reader decides the outcome (a recycled PID is never signalled: fate != running).
            log = adoptable_log(entry.get("output_log"))
            if log:
                fate = "running"
            if fate == "reused":
                logger.info(
                    "Not recovering session %s: pid %d is alive but its "
                    "start time no longer matches — PID was recycled onto "
                    "an unrelated process; refusing to adopt or signal it.",
                    entry.get("session_id", "?"), pid)
                continue
            if fate != "running":
                if entry.get("notify_on_complete"):
                    # Exited while no host watched it: the owning session still gets its wake, whatever
                    # kind of host owns it (Desktop chat, messaging gateway, CLI). [daveey]
                    lost = self._session_from_entry(entry)
                    how = self._deliver_unwatched_exit(
                        lost, owner, UNKNOWN_EXIT_REASON, "readopted_after_restart")
                    logger.info("Process %s (pid=%s) of session %s exited while unwatched; %s",
                                lost.id, pid, lost.session_key or "-",
                                f"waking it via {how}" if how else "no wake route recorded")
                systemd_unit = entry.get("systemd_unit", "")
                if systemd_unit and not _stop_systemd_unit(systemd_unit):
                    logger.warning(
                        "Could not reap persisted scope %s for dead wrapper pid %s; "
                        "retaining checkpoint entry for the next startup",
                        systemd_unit, pid)
                    unresolved_scope_entries.append(entry)
                continue
            # detached: no Popen handle, but can report status + kill; a worker spawn's output log
            # (and recorded exit) outlives its spawner, so tail it from the start.
            session = self._session_from_entry(entry)
            session.output_log = log
            # A TUI backend reports the eventual exit (status unknown) to the owning chat; a gateway keeps the
            # exited session for its recovered watcher to deliver. [daveey]
            session._report_lost_exit = owner == OWNER_TUI
            # File-backed host jobs collect a real exit, just like re-attached sandbox jobs.
            # Route it to the Desktop waker even when the owning chat is not loaded.
            session._completion_via_readopt_sink = owner == OWNER_TUI
            session._lost_exit_via_watcher = owner != OWNER_TUI and session.watcher_interval > 0
            if session.output_log:
                self._track_started(session, self._log_reader_loop, f"proc-log-reader-{session.id}")
            else:
                with self._lock:
                    self._running[session.id] = session
            recovered += 1
            logger.info("Recovered detached process: %s (pid=%d)", session.command[:60], pid)
            # Re-enqueue watcher so gateway can resume notifications
            if session.watcher_interval > 0:
                self.pending_watchers.append(self._recovered_watcher(session))
        # Another profile's jobs stay in this file until hand_to_owners has put them in the owner's. [daveey]
        self._write_checkpoint_locked(extra_entries=unresolved_scope_entries + foreign,
                                      drop_ids=frozenset(settled_ids))
        return recovered, foreign, settled_ids, checkpoint_path

    def _recover_sandbox_entry(self, entry: dict[str, Any], owner: Optional[str]) -> bool:
        """Re-attach a sandbox job's watcher (True), or deliver a "watcher lost" notice to its owning
        session (False). Logs one line either way, naming the session. [daveey]"""
        import shlex
        from tools.process_registry_readopt import OWNER_TUI, WATCHER_LOST_REASON

        session = self._session_from_entry(entry)
        session.sandbox_handle = dict(entry.get("sandbox_handle") or {})
        key = session.session_key or "-"
        temp_dir = str(entry.get("sandbox_temp_dir") or "").rstrip("/")
        log_path, pid_path, exit_path = (f"{temp_dir}/hermes_bg_{session.id}.{ext}" for ext in ("log", "pid", "exit"))
        env, why = None, ""
        if not session.sandbox_handle or not temp_dir:
            why = "no re-attach handle recorded (spawned before handles were persisted, or backend has none)"
        else:
            from tools.environments import reattach as _reattach
            try:
                env = _reattach.reattach_environment(session.sandbox_handle)
            except Exception as exc:
                logger.debug("Sandbox re-attach of %s failed", session.id, exc_info=True)
                env, why = None, f"re-attach failed: {exc}"
            if env is None:
                why = why or f"sandbox is gone ({session.sandbox_handle.get('backend', '?')})"
            elif (isinstance(getattr(env, "started_at", None), (int, float))
                  and isinstance(session.started_at, (int, float))
                  and env.started_at > session.started_at + 5):
                # The container restarted after the job started: the job died with it, and its old PID
                # may now belong to something else. [daveey]
                why = "the sandbox restarted after the job started"
            else:
                try:
                    probe = env.execute(f"test -e {shlex.quote(pid_path)} && echo hermes-bg-present", timeout=10)
                    present = "hermes-bg-present" in str(probe.get("output", ""))
                except Exception as exc:  # health: allow BLE001 -- any probe failure means "cannot re-attach"; logged with traceback
                    logger.debug("Sandbox probe of %s failed", session.id, exc_info=True)
                    present, why = False, f"probe failed: {exc}"
                if not present:
                    why = why or "the job's wrapper files are gone from the sandbox"
        if why:
            how = self._deliver_unwatched_exit(session, owner, WATCHER_LOST_REASON, "restart_watcher_lost")
            logger.warning(
                "Sandbox process %s (pid=%s) of session %s: watcher lost at restart (%s); %s",
                session.id, session.pid, key, why,
                f"notified the session via {how}" if how else "it asked for no notification")
            return False
        session.env_ref = env
        session.detached = False  # the poller collects the real exit code from the wrapper's exit file
        session._completion_via_readopt_sink = owner == OWNER_TUI
        self._track_started(session, self._env_poller_loop, f"proc-poller-{session.id}",
                            (env, log_path, pid_path, exit_path))
        if session.watcher_interval > 0:
            self.pending_watchers.append(self._recovered_watcher(session))
        logger.info("Re-attached sandbox process %s (pid=%s) of session %s: %s",
                    session.id, session.pid, key, session.command[:60])
        return True

    @staticmethod
    def _session_from_entry(entry: dict[str, Any]):
        from tools.process_registry import ProcessSession, _CHECKPOINT_DEFAULTS, _CHECKPOINT_FIELDS

        fields = {f: entry.get(f, _CHECKPOINT_DEFAULTS[f]) for f in _CHECKPOINT_FIELDS}
        # Never share a mutable default (or the parsed entry's own container) between sessions.
        fields = {f: (type(v)(v) if isinstance(v, (list, dict)) else v) for f, v in fields.items()}
        fields.update(
            command=entry.get("command", "unknown"),
            owner_task_id=entry.get("owner_task_id", "") or entry.get("task_id", ""),
            started_at=entry.get("started_at", time.time()), sandbox_key=entry.get("sandbox_key", ""))
        return ProcessSession(id=entry["session_id"], detached=True, **fields)
