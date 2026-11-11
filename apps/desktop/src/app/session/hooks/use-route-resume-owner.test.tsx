import { cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { useRouteResume } from './use-route-resume'

vi.mock('@/store/session-states', () => ({ markSelectionRestore: vi.fn() }))
afterEach(cleanup)

function Harness({
  resume,
  owner = true,
  sequence = 1,
  gatewayState = 'closed',
  view = 'chat'
}: {
  resume: Parameters<typeof useRouteResume>[0]['resumeSession']
  owner?: boolean
  sequence?: number
  gatewayState?: string
  view?: string
}) {
  useRouteResume({
    activeSessionId: null,
    activeSessionIdRef: { current: null },
    creatingSessionRef: { current: false },
    currentView: view,
    freshDraftReady: false,
    gatewayState,
    locationPathname: '/remote-session',
    resumeSession: resume,
    resumeFailedSessionId: null,
    resumeExhaustedSessionId: null,
    sessionResumeRequest: {
      sequence,
      sessionId: 'remote-session',
      ...(owner ? { ownerRoute: { connectionId: 'remote', profile: 'default' } } : {})
    },
    routedSessionId: 'remote-session',
    runtimeIdByStoredSessionIdRef: { current: new Map() },
    selectedStoredSessionId: null,
    selectedStoredSessionIdRef: { current: null },
    startFreshSessionDraft: vi.fn()
  })

  return null
}

it('dispatches an explicit remote open while the previous local gateway remains closed', () => {
  const resume = vi.fn(async () => undefined)
  const { rerender } = render(<Harness resume={resume} />)
  expect(resume).toHaveBeenCalledExactlyOnceWith('remote-session', true, { connectionId: 'remote', profile: 'default' })
  rerender(<Harness gatewayState="connecting" resume={resume} />)
  expect(resume).toHaveBeenCalledTimes(1)
  rerender(<Harness gatewayState="connecting" resume={resume} sequence={2} />)
  expect(resume).toHaveBeenCalledTimes(2)
})

it('still waits for the active gateway when the requested session has no explicit owner', () => {
  const resume = vi.fn(async () => undefined)
  const { rerender } = render(<Harness owner={false} resume={resume} />)
  expect(resume).not.toHaveBeenCalled()
  rerender(<Harness gatewayState="open" owner={false} resume={resume} />)
  expect(resume).toHaveBeenCalledOnce()
})

it('does not resume from a background settings view even with an explicit owner', () => {
  const resume = vi.fn(async () => undefined)
  render(<Harness resume={resume} view="settings" />)
  expect(resume).not.toHaveBeenCalled()
})
