"""In-memory worker-entry custody for timers that have not retired."""
from tui_gateway.automation_replay_dispatch import Delivery


class LocalPermit(Delivery):
    """Keep firing items visible to Stop and retirement until worker admission wins."""

    def __init__(self, key, entry):
        self.key, self.entry = key, entry
        self.ids = {item.id for item in entry.items}
        self.work = None
        self.session = self.staged = None

    def submit(self, rid, sid, session, text, kind, metadata, label):
        from tui_gateway import server
        return server._notif_submit(rid, sid, session, text, f"{label} dispatch failed",
                                    display_kind=kind, display_metadata=metadata, replay_delivery=self)

    def _current(self, ar):
        return (ar._entries.get((self.entry.home, self.key)) is self.entry
                and self.entry.fire_token is self)

    def _finish(self, ar, *, remove):
        if remove:
            self.entry.items = [item for item in self.entry.items if item.id not in self.ids]
        self.entry.fire_token = None
        if self.entry.items:
            ar._arm(self.key, self.entry, max(self.entry.due - ar.time.time(), ar.REPLAY_MIN_DELAY_S), ar._fire)
        else:
            ar._cancel_timer(self.entry)
            ar._entries.pop((self.entry.home, self.key), None)

    def _transfer(self, ar):
        if not self.entry.home:
            with ar._lock:
                if self._current(ar):
                    ar.logger.warning("automation-replay: cannot transfer %s without its profile home", self.key)
                    self._finish(ar, remove=True)
            return
        ar.handoff(self.key, profile_home=self.entry.home)

    def admit(self):
        from tui_gateway import automation_replay as ar
        with ar._lock:
            if not self._current(ar):
                return False
            retired = bool(ar.stale_code_reason())
            if not retired:
                intact = self.ids <= {item.id for item in self.entry.items}
                permitted = intact and not self.entry.transferring
                # A changed mixed payload must be replayed afresh, never run the cancelled members.
                if not self.entry.transferring:
                    self._finish(ar, remove=permitted)
                return permitted
        self._transfer(ar)
        return False

    def refused(self, verdict):
        from tui_gateway import automation_replay as ar
        if ar.stale_code_reason():
            self._transfer(ar)
            return
        with ar._lock:
            if not self._current(ar) or self.entry.transferring:
                return
            if verdict == "busy" and self.entry.busy_rechecks < ar.BUSY_RECHECKS_MAX:
                self.entry.busy_rechecks += 1
                self.entry.fire_token = None
                ar._arm(self.key, self.entry, max(self.entry.due - ar.time.time(), ar.BUSY_RECHECK_S), ar._fire)
            else:
                ar.logger.warning("automation-replay: gave up local replay of %s (%s)", self.key, verdict)
                self._finish(ar, remove=True)
