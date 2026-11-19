"""``<kanban home>/llm_pacing_history.sqlite``: the capacity model's memory (``kanban_llm_pacing`` v2). [daveey]

``llm_pacing.json`` keeps two hours of samples, so no host had a week of percent-versus-token history to measure
an exchange rate from (11-19). This store keeps ``history_days`` (8) of:

- ``samples(ts, provider, account_hash, kind, used, reset_at)``: every successful pool read, account windows and the
  model-scoped ones (``seven_day_opus``/``seven_day_sonnet``, kept for later modelling only);
- ``tokens(ts, provider, cumulative_io)``: the fleet's input+output tokens on the provider at each read, a running
  sum of per-machine increases (``machine_counters``: the last value seen per machine), so a machine joining with
  a large total, or one whose counter went down (a prune), never reads as spend;
- ``events(ts, provider, account_hash, type, detail_json)``: :func:`kanban_llm_pacing.detect_events`.

Account labels are e-mail addresses: only :func:`kanban_llm_pacing.account_id` hashes are stored. A new file is
created with the schema; there is no older schema to migrate (``user_version`` 1).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional

from hermes_cli import kanban_llm_pacing as lp

SCHEMA_VERSION = 1
FILE_NAME = "llm_pacing_history.sqlite"
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS samples (ts REAL NOT NULL, provider TEXT NOT NULL, account_hash TEXT NOT NULL, "
    "kind TEXT NOT NULL, used REAL NOT NULL, reset_at REAL)",
    "CREATE INDEX IF NOT EXISTS samples_by_provider ON samples (provider, ts)",
    "CREATE TABLE IF NOT EXISTS tokens (ts REAL NOT NULL, provider TEXT NOT NULL, cumulative_io REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS tokens_by_provider ON tokens (provider, ts)",
    "CREATE TABLE IF NOT EXISTS events (ts REAL NOT NULL, provider TEXT NOT NULL, account_hash TEXT, "
    "type TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}')",
    "CREATE INDEX IF NOT EXISTS events_by_provider ON events (provider, ts)",
    "CREATE TABLE IF NOT EXISTS machine_counters (provider TEXT NOT NULL, machine TEXT NOT NULL, "
    "value REAL NOT NULL, ts REAL NOT NULL, PRIMARY KEY (provider, machine))",
)


LOCK_TIMEOUT_SECONDS = 0.5  # a busy store skips history for one refresh; it never delays a claim longer


def connect(path: Path) -> sqlite3.Connection:
    """The store, its schema created on first use only (``user_version`` 0 -> 1)."""
    # Not a board: the pacing history file, opened and closed within one refresh (``History.open``).
    db = sqlite3.connect(str(path), timeout=LOCK_TIMEOUT_SECONDS, isolation_level=None)  # noqa: kanban-lockguard
    try:
        if db.execute("PRAGMA user_version").fetchone()[0] == 0:
            for statement in _SCHEMA:
                db.execute(statement)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except BaseException:
        db.close()
        raise
    return db


class History:
    """One provider's view of the store for one refresh: one connection, one write transaction (taken with a
    :data:`LOCK_TIMEOUT_SECONDS` wait at most), closed on exit. A fault rolls the refresh's writes back."""

    def __init__(self, db: sqlite3.Connection, provider: str):
        self.db, self.provider = db, provider

    @classmethod
    @contextmanager
    def open(cls, path: Path, provider: str) -> Iterator[History]:
        with closing(connect(Path(path))) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield cls(db, provider)
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    def samples(self, since: float, kinds: Iterable[str] = lp.GOVERNED_KINDS) -> list[lp.Reading]:
        wanted = tuple(kinds)
        marks = ",".join("?" * len(wanted))
        db = self.db
        rows = db.execute(f"SELECT ts, account_hash, kind, used, reset_at FROM samples WHERE provider = ? "
                          f"AND ts >= ? AND kind IN ({marks}) ORDER BY ts", (self.provider, since, *wanted))
        return [lp.Reading(float(t), a, k, float(u), None if r is None else float(r)) for t, a, k, u, r in rows]

    def tokens(self, since: float) -> list[tuple[float, float]]:
        db = self.db
        return [(float(t), float(v)) for t, v in db.execute(
            "SELECT ts, cumulative_io FROM tokens WHERE provider = ? AND ts >= ? ORDER BY ts",
            (self.provider, since))]

    def event_marks(self, since: float) -> list[tuple[str, float]]:
        db = self.db
        return [(a, float(t)) for a, t in db.execute(
            "SELECT account_hash, ts FROM events WHERE provider = ? AND ts >= ? AND account_hash IS NOT NULL",
            (self.provider, since))]

    def record(self, now: float, readings: Iterable[lp.Reading], machines: Optional[Mapping[str, float]],
               events: Iterable[dict], keep_seconds: float) -> None:
        """One successful read: its (hashed) samples, the fleet token total (when ``machines`` read) and events;
        rows older than ``keep_seconds`` are dropped."""
        db = self.db
        db.executemany("INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?)",
                       [(r.ts, self.provider, r.account, r.kind, r.used, r.reset_at) for r in readings])
        if machines is not None:
            db.execute("INSERT INTO tokens VALUES (?, ?, ?)", (now, self.provider, self._advance(db, now, machines)))
        db.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                       [(e["ts"], self.provider, e.get("account"), e["type"],
                         json.dumps(e.get("detail") or {}, separators=(",", ":"))) for e in events])
        cutoff = now - keep_seconds
        for table in ("samples", "tokens", "events"):
            db.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
        db.execute("DELETE FROM machine_counters WHERE ts < ?", (cutoff,))

    def _advance(self, db: sqlite3.Connection, now: float, machines: Mapping[str, float]) -> float:
        row = db.execute("SELECT cumulative_io FROM tokens WHERE provider = ? ORDER BY ts DESC LIMIT 1",
                         (self.provider,)).fetchone()
        total = float(row[0]) if row else 0.0
        last = dict(db.execute("SELECT machine, value FROM machine_counters WHERE provider = ?", (self.provider,)))
        for machine, value in machines.items():
            if machine in last:
                total += max(0.0, float(value) - float(last[machine]))
            db.execute("INSERT OR REPLACE INTO machine_counters VALUES (?, ?, ?, ?)",
                       (self.provider, machine, float(value), now))
        return total

    def recent_reads(self, before: float, limit: int) -> list[tuple[float, set[str]]]:
        """``(ts, accounts)`` of the newest ``limit`` successful reads before ``before``, newest first."""
        db = self.db
        times = [float(t) for (t,) in db.execute(
            "SELECT DISTINCT ts FROM samples WHERE provider = ? AND ts < ? ORDER BY ts DESC LIMIT ?",
            (self.provider, before, limit))]
        return [(t, {a for (a,) in db.execute("SELECT DISTINCT account_hash FROM samples WHERE provider = ? "
                                              "AND ts = ?", (self.provider, t))}) for t in times]

    def seen_since(self, since: float, before: float) -> set[str]:
        db = self.db
        return {a for (a,) in db.execute("SELECT DISTINCT account_hash FROM samples WHERE provider = ? AND "
                                         "ts >= ? AND ts < ?", (self.provider, since, before))}

    def read_at(self, ts: float) -> list[lp.Reading]:
        return [r for r in self.samples(ts) if r.ts == ts]


