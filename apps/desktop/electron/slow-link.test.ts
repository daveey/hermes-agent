import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it, vi } from 'vitest'

import { isPowerSettlingError } from './power-settle'
import { isRemoteTimeout } from './remote-down-gate'
import {
  ensureHealthyPooledRemoteBackendForDispatch,
  POOLED_REMOTE_DISPATCH_PROBE_TIMEOUT_MS,
  RemoteAnswerLedger,
  remoteAnswerLedger
} from './remote-liveness'
import {
  dispatchSlowLink,
  isThinnedPollPath,
  noteRemoteAnswer,
  noteRemoteConnect,
  SLOW_LINK_BACKGROUND_GAP_MS,
  SLOW_LINK_EXIT_SAMPLES,
  SLOW_LINK_FAST_MS,
  SLOW_LINK_IDLE_EXPIRY_MS,
  SLOW_LINK_STRIKE_LIMIT,
  SLOW_LINK_TIMEOUT_CAP_MS,
  SLOW_LINK_TIMEOUT_FACTOR,
  SlowLinkHeldError,
  SlowLinkTracker,
  thinnedPoll
} from './slow-link'

const HUB = 'http://127.0.0.1:49882'
const SCOPE = 'conn:hermes-hub::default'
const POLL = '/api/plugins/kanban/board?board=default'

function harness() {
  let clock = 1_000_000
  const lines: string[] = []
  const tracker = new SlowLinkTracker({ log: line => lines.push(line), now: () => clock })

  tracker.bind(SCOPE, `${HUB}/`)

  return { advance: (ms: number) => (clock += ms), lines, tracker }
}

