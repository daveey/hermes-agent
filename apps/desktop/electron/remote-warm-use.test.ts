/**
 * Only real use stamps the remote warm set. A post-reconnect sweep (every pooled hub-profile socket redialing in
 * the same millisecond, roster and session-list reads) once ran ensureRegistryBackend non-passive for every listed
 * profile, and each such dial touched the cap-4 prewarm recent set and took a 30-min keep-alive hold: four hub
 * profiles ended up stamped with one timestamp, and a profile nobody dispatched to was respawned and kept warm.
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { createRemoteWarmSet, registryDialCountsAsUse } from './remote-warm-set'
import { createSshIsolatedKeepaliveRegistry } from './ssh-isolated-keepalive'

const HUB_PROFILES = ['bb-tuner', 'coach', 'research', 'alpha', 'designer', 'ops']
const dirs: string[] = []

afterEach(() => {
  for (const dir of dirs.splice(0)) {
    fs.rmSync(dir, { force: true, recursive: true })
  }
})

function warmSet(recent: Record<string, Record<string, number>>) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'remote-warm-use-'))
  dirs.push(dir)
  const statePath = path.join(dir, 'remote-prewarm-profiles.json')
  fs.writeFileSync(statePath, JSON.stringify(recent))
  const keepalives = createSshIsolatedKeepaliveRegistry({ WebSocketImpl: class {} as any, reconnectDelayMs: 10 })
  const setHeld = vi.spyOn(keepalives, 'setHeld')

  const set = createRemoteWarmSet({
    statePath: () => statePath,
    readRegistry: () => ({
      connections: [
        { id: 'hermes-hub', kind: 'ssh' },
        { id: 'local', kind: 'local' }
      ]
    }),
    keepalives,
    isPrimaryScope: () => false,
    log: () => {}
  })

  return { set, setHeld, stateBytes: () => fs.readFileSync(statePath, 'utf8') }
}

describe('remote warm set: what counts as use', () => {
  it('a reconnect sweep of N profiles touches neither the prewarm recent set nor the keep-alive hold', () => {
    const stamped = Date.now() - 60 * 60_000
    const { set, setHeld, stateBytes } = warmSet({ 'hermes-hub': { research: stamped } })
    const before = stateBytes()

    // What ensureRegistryBackend passes for a socket (re)connect, a ws-url mint, a roster/session-list read
    // (no flag), a passive read and the prewarm probe (forceProbe).
    for (const profile of HUB_PROFILES) {
      expect(set.noteDial('hermes-hub', profile, {})).toBe(false)
      expect(set.noteDial('hermes-hub', profile, { passive: true })).toBe(false)
      expect(set.noteDial('hermes-hub', profile, { forceProbe: true })).toBe(false)
    }

    expect(stateBytes()).toBe(before)
    expect(setHeld).not.toHaveBeenCalled()
    expect(set.listTargets()).toEqual([
      { connectionId: 'hermes-hub', profile: 'default' },
      { connectionId: 'hermes-hub', profile: 'research' }
    ])
    expect(set.holds.shouldHold('conn:hermes-hub::bb-tuner')).toBe(false)
  })

  it('a real dispatch (renderer note-use, or a dial flagged use) stamps the recent set and holds the scope', () => {
    const { set, setHeld, stateBytes } = warmSet({})

    expect(set.noteUse('hermes-hub', 'bb-tuner')).toBe(true)
    expect(JSON.parse(stateBytes())['hermes-hub']).toHaveProperty('bb-tuner')
    expect(setHeld).toHaveBeenCalledWith('conn:hermes-hub::bb-tuner', true)
    expect(set.holds.shouldHold('conn:hermes-hub::bb-tuner')).toBe(true)

    expect(set.noteDial('hermes-hub', 'coach', { use: true })).toBe(true)
    expect(JSON.parse(stateBytes())['hermes-hub']).toHaveProperty('coach')
    expect(setHeld).toHaveBeenCalledWith('conn:hermes-hub::coach', true)
  })

  it('note-use is a no-op for a non-SSH or unknown connection and an invalid profile name', () => {
    const { set, setHeld, stateBytes } = warmSet({})
    const before = stateBytes()

    expect(set.noteUse('local', 'research')).toBe(false)
    expect(set.noteDial('local', 'research', { use: true })).toBe(false)
    expect(set.noteUse('nope', 'research')).toBe(false)
    expect(set.noteUse('hermes-hub', 'default@hermes-hub')).toBe(false)
    expect(set.noteUse('', 'research')).toBe(false)

    expect(stateBytes()).toBe(before)
    expect(setHeld).not.toHaveBeenCalled()
  })

  it.each(['note-use', 'use dial'])('%s accepts only dialable lowercase profile names', entry => {
    const { set, setHeld, stateBytes } = warmSet({})
    const touch = vi.spyOn(set.recent, 'touch')
    const noteUse = vi.spyOn(set.holds, 'noteUse')
    const before = stateBytes()

    const report = (profile: string) =>
      entry === 'note-use' ? set.noteUse('hermes-hub', profile) : set.noteDial('hermes-hub', profile, { use: true })

    expect(report('Research')).toBe(false)
    expect(report('reSearch')).toBe(false)
    expect(touch).not.toHaveBeenCalled()
    expect(noteUse).not.toHaveBeenCalled()
    expect(setHeld).not.toHaveBeenCalled()
    expect(stateBytes()).toBe(before)

    expect(report('research')).toBe(true)
    expect(touch).toHaveBeenCalledExactlyOnceWith('hermes-hub', 'research')
    expect(noteUse).toHaveBeenCalledExactlyOnceWith('conn:hermes-hub::research')
    expect(setHeld).toHaveBeenCalledWith('conn:hermes-hub::research', true)
    expect(JSON.parse(stateBytes())['hermes-hub']).toHaveProperty('research')
  })

  it('registryDialCountsAsUse: only an explicit use flag on a non-passive, non-probe dial', () => {
    expect(registryDialCountsAsUse()).toBe(false)
    expect(registryDialCountsAsUse({})).toBe(false)
    expect(registryDialCountsAsUse({ use: true })).toBe(true)
    expect(registryDialCountsAsUse({ use: true, passive: true })).toBe(false)
    expect(registryDialCountsAsUse({ use: true, forceProbe: true })).toBe(false)
  })
})

describe('main.ts wiring', () => {
  const main = fs.readFileSync(path.join(__dirname, 'main.ts'), 'utf8')

  it('ensureRegistryBackend stamps only through noteDial (no unconditional touch / noteUse left)', () => {
    expect(main).toContain('remoteWarmSet.noteDial(id, profileKey, opts)')
    expect(main).not.toMatch(/remoteWarmSet\.recent\.touch\(/)
    expect(main).not.toMatch(/remoteWarmSet\.holds\.noteUse\(/)
  })

  it('the renderer note-use door is wired to the warm set', () => {
    expect(main).toContain(
      "ipcMain.handle('hermes:remote:note-use', (_e, p) => remoteWarmSet.noteUse(p?.connectionId, p?.profile))"
    )
  })

  it('socket dials, ws-url mints and REST reads pass no use flag', () => {
    expect(main).toMatch(
      /const connection = await ensureRegistryBackend\(payload\?\.connectionId, payload\?\.profile\)\n/
    )
    expect(main).toContain('ensureRegistryBackend(registryConnectionId, routeProfile)\n')
    expect(main).toMatch(
      /route\.connectionId\s*\? ensureRegistryBackend\(route\.connectionId, route\.profile\)\s*: ensureBackend\(route\.profile, \{ inheritsPrimary \}\)/
    )
  })
})
