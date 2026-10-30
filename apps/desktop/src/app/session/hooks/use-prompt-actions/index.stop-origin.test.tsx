import { cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { actRender, Harness, type HarnessHandle, RUNTIME_SESSION_ID } from './index-test-utils'

// [daveey] Kept out of index.test.tsx, which is over the file-length cap.
describe('cancelRun names the Stop gesture', () => {
  afterEach(cleanup)

  // Stop button and Esc both land on cancelRun; the gesture it is handed must reach the gateway so agent.log
  // can tell them apart. A MouseEvent (cancelRun wired straight to onClick) or no argument sends no origin.
  it.each([
    ['stop_button', { origin: 'stop_button', session_id: RUNTIME_SESSION_ID }],
    ['esc', { origin: 'esc', session_id: RUNTIME_SESSION_ID }],
    [undefined, { session_id: RUNTIME_SESSION_ID }],
    [{ type: 'click' }, { session_id: RUNTIME_SESSION_ID }]
  ])('origin %o', async (origin, params) => {
    const requestGateway = vi.fn(
      async (method: string, p?: Record<string, unknown>) =>
        (method === 'prompt.submit' ? { admission_id: p?.submission_id, status: 'started' } : {}) as never
    )

    let handle: HarnessHandle | null = null
    await actRender(
      <Harness
        onReady={h => (handle = h)}
        rawAdmissionReceipts
        refreshSessions={async () => undefined}
        requestGateway={requestGateway}
      />
    )
    await handle!.submitText('first turn')
    await (handle!.cancelRun as (o?: unknown) => Promise<void>)(origin)

    expect(requestGateway).toHaveBeenCalledWith('session.interrupt', params)
  })
})
