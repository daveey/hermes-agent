"""Durable metadata-only occurrence journal and bounded daily intake delivery.

The caller holds the journal lock for discovery, delivery and cursor checkpointing.
SQLite is the backlog truth; the bounded JSON polling cache is expendable.
"""
from __future__ import annotations

import datetime as dt

import hashlib
import json
import os
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

try:
    from scripts.ci import flaky_artifact as fa
except ImportError:
    import flaky_artifact as fa  # type: ignore[no-redef]

PAGE_BYTES = 6000
OWNER_CHECK_BYTES = 1024


def utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def occurrence_metadata(entry: dict, run: dict, job: int) -> dict:
    """Allowlisted, validated metadata only; never persist failure output."""
    if not isinstance(entry, dict):
        raise ValueError("invalid intake metadata")
    meta = {"test_id": entry.get("test_id"), "retried": entry.get("retried", "tests"),
            "run_id": run.get("id"), "run_attempt": entry.get("run_attempt", run.get("run_attempt", 1)),
            "job_id": job, "head_sha": run.get("head_sha")}
    if (not isinstance(meta["test_id"], str)
            or fa.canonical_test_id(meta["test_id"]) != meta["test_id"]
            or not isinstance(meta["retried"], str) or meta["retried"] not in fa.RETRIED
            or not all(fa._int_in(meta[k], 1, fa.MAX_INT) for k in ("run_id", "run_attempt", "job_id"))
            or not isinstance(meta["head_sha"], str) or not fa._SHA.fullmatch(meta["head_sha"])):
        raise ValueError("invalid intake metadata")
    if "source_sha" in entry:
        if not isinstance(entry["source_sha"], str) or not fa._SHA.fullmatch(entry["source_sha"]):
            raise ValueError("invalid intake metadata")
        meta["source_sha"] = entry["source_sha"]
    if "slice" in entry or "slices" in entry:
        if not all(fa._int_in(entry.get(k), 1, fa.MAX_SLICES) for k in ("slice", "slices")):
            raise ValueError("invalid intake metadata")
        if entry["slice"] > entry["slices"]:
            raise ValueError("invalid intake metadata")
        meta.update(slice=entry["slice"], slices=entry["slices"])
    return meta


def run_metadata(run: dict) -> dict:
    """Bound descriptor values while retaining branch/workflow supersession identity."""
    if not isinstance(run, dict) or not fa._int_in(run.get("id"), 1, fa.MAX_INT):
        raise ValueError("invalid intake run")
    fields = ("id", "run_attempt", "head_sha", "workflow_id", "head_branch", "conclusion",
              "run_started_at", "updated_at")
    data = {k: run[k] for k in fields if k in run}
    for key in ("run_attempt", "workflow_id"):
        if key in data and not fa._int_in(data[key], 1, fa.MAX_INT):
            raise ValueError("invalid intake run")
    if not isinstance(data.get("head_sha"), str) or not fa._SHA.fullmatch(data["head_sha"]):
        raise ValueError("invalid intake run")
    branch = data.get("head_branch")
    if "head_branch" in data and (not isinstance(branch, str) or not 1 <= len(branch) <= 255
                                   or any(ord(c) < 32 or ord(c) == 127 for c in branch)):
        raise ValueError("invalid intake run")
    if "conclusion" in data and data["conclusion"] not in (
        None, "success", "failure", "neutral", "cancelled", "skipped", "timed_out",
        "action_required", "stale", "startup_failure",
    ):
        raise ValueError("invalid intake run")
    for key in ("run_started_at", "updated_at"):
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, str) or len(value) != 20:
            raise ValueError("invalid intake run")
        try:
            dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise ValueError("invalid intake run") from None
    return data


def _incident_destination(board, key, cached_id, payload):
    """Resolve one daily subject, retaining exact-write recovery after closure."""
    cards = [c for c in board.list_cards() if c.get("idempotency_key") == key or
             (str(c.get("idempotency_key", "")).startswith(key + "#") and
              str(c["idempotency_key"])[len(key) + 1:].isdigit())]
    cached = board.card(cached_id) if cached_id else None
    if cached_id:
        if not cached or cached.get("idempotency_key") not in {c["idempotency_key"] for c in cards}:
            raise RuntimeError("intake cached identity unavailable")
        if payload in board.comments(cached_id):
            return cached_id, cached["idempotency_key"], True
    actionable = {"todo", "ready", "running", "scheduled", "blocked", "review", "triage"}
    if any(c.get("status") not in actionable | {"done", "archived"} for c in cards):
        raise RuntimeError("intake status unavailable")
    opened = [c for c in cards if c.get("status") in actionable]
    owner = cached if cached and cached.get("status") in actionable else (opened[0] if opened else None)
    if owner:
        return owner["id"], owner["idempotency_key"], False
    used = {c["idempotency_key"] for c in cards}
    base_key = key
    generation = 0
    while key in used:
        generation += 1
        key = f"{base_key}#{generation}"
    return None, key, False