def _machine_tokens(entry: object, provider: str) -> Optional[float]:
    """One registry machine's cumulative input+output tokens on ``provider``: its ``tokens_by_provider`` when it
    reports one, else its ``tokens_by_model`` attributed by model prefix; None when it reports neither."""
    from hermes_cli.machine_throughput_metrics import provider_for_model
    metrics = (entry or {}).get("metrics") if isinstance(entry, dict) else None
    metrics = metrics if isinstance(metrics, dict) else {}
    for key, match in (("tokens_by_provider", lambda name: str(name).lower() == provider),
                       ("tokens_by_model", lambda name: provider_for_model(name) == provider)):
        found = metrics.get(key)
        if isinstance(found, dict):
            return float(sum(v[0] for name, v in found.items() if match(name) and isinstance(v, (list, tuple))
                             and v and isinstance(v[0], (int, float)) and not isinstance(v[0], bool) and v[0] >= 0))
    return None


def fleet_tokens(provider: str) -> Optional[dict[str, float]]:
    """``machine -> cumulative input+output tokens on provider``: this host from its own ``state.db`` files
    (``tokens_by_provider``), every other machine from the Board registry (``GET /v2/machines``, cached 60 s).
    None when neither reads: the read then records no token point."""
    from hermes_cli import machine_throughput_metrics as mt
    machines: dict[str, float] = {}
    try:
        from hermes_cli.kanban_worker_node import registry
        reg = registry() or {}
    except Exception:  # health: allow BLE001 -- no Board (standalone host): this host's own tokens only
        reg = {}
    me = str(reg.get("me") or "local")
    local = mt.provider_counters().get("tokens_by_provider")
    if isinstance(local, dict):
        machines[me] = float((local.get(provider) or [0])[0])
    for name, entry in (reg.get("machines") or {}).items():
        value = None if name == me else _machine_tokens(entry, provider)
        if value is not None:
            machines[str(name)] = value
    return machines or None


