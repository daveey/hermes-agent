"""Atomic transcript custody and worker-entry fencing for retired automation."""
from pathlib import Path
import sqlite3

from hermes_state_errors import SessionTurnLeaseLostError, CompressionSessionClosedError


class Delivery:
    def __init__(self, journal, batch):
        self.journal, self.batch = journal, batch
        self.work = None
        self.session = self.staged = None

    def stage(self, db, session, text, kind, metadata):
        """Write on the journal's transaction, then adopt its exact committed content."""
        from agent.context_compressor import _DB_PERSISTED_MARKER
        from agent.message_metadata import stamp_message_timestamp
        from tui_gateway import server
        if db is None or Path(db.db_path).resolve() != self.journal.path:
            raise ValueError("replay profile store mismatch")
        target = server._submit_row_target_key(session)
        staged = stamp_message_timestamp({"role": "user", "content": text,
                                          "display_kind": kind, "display_metadata": metadata})

        def write(conn, uid):
            db._check_transcript_write_guards(conn, target, None, reject_active_turn_lease=True)
            row = conn.execute("SELECT ended_at,end_reason FROM sessions WHERE id=?", (target,)).fetchone()
            if row is None or row["end_reason"] == "compression":
                raise ValueError("replay target missing or rotated")
            if row["ended_at"] is not None:
                db._reopen_session_conn(conn, target)
            staged["message_uid"] = uid
            db._insert_message_rows(conn, target, [staged])
            db._bump_session_counters(conn, target, 1, 0, unit=True)
            return staged["_row_id"], target

        receipt = self.journal.commit_message(self.batch, write)
        if receipt is None or receipt["dispatch"] != "pending":
            return None
        with self.journal.transaction() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=? AND session_id=? AND message_uid=? AND role='user'",
                               (receipt["message_row_id"], receipt["target"], receipt["message_uid"])).fetchone()
            if row is None:
                raise ValueError("replay transcript identity cannot be restaged")
            import json
            staged = {"role": "user", "content": row["content"], "timestamp": row["timestamp"],
                      "message_uid": row["message_uid"], "_row_id": row["id"],
                      "display_kind": row["display_kind"],
                      "display_metadata": json.loads(row["display_metadata"] or "{}")}
        staged[_DB_PERSISTED_MARKER] = True
        staged[server._SUBMIT_ROW_SESSION_KEY] = receipt["target"]
        session["_submit_user_row"] = staged
        session["_marker_until_tool"] = True
        self.session, self.staged = session, staged
        return staged["content"]

    def submit(self, rid, sid, session, text, kind, metadata, label):
        from tui_gateway import server
        accepted = False
        claim = session.get("_notif_turn_claim")
        try:
            if server._ensure_session_db_row(session) is False:
                return False
            with server._session_db(session) as db:
                text = self.stage(db, session, text, kind, metadata)
            if text is None:
                return False
            accepted = server._notif_submit(rid, sid, session, text, f"{label} dispatch failed",
                                             display_kind=self.staged.get("display_kind"),
                                             display_metadata=self.staged.get("display_metadata"),
                                             replay_delivery=self)
            return accepted
        except (sqlite3.Error, SessionTurnLeaseLostError, CompressionSessionClosedError):
            # Storage/ownership/rotation waits are not failed model or build attempts.
            return False
        finally:
            if not accepted:
                self.cleanup()
                server._notif_release_claim(session, claim)

    def admit(self):
        from tui_gateway.automation_replay import stale_code_reason
        if stale_code_reason():
            self.journal.release(self.batch)
            return False
        return self.journal.admit(self.batch)

    def cleanup(self):
        if self.session is not None:
            with self.session["history_lock"]:
                if self.session.get("_submit_user_row") is self.staged:
                    self.session.pop("_submit_user_row", None)
                    self.session.pop("_marker_until_tool", None)

    def bind_work(self, work):
        self.work = work

    def alive(self):
        # PreparedWork owns cancellation/finish even if thread start or admission fails.
        return self.work is not None and not self.work._finished