class Journal:
    def __init__(self, path: Path, *, identity_path: Path | None = None, initialize: bool = False):
        import fcntl

        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.path.with_suffix(".lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock.seek(0)
            initialized = bool(self.lock.read())
            if identity_path is not None:
                if initialize:
                    if initialized or self.path.exists() or identity_path.exists():
                        raise OSError("intake_already_initialized")
                elif not self.path.is_file() or not identity_path.is_file():
                    raise OSError("intake_identity_missing")
            if initialized and (not self.path.is_file() or self.path.stat().st_size == 0):
                raise OSError("intake_queue_missing")
            self.db = sqlite3.connect(self.path)
        except (OSError, sqlite3.Error):
            self.lock.close()
            raise
        self.db.row_factory = sqlite3.Row
        try:
            if identity_path is not None and not initialize:
                # Validate before schema migration or any discovery/delivery operation.
                expected = identity_path.read_text(encoding="utf-8-sig").strip()
                actual = self.db.execute("SELECT uuid FROM journal_identity").fetchall()
                if (len(actual) != 1 or actual[0][0] != expected
                        or str(uuid.UUID(expected)) != expected):
                    raise ValueError("intake_identity_mismatch")
            self._initialize()
            if identity_path is not None and initialize:
                identity = str(uuid.uuid4())
                with self.db:
                    self.db.execute("CREATE TABLE journal_identity (uuid TEXT NOT NULL)")
                    self.db.execute("INSERT INTO journal_identity VALUES (?)", (identity,))
                identity_path.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive creation: a partial bootstrap is never silently retried.
                with identity_path.open("x", encoding="utf-8") as anchor:
                    anchor.write(identity + "\n")
                    anchor.flush()
                    os.fsync(anchor.fileno())
                for directory in {self.path.parent, identity_path.parent}:
                    fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            if not initialized:
                self.lock.write("initialized\n")
                self.lock.flush()
                os.fsync(self.lock.fileno())
        except (ValueError, OSError, sqlite3.Error):
            self.close()
            raise

    def _initialize(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS run_failures (
                repo TEXT, key TEXT, polls INTEGER NOT NULL, PRIMARY KEY(repo,key));
            CREATE TABLE IF NOT EXISTS scheduling (
                repo TEXT, kind TEXT, item TEXT, ticket INTEGER NOT NULL,
                PRIMARY KEY(repo,kind,item));
            CREATE INDEX IF NOT EXISTS scheduling_ticket ON scheduling(ticket);
            CREATE TABLE IF NOT EXISTS runs (
                repo TEXT, key TEXT, descriptor TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(repo,key));
            CREATE TABLE IF NOT EXISTS occurrences (
                id INTEGER PRIMARY KEY, repo TEXT NOT NULL, identity TEXT NOT NULL,
                day TEXT NOT NULL, target_day TEXT NOT NULL, metadata TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0, owner TEXT, page INTEGER,
                UNIQUE(repo,identity));
            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY, repo TEXT NOT NULL, day TEXT NOT NULL,
                payload TEXT NOT NULL, card TEXT, delivered INTEGER NOT NULL DEFAULT 0,
                attempted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS owner_check_details (
                digest TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS occurrences_run ON occurrences(repo,json_extract(metadata,'$.run_id'),delivered);
            CREATE INDEX IF NOT EXISTS occurrences_page ON occurrences(page);
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(pages)")}
        if "attempted" not in columns:
            # Pre-marker pages might already have escaped. Preserve them immutably.
            self.db.execute("ALTER TABLE pages ADD COLUMN attempted INTEGER NOT NULL DEFAULT 1")
            self.db.commit()

        with self.db:
            for table, column in (("runs", "retained_at"), ("occurrences", "retained_at"), ("pages", "retained_at")):
                if column not in {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} REAL NOT NULL DEFAULT 0")
                    # Unknown legacy age starts now, never from an inferred old run date.
                    self.db.execute(f"UPDATE {table} SET {column}=?", (time.time(),))

        # Backfill older queues before admitting new work. Replay preserves tickets.
        with self.db:
            for kind, query in (
                ("run", "SELECT repo,key FROM runs WHERE complete=0 ORDER BY rowid"),
                ("occurrence", "SELECT repo,id FROM occurrences WHERE delivered=0 ORDER BY id"),
                ("page", "SELECT repo,id FROM pages WHERE delivered=0 ORDER BY id"),
            ):
                for repo, item in self.db.execute(query).fetchall():
                    self.enqueue(repo, kind, str(item))

    def enqueue(self, repo: str, kind: str, item: str):
        """Join the FIFO once, in the caller's insertion transaction."""
        self.db.execute("INSERT INTO scheduling(repo,kind,item,ticket) "
                        "SELECT ?,?,?,COALESCE(MAX(ticket),0)+1 FROM scheduling WHERE 1 "
                        "ON CONFLICT(repo,kind,item) DO NOTHING", (repo, kind, str(item)))

    def close(self):
        self.db.close()
        self.lock.close()

    def remember_runs(self, repo: str, runs: list[dict]) -> list[dict]:
        # Validation and writes share one transaction, including the whole listing.
        with self.db:
            for run in runs:
                descriptor = run_metadata(run)
                key = f"{run['id']}/{run.get('run_attempt', 1)}"
                self.db.execute("INSERT INTO runs(repo,key,descriptor,retained_at) VALUES(?,?,?,?) "
                                "ON CONFLICT(repo,key) DO UPDATE SET descriptor=excluded.descriptor, "
                                "retained_at=excluded.retained_at",
                                (repo, key, json.dumps(descriptor), time.time()))
                self.enqueue(repo, "run", key)
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT r.descriptor FROM runs r LEFT JOIN scheduling s ON "
            "s.repo=r.repo AND s.kind='run' AND s.item=r.key WHERE r.repo=? AND r.complete=0 "
            "ORDER BY COALESCE(s.ticket,0),r.key", (repo,))]

    def note_unavailable(self, repo: str, key: str) -> int:
        # Count actual failed observations, not invocations skipped by fair rotation.
        with self.db:
            return self.db.execute("INSERT INTO run_failures VALUES (?,?,1) "
                                   "ON CONFLICT(repo,key) DO UPDATE SET polls=polls+1 RETURNING polls",
                                   (repo, key)).fetchone()[0]

    def finish_run(self, repo: str, key: str):
        with self.db:
            self.db.execute("UPDATE runs SET complete=1,retained_at=? WHERE repo=? AND key=?",
                            (time.time(), repo, key))
            self.db.execute("DELETE FROM run_failures WHERE repo=? AND key=?", (repo, key))

    def record(self, repo: str, jobs: list[tuple[int, list[dict]]], run: dict):
        if not isinstance(repo, str) or not fa._REPO.fullmatch(repo):
            raise ValueError("invalid intake metadata")
        day = utc_day()
        with self.db:
            for job, entries in jobs:
                for entry in entries:
                    meta = occurrence_metadata(entry, run, job)
                    identity = json.dumps([meta[k] for k in ("run_id", "run_attempt", "job_id", "test_id")])
                    self.db.execute("INSERT OR IGNORE INTO occurrences(repo,identity,day,target_day,metadata) "
                                    "VALUES(?,?,?,?,?)", (repo, identity, day, day, json.dumps(meta, sort_keys=True)))
                    row_id = self.db.execute("SELECT id FROM occurrences WHERE repo=? AND identity=?",
                                             (repo, identity)).fetchone()[0]
                    self.enqueue(repo, "occurrence", str(row_id))

    def pending(self, repo: str):
        return self.db.execute("SELECT o.* FROM occurrences o LEFT JOIN scheduling s ON "
                               "s.repo=o.repo AND s.kind='occurrence' AND s.item=CAST(o.id AS TEXT) "
                               "WHERE o.repo=? AND o.delivered=0 AND o.page IS NULL "
                               "ORDER BY COALESCE(s.ticket,0),o.id", (repo,)).fetchall()

    def rotate(self, repo: str, kind: str, item: str):
        """Persist selection BEFORE external work, including failures and process death."""
        with self.db:
            self.db.execute("INSERT INTO scheduling(repo,kind,item,ticket) "
                            "SELECT ?,?,?,COALESCE(MAX(ticket),0)+1 FROM scheduling WHERE 1 "
                            "ON CONFLICT(repo,kind,item) DO UPDATE SET ticket=excluded.ticket",
                            (repo, kind, str(item)))

    def recheck_unattempted(self, repo: str):
        """Only pages known never to have escaped can be repartitioned for new owners."""
        with self.db:
            self.db.execute("UPDATE occurrences SET page=NULL WHERE page IN "
                            "(SELECT id FROM pages WHERE repo=? AND attempted=0 AND delivered=0)", (repo,))
            self.db.execute("DELETE FROM pages WHERE repo=? AND attempted=0 AND delivered=0", (repo,))

    def acknowledge(self, row_id: int, owner: str):
        with self.db:
            self.db.execute("UPDATE occurrences SET delivered=1,owner=?,retained_at=? WHERE id=?",
                            (owner, time.time(), row_id))

    def plan_pages(self, repo: str, rows: list, owner_check: dict[int, list] | None = None):
        """Commit immutable pages before any external write; never shorten a manifest.

        ``owner_check`` names unverifiable prior-owner candidates per occurrence
        (an empty list means none were found in the inventoried scopes)."""
        groups: list[list] = []
        details: dict[str, str] = {}
        for row in rows:
            item = {**json.loads(row["metadata"]), "occurrence_id": row["id"],
                    "observed_day": row["day"], "status": "needs_triage"}
            if owner_check is not None:
                notes = owner_check.get(row["id"], [])
                encoded = json.dumps(notes, sort_keys=True)
                if len(encoded.encode("utf-8")) > OWNER_CHECK_BYTES:
                    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                    details[digest] = encoded
                    item["owner_check_ref"] = {"sha256": digest, "count": len(notes)}
                else:
                    item["owner_check"] = notes
            if not groups or groups[-1][0]["target_day"] != row["target_day"] or len(
                json.dumps([r["item"] for r in groups[-1]] + [item]).encode("utf-8")
            ) > PAGE_BYTES - 300:
                groups.append([])
            groups[-1].append({"id": row["id"], "target_day": row["target_day"], "item": item})
        with self.db:
            # Full diagnostics and their manifest references commit atomically.
            self.db.executemany("INSERT OR IGNORE INTO owner_check_details VALUES (?,?)", details.items())
            for group in groups:
                data = json.dumps({"version": 1, "occurrences": [r["item"] for r in group]}, sort_keys=True)
                marker = hashlib.sha256(data.encode()).hexdigest()
                payload = f"<!-- ci-flaky-page:{marker} -->\n```json\n{data}\n```"
                if len(payload.encode()) > PAGE_BYTES:
                    raise ValueError("intake_page_oversize")
                page = self.db.execute("INSERT INTO pages(repo,day,payload,attempted) VALUES(?,?,?,0)",
                                       (repo, group[0]["target_day"], payload)).lastrowid
                # IDs may be reused after unplanning; this is a new queue item.
                self.db.execute("DELETE FROM scheduling WHERE repo=? AND kind='page' AND item=?",
                                (repo, str(page)))
                self.enqueue(repo, "page", str(page))
                self.db.executemany("UPDATE occurrences SET page=? WHERE id=?", [(page, r["id"]) for r in group])

    def unplan_page(self, page_id: int):
        """Only called after a definitive no-create refusal, never an ambiguous write."""
        with self.db:
            self.db.execute("UPDATE occurrences SET page=NULL WHERE page=? AND delivered=0", (page_id,))
            self.db.execute("DELETE FROM pages WHERE id=?", (page_id,))

    def deliver(self, repo: str, board: Any, *, deadline: float | None = None,
                create: Any = None, page_ids: set[int] | None = None) -> list[str]:
        log = []
        pages = self.db.execute("SELECT p.* FROM pages p LEFT JOIN scheduling s ON "
                                "s.repo=p.repo AND s.kind='page' AND s.item=CAST(p.id AS TEXT) "
                                "WHERE p.repo=? AND p.delivered=0 ORDER BY COALESCE(s.ticket,0),p.id",
                                (repo,)).fetchall()
        for page in pages:
            if page_ids is not None and page["id"] not in page_ids:
                continue
            if deadline is not None and time.monotonic() >= deadline:
                log.append("FAILED intake_budget; durable retry pending")
                break
            if page["day"] > utc_day():
                log.append(f"FAILED intake carry-forward pending day={page['day']}")
                continue
            self.rotate(repo, "page", str(page["id"]))
            key = f"ci-flaky-intake:{repo}:{page['day']}"
            try:
                card, key, delivered = _incident_destination(board, key, page["card"], page["payload"])
                if delivered:
                    self.acknowledge_page(page["id"], card)
                    log.append(f"intake delivered {card} page={page['id']}")
                    continue
                # Fence before create as well as comment: either response can be lost.
                with self.db:
                    self.db.execute("UPDATE pages SET attempted=1 WHERE id=?", (page["id"],))
                if card is None:
                    body = (f"Daily CI flake intake for {repo}, UTC {page['day']}.\n\n"
                            f"Durable metadata queue on producer host: {self.path}\n"
                            "Every JSON manifest page lists occurrence provenance and per-test needs_triage status. "
                            "Triage related fixes together; create named follow-ups only for genuinely distinct fixes. "
                            "Existing exact-test owners remain owners. Owner inventory before admission: this "
                            "board plus hermes-hub replica cards on fresh scopes; unsynced legacy profile boards "
                            "are not inventoried, and rows with owner_check or owner_check_ref had clipped closed-card "
                            "history, so check those first. Read the full durable queue before closing; "
                            "record each test's disposition and follow-up owner. No retry threshold changes.\n"
                            "Backlog/export: see docs/ci-flaky-intake.md. New arrivals after closure use a new intake generation.")
                    title = f"CI flaky intake {repo} {page['day']}"
                    card = create(title, body, key, page) if create else board.create(title, body, key)
                    if card is None:
                        log.append(f"FAILED intake ownership changed page={page['id']}; durable retry pending")
                        continue
                with self.db:
                    self.db.execute("UPDATE pages SET card=? WHERE id=?", (card, page["id"]))
                current = board.card(card)
                if not current or current.get("idempotency_key") != key:
                    raise RuntimeError("intake identity readback unavailable")
                # Reconcile a successful write BEFORE interpreting a later archive/done.
                if page["payload"] in board.comments(card):
                    self.acknowledge_page(page["id"], card)
                    log.append(f"intake delivered {card} page={page['id']}")
                    continue
                status = current.get("status")
                # Closure raced selection: leave the page pending; the next fire
                # selects a fresh generation after reconciling any successful write.
                if status not in {"todo", "ready", "running", "scheduled", "blocked", "review", "triage"}:
                    raise RuntimeError("intake not actionable")
                if page["payload"] not in board.comments(card):
                    try:
                        board.comment(card, page["payload"])
                    except RuntimeError:
                        # An ambiguous write may have succeeded: exact readback decides.
                        if page["payload"] not in board.comments(card):
                            raise
                if page["payload"] not in board.comments(card):
                    raise RuntimeError("intake comment readback unavailable")
                self.acknowledge_page(page["id"], card)
                log.append(f"intake delivered {card} page={page['id']}")
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
                log.append(f"FAILED intake delivery page={page['id']}; durable retry pending")
        return log

    def acknowledge_page(self, page_id: int, card: str):
        with self.db:
            self.db.execute("UPDATE pages SET delivered=1,retained_at=? WHERE id=?", (time.time(), page_id))
            self.db.execute("UPDATE occurrences SET delivered=1,owner=?,retained_at=? WHERE page=?",
                            (card, time.time(), page_id))

    def prune(self, repo: str, *, lookback_hours: float = 6.0):
        """Retain terminal history for 30 days plus discovery lookback; never pending work.

        No VACUUM: SQLite reuses freed pages without a blocking file rewrite.
        Recently listed runs retain all their occurrence replay receipts.
        """
        cutoff = time.time() - 30 * 86400 - max(0, lookback_hours) * 3600
        with self.db:
            self.db.execute("DELETE FROM runs WHERE repo=? AND complete=1 AND retained_at<? "
                            "AND NOT EXISTS (SELECT 1 FROM occurrences o WHERE o.repo=runs.repo "
                            "AND json_extract(o.metadata,'$.run_id')=json_extract(runs.descriptor,'$.id') "
                            "AND o.delivered=0)", (repo, cutoff))
            self.db.execute("DELETE FROM occurrences WHERE repo=? AND delivered=1 AND retained_at<? "
                            "AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.repo=occurrences.repo "
                            "AND r.key=json_extract(occurrences.metadata,'$.run_id') || '/' || "
                            "json_extract(occurrences.metadata,'$.run_attempt')) "
                            "AND NOT EXISTS (SELECT 1 FROM pages p WHERE p.id=occurrences.page AND p.delivered=0)",
                            (repo, cutoff))
            self.db.execute("DELETE FROM pages WHERE repo=? AND delivered=1 AND retained_at<? "
                            "AND NOT EXISTS (SELECT 1 FROM occurrences o WHERE o.page=pages.id)", (repo, cutoff))
            for kind, table, key, pending in (
                ("run", "runs", "key", "complete=0"),
                ("occurrence", "occurrences", "id", "delivered=0"),
                ("page", "pages", "id", "delivered=0"),
            ):
                self.db.execute(f"DELETE FROM scheduling WHERE repo=? AND kind=? AND NOT EXISTS "
                                f"(SELECT 1 FROM {table} t WHERE t.repo=scheduling.repo "
                                f"AND t.{key}=scheduling.item AND {pending})", (repo, kind))
