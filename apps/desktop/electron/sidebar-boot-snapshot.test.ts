import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  hubDbPath,
  installSidebarBootSnapshot,
  readHubSessions,
  readTreeSnapshot,
  writeTreeSnapshot
} from './sidebar-boot-snapshot'

// [daveey] Sidebar boot snapshot: Electron main reads hub.db the way hub-sync's hubview.py `sessions()` does, so the
// first window can paint every gateway's rows (this one's included) before the local backend is up.

// The hermes-hub client's tables this reader touches (hermes_hub.SCHEMA).
const SCHEMA = `
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE published (coll TEXT, key TEXT, sha TEXT, data TEXT, version INTEGER, seq INTEGER, PRIMARY KEY (coll, key));
CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, version INTEGER, seq INTEGER, data TEXT, updated_at INTEGER,
  PRIMARY KEY (gw, coll, key));
CREATE TABLE sent (id TEXT PRIMARY KEY, body TEXT, state TEXT, result TEXT, created_at REAL, done_at REAL, posted INTEGER DEFAULT 0);
CREATE TABLE overrides (gw TEXT, coll TEXT, key TEXT, field TEXT, value TEXT, hlc TEXT, by_gw TEXT, seq INTEGER,
  PRIMARY KEY (gw, coll, key, field));
`

const NOW = 1_800_000_000

let dir: string
let dbFile: string

function buildDb(opts: { lastOk?: number; shared?: string } = {}) {
  const db = new DatabaseSync(dbFile)
  db.exec(SCHEMA)
  const meta = db.prepare('INSERT INTO meta (k, v) VALUES (?, ?)')
  meta.run('gateway', 'macbook')
  meta.run('last_ok', String(opts.lastOk ?? NOW - 5))

  if (opts.shared) {
    meta.run('shared_fields', opts.shared)
  }

  const pub = db.prepare("INSERT INTO published (coll, key, data, version) VALUES ('sessions', ?, ?, ?)")
  pub.run(
    'default/s_own',
    JSON.stringify({ activity: NOW - 10, id: 's_own', profile: 'default', title: 'Own chat' }),
    3
  )
  pub.run('default/s_arch', JSON.stringify({ activity: NOW, archived: true, id: 's_arch', title: 'Archived' }), 1)
  pub.run('default/s_bad', 'not json', 1)

  const rep = db.prepare(
    "INSERT INTO replica (gw, coll, key, version, data, updated_at) VALUES (?, 'sessions', ?, ?, ?, ?)"
  )
  rep.run(
    'mini',
    'default/s_mini',
    7,
    JSON.stringify({ activity: NOW - 1, id: 's_mini', title: 'Mini chat' }),
    (NOW - 60) * 1000
  )
  rep.run(
    'hub',
    'default/s_hub',
    2,
    JSON.stringify({ activity: NOW - 100, id: 's_hub', title: 'Hub chat' }),
    (NOW - 600) * 1000
  )
  // A replica copy of this gateway's own row is never shown twice.
  rep.run('macbook', 'default/s_own', 3, JSON.stringify({ id: 's_own', title: 'Own chat (echo)' }), NOW * 1000)

  db.prepare("INSERT INTO sent (id, body, state, created_at) VALUES ('r1', ?, 'queued', ?)").run(
    JSON.stringify({ action: 'pin', args: {}, coll: 'sessions', key: 'default/s_mini', to_gw: 'mini' }),
    NOW - 3
  )
  db.prepare("INSERT INTO sent (id, body, state, created_at) VALUES ('r2', ?, 'done', ?)").run(
    JSON.stringify({ action: 'pin', coll: 'sessions', key: 'default/s_hub', to_gw: 'hub' }),
    NOW - 3
  )
  db.prepare(
    "INSERT INTO overrides (gw, coll, key, field, value, hlc, by_gw, seq) VALUES ('mini', 'sessions', 'default/s_mini', 'title', ?, 'h', 'macbook', 1)"
  ).run(JSON.stringify('Renamed on the hub'))
  db.close()
}

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'boot-snapshot-'))
  dbFile = path.join(dir, 'hub.db')
})

afterEach(() => {
  fs.rmSync(dir, { force: true, recursive: true })
})

