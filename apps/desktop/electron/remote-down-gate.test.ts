import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it, vi } from 'vitest'

import {
  dispatchThroughRemoteDownGate,
  isRemoteBackendDownError,
  isRemoteTransportFailure,
  REMOTE_DOWN_INITIAL_BACKOFF_MS,
  REMOTE_DOWN_MAX_BACKOFF_MS,
  REMOTE_DOWN_TRIAL_DEADLINE_MS,
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
    const error: any = await h.run(request).catch(e => e)

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

    // Every further failed trial doubles again, up to the cap.
    const capped = new RemoteDownGate(h.time.now)
    capped.markDown(KEY, 'reset')

    for (let i = 0; i < 20; i++) {
      h.time.advance(REMOTE_DOWN_MAX_BACKOFF_MS)
      capped.markDown(KEY, 'reset', capped.admit(KEY))
    }

    h.time.advance(REMOTE_DOWN_MAX_BACKOFF_MS - 1)
    expect(() => capped.admit(KEY)).toThrow()
    h.time.advance(1)
    expect(capped.admit(KEY)).toMatchObject({ trial: true })
  })

  it('many requests failing together in one outage cost one initial backoff, not the cap', async () => {
    const h = harness()

    // Several renderer polls (hub-sync/status, kanban/board, runners) are in flight when the hub restarts.
    await Promise.allSettled([1, 2, 3, 4, 5].map(() => h.run(async () => Promise.reject(reset()))))
    expect(h.gate.isDown(KEY)).toBe(true)

    const blocked = (() => {
      try {
        h.gate.admit(KEY)
      } catch (error) {
        return error as { retryInMs: number }
      }

      return null
    })()

    expect(blocked?.retryInMs).toBe(REMOTE_DOWN_INITIAL_BACKOFF_MS)
    h.time.advance(REMOTE_DOWN_INITIAL_BACKOFF_MS - 1)
    expect(() => h.gate.admit(KEY)).toThrow()
    h.time.advance(1)
    await expect(h.run(async () => 'back')).resolves.toBe('back')
    expect(h.gate.isDown(KEY)).toBe(false)
  })

  it('a stale failure during the trial neither frees the trial slot nor extends the backoff', async () => {
    const h = harness()

    let failStale: (error: Error) => void = () => {}
    const stale = h.run(() => new Promise((_, reject) => (failStale = reject))).catch(e => e)

    await vi.waitFor(() => expect(h.connect).toHaveBeenCalledTimes(1))
    await h.run(async () => Promise.reject(reset())).catch(() => {})
    h.time.advance(REMOTE_DOWN_INITIAL_BACKOFF_MS)

    let failTrial: (error: Error) => void = () => {}
    const trial = h.run(() => new Promise((_, reject) => (failTrial = reject))).catch(e => e)

    await vi.waitFor(() => expect(h.connect).toHaveBeenCalledTimes(3))

    // The request admitted before the outage fails late, while the trial is still running.
    failStale(reset())
    await stale

    const during = (() => {
      try {
        h.gate.admit(KEY)
      } catch (error) {
        return error as { retryInMs: number }
      }

      return null
    })()

    expect(during).not.toBeNull()
    expect(during?.retryInMs).toBe(0)
    h.time.advance(REMOTE_DOWN_MAX_BACKOFF_MS)
    expect(() => h.gate.admit(KEY)).toThrow(/reconnect in progress/)

    // The trial's own failure escalates exactly once: 2 s -> 4 s.
    failTrial(reset())
    await trial
    h.time.advance(2 * REMOTE_DOWN_INITIAL_BACKOFF_MS - 1)
    expect(() => h.gate.admit(KEY)).toThrow()
    h.time.advance(1)
    expect(h.gate.admit(KEY)).toMatchObject({ trial: true })
  })

  it('an answer to a request admitted before the outage does not reopen the gate', async () => {
    const h = harness()

    let finishStale: (value: unknown) => void = () => {}
    const stale = h.run(() => new Promise(resolve => (finishStale = resolve)))

    await vi.waitFor(() => expect(h.connect).toHaveBeenCalledTimes(1))
    await h.run(async () => Promise.reject(reset())).catch(() => {})
    finishStale({ ok: true })
    await expect(stale).resolves.toEqual({ ok: true })
    expect(h.gate.isDown(KEY)).toBe(true)
  })

  it('a dispatch probe that marks the remote down, then reconnects and answers, reopens the gate', async () => {
    const h = harness()

    // connect() runs the dispatch probe; its retire path marks the key down before redialing.
    h.connect.mockImplementationOnce(async () => {
      h.gate.markDown(KEY, 'dispatch probe failed: socket hang up')

      return { baseUrl: BASE_URL }
    })
    await expect(h.run(async () => 'fresh')).resolves.toBe('fresh')
    expect(h.gate.isDown(KEY)).toBe(false)
  })

  it('outside evidence (a failed dispatch probe) never escalates an existing outage', () => {
    const h = harness()

    h.gate.markDown(KEY, 'reset')

    for (let i = 0; i < 5; i++) {
      h.gate.markDown(KEY, 'dispatch probe failed')
    }

    h.time.advance(REMOTE_DOWN_INITIAL_BACKOFF_MS)
    expect(h.gate.admit(KEY)).toMatchObject({ trial: true })
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

// [daveey] 2026-11-12: a trial on a ~1-2 Mbps link held the slot from 22:47:46 to 22:51:13; every hub call meanwhile
// failed fast with "reconnect in progress" although the tunnel answered /api/health in 0.44 s.
describe('remote down gate: trial deadline', () => {
  const TRIAL_DEADLINE_MS = 60

  function downHarness() {
    const h = harness()
    h.gate.markDown(KEY, 'reset')
    h.time.advance(REMOTE_DOWN_INITIAL_BACKOFF_MS)

    const runTrial = (connect: () => Promise<{ baseUrl: string }>, request: () => Promise<unknown>) =>
      dispatchThroughRemoteDownGate({
        gate: h.gate,
        key: KEY,
        connect,
        request,
        forgetAnswer: h.forgetAnswer,
        log: h.log,
        trialDeadlineMs: TRIAL_DEADLINE_MS
      })

    return { ...h, runTrial }
  }

  async function settleWithin(promise: Promise<unknown>, ms: number) {
    return Promise.race([
      promise.then(
        value => ({ value }),
        (error: Error) => ({ error })
      ),
      new Promise<{ pending: true }>(resolve => setTimeout(() => resolve({ pending: true }), ms))
    ]) as Promise<{ error?: Error; pending?: true; value?: unknown }>
  }

  it('defaults to a 20 s trial budget', () => {
    expect(REMOTE_DOWN_TRIAL_DEADLINE_MS).toBe(20_000)
  })

  it('a trial whose connect hangs frees the slot by its deadline and the next trial runs', async () => {
    const h = downHarness()

    const hung = h.runTrial(
      () => new Promise(() => {}),
      async () => 'never'
    )

    // While the trial is in flight, others fail fast.
    await expect(h.run(async () => 'other')).rejects.toSatisfy(isRemoteBackendDownError)

    const outcome = await settleWithin(hung, 1_000)
    expect(outcome.pending).toBeUndefined()
    expect(String(outcome.error?.message)).toMatch(/Timed out connecting to Hermes backend/)
    expect(h.gate.isDown(KEY)).toBe(true)

    // A failed trial escalates the backoff (2x), then admits the next trial.
    h.time.advance(REMOTE_DOWN_INITIAL_BACKOFF_MS)
    await expect(h.run(async () => 'x')).rejects.toSatisfy(isRemoteBackendDownError)
    h.time.advance(REMOTE_DOWN_INITIAL_BACKOFF_MS)
    await expect(
      h.runTrial(
        async () => ({ baseUrl: BASE_URL }),
        async () => 'back'
      )
    ).resolves.toBe('back')
    expect(h.gate.isDown(KEY)).toBe(false)
  })

  it('a trial whose request hangs frees the slot by its deadline and the next trial runs', async () => {
    const h = downHarness()

    const hung = h.runTrial(
      async () => ({ baseUrl: BASE_URL }),
      () => new Promise(() => {})
    )

    const outcome = await settleWithin(hung, 1_000)
    expect(outcome.pending).toBeUndefined()
    expect(String(outcome.error?.message)).toMatch(/Timed out connecting to Hermes backend/)
    expect(h.forgetAnswer).toHaveBeenCalledWith(BASE_URL)

    h.time.advance(2 * REMOTE_DOWN_INITIAL_BACKOFF_MS)
    await expect(
      h.runTrial(
        async () => ({ baseUrl: BASE_URL }),
        async () => 'back'
      )
    ).resolves.toBe('back')
    expect(h.gate.isDown(KEY)).toBe(false)
  })

  it('a late failure from the abandoned trial cannot escalate the backoff again', async () => {
    const h = downHarness()

    let failLate: (error: Error) => void = () => {}

    const hung = h.runTrial(
      async () => ({ baseUrl: BASE_URL }),
      () => new Promise((_r, reject) => (failLate = reject))
    )

    await settleWithin(hung, 1_000)
    failLate(reset())
    await new Promise(resolve => setTimeout(resolve, 0))

    // Still the 2x backoff from the deadline, not 4x.
    h.time.advance(2 * REMOTE_DOWN_INITIAL_BACKOFF_MS)
    await expect(
      h.runTrial(
        async () => ({ baseUrl: BASE_URL }),
        async () => 'back'
      )
    ).resolves.toBe('back')
  })

  it('a late success from the abandoned trial still reopens the gate (it was sent in the current epoch)', async () => {
    const h = downHarness()

    let answerLate: (value: string) => void = () => {}

    const hung = h.runTrial(
      async () => ({ baseUrl: BASE_URL }),
      () => new Promise<string>(resolve => (answerLate = resolve))
    )

    const outcome = await settleWithin(hung, 1_000)
    expect(String(outcome.error?.message)).toMatch(/down-gate trial deadline/)
    // The deadline marked the trial failed: still down, and the slot is free but behind the escalated backoff.
    expect(h.gate.isDown(KEY)).toBe(true)
    await expect(h.run(async () => 'x')).rejects.toSatisfy(isRemoteBackendDownError)

    // The abandoned request answers after its caller gave up: the remote is back, so the gate reopens at once,
    // without waiting out the backoff for another trial.
    answerLate('late')
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(h.gate.isDown(KEY)).toBe(false)
    await expect(h.run(async () => 'open')).resolves.toBe('open')
  })

  it('a non-trial request is not cut by the trial deadline', async () => {
    const h = harness()

    const slow = dispatchThroughRemoteDownGate({
      gate: h.gate,
      key: KEY,
      connect: h.connect,
      request: () => new Promise(resolve => setTimeout(() => resolve('slow'), 3 * TRIAL_DEADLINE_MS)),
      trialDeadlineMs: TRIAL_DEADLINE_MS
    })

    await expect(slow).resolves.toBe('slow')
  })
})

describe('main.ts wiring', () => {
  const main = readFileSync(join(__dirname, 'main.ts'), 'utf8')

  // fetchJson's own pool choice is proven by behaviour in json-fetch.test.ts.
  it('fetchJsonForBackend forwards the pool option', () => {
    const body = main.slice(main.indexOf('async function fetchJsonForBackendOnce('))
    expect(body.slice(0, 4000)).toMatch(/pool: opts\.pool/)
  })

  it('routes registry requests to remote connections through the gate', () => {
    const body = main.slice(main.indexOf('async function dispatchRegistryApiRequest('))

    expect(body.slice(0, 4000)).toMatch(/dispatchThroughRemoteDownGate\(\{\s*gate: remoteDownGate,\s*key: gateKey/)
  })

  it('a failed dispatch probe marks the pool key down', () => {
    expect(main).toMatch(/failed its dispatch probe[\s\S]{0,200}remoteDownGate\.markDown\(key,/)
  })
})
