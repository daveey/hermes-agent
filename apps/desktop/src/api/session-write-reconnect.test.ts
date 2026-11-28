import { describe, expect, it, vi } from 'vitest'

import {
  isOwnerRestartingSessionWriteError,
  isReconnectableSessionWriteError,
  NonReplayableSessionWriteError,
  withSessionWriteReconnect
} from './session-write-reconnect'

// The failures the MacBook logged for hub session PATCHes on 2026-11-12 (desktop.log, conn=hermes-hub).
const HUB_FAILURES = [
  "Error invoking remote method 'hermes:api': Error: socket hang up",
  'Remote backend "conn:hermes-hub::default" is down (socket hang up, 3s ago); reconnect in progress. Failing fast instead of waiting for a timeout.',
  'Remote backend "conn:hermes-hub::default" is down (dispatch probe failed: Timed out connecting to Hermes backend after 10000ms, 10s ago); next try in 4s. Failing fast instead of waiting for a timeout.',
  'Passive read: no warm backend for "conn:hermes-hub::default"',
  'Timed out connecting to Hermes backend after 30000ms',
  'connect ECONNREFUSED 127.0.0.1:58610'
]

const DEFINITIVE = [
  '503: {"detail":"owner_unavailable: control_timeout: the gateway that owns this profile did not answer in time; nothing was changed, retry"}',
  '409: {"detail":"revision_conflict"}',
  '404: {"detail":"not_found"}',
  '409: {"detail":"client_upgrade_required: this app is out of sync (different versions) with the Hermes backend; update the app to edit sessions"}',
  '409: {"detail":"mutation_identity_required"}',
  'Session mutation snapshot unavailable; refresh before trying again'
]

function clock() {
  let t = 0
  const waits: number[] = []

  return {
    now: () => t,
    sleep: async (ms: number) => {
      waits.push(ms)
      t += ms
    },
    advance: (ms: number) => {
      t += ms
    },
    waits
  }
}

describe('isReconnectableSessionWriteError', () => {
  it.each(HUB_FAILURES)('replays a transport failure: %s', message => {
    expect(isReconnectableSessionWriteError(new Error(message))).toBe(true)
  })

  it.each(DEFINITIVE)('never replays a backend answer: %s', message => {
    expect(isReconnectableSessionWriteError(new Error(message))).toBe(false)
  })
})

describe('withSessionWriteReconnect', () => {
  it('passes the remaining window to each attempt, including time spent dialing', async () => {
    const c = clock()
    const down = new Error('socket hang up')

    const run = vi.fn(async (remainingMs: number) => {
      c.advance(Math.min(30_000, remainingMs ?? 30_000))
      throw down
    })

    await expect(
      withSessionWriteReconnect(run, {
        label: 'PATCH s',
        now: c.now,
        sleep: c.sleep,
        log: vi.fn()
      })
    ).rejects.toBe(down)
    expect(run.mock.calls).toEqual([[60_000], [29_000]])
    expect(c.now()).toBe(60_000)
  })

  it.each([1_000, 2_000])('does not start an attempt after a sleep exhausts the budget (%i)', async elapsed => {
    const c = clock()
    const down = new Error('socket hang up')
    const run = vi.fn().mockRejectedValue(down)

    await expect(
      withSessionWriteReconnect(run, {
        label: 'PATCH s',
        budgetMs: 1_500,
        now: c.now,
        sleep: async () => {
          c.advance(elapsed + 500)
        },
        log: vi.fn()
      })
    ).rejects.toBe(down)
    expect(run).toHaveBeenCalledOnce()
  })

  it('rides out a hub reconnect and lands the write without a user retry', async () => {
    const c = clock()
    const log = vi.fn()

    const run = vi
      .fn()
      .mockRejectedValueOnce(new Error('socket hang up'))
      .mockRejectedValueOnce(new Error(HUB_FAILURES[1]))
      .mockResolvedValueOnce({ ok: true })

    await expect(
      withSessionWriteReconnect(run, { label: 'PATCH s', log, now: c.now, sleep: c.sleep })
    ).resolves.toEqual({ ok: true })
    expect(run).toHaveBeenCalledTimes(3)
    expect(c.waits).toEqual([1_000, 2_000])
    expect(log.mock.calls.at(-1)?.[0]).toMatch(/\[session-write\] PATCH s landed on attempt 3/)
  })

  it('waits at least as long as the down gate says before the next try', async () => {
    const c = clock()
    const run = vi.fn().mockRejectedValueOnce(new Error(HUB_FAILURES[2])).mockResolvedValueOnce({ ok: true })

    await withSessionWriteReconnect(run, { label: 'PATCH s', log: vi.fn(), now: c.now, sleep: c.sleep })
    expect(c.waits).toEqual([4_000])
  })

  it('does not retry a definitive refusal', async () => {
    const c = clock()
    const refusal = new Error(DEFINITIVE[0])
    const run = vi.fn().mockRejectedValue(refusal)

    await expect(
      withSessionWriteReconnect(run, { label: 'PATCH s', log: vi.fn(), now: c.now, sleep: c.sleep })
    ).rejects.toBe(refusal)
    expect(run).toHaveBeenCalledOnce()
    expect(c.waits).toEqual([])
  })

  it('gives the failure back to the caller once the reconnect window is spent', async () => {
    const c = clock()
    const log = vi.fn()
    const down = new Error('socket hang up')

    const run = vi.fn(async () => {
      c.advance(5_000)
      throw down
    })

    await expect(
      withSessionWriteReconnect(run, { budgetMs: 30_000, label: 'PATCH s', log, now: c.now, sleep: c.sleep })
    ).rejects.toBe(down)
    // 5 s per attempt plus 1, 2, 4 s of backoff: the fourth failure (at 27 s) has no room for an 8 s wait.
    expect(run).toHaveBeenCalledTimes(4)
    expect(log.mock.calls.at(-1)?.[0]).toMatch(/failed on attempt 4/)
  })
})