describe('readHubSessions', () => {
  it('returns own (published) and other gateways (replica) rows, newest activity first, archived and torn rows dropped', () => {
    buildDb()
    const snap = readHubSessions(dbFile, NOW)!

    expect(snap.gateway).toBe('macbook')
    expect(snap.stale).toBe(false)
    expect(snap.sessions.map(r => [r.gateway, r.id, r.local])).toEqual([
      ['mini', 's_mini', false],
      ['macbook', 's_own', true],
      ['hub', 's_hub', false]
    ])
  })

  it('stamps synced_at like hubview: now for own rows, min(owner publish, last pull) for replica rows', () => {
    buildDb({ lastOk: NOW - 5 })
    const byId = Object.fromEntries(readHubSessions(dbFile, NOW)!.sessions.map(r => [r.id, r]))

    expect(byId.s_own.synced_at).toBe(NOW)
    expect(byId.s_mini.synced_at).toBe(NOW - 60)
    expect(byId.s_hub.synced_at).toBe(NOW - 600)
    expect(byId.s_mini.updated_at_hub).toBe(NOW - 60)
  })

  it('marks every replica row stale when the last pull is older than the stale window', () => {
    buildDb({ lastOk: NOW - 1000 })
    const snap = readHubSessions(dbFile, NOW)!

    expect(snap.stale).toBe(true)
    expect(snap.sessions.filter(r => !r.local).every(r => r.stale === true)).toBe(true)
    expect(snap.sessions.find(r => r.local)!.stale).toBe(false)
  })

  it('attaches pending (local/queued) requests to their row', () => {
    buildDb()
    const byId = Object.fromEntries(readHubSessions(dbFile, NOW)!.sessions.map(r => [r.id, r]))

    expect(byId.s_mini.pending).toEqual([{ action: 'pin', args: {}, created_at: NOW - 3, id: 'r1', state: 'queued' }])
    expect(byId.s_hub.pending).toEqual([]) // its request is done
  })

  it('applies shared-field overrides only in primary mode, marking a value the owner has not applied', () => {
    buildDb({ shared: JSON.stringify({ sessions: 'primary' }) })
    const primary = readHubSessions(dbFile, NOW)!.sessions.find(r => r.id === 's_mini')!

    expect(primary.title).toBe('Renamed on the hub')
    expect(primary.shared).toEqual({ title: { by: 'macbook', owner_value: 'Mini chat' } })

    fs.rmSync(dbFile)
    buildDb({ shared: JSON.stringify('shadow') })
    const shadow = readHubSessions(dbFile, NOW)!.sessions.find(r => r.id === 's_mini')!

    expect(shadow.title).toBe('Mini chat')
    expect(shadow.shared).toBeUndefined()
  })

  it('answers null for a missing db and never creates one', () => {
    expect(readHubSessions(dbFile, NOW)).toBeNull()
    expect(fs.existsSync(dbFile)).toBe(false)
  })

  it('caps the row count', () => {
    buildDb()

    expect(readHubSessions(dbFile, NOW, 1)!.sessions.map(r => r.id)).toEqual(['s_mini'])
  })
})

describe('hubDbPath', () => {
  it('follows HERMES_HUB_STATE like hubview, else ~/.local/state/hermes-hub', () => {
    expect(hubDbPath({}, '/home/me')).toBe('/home/me/.local/state/hermes-hub/hub.db')
    expect(hubDbPath({ HERMES_HUB_STATE: '~/hub' }, '/home/me')).toBe('/home/me/hub/hub.db')
  })
})

describe('tree snapshot file', () => {
  it('round-trips and rejects a torn or foreign file', () => {
    const file = path.join(dir, 'tree.json')
    writeTreeSnapshot(file, { activeProjectId: 'p_1', connectionId: 'local', tree: [{ id: 'p_1' }] }, 123)

    expect(readTreeSnapshot(file)).toEqual({
      activeProjectId: 'p_1',
      connectionId: 'local',
      savedAt: 123,
      tree: [{ id: 'p_1' }]
    })

    fs.writeFileSync(file, '{"v":1,"tree":')
    expect(readTreeSnapshot(file)).toBeNull()
    fs.writeFileSync(file, JSON.stringify({ connectionId: 'local', tree: [], v: 99 }))
    expect(readTreeSnapshot(file)).toBeNull()
    expect(readTreeSnapshot(path.join(dir, 'missing.json'))).toBeNull()
  })
})

describe('installSidebarBootSnapshot IPC', () => {
  function install() {
    const handlers = new Map<string, (event: any, payload?: any) => void>()
    const ipcMain = { on: vi.fn((channel: string, fn: any) => void handlers.set(channel, fn)) }
    installSidebarBootSnapshot({
      appStartMs: 42,
      hubDb: dbFile,
      ipcMain: ipcMain as any,
      log: () => {},
      userDataDir: dir
    })

    return handlers
  }

  it('read (sendSync) returns the process start, hub rows and the saved tree', () => {
    buildDb()
    const handlers = install()
    handlers.get('hermes:boot-snapshot:save-tree')!(
      {},
      { activeProjectId: null, connectionId: 'local', tree: [{ id: 'p' }] }
    )
    const event: { returnValue?: any } = {}
    handlers.get('hermes:boot-snapshot:read')!(event)

    expect(event.returnValue.appStartMs).toBe(42)
    expect(event.returnValue.hub.gateway).toBe('macbook')
    expect(event.returnValue.hub.sessions.length).toBe(3)
    expect(event.returnValue.tree.tree).toEqual([{ id: 'p' }])
    expect(typeof event.returnValue.readMs).toBe('number')
  })

  it('read without hub.db or a saved tree still answers (nothing to paint)', () => {
    const handlers = install()
    const event: { returnValue?: any } = {}
    handlers.get('hermes:boot-snapshot:read')!(event)

    expect(event.returnValue).toMatchObject({ appStartMs: 42, hub: null, tree: null })
  })

  it('save-tree ignores a malformed payload', () => {
    const handlers = install()
    handlers.get('hermes:boot-snapshot:save-tree')!({}, { tree: 'nope' })
    handlers.get('hermes:boot-snapshot:save-tree')!({}, null)

    expect(fs.existsSync(path.join(dir, 'sidebar-boot-snapshot.json'))).toBe(false)
  })
})