describe('SlowLinkTracker', () => {
  it('leaves a backend alone until a probe times out: slow answers alone never enter slow mode', () => {
    const { lines, tracker } = harness()

    for (let i = 0; i < 20; i += 1) {
      tracker.noteAnswer(HUB, 9_000) // a heavy endpoint on a fast link
    }

    expect(tracker.isSlow(HUB)).toBe(false)
    expect(tracker.scaleTimeout(HUB, 4_000)).toBe(4_000)
    tracker.admitBackground(SCOPE, POLL)(true)
    tracker.admitBackground(SCOPE, POLL)(true)
    expect(lines).toEqual([])
  })

  it('enters slow mode on a probe timeout with the transport alive, and stretches timeouts up to the cap', () => {
    const { lines, tracker } = harness()

    expect(tracker.noteTimeoutWhileAlive(HUB)).toEqual({ giveUp: false, strikes: 1 })
    expect(tracker.isSlow(HUB)).toBe(true)
    expect(tracker.isScopeSlow(SCOPE)).toBe(true)
    expect(tracker.isConnectionSlow('hermes-hub')).toBe(true)
    expect(tracker.isConnectionSlow('mac-mini')).toBe(false)
    expect(tracker.scaleTimeout(HUB, 4_000)).toBe(4_000 * SLOW_LINK_TIMEOUT_FACTOR)
    expect(tracker.scaleTimeout(HUB, 30_000)).toBe(SLOW_LINK_TIMEOUT_CAP_MS)
    // A budget already above the cap is never shortened.
    expect(tracker.scaleTimeout(HUB, 120_000)).toBe(120_000)
    expect(lines).toHaveLength(1)
    expect(lines[0]).toContain('[slow-link]')
  })

  it('leaves slow mode when the median of the last answers is fast; one heavy answer does not keep it slow', () => {
    const { lines, tracker } = harness()

    tracker.noteTimeoutWhileAlive(HUB)

    for (let i = 0; i < SLOW_LINK_EXIT_SAMPLES; i += 1) {
      tracker.noteAnswer(HUB, SLOW_LINK_FAST_MS + 3_000)
    }

    expect(tracker.isSlow(HUB)).toBe(true)

    tracker.noteAnswer(HUB, 50)
    tracker.noteAnswer(HUB, 50)
    expect(tracker.isSlow(HUB)).toBe(true) // 3 slow, 2 fast

    tracker.noteAnswer(HUB, 6_000) // a heavy endpoint among fast answers
    tracker.noteAnswer(HUB, 50)
    tracker.noteAnswer(HUB, 50)
    tracker.noteAnswer(HUB, 50)

    expect(tracker.isSlow(HUB)).toBe(false)
    expect(tracker.scaleTimeout(HUB, 4_000)).toBe(4_000)
    expect(lines.at(-1)).toContain('fast again')
  })

  it('expires slow mode after SLOW_LINK_IDLE_EXPIRY_MS with no slow answer or timeout', () => {
    const { advance, tracker } = harness()

    tracker.noteTimeoutWhileAlive(HUB)
    advance(SLOW_LINK_IDLE_EXPIRY_MS - 1)
    expect(tracker.isSlow(HUB)).toBe(true)

    tracker.noteAnswer(HUB, 50) // fast answers are not evidence of slowness
    advance(1)
    expect(tracker.isSlow(HUB)).toBe(false)

    tracker.noteTimeoutWhileAlive(HUB)
    advance(SLOW_LINK_IDLE_EXPIRY_MS - 1)
    tracker.noteAnswer(HUB, SLOW_LINK_FAST_MS) // a slow answer renews it
    advance(SLOW_LINK_IDLE_EXPIRY_MS - 1)
    expect(tracker.isSlow(HUB)).toBe(true)
  })

  it('gives up after the strike limit with no answer in between; an answer resets the strikes', () => {
    const { tracker } = harness()

    for (let strike = 1; strike < SLOW_LINK_STRIKE_LIMIT; strike += 1) {
      expect(tracker.noteTimeoutWhileAlive(HUB)).toEqual({ giveUp: false, strikes: strike })
    }

    tracker.noteAnswer(HUB, 8_000) // slow, but it answered

    for (let strike = 1; strike < SLOW_LINK_STRIKE_LIMIT; strike += 1) {
      expect(tracker.noteTimeoutWhileAlive(HUB).giveUp).toBe(false)
    }

    expect(tracker.noteTimeoutWhileAlive(HUB)).toEqual({ giveUp: true, strikes: SLOW_LINK_STRIKE_LIMIT })
    // Given up: the rebuilt backend starts clean.
    expect(tracker.isSlow(HUB)).toBe(false)
  })

  it('thins timer polls on a slow link: one in flight per scope and path, then a minimum gap', () => {
    const { advance, tracker } = harness()

    tracker.noteTimeoutWhileAlive(HUB)

    const release = tracker.admitBackground(SCOPE, POLL)

    expect(() => tracker.admitBackground(SCOPE, POLL)).toThrow(SlowLinkHeldError)
    // Another path is its own slot.
    tracker.admitBackground(SCOPE, '/api/plugins/hub-sync/status')(true)

    release(true)
    advance(SLOW_LINK_BACKGROUND_GAP_MS - 1)
    expect(() => tracker.admitBackground(SCOPE, POLL)).toThrow(SlowLinkHeldError)

    advance(1)
    tracker.admitBackground(SCOPE, POLL)(true)
  })

  it('never thins a read that is not a known timer poll, such as opening a session', () => {
    const { tracker } = harness()

    tracker.noteTimeoutWhileAlive(HUB)

    expect(isThinnedPollPath('/api/sessions/20261112_185653_5ff5dc/messages?limit=200')).toBe(false)
    expect(isThinnedPollPath('/api/profiles/sessions')).toBe(false)
    expect(isThinnedPollPath(POLL)).toBe(true)
    expect(isThinnedPollPath('/api/session-kanban-tasks?session_id=x')).toBe(true)

    for (let i = 0; i < 3; i += 1) {
      tracker.admitBackground(SCOPE, '/api/sessions/20261112_185653_5ff5dc/messages?limit=200')
    }
  })

  it('keeps one profile from holding another profile on the same backend', () => {
    const { tracker } = harness()
    const other = 'conn:hermes-hub::alpha'

    tracker.bind(other, HUB) // named profiles on an SSH connection share the default backend's baseUrl
    tracker.noteTimeoutWhileAlive(HUB)

    tracker.admitBackground(SCOPE, POLL)
    tracker.admitBackground(other, POLL)
    expect(() => tracker.admitBackground(other, POLL)).toThrow(SlowLinkHeldError)
  })

  it('gives the slot straight back when the poll never reached the wire', () => {
    const { tracker } = harness()

    tracker.noteTimeoutWhileAlive(HUB)

    tracker.admitBackground(SCOPE, POLL)(false) // down gate refused it, or the dial failed
    tracker.admitBackground(SCOPE, POLL)(true)
    expect(() => tracker.admitBackground(SCOPE, POLL)).toThrow(SlowLinkHeldError)
  })

  it('a held poll is a power-settling error, so the renderer guard and the api handler treat it as a quiet skip', () => {
    const held = new SlowLinkHeldError()

    expect(isPowerSettlingError(held)).toBe(true)
    expect(held.message).toContain('slow')
    // Electron strips `code` across IPC: the renderer only sees "<name>: <message>" inside the invoke error, and
    // its guard (a separate tsconfig project, so read as text here) matches on exactly that name marker.
    const guard = readFileSync(join(__dirname, '..', 'src', 'lib', 'power-settling-rejections.ts'), 'utf8')
    const marker = /POWER_SETTLING_MARKER = '([^']+)'/.exec(guard)?.[1]

    expect(marker).toBe('PowerSettlingError:')
    expect(`${held.name}: ${held.message}`).toContain(marker)
  })

  it('forgetScope drops the slow state and the poll slots of a retired backend', () => {
    const { tracker } = harness()

    tracker.noteTimeoutWhileAlive(HUB)
    tracker.admitBackground(SCOPE, POLL)
    tracker.forgetScope(SCOPE)

    expect(tracker.isSlow(HUB)).toBe(false)
    expect(tracker.isScopeSlow(SCOPE)).toBe(false)

    // A rebuilt tunnel that lands on the same local port starts clean.
    tracker.bind(SCOPE, HUB)
    expect(tracker.isSlow(HUB)).toBe(false)
    tracker.admitBackground(SCOPE, POLL)(true)
    tracker.admitBackground(SCOPE, POLL)(true)
  })
})

