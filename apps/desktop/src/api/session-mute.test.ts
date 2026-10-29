import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestProfile, setSessionMutedRemote } from '@/hermes'

// [daveey] session-outcome: the ⌘B park's write, through the real request helper (only the Electron transport is
// stubbed), so the path, method and body the backend sees are pinned.
describe('setSessionMutedRemote', () => {
  const api = vi.fn(async (_req: { body?: unknown; method?: string; path: string; profile?: string }) => ({
    muted: true,
    ok: true
  }))

  beforeEach(() => {
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = { api }
    api.mockClear()
  })

  afterEach(() => {
    setApiRequestProfile(null)
    delete (window as { hermesDesktop?: unknown }).hermesDesktop
  })

  const last = () => api.mock.calls.at(-1)![0]

  it('POSTs /api/sessions/{id}/mute with {muted, profile}', async () => {
    await expect(setSessionMutedRemote({ id: 's/1', profile: 'coder' }, true)).resolves.toEqual({
      muted: true,
      ok: true
    })

    expect(last().method).toBe('POST')
    expect(last().path).toBe('/api/sessions/s%2F1/mute')
    expect(last().body).toEqual({ muted: true, profile: 'coder' })
  })

  it('sends muted=false to unpark', async () => {
    await setSessionMutedRemote({ id: 's1', profile: 'coder' }, false)

    expect(last().body).toEqual({ muted: false, profile: 'coder' })
  })

  it('omits profile when neither the row nor the request scope names one', async () => {
    await setSessionMutedRemote({ id: 's1' }, true)

    expect(last().path).toBe('/api/sessions/s1/mute')
    expect(last().body).toEqual({ muted: true })
  })

  it('names the active profile for a row that carries none', async () => {
    setApiRequestProfile('ops')

    await setSessionMutedRemote({ id: 's1' }, true)

    expect(last().body).toEqual({ muted: true, profile: 'ops' })
  })
})
