import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it, vi } from 'vitest'

import {
  dispatchThroughRemoteDownGate,
  isRemoteBackendDownError,
  isRemoteTransportFailure,
  REMOTE_DOWN_INITIAL_BACKOFF_MS,
  REMOTE_DOWN_MAX_BACKOFF_MS,
  RemoteDownGate
} from './remote-down-gate'

// 2026-10-28: a hub/mini `hermes update` restarts the remote serve backend; the Desktop kept dialing the dead tunnel
// and every renderer poll burned a 4-10 s "Timed out connecting to Hermes backend". While a pooled remote is known
// down its requests must fail fast, with one backoff trial at a time reopening the gate.

const KEY = 'conn:hermes-hub::default'
const BASE_URL = 'http://127.0.0.1:62511'

function reset(): Error {
  return Object.assign(new Error('read ECONNRESET'), { code: 'ECONNRESET' })
}

function clock(start = 1_000_000) {
  let now = start

  return {
    now: () => now,
    advance: (ms: number) => {
      now += ms
    }
  }
}

function harness() {
  const time = clock()
  const gate = new RemoteDownGate(time.now)
  const connection = { baseUrl: BASE_URL }
  const connect = vi.fn(async () => connection)
  const forgetAnswer = vi.fn()
  const log = vi.fn()

  const run = (request: () => Promise<unknown>) =>
    dispatchThroughRemoteDownGate({ gate, key: KEY, connect, request, forgetAnswer, log })

  return { connect, forgetAnswer, gate, log, run, time }
}

