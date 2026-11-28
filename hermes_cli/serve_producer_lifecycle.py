"""Panel producer lifecycle: one supported seam, both directions, no serve PID restart (dormant).

OFF serves watch the installation-local revisioned mode (``serve_producer_mode``). A transition
drains the old custodian completely before the successor starts:

* to EXTERNAL: strict COMPLETE admission under the held LaunchGate, positive legacy retirement,
  start the one shared exec child, completed initial publication, then atomic attach (route + events).
* to OFF: revoke the old output/ACK generation, close admission (detach), drain every lane by
  waiting for the child family; only a fully exited family lets the legacy owner start.

A surviving child/descendant or any failed step leaves the panel UNAVAILABLE with fencing retained:
never kill, timeout takeover, LOCK_UN, inode replacement or automatic fallback. Callbacks retained
from a retired generation cannot revive it (every step checks the transition token).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from hermes_cli.serve_producer_mode import effective_external, mode_path, read_mode

OFF, STARTING, EXTERNAL, DRAINING, UNAVAILABLE = 'off', 'starting', 'external', 'draining', 'unavailable'


class PanelLifecycle:
    def __init__(self, installation: Path, *, admit, retire_legacy, start_legacy, make_relay,
                 publish_initial, attach, detach, environ=None, close_timeout: float = 10,
                 legacy_running: bool = True):
        self.installation = Path(installation)
        self._admit, self._retire_legacy, self._start_legacy = admit, retire_legacy, start_legacy
        self._make_relay, self._publish_initial = make_relay, publish_initial
        self._attach, self._detach = attach, detach
        self._environ = environ
        self._close_timeout = close_timeout
        self._lock = threading.RLock()
        self.state = OFF
        self.revision_applied = -1
        self.relay = None
        self.reason = None
        self._token = 0  # advances on every transition; stale callbacks compare against it
        self._stop = threading.Event()
        self._thread = None
        self._legacy_running = legacy_running
        self._stamp_seen = object()  # no observation yet: the first poll always reads

    def _stamp(self):
        """Cheap identity of the mode file (None when absent): an idle OFF watcher only stats it."""
        try:
            st = os.stat(mode_path(self.installation))
        except FileNotFoundError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    # -- observation -------------------------------------------------------------------------
    def target(self):
        """(external?, revision). Master OFF is a process-level override of the stored mode."""
        revision = read_mode(self.installation)['revision']
        return effective_external(self.installation, self._environ), revision

    def serving(self):
        return self.state == EXTERNAL

    # -- transitions -------------------------------------------------------------------------
    def poll_once(self):
        try:
            stamp = self._stamp()
            if stamp == self._stamp_seen:
                return self.state  # unchanged since the last completed read
            external, revision = self.target()
        except (OSError, ValueError):
            self._fail('mode file unreadable')
            return self.state
        with self._lock:
            self._stamp_seen = stamp
            if revision == self.revision_applied:
                return self.state
            if self.state == UNAVAILABLE:
                return self.state  # fenced: only a supervised restart clears it
            self.revision_applied = revision
            if external and self.state == OFF:
                self._to_external()
            elif not external and self.state == EXTERNAL:
                self._to_off()
            return self.state

    def _fail(self, reason):
        with self._lock:
            self._token += 1
            self.state, self.reason = UNAVAILABLE, reason

    def _to_external(self):
        self._token += 1
        token = self._token
        self.state = STARTING
        relay = None
        try:
            gate, cut = self._admit()
            try:
                if not cut.complete:
                    raise RuntimeError('admission incomplete: ' + ','.join(cut.errors))
                if self._legacy_running:
                    if self._retire_legacy() is not True:
                        raise RuntimeError('legacy owner not positively retired')
                    self._legacy_running = False
                relay = self._make_relay()
                relay.start_for_production(cut=cut, gate=gate)
            finally:
                gate.close()
            if not self._publish_initial(relay):
                raise RuntimeError('initial publication incomplete')
            if token != self._token:
                raise RuntimeError('transition superseded')
            self._attach(relay)  # route + event retainer become READY together
            self.relay, self.state, self.reason = relay, EXTERNAL, None
        except Exception as exc:  # health: allow BLE001 -- every refusal fences UNAVAILABLE and the cause is retained in reason
            self.reason = f'{type(exc).__name__}: {exc}'
            drained = relay is None or relay.retire(timeout=self._close_timeout)
            self.relay = relay if not drained else None
            self.state = UNAVAILABLE

    def _to_off(self):
        self._token += 1
        relay = self.relay
        self.state = DRAINING
        if relay is None:
            self.state, self.reason = UNAVAILABLE, 'external custody missing'
            return
        relay.withdraw()               # old ACKs/outputs/events unobservable; no retained callback can re-attach
        self._detach(relay)            # admission closed: requests refuse, sink dropped
        if not relay.retire(timeout=self._close_timeout):
            self.state, self.reason = UNAVAILABLE, 'producer family still draining'
            return                      # custody retained; no kill, no takeover, no legacy start
        self.relay = None
        try:
            if self._start_legacy() is False:
                raise RuntimeError('legacy startup refused')
        except Exception as exc:  # health: allow BLE001 -- failed successor custody remains unavailable with its cause
            self._legacy_running = False
            self.state, self.reason = UNAVAILABLE, f'legacy startup failed: {exc}'
            return
        self._legacy_running = True
        self.state, self.reason = OFF, None

    # -- watching ----------------------------------------------------------------------------
    def start_watching(self, *, interval: float = 0.5):
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._watch, args=(interval,), daemon=True,
                                            name='panel-producer-mode-watch')
            self._thread.start()

    def _watch(self, interval):
        while not self._stop.wait(interval):
            self.poll_once()

    def stop_watching(self):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(5)


def default_admit(runtime: Path, installation: Path, *, root: Path):
    """Gate-held strict cut for the production start (never an HTTP path)."""
    from hermes_cli.serve_producer_admission import (
        AdmissionUnavailable, LaunchGate, LinuxProcessCensus, host_collectors,
        incarnation_exited, launcher_generation, read_participants_strict,
    )
    from hermes_cli.serve_producer_enrollment import classify_with_broker
    from hermes_cli.serve_producer_participation import nonhosting_participant, retired_participant
    from hermes_cli.serve_producer_retirement import request_retirement
    from hermes_cli.serve_producer_observation import observe_shared_family
    from dataclasses import replace
    import sys

    if sys.platform != 'linux':
        raise AdmissionUnavailable('no linux census adapter')
    installation = installation.resolve(strict=True)
    adapter = LinuxProcessCensus()
    gate = LaunchGate.acquire(runtime)
    try:
        participants = ()
        if (runtime / 'participants.json').exists():
            participants = read_participants_strict(runtime)
        generation = launcher_generation(installation)
        legacy, refusals = [], []
        for ticket in participants:
            if incarnation_exited(adapter, ticket.key):
                legacy.append(ticket.key)  # independently re-probed by the strict cut below
                continue
            if ticket.generation != generation or ticket.installation != str(installation):
                legacy.append(ticket.key)  # old/uncooperative spawners need actual process retirement
                continue
            if ticket.role == 'producer_broker':
                continue  # local retirement cannot attest an external broker
            if nonhosting_participant(ticket.key, participants, generation, str(installation)):
                continue  # this role cannot host the panel: positively non-participating
            try:
                request_retirement(runtime, ticket, root, gate)
            except (OSError, ValueError, AdmissionUnavailable) as exc:
                refusals.append(f'retirement:{ticket.identity.pid}:{type(exc).__name__}')

        shared_family = observe_shared_family(runtime, installation, participants, generation, gate=gate)

        def classify(record):
            from hermes_cli.serve_producer_admission import Classification
            if record in shared_family:
                return Classification(record.key, 'participant', f'panel-shared-kernel:{generation}')
            if nonhosting_participant(record.key, participants, generation, str(installation)):
                return Classification(record.key, 'participant', f'panel-nonhosting-role:{generation}')
            baseline = classify_with_broker(
                record, participants, generation, installation=str(installation),
                runtime=runtime, broker=None, home=str(root))
            if (baseline.kind == 'legacy'
                    and retired_participant(record, participants, runtime, generation, str(installation))):
                return Classification(record.key, 'participant', f'panel-retired-kernel:{generation}')
            return baseline

        cut = gate.inspect(
            boot_id=adapter.boot_id(), generation=generation, installation=str(installation),
            census=adapter.collect, collectors=host_collectors(root, installation),
            classify=classify, legacy_candidates=tuple(legacy),
            exited=lambda identity: incarnation_exited(adapter, identity))
        cut = replace(cut, errors=(*cut.errors, *refusals))
    except BaseException:
        gate.close()
        raise
    return gate, cut
