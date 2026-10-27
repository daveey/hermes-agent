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

const SCHEMA = `
CREATE TABLE IF NOT EXISTS transcripts (
  connection_id TEXT NOT NULL,
  profile TEXT NOT NULL,
  session_id TEXT NOT NULL,
  body TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  saved_at REAL NOT NULL,
  used_at REAL NOT NULL,
  activity REAL,
  message_count INTEGER,
  PRIMARY KEY (connection_id, profile, session_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS transcripts_used ON transcripts(used_at);
CREATE INDEX IF NOT EXISTS transcripts_session ON transcripts(session_id);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
`

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

    this.db = new DatabaseSync(file)
    this.limits = { ...DEFAULT_LIMITS, ...options.limits }
    this.now = options.now ?? Date.now

    this.db.exec('PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL; PRAGMA busy_timeout = 2000;')
    this.db.exec(SCHEMA)

    if (file !== ':memory:') {
      try {
        fs.chmodSync(file, 0o600)
      } catch {
        // best effort: same posture as state.db
      }
    }
  }

  close(): void {
    this.db.close()
  }

  read(key: TranscriptCacheKey, options: { touch?: boolean } = {}): CachedTranscript | null {
    const row = this.db
      .prepare(
        'SELECT body, saved_at, activity, message_count FROM transcripts WHERE connection_id = ? AND profile = ? AND session_id = ?'
      )
      .get(key.connectionId, key.profile, key.sessionId) as
      | { activity: null | number; body: string; message_count: null | number; saved_at: number }
      | undefined

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
        .prepare('UPDATE transcripts SET used_at = ? WHERE connection_id = ? AND profile = ? AND session_id = ?')
        .run(this.now(), key.connectionId, key.profile, key.sessionId)
    }

    return { activity: row.activity, messageCount: row.message_count, messages, savedAt: row.saved_at }
  }

  /** Metadata only (prefetch freshness checks): no body parse, no LRU touch. */
  meta(key: TranscriptCacheKey): { activity: null | number; messageCount: null | number; savedAt: number } | null {
    const row = this.db
      .prepare(
        'SELECT saved_at, activity, message_count FROM transcripts WHERE connection_id = ? AND profile = ? AND session_id = ?'
      )
      .get(key.connectionId, key.profile, key.sessionId) as
      | { activity: null | number; message_count: null | number; saved_at: number }
      | undefined

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
    this.db
      .prepare('DELETE FROM transcripts WHERE connection_id = ? AND profile = ? AND session_id = ?')
      .run(key.connectionId, key.profile, key.sessionId)
  }

  /** Every scope's entry for a deleted stored id. */
  dropEverywhere(sessionId: string): void {
    this.db.prepare('DELETE FROM transcripts WHERE session_id = ?').run(sessionId)
  }

  /** Profile rename on one connection: re-key its entries. */
  migrateProfile(connectionId: string, from: string, to: string): void {
    if (from === to) {
      return
    }

    this.db
      .prepare(
        'UPDATE OR REPLACE transcripts SET profile = ? WHERE connection_id = ? AND profile = ?'
      )
      .run(to, connectionId, from)
  }

  clear(): void {
    this.db.exec('DELETE FROM transcripts')
  }

  keys(): TranscriptCacheKey[] {
    return (
      this.db
        .prepare('SELECT connection_id, profile, session_id FROM transcripts ORDER BY used_at DESC')
        .all() as { connection_id: string; profile: string; session_id: string }[]
    ).map(row => ({ connectionId: row.connection_id, profile: row.profile, sessionId: row.session_id }))
  }

  stats(): { bytes: number; sessions: number } {
    const row = this.db.prepare('SELECT COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b FROM transcripts').get() as {
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

    this.db
      .prepare(
        `INSERT INTO transcripts (connection_id, profile, session_id, body, bytes, saved_at, used_at, activity, message_count)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(connection_id, profile, session_id) DO UPDATE SET
           body = excluded.body, bytes = excluded.bytes, saved_at = excluded.saved_at, used_at = excluded.used_at,
           activity = COALESCE(excluded.activity, transcripts.activity),
           message_count = COALESCE(excluded.message_count, transcripts.message_count)`
      )
      .run(
        key.connectionId,
        key.profile,
        key.sessionId,
        fitted.body,
        fitted.bytes,
        now,
        now,
        meta.activity ?? null,
        meta.messageCount ?? null
      )

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
      .prepare('SELECT connection_id, profile, session_id, bytes FROM transcripts ORDER BY used_at ASC')
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