describe('three probe timeouts with no answer in between retire the backend', () => {
  it('keeps it twice, then retires and reconnects on the third', async () => {
    const tracker = new SlowLinkTracker()
    const answers = new RemoteAnswerLedger()
    const connection = { baseUrl: HUB, mode: 'remote' }
    const replacement = { baseUrl: 'http://127.0.0.1:53968', mode: 'remote' }
    const connectionPromise = Promise.resolve(connection)
    const retire = vi.fn()
    const reconnect = vi.fn(async () => replacement)

    const dispatch = () =>
      ensureHealthyPooledRemoteBackendForDispatch({
        answers,
        connectionPromise,
        currentConnectionPromise: () => connectionPromise,
        probe: async () => {
          throw new Error('Timed out connecting to Hermes backend after 40000ms')
        },
        reconnect,
        retire,
        slowLink: {
          isTimeout: isRemoteTimeout,
          noteTimeout: target => tracker.noteTimeoutWhileAlive(target.baseUrl),
          transportAlive: async () => true
        }
      })

    for (let strike = 1; strike < SLOW_LINK_STRIKE_LIMIT; strike += 1) {
      await expect(dispatch()).resolves.toBe(connection)
      answers.forget(HUB) // the request it let through timed out too (remote-down-gate.ts)
    }

    expect(retire).not.toHaveBeenCalled()

    await expect(dispatch()).resolves.toBe(replacement)
    expect(retire).toHaveBeenCalledOnce()
    expect(tracker.isSlow(HUB)).toBe(false)
  })
})

