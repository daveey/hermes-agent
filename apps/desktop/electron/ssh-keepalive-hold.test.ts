/**
 * Retention bound for Desktop main's SSH-isolated keep-alive: only the primary,
 * default, prewarm targets and recently used scopes keep the socket that stops
 * a remote backend idle-exiting. A one-off sweep over every profile of a host
 * must not pin those backends for the whole Desktop session.
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { createRemoteWarmSet } from './remote-warm-set'
import { createSshIsolatedKeepaliveRegistry } from './ssh-isolated-keepalive'
import { ACTIVE_USE_HOLD_MS, SshKeepaliveHolds } from './ssh-keepalive-hold'

function makeFakeWs() {
  const instances: any[] = []

  class FakeWs {
    url: string
    closed = false
    listeners: Record<string, Array<(event?: any) => void>> = {}

    constructor(url: string) {
      this.url = url
      instances.push(this)
    }

    addEventListener(type: string, fn: (event?: any) => void) {
      ;(this.listeners[type] ||= []).push(fn)
    }

    close() {
      this.closed = true
      this.emit('close', { code: 1000 })
    }

    emit(type: string, event?: any) {
      for (const fn of this.listeners[type] || []) {
        fn(event)
      }
    }
  }

  return { FakeWs, instances }
}

const SCOPES = [
  'conn:mini::default',
  'conn:mini::bb-aux',
  'conn:mini::bb-dev',
  'conn:mini::bb-device',
  'conn:mini::bb-voice',
  'conn:hub::work'
]

function setup(overrides: { prewarm?: string[]; primary?: string[]; ssh?: string[] } = {}) {
  const { FakeWs, instances } = makeFakeWs()
  let now = 1_000_000
  const keepalives = createSshIsolatedKeepaliveRegistry({ WebSocketImpl: FakeWs, reconnectDelayMs: 10 })

  SCOPES.forEach((scope, i) => keepalives.start(scope, { baseUrl: `http://127.0.0.1:${50_000 + i}`, token: 't' }))
  const log = vi.fn()

  const holds = new SshKeepaliveHolds({
    keepalives,
    prewarmKeys: () => overrides.prewarm ?? ['mini::default', 'mini::bb-aux', 'hub::default'],
    isPrimaryScope: scope => (overrides.primary ?? ['conn:hub::work']).includes(scope),
    isSshConnection: id => (overrides.ssh ?? ['mini', 'hub']).includes(id),
    log,
    now: () => now
  })

  const openSocket = (scope: string) =>
    instances.filter(ws => !ws.closed && ws.url === `ws://127.0.0.1:${50_000 + SCOPES.indexOf(scope)}/api/ws?token=t`)

  return { advance: (ms: number) => (now += ms), holds, instances, keepalives, log, openSocket }
}

describe('SSH keep-alive retention bound', () => {
  it('releases only scopes outside primary/default/prewarm/recent use, without disarming them', () => {
    const { holds, keepalives, openSocket } = setup()

    holds.reconcile()

    for (const scope of ['conn:mini::default', 'conn:mini::bb-aux', 'conn:hub::work']) {
      expect(keepalives.isHeld(scope)).toBe(true)
      expect(openSocket(scope)).toHaveLength(1)
    }

    for (const scope of ['conn:mini::bb-dev', 'conn:mini::bb-device', 'conn:mini::bb-voice']) {
      expect(keepalives.isHeld(scope)).toBe(false)
      expect(keepalives.isArmed(scope)).toBe(true)
      expect(openSocket(scope)).toHaveLength(0)
    }
  })

  it('a released socket is never redialled until the scope is used again', async () => {
    vi.useFakeTimers()

    try {
      const { holds, instances, keepalives, openSocket } = setup()

      holds.reconcile()
      const dialled = instances.length
      await vi.advanceTimersByTimeAsync(60_000)
      expect(instances).toHaveLength(dialled)

      holds.noteUse('conn:mini::bb-device')
      holds.reconcile()
      expect(keepalives.isHeld('conn:mini::bb-device')).toBe(true)
      expect(openSocket('conn:mini::bb-device')).toHaveLength(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('holds a used scope for ACTIVE_USE_HOLD_MS, then lets it go', () => {
    const { advance, holds, keepalives, log } = setup()

    holds.noteUse('conn:mini::bb-dev')
    holds.reconcile()
    expect(keepalives.isHeld('conn:mini::bb-dev')).toBe(true)

    advance(ACTIVE_USE_HOLD_MS - 1)
    holds.reconcile()
    expect(keepalives.isHeld('conn:mini::bb-dev')).toBe(true)

    advance(2)
    holds.reconcile()
    expect(keepalives.isHeld('conn:mini::bb-dev')).toBe(false)
    expect(log).toHaveBeenCalledWith(expect.stringContaining('keep-alive released for conn:mini::bb-dev'))
  })

  it('a profile entering the prewarm set is held again', () => {
    const prewarm = ['mini::default']
    const { holds, keepalives } = setup({ prewarm })

    holds.reconcile()
    expect(keepalives.isHeld('conn:mini::bb-voice')).toBe(false)

    prewarm.push('mini::bb-voice')
    holds.reconcile()
    expect(keepalives.isHeld('conn:mini::bb-voice')).toBe(true)
  })

  it('never releases bare, local or non-SSH scopes (resume rebuild unchanged for them)', () => {
    const { holds } = setup({ prewarm: [] })

    expect(holds.shouldHold('')).toBe(true)
    expect(holds.shouldHold('work')).toBe(true)
    expect(holds.shouldHold('conn:local::work')).toBe(true)
    expect(holds.shouldHold('conn:https-remote::work')).toBe(true)
    expect(holds.shouldHold('conn:mini::default')).toBe(true)
    expect(holds.shouldHold('conn:mini::bb-shadow')).toBe(false)
  })

  it('a release cancels a pending reconnect; noteUse and a republish re-hold at once', async () => {
    vi.useFakeTimers()

    try {
      const { holds, instances, keepalives, openSocket } = setup()
      const scope = 'conn:mini::bb-device'
      const [socket] = openSocket(scope)

      socket.emit('error', new Error('tunnel blip')) // schedules a reconnect
      holds.reconcile()
      const dialled = instances.length
      await vi.advanceTimersByTimeAsync(60_000)
      expect(instances).toHaveLength(dialled)
      expect(keepalives.openUrl(scope)).toBeNull()

      holds.noteUse(scope) // no reconcile tick needed
      expect(keepalives.isHeld(scope)).toBe(true)
      expect(instances).toHaveLength(dialled + 1)
      expect(keepalives.openUrl(scope)).toBe(instances.at(-1).url)

      keepalives.setHeld('conn:mini::bb-voice', false)
      keepalives.start('conn:mini::bb-voice', { baseUrl: 'http://127.0.0.1:50004', token: 't' })
      expect(keepalives.isHeld('conn:mini::bb-voice')).toBe(true)
      expect(openSocket('conn:mini::bb-voice')).toHaveLength(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('post-resume: retires a dead unheld scope without redialling it; held scopes are redialled', async () => {
    const { holds, log } = setup()
    const redial = vi.fn(async () => {})

    holds.noteUse('conn:mini::bb-dev')

    for (const scope of [
      '',
      'work',
      'conn:local::work',
      'conn:https-remote::work',
      'conn:mini::default',
      'conn:mini::bb-aux',
      'conn:mini::bb-dev',
      'conn:hub::work'
    ]) {
      expect(await holds.rebuildIfHeld(scope, redial)).toBe(true)
    }

    expect(redial).toHaveBeenCalledTimes(8)
    expect(await holds.rebuildIfHeld('conn:mini::bb-device', redial)).toBe(false)
    expect(redial).toHaveBeenCalledTimes(8)
    expect(log).toHaveBeenCalledWith(expect.stringContaining('retired conn:mini::bb-device without rebuild'))
  })

  it('start/stop drive a periodic reconcile', () => {
    const timers: Array<() => void> = []
    const clearTimer = vi.fn()
    const { keepalives } = setup()

    const holds = new SshKeepaliveHolds({
      keepalives,
      prewarmKeys: () => [],
      isPrimaryScope: () => false,
      isSshConnection: () => true,
      setTimer: fn => timers.push(fn),
      clearTimer
    })

    holds.start()
    holds.start()
    expect(timers).toHaveLength(1)
    timers[0]()
    expect(keepalives.isHeld('conn:mini::bb-aux')).toBe(false)
    expect(keepalives.isHeld('conn:mini::default')).toBe(true)
    holds.stop()
    expect(clearTimer).toHaveBeenCalledTimes(1)
  })
})

describe('remote warm set wiring', () => {
  const dirs: string[] = []

  afterEach(() => {
    for (const dir of dirs.splice(0)) {
      fs.rmSync(dir, { force: true, recursive: true })
    }
  })

  function warmSet(recent: Record<string, Record<string, number>>, env: Record<string, string> = {}) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'warm-set-'))
    dirs.push(dir)
    const statePath = path.join(dir, 'remote-prewarm-profiles.json')
    fs.writeFileSync(statePath, JSON.stringify(recent))
    const { FakeWs } = makeFakeWs()
    const keepalives = createSshIsolatedKeepaliveRegistry({ WebSocketImpl: FakeWs })

    for (const scope of ['conn:mini::default', 'conn:mini::bb-aux', 'conn:mini::bb-voice', 'conn:url::work']) {
      keepalives.start(scope, { baseUrl: 'http://127.0.0.1:1', token: 't' })
    }

    const set = createRemoteWarmSet({
      statePath: () => statePath,
      readRegistry: () => ({
        connections: [
          { id: 'mini', kind: 'ssh' },
          { id: 'url', kind: 'remote' }
        ]
      }),
      keepalives,
      isPrimaryScope: () => false,
      log: () => {},
      env
    })

    return { keepalives, set, statePath }
  }

  it('holds exactly the persisted recent profiles + default; a touch joins the set and persists', () => {
    const { keepalives, set, statePath } = warmSet({ mini: { 'bb-aux': Date.now() } })

    expect(set.listTargets()).toEqual([
      { connectionId: 'mini', profile: 'default' },
      { connectionId: 'mini', profile: 'bb-aux' }
    ])
    set.holds.reconcile()
    expect(keepalives.isHeld('conn:mini::default')).toBe(true)
    expect(keepalives.isHeld('conn:mini::bb-aux')).toBe(true)
    expect(keepalives.isHeld('conn:mini::bb-voice')).toBe(false)
    expect(keepalives.isHeld('conn:url::work')).toBe(true)

    set.recent.touch('mini', 'bb-voice')
    set.holds.reconcile()
    expect(keepalives.isHeld('conn:mini::bb-voice')).toBe(true)
    expect(Object.keys(JSON.parse(fs.readFileSync(statePath, 'utf8')).mini)).toContain('bb-voice')
  })

  it('HERMES_DESKTOP_REMOTE_PREWARM=0 empties the warm set but keeps default held', () => {
    const { keepalives, set } = warmSet({ mini: { 'bb-aux': Date.now() } }, { HERMES_DESKTOP_REMOTE_PREWARM: '0' })

    expect(set.listTargets()).toEqual([])
    set.holds.reconcile()
    expect(keepalives.isHeld('conn:mini::default')).toBe(true)
    expect(keepalives.isHeld('conn:mini::bb-aux')).toBe(false)
  })
})