def update_v2(state: dict, provider: str, current: list[lp.Reading], model_scoped: list[lp.Reading], now: float,
              cfg: lp.PacingConfig, path: Path) -> list[str]:
    """Record this read in the history at ``path``, re-estimate the pool's exchange rates and replace its ``v2``
    entry in ``state`` (``version: 2`` added, legacy provider keys untouched). Returns the fault messages.

    Fails open in layers, and never raises: a history fault (a busy store included) plans from the previous rates
    or seeds without events; a planning fault stores :func:`kanban_llm_pacing.reserve_pool` (the same at-reserve
    rule as the legacy ``held`` verdict); every field of the previous ``v2`` is type-checked (malformed = absent)."""
    previous = lp._dict(state.get("v2"))
    prior = lp._dict(lp._dict(previous.get("pools")).get(provider))
    readings = lp.hashed(lp.valid_readings(current))
    faults: list[str] = []
    try:
        seeds = {kind: lp.seed_tpp(provider, kind, cfg) for kind in lp.GOVERNED_KINDS}
        tpps = {kind: lp.smooth_tpp(lp.stored_tpp(prior, kind), None, now, cfg, seeds[kind])
                for kind in lp.GOVERNED_KINDS}
        events: list[dict] = []
        try:
            machines = fleet_tokens(provider) if readings else None
            with History.open(path, provider) as history:
                events, tpps = _record_and_estimate(history, readings, lp.hashed(lp.valid_readings(model_scoped)),
                                                    machines, prior, seeds, now, cfg)
        except Exception as exc:  # health: allow BLE001 -- a history fault keeps the plan on the last rates
            faults.append(f"history {type(exc).__name__}: {exc}")
        pool, curve = lp.plan_pool(provider, readings, tpps[lp.SEVEN_DAY], tpps[lp.FIVE_HOUR], now, cfg)
        v2 = lp.assemble_v2(previous, provider, pool, curve, events, now, cfg)
    except Exception as exc:  # health: allow BLE001 -- a planning fault must not change what holds a start
        faults.append(f"plan {type(exc).__name__}: {exc}")
        v2 = fallback_v2(provider, current, now, cfg)
    state["version"] = lp.V2_VERSION
    state["v2"] = v2
    return faults


def fallback_v2(provider: str, current: list[lp.Reading], now: float, cfg: lp.PacingConfig) -> dict:
    """A ``v2`` holding only this provider's at-reserve verdict (:func:`kanban_llm_pacing.reserve_pool`)."""
    pool = lp.reserve_pool(lp.hashed(lp.valid_readings(current)), now, cfg)
    return {"computed_at": now, "margin_s": cfg.margin, "pools": {provider: pool}, "global_rate_tokens_per_s": None,
            "events": [], "curve": {}}


def _record_and_estimate(history: History, readings, scoped, machines, prior, seeds, now, cfg):
    events: list[dict] = []
    if readings:
        recent = history.recent_reads(now, lp.REMOVED_AFTER_READS)
        if recent:  # a first read ever has nothing to compare with: no event
            seen = history.seen_since(now - lp.ADDED_UNSEEN_SECONDS, now)
            events = lp.detect_events(history.provider, readings, history.read_at(recent[0][0]),
                                      [accounts for _, accounts in recent[1:]], seen, now)
        history.record(now, readings + scoped, machines, events, cfg.history_days * 86400)
    since = now - cfg.tpp_window
    samples, tokens, marks = history.samples(since), history.tokens(since), history.event_marks(since)
    tpps = {kind: lp.smooth_tpp(lp.stored_tpp(prior, kind), lp.tpp_estimate(samples, tokens, kind, now, cfg, marks),
                                now, cfg, seeds[kind]) for kind in lp.GOVERNED_KINDS}
    return events, tpps
