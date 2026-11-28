import { JsonRpcGatewayError } from '@hermes/shared'
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { en } from '@/i18n/en'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection, $sessions } from '@/store/session'
import { $restartingSessionResumes } from '@/store/session-resume-restart'
import { $sessionStates } from '@/store/session-states'

import { storedJournal } from './prepared-submissions-test-utils'
import { useSubmitPrompt } from './submit'

vi.mock('@/store/gateway', async original => ({
  ...(await original<Record<string, unknown>>()),
  retainGatewayForSessionTurn: vi.fn(async () => () => undefined)
}))

beforeEach(() => {
  localStorage.clear()
  $sessions.set([])
  $sessionStates.set({})
  $connection.set(null)
  $activeGatewayProfile.set('default')
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

const draining = () => new JsonRpcGatewayError('runtime_draining', { code: 4001, data: { reason: 'runtime_draining' } })
const legacyRefusal = () => new JsonRpcGatewayError("unknown field 'submission_id'", { code: 4000 })

function mount(request: ReturnType<typeof vi.fn>, runtime: string | null = 'old-runtime') {
  let state = createClientSessionState('stored-a')
  const selected = { current: 'stored-a' as string | null }

  const deps = {
    activeSessionIdRef: { current: runtime },
    selectedStoredSessionIdRef: selected,
    busyRef: { current: false },
    copy: en.desktop,
    createBackendSessionForSend: vi.fn(async () => null),
    getRoutedStoredSessionId: () => null,
    getRuntimeIdForStoredSession: () => runtime,
    getRouteToken: () => 'same-route',
    requestGateway: request,
    runtimeIdByStoredSessionIdRef: { current: new Map([['stored-a', runtime]]) },
    resumeStoredSession: vi.fn(),
    syncAttachmentsForSubmit: vi.fn(async (sessionId: string) => ({ sessionId, attachments: [] })),
    updateSessionState: vi.fn((_id: string, update: (s: typeof state) => typeof state) => {
      state = update(state)

      return state
    })
  }

  const hook = renderHook(() => useSubmitPrompt(deps as never))

  return {
    send: () => hook.result.current('after restart', { submission_id: 'same-input' }),
    selected,
    state: () => state
  }
}

function receipt(params?: Record<string, unknown>) {
  return { status: 'started', session_id: params?.session_id, submission_id: params?.submission_id, user_row_id: 42 }
}

it('actual pre-send resume waits through drain before admitting the one prompt', async () => {
  vi.useFakeTimers()
  let resumes = 0

  const request = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === 'session.resume') {
      if (++resumes === 1) {
        throw draining()
      }

      return { session_id: 'new-runtime' }
    }

    return receipt(params)
  })

  const view = mount(request, null)
  let sent!: Promise<boolean>
  await act(async () => {
    sent = view.send()
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(request.mock.calls.filter(([method]) => method === 'prompt.submit')).toHaveLength(0)
  expect($restartingSessionResumes.get().size).toBe(1)
  await act(async () => {
    await vi.runAllTimersAsync()
    expect(await sent).toBe(true)
  })
  const submits = request.mock.calls.filter(([method]) => method === 'prompt.submit')
  expect(submits).toHaveLength(1)
  expect(submits[0][1]).toMatchObject({ session_id: 'new-runtime', submission_id: 'same-input' })
  expect(view.state().messages.some(message => message.error)).toBe(false)
  expect($restartingSessionResumes.get().size).toBe(0)
})

it.each(['drain', 'closed'] as const)(
  'identified submit survives %s using the same payload and no error bubble',
  async failure => {
    vi.useFakeTimers()

    const request = vi
      .fn()
      .mockRejectedValueOnce(failure === 'drain' ? draining() : new Error('Hermes gateway connection closed'))
      .mockImplementation(async (_method, params) => receipt(params))

    const view = mount(request)
    let sent!: Promise<boolean>
    await act(async () => {
      sent = view.send()
      await vi.advanceTimersByTimeAsync(0)
    })
    expect($restartingSessionResumes.get().size).toBe(1)
    await act(async () => {
      await vi.runAllTimersAsync()
      expect(await sent).toBe(true)
    })
    expect(request).toHaveBeenCalledTimes(2)
    expect(request.mock.calls[0][1]).toEqual(request.mock.calls[1][1])
    expect(view.state().messages.some(message => message.error)).toBe(false)
    expect(storedJournal()).toEqual({})
    expect($restartingSessionResumes.get().size).toBe(0)
  }
)

it.each(['queued', 'unknown'] as const)('a restart-wait %s receipt is accepted, never replayed', async status => {
  const request = vi.fn(async (_method, params) => ({
    ...receipt(params),
    status,
    queued_reason: 'core_update',
    admission_id: 'held'
  }))
  const view = mount(request)
  await act(async () => {
    expect(await view.send()).toBe(true)
  })
  expect(request).toHaveBeenCalledOnce()
  expect(view.state().messages.some(message => message.error)).toBe(false)
})

it.each(['closed', 'drain'] as const)(
  'never downgrades an uncertain %s replay on the replacement legacy owner',
  async failure => {
    vi.useFakeTimers()
    const request = vi
      .fn()
      .mockRejectedValueOnce(failure === 'drain' ? draining() : new Error('Hermes gateway connection closed'))
      .mockRejectedValue(legacyRefusal())
    const view = mount(request)
    await act(async () => {
      const sent = view.send()
      await vi.runAllTimersAsync()
      expect(await sent).toBe(false)
    })
    expect(request).toHaveBeenCalledTimes(2)
    expect(request.mock.calls.every(([, params]) => params.submission_id === 'same-input')).toBe(true)
  }
)

it('does not replay an identityless legacy send whose ACK was lost', async () => {
  const request = vi
    .fn()
    .mockRejectedValueOnce(legacyRefusal())
    .mockRejectedValue(new Error('Hermes gateway connection closed'))
  const view = mount(request)
  await act(async () => {
    expect(await view.send()).toBe(false)
  })
  expect(request).toHaveBeenCalledTimes(2)
  expect(request.mock.calls[1][1]).not.toHaveProperty('submission_id')
  expect($restartingSessionResumes.get().size).toBe(0)
})

it('navigation during restart cancels the pending intent without sending into the new chat', async () => {
  vi.useFakeTimers()
  const request = vi
    .fn()
    .mockRejectedValueOnce(draining())
    .mockImplementation(async (_method, params) => receipt(params))
  const view = mount(request)
  let sent!: Promise<boolean>
  await act(async () => {
    sent = view.send()
    await vi.advanceTimersByTimeAsync(0)
  })
  view.selected.current = 'other-chat'
  await act(async () => {
    await vi.runAllTimersAsync()
    expect(await sent).toBe(false)
  })
  expect(request).toHaveBeenCalledOnce()
  expect(view.state().messages.some(message => message.error)).toBe(false)
  expect($restartingSessionResumes.get().size).toBe(0)
})
