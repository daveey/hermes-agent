/**
 * [daveey] Persistent local transcript cache (instant session loads, C1).
 *
 * One SQLite file under Desktop's userData holds the raw `/api/sessions/{id}/messages`
 * rows of recently active sessions on every gateway, keyed by (connection, profile,
 * stored session id). A click paints from here before any SSH/REST round trip; the
 * open path and the background prefetcher keep it current. Same trust domain as the
 * gateways' own state.db on this Mac: plaintext, local only, never sent anywhere.
 *
 * Each entry is the newest contiguous run of a session's display rows: the latest
 * page (LATEST_ROWS) plus any older pages the user loaded. A newer latest page
 * replaces the tail and keeps the loaded prefix only when the two overlap, so a
 * session that grew past the window never stitches a gap into its history.
 *
 * Bounds: MAX_SESSIONS entries and MAX_TOTAL_BYTES overall, least recently used
 * first; one entry is capped at MAX_ENTRY_BYTES (older loaded pages go first).
 */
import fs from 'node:fs'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'

export const LATEST_ROWS = 200
export const DEFAULT_LIMITS = {
  maxEntryBytes: 8 * 1024 * 1024,
  maxSessions: 1000,
  maxTotalBytes: 256 * 1024 * 1024
}

export interface TranscriptCacheKey {
  connectionId: string
  profile: string
  sessionId: string
}

export interface TranscriptCacheMeta {
  /** Hub replica `activity` / `message_count` the entry was fetched against. */
  activity?: null | number
  messageCount?: null | number
}

export interface CachedTranscript {
  activity: null | number
  messageCount: null | number
  messages: unknown[]
  savedAt: number
}

type Limits = typeof DEFAULT_LIMITS

// Metadata and bodies live in separate tables. A body is up to MAX_ENTRY_BYTES of
// JSON and spills into an overflow-page chain; SQLite reads a row's columns in order,
// so any column stored after the body (bytes, saved_at, ...) forced a walk of that
// whole chain. With bodies inline, every freshness check, stats() and LRU scan read
// the full cache (~256 MB at the bound) synchronously on Electron's main thread and
// froze input for 0.5-0.85 s per prefetch write: choppy scrolling everywhere.
// Keep every query that does not need the text on `entries`.
const SCHEMA = `
CREATE TABLE IF NOT EXISTS entries (
  id INTEGER PRIMARY KEY,
  connection_id TEXT NOT NULL,
  profile TEXT NOT NULL,
  session_id TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  saved_at REAL NOT NULL,
  used_at REAL NOT NULL,
  activity REAL,
  message_count INTEGER,
  UNIQUE (connection_id, profile, session_id)
);
CREATE INDEX IF NOT EXISTS entries_used ON entries(used_at);
CREATE INDEX IF NOT EXISTS entries_session ON entries(session_id);
CREATE TABLE IF NOT EXISTS bodies (
  id INTEGER PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
`

/** Pre-split layout (body inline). A cache: discard the file rather than migrate it. */
const LEGACY_TABLE = 'transcripts'

const KEY_WHERE = 'connection_id = ? AND profile = ? AND session_id = ?'

/** Stable identity of one display row: the server row id when present. */
export function messageIdentity(message: unknown): string {
  const row = (message ?? {}) as Record<string, unknown>

  if (typeof row.id === 'number' || typeof row.id === 'string') {
    return `i:${row.id}`
  }

  return `h:${String(row.role ?? '')}|${String(row.timestamp ?? '')}|${String(row.tool_call_id ?? '')}`
}

/** Newer latest page `latest` over cached run `cached`: keep the loaded prefix only on overlap. */
export function mergeLatest(cached: readonly unknown[], latest: readonly unknown[]): unknown[] {
  if (latest.length === 0) {
    return [...cached]
  }

  const anchor = messageIdentity(latest[0])
  const at = cached.findIndex(message => messageIdentity(message) === anchor)

  return at > 0 ? [...cached.slice(0, at), ...latest] : [...latest]
}

/** [daveey] The server row id of a cached run's last row: the anchor a push delta (`?after_id=`) extends. Null when
 *  the last row has no numeric id (a client-side row): such a run is only ever replaced by a latest page. */
export function tailRowId(messages: readonly unknown[]): null | number {
  const last = messages.at(-1) as Record<string, unknown> | undefined
  const id = typeof last?.id === 'number' ? last.id : typeof last?.id === 'string' ? Number(last.id) : NaN

  return Number.isSafeInteger(id) ? id : null
}

/** An older page loaded above the cached run: prepend what the run does not already hold. */
export function mergeOlder(cached: readonly unknown[], older: readonly unknown[]): unknown[] {
  if (cached.length === 0) {
    return [] // no anchor: an older page alone is not "the latest messages"
  }

  const held = new Set(cached.map(messageIdentity))

  return [...older.filter(message => !held.has(messageIdentity(message))), ...cached]
}