// Hub, 2026-11-24 05:23:45Z: an archive 10 s into a planned gateway restart (30 s drain, then start).
const DRAINING =
  '503: {"detail":"owner_unavailable: draining: the gateway that owns this profile is restarting; nothing was changed, retry"}'
const DRAINING_OLD =
  '503: {"detail":"owner_unavailable: draining: Exclusive maintenance refused: Gateway runtime already owns profile /home/ubuntu/.hermes. Drain and stop the gateway, then retry."}'
const STARTING =
  '503: {"detail":"owner_unavailable: starting: the gateway that owns this profile is starting; nothing was changed, retry"}'

describe('a gateway restart answered by the owner', () => {
  it.each([DRAINING, DRAINING_OLD, STARTING])('is replayed: %s', message => {
    expect(isOwnerRestartingSessionWriteError(new Error(message))).toBe(true)
  })

  it.each([
    DEFINITIVE[0],
    '503: {"detail":"owner_unavailable: update_paused: Exclusive maintenance refused"}',
    '503: {"detail":"owner_unreachable"}'
  ])('leaves every other owner answer to the user: %s', message => {
    expect(isOwnerRestartingSessionWriteError(new Error(message))).toBe(false)
  })

  it('never replays a legacy write without identity', () => {
    expect(isOwnerRestartingSessionWriteError(new NonReplayableSessionWriteError(DRAINING))).toBe(false)
  })

  it('rides out a whole drain + start past the transport window and lands the write', async () => {
    const c = clock()
    const log = vi.fn()
    const drain = new Error(DRAINING)
    const start = new Error(STARTING)
    let calls = 0

    // Each answer arrives after the backend's own ~24 s wait; the owner is ready on the fourth send.
    const run = vi.fn(async () => {
      calls += 1
      c.advance(24_000)

      if (calls === 1 || calls === 2) {
        throw drain
      }

      if (calls === 3) {
        throw start
      }

      return { ok: true }
    })

    await expect(
      withSessionWriteReconnect(run, { label: 'PATCH s', log, now: c.now, sleep: c.sleep })
    ).resolves.toEqual({ ok: true })
    expect(run).toHaveBeenCalledTimes(4)
    expect(c.now()).toBeGreaterThan(60_000)
    expect(log.mock.calls.some(([line]) => /hit a gateway restart/.test(line))).toBe(true)
    expect(log.mock.calls.at(-1)?.[0]).toMatch(/landed on attempt 4/)
  })

  it('gives up once the restart window is spent', async () => {
    const c = clock()
    const drain = new Error(DRAINING)

    const sends: number[] = []

    const run = vi.fn(async () => {
      sends.push(c.now())
      c.advance(24_000)
      throw drain
    })

    await expect(
      withSessionWriteReconnect(run, {
        label: 'PATCH s',
        ownerRestartBudgetMs: 100_000,
        log: vi.fn(),
        now: c.now,
        sleep: c.sleep
      })
    ).rejects.toBe(drain)
    // No replay starts once the window is spent; the last answer may land just after it.
    expect(sends.length).toBeGreaterThan(2)
    expect(Math.max(...sends)).toBeLessThanOrEqual(100_000)
  })
})
