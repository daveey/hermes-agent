import { afterEach, describe, expect, it } from 'vitest'

import { peekUiLatency, resetUiLatencyForTests, UI_LATENCY_UNBOUND } from '@/lib/ui-latency'

import { newSessionLatency } from './new-session-latency'

afterEach(resetUiLatencyForTests)

describe('newSessionLatency', () => {
  it('a user New session starts the span expecting an unbound draft composer', () => {
    newSessionLatency.draft(false)

    expect(peekUiLatency('ui.new_session')).toEqual({ expect: { runtimeId: null }, ids: {} })
  })

  it.each([
    ['route-replacing teardown (delete / archive / dead id)', true],
    ['route resume', { replaceRoute: true, rotateFreshDraftKey: false }],
    ['dashboard request that keeps the route', { preserveRoute: true }]
  ] as const)('the %s form is not a click and starts nothing', (_label, options) => {
    newSessionLatency.draft(options)

    expect(peekUiLatency('ui.new_session')).toBeNull()
  })

  it('a workspace-targeted draft (project "+") is a click', () => {
    newSessionLatency.draft({ workspaceTarget: '/ws' })

    expect(peekUiLatency('ui.new_session')).not.toBeNull()
  })

  it('a tile "+" is unbound until the created session is known, then bound to it, ids only', () => {
    newSessionLatency.tileClick()
    expect(peekUiLatency('ui.new_session')?.expect.runtimeId).toBe(UI_LATENCY_UNBOUND)

    newSessionLatency.tileCreated({ connectionId: 'mini', profile: 'p' }, 'stored-1', 'rt-1')

    expect(peekUiLatency('ui.new_session')).toEqual({
      expect: { runtimeId: 'rt-1' },
      ids: { gateway: 'mini', profile: 'p', session_id: 'stored-1' }
    })
  })

  it('a failed create cancels the span', () => {
    newSessionLatency.tileClick()
    newSessionLatency.cancel()

    expect(peekUiLatency('ui.new_session')).toBeNull()
  })
})
