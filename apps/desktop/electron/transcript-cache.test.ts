import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'

import { afterEach, describe, expect, it } from 'vitest'

import { LATEST_ROWS, mergeLatest, mergeOlder, TranscriptCacheStore } from './transcript-cache'

const rows = (from: number, to: number) =>
  Array.from({ length: to - from + 1 }, (_, index) => ({
    content: `m${from + index}`,
    id: from + index,
    role: 'user'
  }))

const key = (sessionId: string, connectionId = 'mac-mini', profile = 'default') => ({
  connectionId,
  profile,
  sessionId
})

const dirs: string[] = []

function tempFile() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'transcript-cache-'))
  dirs.push(dir)

  return path.join(dir, 'transcript-cache.sqlite')
}

afterEach(() => {
  for (const dir of dirs.splice(0)) {
    fs.rmSync(dir, { force: true, recursive: true })
  }
})

describe('transcript cache: write and read', () => {
  it('round-trips the latest rows on disk, scoped by connection and profile', () => {
    const file = tempFile()
    const store = new TranscriptCacheStore(file)

    expect(store.putLatest(key('s1'), rows(1, 3), { activity: 10, messageCount: 3 })).toBe(true)
    store.close()

    const reopened = new TranscriptCacheStore(file)
    const hit = reopened.read(key('s1'))

    expect(hit?.messages).toEqual(rows(1, 3))
    expect(hit?.activity).toBe(10)
    expect(hit?.messageCount).toBe(3)
    // Same stored id under another gateway or profile is a different session.
    expect(reopened.read(key('s1', 'hermes-hub'))).toBeNull()
    expect(reopened.read(key('s1', 'mac-mini', 'alpha'))).toBeNull()
    expect((fs.statSync(file).mode & 0o777).toString(8)).toBe('600')
  })

  it('keeps only the latest LATEST_ROWS of a page', () => {
    const store = new TranscriptCacheStore(':memory:')

    store.putLatest(key('s1'), rows(1, LATEST_ROWS + 50))

    const hit = store.read(key('s1'))!
    expect(hit.messages).toHaveLength(LATEST_ROWS)
    expect(hit.messages[0]).toMatchObject({ id: 51 })
  })

  it('keeps loaded older pages when the next latest page overlaps, and drops them across a gap', () => {
    const store = new TranscriptCacheStore(':memory:')

    store.putLatest(key('s1'), rows(101, 300))
    store.putOlder(key('s1'), rows(1, 100))
    expect(store.read(key('s1'))!.messages).toHaveLength(300)

    // Ten new rows: the new latest page starts inside the cached run.
    store.putLatest(key('s1'), rows(111, 310))
    const grown = store.read(key('s1'))!.messages
    expect(grown).toHaveLength(310)
    expect(grown[0]).toMatchObject({ id: 1 })
    expect(grown.at(-1)).toMatchObject({ id: 310 })

    // The session outgrew the window between fetches: no stitching across the gap.
    store.putLatest(key('s1'), rows(601, 800))
    expect(store.read(key('s1'))!.messages[0]).toMatchObject({ id: 601 })
  })

  it('ignores an older page with nothing to extend and dedupes overlapping rows', () => {
    expect(mergeOlder([], rows(1, 3))).toEqual([])
    expect(mergeOlder(rows(3, 5), rows(1, 4)).map(row => (row as { id: number }).id)).toEqual([1, 2, 3, 4, 5])
    expect(mergeLatest(rows(1, 5), rows(1, 6))).toEqual(rows(1, 6))
  })

  it('drops one scope, every scope of a deleted id, and re-keys a renamed profile', () => {
    const store = new TranscriptCacheStore(':memory:')

    store.putLatest(key('s1'), rows(1, 2))
    store.putLatest(key('s1', 'hermes-hub'), rows(1, 2))
    store.putLatest(key('s2', 'local', 'old'), rows(1, 2))

    store.drop(key('s1'))
    expect(store.read(key('s1'))).toBeNull()
    expect(store.read(key('s1', 'hermes-hub'))).not.toBeNull()

    store.dropEverywhere('s1')
    expect(store.read(key('s1', 'hermes-hub'))).toBeNull()

    store.migrateProfile('local', 'old', 'new')
    expect(store.read(key('s2', 'local', 'new'))?.messages).toEqual(rows(1, 2))
    expect(store.read(key('s2', 'local', 'old'))).toBeNull()
  })

  it('self-evicts a corrupt entry', () => {
    const file = tempFile()
    const store = new TranscriptCacheStore(file)

    store.putLatest(key('s1'), rows(1, 2))
    store.close()

    const raw = new DatabaseSync(file)
    raw.prepare("UPDATE bodies SET body = '{not json'").run()
    raw.close()

    const reopened = new TranscriptCacheStore(file)
    expect(reopened.read(key('s1'))).toBeNull()
    expect(reopened.stats().sessions).toBe(0)
  })
})