const byteLength = (text: string) => Buffer.byteLength(text, 'utf8')

/** Serialize, dropping the oldest rows until the entry fits (latest rows always win). */
function fitEntry(messages: unknown[], maxBytes: number): { body: string; bytes: number } | null {
  let start = 0

  while (start < messages.length) {
    const body = JSON.stringify(messages.slice(start))
    const bytes = byteLength(body)

    if (bytes <= maxBytes) {
      return { body, bytes }
    }

    // Drop roughly the overflow's share of rows, at least one.
    start += Math.max(1, Math.floor(((bytes - maxBytes) / bytes) * (messages.length - start)))
  }

  return null
}

export class TranscriptCacheStore {
  private readonly db: DatabaseSync
  private readonly limits: Limits
  private readonly now: () => number

  constructor(file: string, options: { limits?: Partial<Limits>; now?: () => number } = {}) {
    if (file !== ':memory:') {
      fs.mkdirSync(path.dirname(file), { recursive: true })
    }

    this.db = TranscriptCacheStore.open(file)
    this.limits = { ...DEFAULT_LIMITS, ...options.limits }
    this.now = options.now ?? Date.now

    if (file !== ':memory:') {
      try {
        fs.chmodSync(file, 0o600)
      } catch {
        // best effort: same posture as state.db
      }
    }
  }

  private static open(file: string): DatabaseSync {
    let db = new DatabaseSync(file)

    if (file !== ':memory:' && TranscriptCacheStore.hasLegacyTable(db)) {
      // Unlinking is instant; DROP TABLE on a full legacy cache would walk every
      // overflow page on the main thread, the very stall this layout removes.
      db.close()

      for (const suffix of ['', '-wal', '-shm']) {
        fs.rmSync(`${file}${suffix}`, { force: true })
      }

      db = new DatabaseSync(file)
    }

    db.exec(
      'PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL; PRAGMA busy_timeout = 2000; PRAGMA foreign_keys = ON;'
    )
    db.exec(SCHEMA)

    return db
  }

  private static hasLegacyTable(db: DatabaseSync): boolean {
    return Boolean(db.prepare("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?").get(LEGACY_TABLE))
  }

  close(): void {
    this.db.close()
  }

  read(key: TranscriptCacheKey, options: { touch?: boolean } = {}): CachedTranscript | null {
    const row = this.db
      .prepare(
        `SELECT b.body, e.saved_at, e.activity, e.message_count FROM entries e JOIN bodies b ON b.id = e.id WHERE ${KEY_WHERE}`
      )
      .get(key.connectionId, key.profile, key.sessionId) as
      { activity: null | number; body: string; message_count: null | number; saved_at: number } | undefined

    if (!row) {
      return null
    }

    let messages: unknown

    try {
      messages = JSON.parse(row.body)
    } catch {
      messages = null
    }

    if (!Array.isArray(messages) || messages.length === 0) {
      this.drop(key) // corrupt entries self-evict

      return null
    }

    if (options.touch !== false) {
      this.db
        .prepare(`UPDATE entries SET used_at = ? WHERE ${KEY_WHERE}`)
        .run(this.now(), key.connectionId, key.profile, key.sessionId)
    }

    return { activity: row.activity, messageCount: row.message_count, messages, savedAt: row.saved_at }
  }

  /** Metadata only (prefetch freshness checks): no body parse, no LRU touch. */
  meta(key: TranscriptCacheKey): { activity: null | number; messageCount: null | number; savedAt: number } | null {
    const row = this.db
      .prepare(`SELECT saved_at, activity, message_count FROM entries WHERE ${KEY_WHERE}`)
      .get(key.connectionId, key.profile, key.sessionId) as
      { activity: null | number; message_count: null | number; saved_at: number } | undefined

    return row ? { activity: row.activity, messageCount: row.message_count, savedAt: row.saved_at } : null
  }

  /** Store a freshly fetched latest page (chronological rows, newest last). */
  putLatest(key: TranscriptCacheKey, latest: readonly unknown[], meta: TranscriptCacheMeta = {}): boolean {
    if (!Array.isArray(latest) || latest.length === 0) {
      return false
    }

    const cached = this.read(key, { touch: false })?.messages ?? []

    return this.write(key, mergeLatest(cached, latest.slice(-LATEST_ROWS)), meta)
  }

  /** [daveey] The cached run's last server row id (see tailRowId), or null when there is no run or no anchor. */
  tailId(key: TranscriptCacheKey): null | number {
    const current = this.read(key, { touch: false })

    return current ? tailRowId(current.messages) : null
  }

