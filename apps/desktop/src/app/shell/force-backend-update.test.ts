import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { en } from '@/i18n/en'
import { $confirmRequest, settleConfirm } from '@/store/confirm'
import { $notifications, clearNotifications } from '@/store/notifications'

import { forceBackendUpdate } from './force-backend-update'

const api = vi.hoisted(() => ({ forceHermesUpdate: vi.fn(), getForceUpdateState: vi.fn() }))

vi.mock('@/api/system', () => api)

const copy = en.shell.statusbar.forceUpdate

async function untilConfirmAsked() {
  await vi.waitFor(() => expect($confirmRequest.get()).not.toBeNull())

  return $confirmRequest.get()!
}

beforeEach(() => {
  api.getForceUpdateState.mockResolvedValue({ available: true, pending: null, running_turns: 3 })
  api.forceHermesUpdate.mockResolvedValue({ expires_at: 1800, ok: true, requested_at: 0, running_turns: 3 })
})

afterEach(() => {
  settleConfirm(false)
  $confirmRequest.set(null)
  clearNotifications()
  $notifications.set([])
  vi.clearAllMocks()
})

describe('forceBackendUpdate', () => {
  it('confirms with the running-turn count, POSTs on confirm and reports the request', async () => {
    const done = forceBackendUpdate(copy)
    const asked = await untilConfirmAsked()

    expect(asked.title).toBe('Force the backend update now?')
    expect(asked.description).toBe(
      '3 running turns will be stopped. Turns that can resume continue after the update; others need to be sent again.'
    )
    expect(api.forceHermesUpdate).not.toHaveBeenCalled()

    settleConfirm(true, asked)
    await done

    expect(api.forceHermesUpdate).toHaveBeenCalledTimes(1)
    expect($notifications.get()[0]).toMatchObject({
      kind: 'success',
      message: 'Update requested; the backend restarts within a few minutes'
    })
  })

  it('names one running turn, and says when none is running', () => {
    expect(copy.confirmBody(1)).toMatch(/^1 running turn will be stopped\./)
    expect(copy.confirmBody(0)).toMatch(/^No turns are running right now\./)
  })

  it('does not POST when the confirm is cancelled', async () => {
    const done = forceBackendUpdate(copy)

    settleConfirm(false, await untilConfirmAsked())
    await done

    expect(api.forceHermesUpdate).not.toHaveBeenCalled()
    expect($notifications.get()).toEqual([])
  })

  it('shows an error toast with the server message when the POST fails', async () => {
    api.forceHermesUpdate.mockRejectedValue(new Error('409: {"detail":"No background core update is pending"}'))
    const done = forceBackendUpdate(copy)

    settleConfirm(true, await untilConfirmAsked())
    await done

    const toast = $notifications.get()[0]

    expect(toast?.kind).toBe('error')
    expect(`${toast?.title ?? ''} ${toast?.message ?? ''} ${toast?.detail ?? ''}`).toContain(
      'No background core update is pending'
    )
  })

  it('shows the server message and asks nothing when no update can be forced', async () => {
    api.getForceUpdateState.mockResolvedValue({
      available: false,
      message: 'No background core update is pending on this host.',
      pending: null,
      running_turns: 2
    })

    await forceBackendUpdate(copy)

    expect($confirmRequest.get()).toBeNull()
    expect(api.forceHermesUpdate).not.toHaveBeenCalled()
    expect($notifications.get()[0]).toMatchObject({
      kind: 'warning',
      message: 'No background core update is pending on this host.'
    })
  })
})