describe('transcript cache: LRU and size bounds', () => {
  it('evicts the least recently used session past the session bound', () => {
    let clock = 0
    const store = new TranscriptCacheStore(':memory:', { limits: { maxSessions: 3 }, now: () => (clock += 1) })

    store.putLatest(key('a'), rows(1, 2))
    store.putLatest(key('b'), rows(1, 2))
    store.putLatest(key('c'), rows(1, 2))
    store.read(key('a')) // a is now the most recently used
    store.putLatest(key('d'), rows(1, 2))

    expect(
      store
        .keys()
        .map(entry => entry.sessionId)
        .sort()
    ).toEqual(['a', 'c', 'd'])
  })

  it('evicts by total bytes', () => {
    let clock = 0
    const store = new TranscriptCacheStore(':memory:', { limits: { maxTotalBytes: 2500 }, now: () => (clock += 1) })
    const big = (sessionId: string) => store.putLatest(key(sessionId), [{ content: 'x'.repeat(1000), id: 1 }])

    big('a')
    big('b')
    big('c')

    expect(store.stats().bytes).toBeLessThanOrEqual(2500)
    expect(store.keys().map(entry => entry.sessionId)).toEqual(['c', 'b'])
  })

  it('trims the oldest rows of an oversized entry instead of dropping its latest rows', () => {
    const store = new TranscriptCacheStore(':memory:', { limits: { maxEntryBytes: 600 } })
    const page = Array.from({ length: 20 }, (_, index) => ({ content: 'y'.repeat(50), id: index + 1 }))

    store.putLatest(key('s1'), page)

    const hit = store.read(key('s1'))!
    expect(hit.messages.length).toBeGreaterThan(0)
    expect(hit.messages.length).toBeLessThan(20)
    expect(hit.messages.at(-1)).toMatchObject({ id: 20 })
    expect(store.stats().bytes).toBeLessThanOrEqual(600)
  })

  it('refuses a single row larger than the entry bound', () => {
    const store = new TranscriptCacheStore(':memory:', { limits: { maxEntryBytes: 100 } })

    expect(store.putLatest(key('s1'), [{ content: 'z'.repeat(500), id: 1 }])).toBe(false)
    expect(store.read(key('s1'))).toBeNull()
  })
})

const bodyRows = (file: string) => {
  const raw = new DatabaseSync(file)
  const n = (raw.prepare('SELECT COUNT(*) AS n FROM bodies').get() as { n: number }).n
  raw.close()

  return Number(n)
}