describe('a backend that answers every request after 12 s over a live tunnel', () => {
  const LATENCY_MS = 12_000

  // The transport the real fetch gives: an answer after LATENCY_MS, or the timeout error when the budget is shorter.
  const slowFetch = async (timeoutMs: number) => {
    if (timeoutMs < LATENCY_MS) {
      throw new Error(`Timed out connecting to Hermes backend after ${timeoutMs}ms`)
    }

    return { ok: true }
  }

  it('is kept (no retire, no reconnect) and a 4 s request then fits its stretched timeout', async () => {
    const tracker = new SlowLinkTracker()
    const answers = new RemoteAnswerLedger()
    const connection = { baseUrl: HUB, mode: 'remote' }
    const connectionPromise = Promise.resolve(connection)
    const retire = vi.fn()
    const reconnect = vi.fn()

    const dispatch = () =>
      ensureHealthyPooledRemoteBackendForDispatch({
        answers,
        connectionPromise,
        currentConnectionPromise: () => connectionPromise,
        probe: (target, _path, options) => slowFetch(tracker.scaleTimeout(target.baseUrl, options.timeoutMs)),
        reconnect,
        retire,
        slowLink: {
          isTimeout: isRemoteTimeout,
          noteTimeout: target => tracker.noteTimeoutWhileAlive(target.baseUrl),
          transportAlive: async () => true
        }
      })

    // Before: the 10 s probe cannot fit 12 s, and a 4 s request would time out too.
    expect(POOLED_REMOTE_DISPATCH_PROBE_TIMEOUT_MS).toBeLessThan(LATENCY_MS)
    await expect(slowFetch(tracker.scaleTimeout(HUB, 4_000))).rejects.toThrow(/Timed out/)

    await expect(dispatch()).resolves.toBe(connection)
    expect(tracker.isSlow(HUB)).toBe(true)

    // The request the dispatch was for now gets 16 s and completes.
    await expect(slowFetch(tracker.scaleTimeout(HUB, 4_000))).resolves.toEqual({ ok: true })

    // The next dispatch probes with a stretched budget, so it passes outright.
    answers.forget(HUB)
    await expect(dispatch()).resolves.toBe(connection)

    expect(retire).not.toHaveBeenCalled()
    expect(reconnect).not.toHaveBeenCalled()
  })
})

