import { afterEach, describe, expect, it, vi } from 'vitest'

import { emitGatewayEvent } from '@/contrib/events'

import { installTranscriptPushForwarder, sessionsMovedForward } from './transcript-push'

const moved = (payload: unknown, extra: Record<string, unknown> = {}) =>
  ({ payload, type: 'sessions.moved', ...extra }) as unknown as Parameters<typeof emitGatewayEvent>[0]

describe('[daveey] sessions.moved forwarder', () => {
  let dispose: () => void = () => {}

  afterEach(() => dispose())

  it('forwards each socket frame to main with the connection that socket stamped', () => {
    const forward = vi.fn()
    dispose = installTranscriptPushForwarder(forward)
    const sessions = [{ id: 'a', last_message_id: 7, profile: 'default' }]

    emitGatewayEvent(moved({ sessions }, { connectionId: 'hermes-hub' }))
    emitGatewayEvent(moved({ sessions }, { connectionId: 'mac-mini' }))

    expect(forward.mock.calls).toEqual([
      ['hermes-hub', sessions],
      ['mac-mini', sessions]
    ])
  })

  it("names the registry primary ('') for an unstamped legacy-primary frame", () => {
    expect(sessionsMovedForward(moved({ sessions: [{ id: 'a' }] }))).toEqual({
      connectionId: '',
      sessions: [{ id: 'a' }]
    })
  })

  it('drops empty or malformed frames and other event types', () => {
    const forward = vi.fn()
    dispose = installTranscriptPushForwarder(forward)

    emitGatewayEvent(moved({ sessions: [] }, { connectionId: 'x' }))
    emitGatewayEvent(moved(null, { connectionId: 'x' }))
    emitGatewayEvent({ payload: { sessions: [{ id: 'a' }] }, type: 'sessions.changed' } as never)

    expect(forward).not.toHaveBeenCalled()
  })

  it('is a no-op without the Electron bridge (browser host)', () => {
    dispose = installTranscriptPushForwarder(undefined)
    expect(() => emitGatewayEvent(moved({ sessions: [{ id: 'a' }] }))).not.toThrow()
  })
})
