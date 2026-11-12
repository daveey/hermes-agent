"""Presentation and grouped custody for eligible remote arrivals with old owner times."""
from __future__ import annotations

import contextlib
import logging
import sqlite3
from datetime import datetime
from functools import partial
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def owner_time(task: dict, now: float):
    from tui_gateway import kanban_remote_notices as notices

    at = notices._fresh_at(task)
    return at if at is not None and now - at > notices.FRESH_S else None


def representative(items: list[dict]) -> dict:
    """The member that speaks for the group's wake policy: the first one that is not parked on a human, so a
    done card is not silenced by a human-blocked sibling. An all-human group keeps its first member (passive)."""
    return next((item for item in items if not item.get("blocked_on_human")), items[0])


def release_undelivered(claims: list[dict], ledger) -> None:
    """Hand back claims whose row is not in a transcript. Unlike ``release`` it never deletes a delivered row, so
    it is safe after a partial failure. Never raises."""
    from tui_gateway import kanban_remote_notices as notices

    try:
        with contextlib.closing(notices._ledger(ledger)) as conn:
            with conn:
                for claim in claims:
                    conn.execute("DELETE FROM notices WHERE session=? AND ref=? AND state=? AND delivered=0",
                                 (claim["session"], claim["ref"], claim["state"]))
    except (sqlite3.Error, OSError, KeyError):
        logger.warning("late remote notices: could not release %d claim(s)", len(claims), exc_info=True)


def group(items: list[dict], now: float) -> dict:
    """Keep the first member's legacy fields, but carry every real key and claim."""
    lines = ["Late notices — remote updates:"]
    zone = ZoneInfo("America/Los_Angeles")
    for item in items[:20]:
        at = item["owner_at"]
        stamp = datetime.fromtimestamp(at, zone).strftime("%Y-%m-%d %H:%M %Z")
        minutes = int((now - at) // 60)
        age = f"{minutes // 60}h {minutes % 60}m"
        lines.append(f"- {stamp} ({age} ago; not delivered then)\n{item['text']}")
    if len(items) > 20:
        lines.append(f"+{len(items) - 20} more")
    return {**representative(items), "text": "\n".join(lines), "children": items, "late_at": now,
            "event_keys": [item["event_key"] for item in items],
            "blocked_on_human": all(item.get("blocked_on_human") for item in items)}


def collect_groups(items: list[dict], now: float) -> list[dict]:
    late = [item for item in items if item.get("owner_at") is not None]
    regular = [item for item in items if item.get("owner_at") is None]
    return regular + ([group(late, now)] if late else [])


def missing_texts(batch: list, done: set) -> list:
    """Re-render partially delivered late groups without changing original claim/text identities.

    The transcript writer rechecks atomically, so a second race returns busy and repeats this on the next poll.
    Original claims retain ALL children: the old rows plus the new row jointly secure their custody.
    """
    result = []
    for text in batch:
        item = getattr(text, "_kb_remote", {})
        children = item.get("children")
        if children and done.intersection(item["event_keys"]):
            missing = [child for child in children if child["event_key"] not in done]
            if not missing:
                continue
            remaining = group(missing, item["late_at"])
            replacement = type(text)(remaining["text"])
            replacement._kb_remote = remaining
            replacement._kb_keys = tuple(remaining["event_keys"])
            replacement._kb_blocked = remaining["blocked_on_human"]
            text = replacement
        if not (getattr(text, "_kb_keys", None) and set(text._kb_keys) <= done):
            result.append(text)
    return result


class SweepBatches:
    """Reuse the ordinary link guards/claims, deferring only delivery until the scan ends."""

    def __init__(self, waker, session_db, ledger, now):
        self.waker = waker
        self.session_db = session_db
        self.ledger = ledger
        self.now = now
        self.pending: dict[tuple, list] = {}
        self.grace_s = waker.grace_s if waker is not None else 0

    def target(self, owner, key, task, link, path):
        from tui_gateway import kanban_remote_notices as notices

        profile, sid = link["profile"], link["session"]
        claim = {"session": sid, "ref": f"{owner}/{key}",
                 "state": notices._notice_state(task),  # the ledger row identity _deliver_link claimed
                 "event_key": notices._shared_key(owner, key, task, profile, sid),
                 "owner_at": notices._fresh_at(task)}
        claim["notice_key"] = claim["event_key"]
        memo = (str(path), profile, sid, notices._notice_id(owner, key, task, profile, sid))
        batches = self

        class Target:
            grace_s = batches.grace_s

            def deliver(self, tip, text, metadata, task, now, *, persist, started):
                batches.pending.setdefault((profile, tip), []).append(
                    ({**claim, "text": text, "blocked_on_human": metadata.get("blocked_on_human")},
                     memo, metadata, task))
                return "batched"

        return Target()

    def flush(self) -> list[dict]:
        """Deliver one group per chat. A failure in one chat releases that chat's unpersisted claims and leaves
        the others alone."""
        delivered = []
        pending, self.pending = self.pending, {}
        for (profile, tip), entries in pending.items():
            try:
                delivered.append(self._flush_one(profile, tip, entries))
            # OverflowError: datetime.fromtimestamp of an absurd owner time (-1e30) in group(); collect catches it too.
            except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, RuntimeError, OverflowError):
                logger.warning("late remote notices for %s not delivered; claims released for the next pass",
                               tip, exc_info=True)
                release_undelivered([entry[0] for entry in entries], self.ledger)
        return delivered

    def _flush_one(self, profile, tip, entries) -> dict:
        from tui_gateway import kanban_remote_notices as notices

        claim = group([entry[0] for entry in entries], self.now)
        claim["memo_keys"] = [entry[1] for entry in entries]
        with self.session_db(profile, tip) as (db, target):
            if db is None:
                raise RuntimeError("late notice destination unavailable")
            done = db.kanban_notice_keys_delivered(target, claim["event_keys"])
        missing = [entry for entry in entries if entry[0]["event_key"] not in done]
        if not missing:
            notices._settle_claim(claim, entries[0][1], self.ledger)
            return {"task_id": entries[0][3]["id"], "session_id": tip, "wake": "delivered"}
        remaining = group([entry[0] for entry in missing], self.now)
        rep = next(e for e in missing if e[0] is representative([x[0] for x in missing]))
        metadata = {**rep[2], "kanban_event_keys": remaining["event_keys"],
                    "blocked_on_human": remaining["blocked_on_human"]}
        memo = entries[0][1]
        sid = claim["session"]
        text = remaining["text"]
        persist = partial(notices._persist_claimed, self.session_db, profile, sid, text, metadata,
                          claim, memo, self.ledger)
        started = partial(notices._settle_started, self.session_db, profile, sid, claim["event_key"],
                          claim, memo, self.ledger)
        if self.waker is None:
            result = "persisted" if persist() else "deferred"
        else:
            result = self.waker.deliver(tip, text, metadata, rep[3], self.now, persist=persist, started=started)
        return {"task_id": rep[3]["id"], "session_id": tip, "wake": result}
