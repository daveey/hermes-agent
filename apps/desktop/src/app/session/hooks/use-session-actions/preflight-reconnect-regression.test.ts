import { afterEach, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { en } from '@/i18n/en'
import { $confirmRequest, settleConfirm } from '@/store/confirm'

import { confirmLinkedArchive } from './confirm-linked-archive'

afterEach(() => { settleConfirm(false); vi.unstubAllGlobals(); vi.useRealTimers() })

it('queues a reconnect instead of asking the user to override unknown task safety', async () => {
  vi.useFakeTimers()
  let failed = false

  const api = vi.fn(async ({ path }) => {
    if (!failed) {failed = true; throw new Error('socket hang up')}

    return path.startsWith('/api/sessions/') ? { id: 'regression' } : { tasks: [] }
  })

  vi.stubGlobal('hermesDesktop', { api })
  const pending = confirmLinkedArchive('regression', { id: 'regression', connection_id: 'owner', kanban_unknown: true } as SessionInfo, 'work', en.desktop)
  await vi.advanceTimersByTimeAsync(0)
  expect($confirmRequest.get()).toBeNull()
  await vi.advanceTimersByTimeAsync(1_000)
  expect(await pending).toBe(true)
  expect(api.mock.calls).toHaveLength(3)
})
