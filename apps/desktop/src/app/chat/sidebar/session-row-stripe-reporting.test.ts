import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import type * as Hermes from '@/hermes'
import { canonicalOutcome } from '@/test/canonical-outcome'

const remote = vi.hoisted(() => ({ committed: vi.fn(), reason: vi.fn() }))

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof Hermes>()),
  reportSessionCommittedStripeRemote: remote.committed,
  reportSessionStripeRemote: remote.reason
}))

beforeEach(() => {
  vi.resetModules()
  vi.useFakeTimers()
  remote.committed.mockReset()
  remote.reason.mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

it.each(['committed', 'reason'] as const)(
  '%s wiring caps a 74-row burst at four per connection without delaying healthy links',
  async kind => {
    const releases: (() => void)[] = []
    const send = remote[kind]

    send.mockImplementation(() => new Promise<void>(resolve => releases.push(resolve)))
    // Import the actual sidebar module: bypassing either wrapper must break this test.
    await import('./session-row-stripe')
    const committed = await import('@/store/committed-stripe-report')
    const reason = await import('@/store/session-dot-state')

    const report = (id: string, connection_id?: string) => {
      const row = { connection_id, id, profile: 'default' }

      if (kind === 'committed') {
        committed.reportCommittedStripe(row, canonicalOutcome('done'), 'row', 'done')
      } else {
        reason.recordStripeTransition(row, 'stuck', 'needs-input')
      }
    }

    for (let i = 0; i < 74; i += 1) {
      report(`hub-${i}`, 'hub')
    }

    await vi.advanceTimersByTimeAsync(0)
    expect(send).toHaveBeenCalledTimes(4)
    expect(send.mock.calls.map(([row]) => row.id)).toEqual(['hub-0', 'hub-1', 'hub-2', 'hub-3'])

    // Four hanging hub POSTs must not hold the local connection or another healthy gateway.
    report('local')
    report('peer', 'peer')
    await vi.advanceTimersByTimeAsync(0)
    expect(send).toHaveBeenCalledTimes(6)
    expect(send.mock.calls.slice(4).map(([row]) => row.id)).toEqual(['local', 'peer'])

    releases[0]!()
    await vi.advanceTimersByTimeAsync(0)
    expect(send).toHaveBeenCalledTimes(7)
    expect(send.mock.calls[6]![0].id).toBe('hub-4')

    // Resolve every send, including queued ones, to exercise FIFO drain and slot reclamation.
    for (let i = 0; i < 76; i += 1) {
      releases[i]!()
      await vi.advanceTimersByTimeAsync(0)
    }

    expect(send.mock.calls.filter(([row]) => row.connection_id === 'hub').map(([row]) => row.id)).toEqual(
      Array.from({ length: 74 }, (_, i) => `hub-${i}`)
    )
    report('hub-next', 'hub')
    await vi.advanceTimersByTimeAsync(0)
    expect(send).toHaveBeenCalledTimes(77)
    releases[76]!()
    await vi.advanceTimersByTimeAsync(0)
    committed.resetCommittedStripeReports()
    reason.resetStripeTransitionLog()
  }
)
