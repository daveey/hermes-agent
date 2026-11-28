import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { canonicalOutcomeStatus } from '@/lib/session-outcome'
import { canonicalOutcome } from '@/test/canonical-outcome'
import type { SessionOutcome } from '@/types/session-outcome'

import {
  COMMITTED_STRIPE_MAX_ATTEMPTS,
  COMMITTED_STRIPE_MAX_ENTRIES,
  flushCommittedStripeReports,
  reportCommittedStripe,
  resetCommittedStripeReports,
  setCommittedStripeReporter
} from './committed-stripe-report'

const row = { connection_id: 'hub', id: 'chat', profile: 'default' }

type Surface = Parameters<typeof reportCommittedStripe>[2]

// The stripe a surface paints for an envelope when it paints the intended one; `painted` overrides to test actual paint.
const report = (
  r: typeof row,
  outcome: SessionOutcome | undefined,
  surface: Surface,
  painted: null | string = canonicalOutcomeStatus(outcome)?.stripe ?? null
) => reportCommittedStripe(r, outcome, surface, painted)

describe('committed stripe report', () => {
  const send = vi.fn()

  beforeEach(() => {
    vi.useFakeTimers()
    resetCommittedStripeReports()
    send.mockReset()
    send.mockResolvedValue({ ok: true })
    setCommittedStripeReporter(send)
  })

  afterEach(() => {
    setCommittedStripeReporter(null)
    resetCommittedStripeReports()
    vi.useRealTimers()
  })

  it.each(['done', 'waiting', 'attention', 'blocked', 'stuck', 'none'] as const)(
    'reports a mounted %s stripe, not red only, with the qualified revision payload',
    async stripe => {
      report(row, canonicalOutcome(stripe), 'row')
      await vi.advanceTimersByTimeAsync(0)

      expect(send).toHaveBeenCalledTimes(1)
      expect(send.mock.calls[0][1]).toEqual({
        mounted: true,
        observation_client: expect.any(String),
        observation_lifetime: 0,
        observation_order: expect.any(Number),
        coverage: { reasons: [], state: 'complete' },
        generation: 3,
        identity: { gateway: 'gateway', owner_epoch: 'epoch', profile: 'default', session_id: 'session' },
        reason_codes: [`test_${stripe}`],
        revision: 7,
        status: 'canonical',
        status_version: 1,
        stripe,
        surface: 'row'
      })
    }
  )

  it('reports unknown with no revision for an outcome without an envelope', async () => {
    report(row, { kind: 'done' }, 'tab', 'done')
    report(row, undefined, 'row', null)
    await vi.advanceTimersByTimeAsync(0)

    expect(send).toHaveBeenCalledTimes(2)

    for (const [, report] of send.mock.calls) {
      expect(report).toMatchObject({ status: 'unknown', revision: null })
    }
  })

  it('reports a local override with the revision it painted over', async () => {
    reportCommittedStripe(row, canonicalOutcome('done', { revision: 9 }), 'row', null, 1, true, 'live_turn')
    await vi.advanceTimersByTimeAsync(0)
    expect(send.mock.calls[0][1]).toMatchObject({ local_override: 'live_turn', revision: 9, stripe: 'none' })

    // The turn ended: the same revision now paints its own stripe, which is a new report, not a duplicate.
    reportCommittedStripe(row, canonicalOutcome('done', { revision: 9 }), 'row', 'done', 1)
    await vi.advanceTimersByTimeAsync(0)
    expect(send).toHaveBeenCalledTimes(2)
    expect(send.mock.calls[1][1]).not.toHaveProperty('local_override')
    expect(send.mock.calls[1][1]).toMatchObject({ revision: 9, stripe: 'done' })
  })

  it('reports an unsupported envelope as unknown attention without a revision', async () => {
    report(row, { kind: 'done', status: { status_version: 2 } }, 'tab')
    await vi.advanceTimersByTimeAsync(0)

    expect(send.mock.calls[0][1]).toMatchObject({
      revision: null,
      status: 'unknown',
      stripe: 'attention',
      surface: 'tab'
    })
    expect(send.mock.calls[0][1]).not.toHaveProperty('identity')
  })

  it('dedupes an acknowledged revision/stripe per surface and fences an older revision', async () => {
    report(row, canonicalOutcome('done', { revision: 9 }), 'row')
    await vi.advanceTimersByTimeAsync(0)
    report(row, canonicalOutcome('done', { revision: 9 }), 'row')
    report(row, canonicalOutcome('stuck', { revision: 8 }), 'row')
    await vi.advanceTimersByTimeAsync(0)

    expect(send).toHaveBeenCalledTimes(1)

    report(row, canonicalOutcome('waiting', { revision: 10 }), 'row')
    await vi.advanceTimersByTimeAsync(0)
    expect(send).toHaveBeenCalledTimes(2)
  })

  it('reports each mounted surface (row, tab, dot) separately with its own surface name', async () => {
    const outcome = canonicalOutcome('done', { revision: 9 })

    for (const surface of ['row', 'tab', 'dot'] as const) {
      report(row, outcome, surface)
    }

    await vi.advanceTimersByTimeAsync(0)
    expect(send.mock.calls.map(call => call[1].surface).sort()).toEqual(['dot', 'row', 'tab'])
  })

  it('reports the stripe a surface actually painted, not the one the envelope intends', async () => {
    report(row, canonicalOutcome('stuck', { revision: 9 }), 'row', 'waiting')
    report(row, canonicalOutcome('done', { revision: 9 }), 'dot', null)
    report(row, { kind: 'done', status: {} }, 'tab', 'attention')
    await vi.advanceTimersByTimeAsync(0)

    const bySurface = Object.fromEntries(send.mock.calls.map(call => [call[1].surface, call[1]]))

    expect(bySurface.row).toMatchObject({ revision: 9, status: 'canonical', stripe: 'waiting' })
    expect(bySurface.dot).toMatchObject({ revision: 9, stripe: 'none' })
    expect(bySurface.tab).toMatchObject({ revision: null, status: 'unknown', stripe: 'attention' })
  })

  it('re-reports the same revision when the painted stripe changes', async () => {
    report(row, canonicalOutcome('done', { revision: 9 }), 'row')
    await vi.advanceTimersByTimeAsync(0)
    report(row, canonicalOutcome('done', { revision: 9 }), 'row', 'attention')
    await vi.advanceTimersByTimeAsync(0)
    expect(send.mock.calls.map(call => call[1].stripe)).toEqual(['done', 'attention'])
  })

  it('lets a new owner (owner_epoch / gateway) restart revisions below the old fence', async () => {
    report(row, canonicalOutcome('done', { revision: 50 }), 'row')
    await vi.advanceTimersByTimeAsync(0)
    report(
      row,
      canonicalOutcome('stuck', {
        revision: 2,
        identity: { gateway: 'gateway', profile: 'default', session_id: 'session', owner_epoch: 'epoch-2' }
      }),
      'row'
    )
    await vi.advanceTimersByTimeAsync(0)
    // An ABA repaint from the retired owner cannot reactivate that owner.
    report(row, canonicalOutcome('done', { revision: 51 }), 'row')
    await vi.advanceTimersByTimeAsync(0)
    expect(send).toHaveBeenCalledTimes(2)
    expect(send.mock.calls[1][1]).toMatchObject({ revision: 2, stripe: 'stuck' })
    expect(send.mock.calls[1][1].identity.owner_epoch).toBe('epoch-2')

    // Within the new owner the fence still applies.
    report(
      row,
      canonicalOutcome('waiting', {
        revision: 1,
        identity: { gateway: 'gateway', profile: 'default', session_id: 'session', owner_epoch: 'epoch-2' }
      }),
      'row'
    )
    await vi.advanceTimersByTimeAsync(0)
    expect(send).toHaveBeenCalledTimes(2)
  })

  it('a late retry of the old owner cannot fire after a new owner commits', async () => {
    send.mockRejectedValueOnce(new Error('offline'))
    report(row, canonicalOutcome('done', { revision: 50 }), 'row')
    await vi.advanceTimersByTimeAsync(0)
    report(
      row,
      canonicalOutcome('stuck', {
        revision: 1,
        identity: { gateway: 'gateway', profile: 'default', session_id: 'session', owner_epoch: 'epoch-2' }
      }),
      'row'
    )
    await vi.advanceTimersByTimeAsync(10 * 60_000)

    expect(send.mock.calls.map(call => [call[1].identity.owner_epoch, call[1].revision])).toEqual([
      ['epoch', 50],
      ['epoch-2', 1]
    ])
  })

  it('treats a transport that resolves { ok: false } as a failed send: bounded retry, never acked', async () => {
    send.mockResolvedValue({ ok: false })
    report(row, canonicalOutcome('stuck'), 'row')
    await vi.advanceTimersByTimeAsync(10 * 60_000)
    expect(send).toHaveBeenCalledTimes(COMMITTED_STRIPE_MAX_ATTEMPTS)

    send.mockResolvedValue({ ok: true })
    report(row, canonicalOutcome('stuck'), 'row')
    await vi.advanceTimersByTimeAsync(0)
    expect(send).toHaveBeenCalledTimes(COMMITTED_STRIPE_MAX_ATTEMPTS + 1)
  })

  it('fences a stale revision still in a retry queue behind a newer one', async () => {
    send.mockRejectedValueOnce(new Error('offline'))
    report(row, canonicalOutcome('done', { revision: 5 }), 'row')
    await vi.advanceTimersByTimeAsync(0)
    report(row, canonicalOutcome('stuck', { revision: 6 }), 'row')
    await vi.advanceTimersByTimeAsync(60_000)

    const revisions = send.mock.calls.map(call => call[1].revision)

    expect(revisions).toEqual([5, 6])
  })

  it('keeps separate sessions and connections independent', async () => {
    report(row, canonicalOutcome('done', { revision: 9 }), 'row')
    report({ ...row, connection_id: 'other' }, canonicalOutcome('done', { revision: 1 }), 'row')
    await vi.advanceTimersByTimeAsync(0)

    expect(send).toHaveBeenCalledTimes(2)
  })

  it('retries a failed report a bounded number of times, then stops', async () => {
    send.mockRejectedValue(new Error('offline'))
    report(row, canonicalOutcome('stuck'), 'row')
    await vi.advanceTimersByTimeAsync(10 * 60_000)

    expect(send).toHaveBeenCalledTimes(COMMITTED_STRIPE_MAX_ATTEMPTS)
    await flushCommittedStripeReports()
    await vi.advanceTimersByTimeAsync(10 * 60_000)
    expect(send).toHaveBeenCalledTimes(COMMITTED_STRIPE_MAX_ATTEMPTS)
  })

  it('re-sends a pending report on reconnect and then stops retrying it', async () => {
    send.mockRejectedValueOnce(new Error('offline'))
    report(row, canonicalOutcome('waiting'), 'row')
    await vi.advanceTimersByTimeAsync(0)
    expect(send).toHaveBeenCalledTimes(1)

    await flushCommittedStripeReports()
    expect(send).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(10 * 60_000)
    expect(send).toHaveBeenCalledTimes(2)
  })

  it('fences delayed mounts and old lifetime cleanup', async () => {
    const outcome = canonicalOutcome('done')
    reportCommittedStripe(row, outcome, 'row', 'done', 1)
    reportCommittedStripe(row, outcome, 'row', 'done', 1, false)
    reportCommittedStripe(row, outcome, 'row', 'done', 1)
    reportCommittedStripe(row, outcome, 'row', 'done', 2)
    reportCommittedStripe(row, outcome, 'row', 'done', 1, false)
    expect(send.mock.calls.map(call => [call[1].observation_lifetime, call[1].mounted])).toEqual([
      [1, true],
      [1, false],
      [2, true]
    ])
  })

  it('keeps simultaneous mounts independent when either one cleans up', async () => {
    const outcome = canonicalOutcome('done')
    reportCommittedStripe(row, outcome, 'row', 'waiting', 1)
    reportCommittedStripe(row, outcome, 'row', 'done', 2)
    reportCommittedStripe(row, outcome, 'row', 'done', 2, false)
    reportCommittedStripe(row, outcome, 'row', 'done', 1, false)
    expect(send.mock.calls.map(call => [call[1].observation_lifetime, call[1].mounted])).toEqual([
      [1, true],
      [2, true],
      [2, false],
      [1, false]
    ])
    const events = send.mock.calls.map(call => call[1])
    expect(new Set(events.map(event => event.observation_client)).size).toBe(1)
    expect(events[0].observation_client).toEqual(expect.any(String))
    expect(events.map(event => event.observation_order)).toEqual(
      [...events.map(event => event.observation_order)].sort((a, b) => a - b)
    )
  })

  it('a repaint back to an acknowledged stripe supersedes a different in-flight paint', async () => {
    reportCommittedStripe(row, canonicalOutcome('done'), 'row', 'done', 1)
    await vi.advanceTimersByTimeAsync(0)
    send.mockImplementationOnce(() => new Promise(() => {}))
    reportCommittedStripe(row, canonicalOutcome('done'), 'row', 'waiting', 1)
    reportCommittedStripe(row, canonicalOutcome('done'), 'row', 'done', 1)
    expect(send.mock.calls.map(call => call[1].stripe)).toEqual(['done', 'waiting', 'done'])
  })

  it('a late acknowledgement cannot ack a newer paint or cancel its retry', async () => {
    let finish!: (result: unknown) => void
    send.mockImplementationOnce(
      () =>
        new Promise(resolve => {
          finish = resolve
        })
    )
    reportCommittedStripe(row, canonicalOutcome('done'), 'row', 'done', 1)
    send.mockRejectedValueOnce(new Error('offline'))
    reportCommittedStripe(row, canonicalOutcome('done'), 'row', 'waiting', 1)
    await vi.advanceTimersByTimeAsync(0)
    finish({ ok: true })
    await vi.advanceTimersByTimeAsync(1_000)
    const events = send.mock.calls.map(call => call[1])
    expect(events.map(event => event.stripe)).toEqual(['done', 'waiting', 'waiting'])
    expect(events[2].observation_order).toBe(events[1].observation_order)
    expect(events[1].observation_order).toBeGreaterThan(events[0].observation_order)
  })

  it('tombstones even when the final envelope is behind the revision fence', async () => {
    reportCommittedStripe(row, canonicalOutcome('done', { revision: 8 }), 'row', 'done', 1)
    reportCommittedStripe(row, canonicalOutcome('done', { revision: 7 }), 'row', 'done', 1, false)
    expect(send.mock.calls.map(call => call[1].mounted)).toEqual([true, false])
  })

  it('bounds retained sessions and cancels evicted retry timers', async () => {
    send.mockRejectedValue(new Error('offline'))

    for (let i = 0; i < COMMITTED_STRIPE_MAX_ENTRIES + 10; i++) {
      report({ ...row, id: `chat-${i}` }, canonicalOutcome('done'), 'row')
      await vi.advanceTimersByTimeAsync(0)
    }

    send.mockClear()
    await flushCommittedStripeReports()
    expect(send).toHaveBeenCalledTimes(COMMITTED_STRIPE_MAX_ENTRIES)
    expect(send.mock.calls.some(call => call[0].id === 'chat-0')).toBe(false)
  })

  it('reconnect cannot duplicate an in-flight send or exceed its budget', async () => {
    let reject!: (error: Error) => void
    send.mockImplementation(
      () =>
        new Promise((_resolve, fail) => {
          reject = fail
        })
    )
    report(row, canonicalOutcome('done'), 'row')
    await flushCommittedStripeReports()
    await flushCommittedStripeReports()
    expect(send).toHaveBeenCalledTimes(1)
    reject(new Error('offline'))
    await vi.advanceTimersByTimeAsync(0)
    send.mockRejectedValue(new Error('offline'))

    for (let i = 0; i < 10; i++) {
      await flushCommittedStripeReports()
    }
    expect(send).toHaveBeenCalledTimes(COMMITTED_STRIPE_MAX_ATTEMPTS)
  })

  it('is a no-op without a reporter and never throws from a throwing reporter', async () => {
    setCommittedStripeReporter(null)
    expect(() => report(row, canonicalOutcome('done'), 'row')).not.toThrow()

    send.mockImplementation(() => {
      throw new Error('sync')
    })
    setCommittedStripeReporter(send)
    expect(() => report(row, canonicalOutcome('done'), 'row')).not.toThrow()
    await vi.advanceTimersByTimeAsync(0)
  })
})
