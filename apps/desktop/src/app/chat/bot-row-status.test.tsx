import { cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { BotRow } from '@/plugins/hermes-bots/bot-row'
import type { RosterRow } from '@/plugins/hermes-bots/types'
import { resetCommittedStripeReports, setCommittedStripeReporter } from '@/store/committed-stripe-report'
import { canonicalOutcome } from '@/test/canonical-outcome'
import type { CanonicalStripe } from '@/types/session-outcome'

const report = vi.fn()
beforeEach(() => {
  resetCommittedStripeReports()
  report.mockReset()
  setCommittedStripeReporter(report)
})
afterEach(() => {
  cleanup()
  setCommittedStripeReporter(null)
  resetCommittedStripeReports()
})

it.each<CanonicalStripe>(['done', 'waiting', 'stuck'])('the actual Bots row paints and reports canonical %s for its own owner', stripe => {
  const outcome = canonicalOutcome(stripe, {
    identity: { gateway: 'remote-gateway', profile: 'alpha', session_id: 'bot-tip', owner_epoch: 'epoch' }
  })

  const bot: RosterRow = {
    name: 'alpha', connectionId: 'remote-source', targetProfile: 'alpha', sourceScoped: true,
    canonical_session: { id: 'bot-root', resolved_id: 'bot-tip', outcome }
  }

  const view = render(<BotRow bot={bot} onDelete={vi.fn()} onEdit={vi.fn()} onGroup={vi.fn()} onNewSection={vi.fn()} />)
  expect(view.container.querySelector('[data-session-status-dot]')?.getAttribute('data-session-status-dot')).toBe(stripe)
  expect(report).toHaveBeenCalledWith(
    { connection_id: 'remote-source', profile: 'alpha', id: 'bot-tip' },
    expect.objectContaining({ mounted: true, surface: 'dot', stripe, revision: 7, identity: outcome.status?.identity })
  )
})

it('a roster without canonical protocol paints unknown rather than deriving green from activity', () => {
  const bot: RosterRow = { name: 'alpha', canonical_session: { id: 'bot-root', last_active: 1 } }
  const view = render(<BotRow bot={bot} onDelete={vi.fn()} onEdit={vi.fn()} onGroup={vi.fn()} onNewSection={vi.fn()} />)
  expect(view.container.querySelector('[data-session-status-dot]')?.getAttribute('data-session-status-dot')).toBe('attention')
})