describe('transcript cache: metadata never reads bodies', () => {
  it('keeps the body out of the metadata table that meta(), stats() and eviction scan', () => {
    const file = tempFile()
    new TranscriptCacheStore(file).close()

    const raw = new DatabaseSync(file)

    const columns = (table: string) =>
      (raw.prepare(`PRAGMA table_info(${table})`).all() as { name: string }[]).map(column => column.name)

    expect(columns('entries')).not.toContain('body')
    expect(columns('entries')).toEqual(
      expect.arrayContaining(['bytes', 'saved_at', 'used_at', 'activity', 'message_count'])
    )
    expect(columns('bodies')).toEqual(['id', 'body'])
    raw.close()
  })

  it('answers meta() and stats() for multi-MB entries without decoding their bodies', () => {
    const file = tempFile()
    const store = new TranscriptCacheStore(file)
    const huge = [{ content: 'q'.repeat(4 * 1024 * 1024), id: 1 }]

    store.putLatest(key('big'), huge, { activity: 5, messageCount: 1 })
    store.close()

    // Corrupt the body in place: a metadata read that decoded it would self-evict the entry.
    const raw = new DatabaseSync(file)
    raw.prepare("UPDATE bodies SET body = '{not json'").run()
    raw.close()

    const reopened = new TranscriptCacheStore(file)
    expect(reopened.meta(key('big'))).toMatchObject({ activity: 5, messageCount: 1 })
    expect(reopened.stats().sessions).toBe(1)
    expect(reopened.stats().bytes).toBeGreaterThan(4 * 1024 * 1024)
  })

  it('leaves no orphan bodies after drop, dropEverywhere, eviction, re-key over an existing scope and clear', () => {
    let clock = 0
    const file = tempFile()
    const store = new TranscriptCacheStore(file, { limits: { maxSessions: 3 }, now: () => (clock += 1) })

    for (const id of ['a', 'b', 'c', 'd']) {
      store.putLatest(key(id), rows(1, 2))
    }

    expect(bodyRows(file)).toBe(3) // 'a' evicted with its body

    store.drop(key('b'))
    expect(bodyRows(file)).toBe(2)

    store.putLatest(key('c', 'local', 'old'), rows(1, 2))
    store.putLatest(key('c', 'local', 'new'), rows(5, 6))
    store.migrateProfile('local', 'old', 'new') // REPLACE deletes the 'new' row it collides with
    expect(store.read(key('c', 'local', 'new'))?.messages).toEqual(rows(1, 2))
    expect(bodyRows(file)).toBe(store.stats().sessions)

    store.dropEverywhere('c')
    expect(bodyRows(file)).toBe(store.stats().sessions)

    store.clear()
    expect(bodyRows(file)).toBe(0)
    expect(store.stats().sessions).toBe(0)
  })

  it('discards a legacy inline-body cache file and starts empty and writable', () => {
    const file = tempFile()
    const legacy = new DatabaseSync(file)
    legacy.exec(`CREATE TABLE transcripts (
      connection_id TEXT NOT NULL, profile TEXT NOT NULL, session_id TEXT NOT NULL, body TEXT NOT NULL,
      bytes INTEGER NOT NULL, saved_at REAL NOT NULL, used_at REAL NOT NULL, activity REAL, message_count INTEGER,
      PRIMARY KEY (connection_id, profile, session_id)) WITHOUT ROWID`)
    legacy.prepare("INSERT INTO transcripts VALUES ('mac-mini', 'default', 's1', '[]', 2, 1, 1, NULL, NULL)").run()
    legacy.close()

    const store = new TranscriptCacheStore(file)
    expect(store.stats().sessions).toBe(0)
    expect(store.putLatest(key('s1'), rows(1, 2))).toBe(true)
    expect(store.read(key('s1'))?.messages).toEqual(rows(1, 2))
    store.close()

    const raw = new DatabaseSync(file)
    expect(raw.prepare("SELECT 1 FROM sqlite_master WHERE name = 'transcripts'").get()).toBeUndefined()
    raw.close()
    expect((fs.statSync(file).mode & 0o777).toString(8)).toBe('600')
  })

  it('surfaces the real SQLite error when a write fails after SQLite rolled back itself', () => {
    const file = tempFile()
    const store = new TranscriptCacheStore(file)
    const raw = new DatabaseSync(file)
    const pages = Number((raw.prepare('PRAGMA page_count').get() as { page_count: number }).page_count)
    raw.close()
    ;(store as unknown as { db: DatabaseSync }).db.exec(`PRAGMA max_page_count = ${pages + 2}`)

    expect(() => store.putLatest(key('full'), [{ content: 'f'.repeat(1024 * 1024), id: 1 }])).toThrow(/full/i)
    expect(store.meta(key('full'))).toBeNull()
  })
})