describe('remote down gate', () => {
  it('fails requests fast after the remote resets, without dialing or waiting for a timeout', async () => {
    const h = harness()

    await expect(h.run(async () => Promise.reject(reset()))).rejects.toThrow('ECONNRESET')
    expect(h.forgetAnswer).toHaveBeenCalledWith(BASE_URL)
    expect(h.gate.isDown(KEY)).toBe(true)
    expect(h.log).toHaveBeenCalledWith(expect.stringContaining(`[remote-down] ${KEY} marked down`))

    const request = vi.fn(async () => ({ ok: true }))
    h.connect.mockClear()

    const startedAt = performance.now()
    const error = await h.run(request).catch(e => e)

    expect(isRemoteBackendDownError(error)).toBe(true)
    expect(String(error.message)).toContain(KEY)
    expect(performance.now() - startedAt).toBeLessThan(50)
    expect(h.connect).not.toHaveBeenCalled()
    expect(request).not.toHaveBeenCalled()
  })

  it('lets one trial through after the backoff; its answer reopens the gate', async () => {
    const h = harness()

    await h.run(async () => Promise.reject(reset())).catch(() => {})
    h.time.advance(REMOTE_DOWN_INITIAL_BACKOFF_MS)

    let finishTrial: (value: unknown) => void = () => {}
    const trial = h.run(() => new Promise(resolve => (finishTrial = resolve)))

    // While the trial (which may be a full SSH redial) is in flight, everything else still fails fast.
    await vi.waitFor(() => expect(h.connect).toHaveBeenCalledTimes(2))
    await expect(h.run(async () => 'other')).rejects.toSatisfy(isRemoteBackendDownError)

    finishTrial({ ok: true })
    await expect(trial).resolves.toEqual({ ok: true })
    expect(h.gate.isDown(KEY)).toBe(false)
    await expect(h.run(async () => 'after')).resolves.toBe('after')
  })

  it('a failed trial doubles the backoff, capped', async () => {
    const h = harness()

    await h.run(async () => Promise.reject(reset())).catch(() => {})
    h.time.advance(REMOTE_DOWN_INITIAL_BACKOFF_MS)
    h.connect.mockRejectedValueOnce(new Error('ssh: connect to host 100.104.201.88 port 22: Operation timed out'))
    await expect(h.run(async () => 'x')).rejects.toThrow('ssh: connect')

    // Second failure: 2x backoff. Halfway through it the gate is still shut.
    h.time.advance(REMOTE_DOWN_INITIAL_BACKOFF_MS)
    await expect(h.run(async () => 'x')).rejects.toSatisfy(isRemoteBackendDownError)
    h.time.advance(REMOTE_DOWN_INITIAL_BACKOFF_MS)
    await expect(h.run(async () => 'back')).resolves.toBe('back')

    const capped = new RemoteDownGate(h.time.now)

    for (let i = 0; i < 20; i++) {
      capped.markDown(KEY, 'reset')
    }

    h.time.advance(REMOTE_DOWN_MAX_BACKOFF_MS - 1)
    expect(() => capped.admit(KEY)).toThrow()
    h.time.advance(1)
    expect(capped.admit(KEY)).toEqual({ trial: true })
  })

  it('an HTTP error is an answer: it does not mark the remote down, and it reopens a down gate', async () => {
    const h = harness()

    await expect(h.run(async () => Promise.reject(new Error('500: boom')))).rejects.toThrow('500')
    expect(h.gate.isDown(KEY)).toBe(false)

    h.gate.markDown(KEY, 'reset')
    h.time.advance(REMOTE_DOWN_INITIAL_BACKOFF_MS)
    await expect(h.run(async () => Promise.reject(new Error('401: unauthorized')))).rejects.toThrow('401')
    expect(h.gate.isDown(KEY)).toBe(false)
  })

  it('a slow endpoint timing out does not take the whole remote down, but forces the next dispatch to re-probe', async () => {
    const h = harness()
    const timeout = new Error('Timed out connecting to Hermes backend after 4000ms')

    await expect(h.run(async () => Promise.reject(timeout))).rejects.toThrow('Timed out')
    expect(h.gate.isDown(KEY)).toBe(false)
    expect(h.forgetAnswer).toHaveBeenCalledWith(BASE_URL)

    // ...but a trial that times out keeps it down.
    h.gate.markDown(KEY, 'reset')
    h.time.advance(REMOTE_DOWN_INITIAL_BACKOFF_MS)
    await expect(h.run(async () => Promise.reject(timeout))).rejects.toThrow('Timed out')
    expect(h.gate.isDown(KEY)).toBe(true)
  })

  it('config refusals say nothing about remote health', async () => {
    const h = harness()

    h.connect.mockRejectedValueOnce(new Error('Passive read: no warm backend for "conn:hermes-hub::default"'))
    await expect(h.run(async () => 'x')).rejects.toThrow('Passive read')
    h.connect.mockRejectedValueOnce(new Error("Profile 'x@hub' does not exist."))
    await expect(h.run(async () => 'x')).rejects.toThrow('does not exist')
    expect(h.gate.isDown(KEY)).toBe(false)
  })

  it('keys are independent: a down hub does not gate mac-mini', async () => {
    const h = harness()

    h.gate.markDown(KEY, 'reset')
    expect(() => h.gate.admit('conn:mac-mini::default')).not.toThrow()
  })

  it('classifies transport failures', () => {
    expect(isRemoteTransportFailure(reset())).toBe(true)
    expect(isRemoteTransportFailure(new Error('socket hang up'))).toBe(true)
    expect(isRemoteTransportFailure(new Error('connect ECONNREFUSED 127.0.0.1:62511'))).toBe(true)
    expect(isRemoteTransportFailure(new Error('Timed out connecting to Hermes backend after 4000ms'))).toBe(false)
    expect(isRemoteTransportFailure(new Error('500: boom'))).toBe(false)
  })
})

describe('main.ts wiring', () => {
  const main = readFileSync(join(__dirname, 'main.ts'), 'utf8')

  it('routes registry requests to remote connections through the gate', () => {
    const body = main.slice(main.indexOf('async function dispatchRegistryApiRequest('))

    expect(body.slice(0, 4000)).toMatch(/dispatchThroughRemoteDownGate\(\{\s*gate: remoteDownGate,\s*key: gateKey/)
  })

  it('a failed dispatch probe marks the pool key down', () => {
    expect(main).toMatch(/failed its dispatch probe[\s\S]{0,200}remoteDownGate\.markDown\(key,/)
  })
})
