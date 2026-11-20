// @vitest-environment jsdom
// [daveey] The version pills poll every 10 s: the request must never cold-start a backend (passive) nor count as
// user activity (no `foreground` priority, which opens the sleep gate in electron/power-settle.ts).
import { afterEach, describe, expect, it, vi } from 'vitest'

import { getHermesUpdateProgress } from './system'

afterEach(() => {
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
})

describe('getHermesUpdateProgress', () => {
  it('is a passive background read, pinned to this machine when asked', async () => {
    const api = vi.fn().mockResolvedValue({ progress: null })

    window.hermesDesktop = { api } as never

    await getHermesUpdateProgress({ connectionId: 'local' })
    await getHermesUpdateProgress()

    const [local, ambient] = api.mock.calls.map(([request]) => request)

    expect(local).toMatchObject({ connectionId: 'local', passive: true, path: '/api/hermes/update/progress' })
    expect(local.priority).toBeUndefined()
    expect(ambient).toMatchObject({ passive: true, path: '/api/hermes/update/progress' })
    expect(ambient.priority).toBeUndefined()
  })
})