  /**
   * [daveey] Append a push delta: the rows the server holds after `anchorId`. Applies only while the cached run
   * still ends at that row (another write since the delta was asked for, an open's latest page, means the delta
   * no longer lines up: false, and the caller refetches the latest page). The meta moves to the delta's.
   */
  putAfter(key: TranscriptCacheKey, anchorId: number, rows: readonly unknown[], meta: TranscriptCacheMeta = {}): boolean {
    const current = this.read(key, { touch: false })

    if (!current || tailRowId(current.messages) !== anchorId || !Array.isArray(rows)) {
      return false
    }

    const held = new Set(current.messages.map(messageIdentity))
    const fresh = rows.filter(row => !held.has(messageIdentity(row)))

    return this.write(key, [...current.messages, ...fresh], meta)
  }

  /** Store an older page the user loaded ("Show earlier"). Ignored without a cached run to extend. */
  putOlder(key: TranscriptCacheKey, older: readonly unknown[]): boolean {
    const current = this.read(key, { touch: false })

    if (!current || !Array.isArray(older) || older.length === 0) {
      return false
    }

    return this.write(key, mergeOlder(current.messages, older), {
      activity: current.activity,
      messageCount: current.messageCount
    })
  }

  drop(key: TranscriptCacheKey): void {
    this.db.prepare(`DELETE FROM entries WHERE ${KEY_WHERE}`).run(key.connectionId, key.profile, key.sessionId)
  }

  /** Every scope's entry for a deleted stored id. */
  dropEverywhere(sessionId: string): void {
    this.db.prepare('DELETE FROM entries WHERE session_id = ?').run(sessionId)
  }

  /** Profile rename on one connection: re-key its entries. */
  migrateProfile(connectionId: string, from: string, to: string): void {
    if (from === to) {
      return
    }

    this.db
      .prepare('UPDATE OR REPLACE entries SET profile = ? WHERE connection_id = ? AND profile = ?')
      .run(to, connectionId, from)
  }

  clear(): void {
    this.db.exec('DELETE FROM bodies; DELETE FROM entries')
  }

  keys(): TranscriptCacheKey[] {
    return (
      this.db.prepare('SELECT connection_id, profile, session_id FROM entries ORDER BY used_at DESC').all() as {
        connection_id: string
        profile: string
        session_id: string
      }[]
    ).map(row => ({ connectionId: row.connection_id, profile: row.profile, sessionId: row.session_id }))
  }

  stats(): { bytes: number; sessions: number } {
    const row = this.db.prepare('SELECT COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b FROM entries').get() as {
      b: number
      n: number
    }

    return { bytes: Number(row.b), sessions: Number(row.n) }
  }

  getMeta(k: string): null | string {
    const row = this.db.prepare('SELECT v FROM meta WHERE k = ?').get(k) as { v: string } | undefined

    return row?.v ?? null
  }

  setMeta(k: string, v: string): void {
    this.db.prepare('INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v').run(k, v)
  }

  private write(key: TranscriptCacheKey, messages: unknown[], meta: TranscriptCacheMeta): boolean {
    const fitted = fitEntry(messages, this.limits.maxEntryBytes)

    if (!fitted) {
      this.drop(key)

      return false
    }

    const now = this.now()

    this.db.exec('BEGIN')

    try {
      const row = this.db
        .prepare(
          `INSERT INTO entries (connection_id, profile, session_id, bytes, saved_at, used_at, activity, message_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(connection_id, profile, session_id) DO UPDATE SET
             bytes = excluded.bytes, saved_at = excluded.saved_at, used_at = excluded.used_at,
             activity = COALESCE(excluded.activity, entries.activity),
             message_count = COALESCE(excluded.message_count, entries.message_count)
           RETURNING id`
        )
        .get(
          key.connectionId,
          key.profile,
          key.sessionId,
          fitted.bytes,
          now,
          now,
          meta.activity ?? null,
          meta.messageCount ?? null
        ) as { id: number }

      this.db
        .prepare('INSERT INTO bodies (id, body) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET body = excluded.body')
        .run(row.id, fitted.body)
      this.db.exec('COMMIT')
    } catch (error) {
      // SQLITE_FULL/IOERR/NOMEM already rolled back; a second ROLLBACK would mask the real error.
      if (this.db.isTransaction) {
        this.db.exec('ROLLBACK')
      }

      throw error
    }

    this.enforceBounds()

    return true
  }

  /** Evict least recently used entries until both the count and byte bounds hold. */
  enforceBounds(): void {
    let { bytes, sessions } = this.stats()

    if (sessions <= this.limits.maxSessions && bytes <= this.limits.maxTotalBytes) {
      return
    }

    const victims = this.db
      .prepare('SELECT connection_id, profile, session_id, bytes FROM entries ORDER BY used_at ASC')
      .all() as { bytes: number; connection_id: string; profile: string; session_id: string }[]

    for (const victim of victims) {
      if (sessions <= this.limits.maxSessions && bytes <= this.limits.maxTotalBytes) {
        break
      }

      this.drop({ connectionId: victim.connection_id, profile: victim.profile, sessionId: victim.session_id })
      sessions -= 1
      bytes -= Number(victim.bytes)
    }
  }
}