describe('main.ts wiring helpers', () => {
  it('dispatchSlowLink: a timeout with a live master binds the scope and counts a strike; a dead or absent master is not alive', async () => {
    const { tracker } = harness()
    const scope = 'conn:mac-mini::default'
    const mini = 'http://127.0.0.1:50111'
    const option = dispatchSlowLink(scope, async () => true, undefined, tracker)

    expect(option.isTimeout(new Error('Timed out connecting to Hermes backend after 10000ms'))).toBe(true)
    expect(option.isTimeout(new Error('socket hang up'))).toBe(false)
    await expect(option.transportAlive()).resolves.toBe(true)
    expect(option.noteTimeout({ baseUrl: mini })).toEqual({ giveUp: false, strikes: 1 })
    expect(tracker.isScopeSlow(scope)).toBe(true)

    await expect(dispatchSlowLink(scope, async () => false, undefined, tracker).transportAlive()).resolves.toBe(false)
    await expect(dispatchSlowLink(scope, () => undefined, undefined, tracker).transportAlive()).resolves.toBe(false)
  })

  it('dispatchSlowLink logs the slow-link entry line when a probe times out before any remote has answered', () => {
    // A fresh process: no log wired yet, and nothing has answered, so noteRemoteAnswer never set one.
    const tracker = new SlowLinkTracker()
    const lines: string[] = []
    const mini = 'http://127.0.0.1:50111'
    const option = dispatchSlowLink('conn:mac-mini::default', async () => true, line => lines.push(line), tracker)

    expect(option.noteTimeout({ baseUrl: mini })).toEqual({ giveUp: false, strikes: 1 })
    expect(lines).toEqual([expect.stringMatching(/^\[slow-link\] http:\/\/127\.0\.0\.1:50111 is slow \(probe timed out/)])
  })

  it('thinnedPoll: holds a repeated timer poll, frees an unsent one, and never holds a foreground or non-GET request', async () => {
    const { tracker } = harness()
    const poll = { method: 'GET', path: POLL }

    tracker.noteTimeoutWhileAlive(HUB)

    // Sent: the slot stays taken for the gap, even though the request failed.
    const first = thinnedPoll(SCOPE, poll, tracker)

    await expect(
      first.run(async () => {
        first.sent(HUB)
        throw new Error('Timed out connecting to Hermes backend after 16000ms')
      })
    ).rejects.toThrow(/Timed out/)
    expect(() => thinnedPoll(SCOPE, poll, tracker)).toThrow(SlowLinkHeldError)

    // Never sent (down gate refused it): the slot is free again at once.
    const other = { method: 'GET', path: '/api/plugins/hub-sync/status' }

    await expect(
      thinnedPoll(SCOPE, other, tracker).run(async () => {
        throw new Error('Remote backend is down')
      })
    ).rejects.toThrow(/down/)
    await expect(thinnedPoll(SCOPE, other, tracker).run(async () => 'ok')).resolves.toBe('ok')

    // User-driven work passes however often it repeats.
    for (const request of [
      { method: 'GET', path: POLL, priority: 'foreground' },
      { method: 'POST', path: POLL },
      { method: 'GET', path: '/api/sessions/abc/messages' }
    ]) {
      thinnedPoll(SCOPE, request, tracker)
      thinnedPoll(SCOPE, request, tracker)
    }

    // A local request has no scope and is never thinned.
    thinnedPoll(null, poll, tracker)
    thinnedPoll(null, poll, tracker)
  })

  it('thinnedPoll.sent binds the scope to its baseUrl, which is what lets prefetch pause for that connection', () => {
    const tracker = new SlowLinkTracker()
    const scope = 'conn:mac-mini::default'
    const mini = 'http://127.0.0.1:50111'

    thinnedPoll(scope, { method: 'GET', path: POLL }, tracker).sent(mini)
    tracker.noteTimeoutWhileAlive(mini)

    expect(tracker.isConnectionSlow('mac-mini')).toBe(true)
  })
})

describe('noteRemoteAnswer', () => {
  it('feeds the shared answer ledger and logs the first request a new descriptor serves, once', () => {
    const url = 'http://127.0.0.1:50999'
    const lines: string[] = []
    const log = (line: string) => lines.push(line)

    remoteAnswerLedger.forget(url)
    noteRemoteConnect(url, 'conn:hermes-hub::default', Date.now() - 250, () => undefined)
    noteRemoteAnswer(url, '/api/status', 40, log)
    noteRemoteAnswer(url, '/api/profiles', 40, log)

    expect(remoteAnswerLedger.answeredWithin(url, 30_000)).toBe(true)
    expect(lines).toHaveLength(1)
    expect(lines[0]).toMatch(/^\[remote-timing\] conn:hermes-hub::default first-request \/api\/status served \+\d+ ms$/)

    remoteAnswerLedger.forget(url)
  })
})

describe('main.ts wiring', () => {
  const main = readFileSync(join(__dirname, 'main.ts'), 'utf8')

  it('keeps a slow SSH tunnel at dispatch: SSH sources get dispatchSlowLink over the live master check', () => {
    const dispatch = main.slice(main.indexOf('ensureHealthyPooledRemoteBackendForDispatch({'))
    const slowLink = dispatch.indexOf('slowLink:')

    expect(slowLink).toBeGreaterThan(0)
    expect(slowLink).toBeLessThan(dispatch.indexOf('retire: async'))
    expect(dispatch.slice(slowLink, slowLink + 300).replace(/\s+/g, ' ')).toContain(
      "source.kind === 'ssh' ? dispatchSlowLink(key, () => sshConnections.get(key)?.ssh?.isAlive(), rememberLog) : undefined"
    )
  })

  it('stretches every backend request timeout and times every answer in fetchJsonForBackend', () => {
    const fetcher = main.slice(main.indexOf('async function fetchJsonForBackendOnce('))
    const body = fetcher.slice(0, fetcher.indexOf('\nipcMain.handle('))

    expect(body).toContain('slowLinkTracker.scaleTimeout(')
    expect(body).not.toMatch(/timeoutMs: opts\.timeoutMs,\s+headers/)
    expect(body.match(/noteRemoteAnswer\(descriptor\.baseUrl, path, Date\.now\(\) - startedAt, rememberLog\)/g)).toHaveLength(2)
  })

  it('wraps the registry dispatch, down gate included, in the poll thinner and reports the send', () => {
    const dispatch = main.slice(main.indexOf('const poll = thinnedPoll(gateKey, request)'))
    const run = dispatch.indexOf('await poll.run(')

    expect(dispatch.indexOf('poll.sent(descriptor.baseUrl)')).toBeGreaterThan(0)
    expect(dispatch.indexOf('poll.sent(descriptor.baseUrl)')).toBeLessThan(dispatch.indexOf('return fetchJsonForBackend('))
    expect(run).toBeGreaterThan(0)
    expect(run).toBeLessThan(dispatch.indexOf('dispatchThroughRemoteDownGate({'))
  })

  it('pauses transcript prefetch for a slow connection and clears slow state wherever a pooled descriptor is dropped', () => {
    expect(main).toContain('prefetchPaused: connectionId => slowLinkTracker.isConnectionSlow(connectionId)')

    const stop = main.slice(main.indexOf('function stopPoolBackend('))

    expect(stop.slice(0, 300)).toContain('slowLinkTracker.forgetScope(profile)')
  })
})
