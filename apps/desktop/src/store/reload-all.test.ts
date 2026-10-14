import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as Notifications from '@/store/notifications'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection } from '@/store/session'

import { reloadAppAndBackend } from './reload-all'

type NotificationsModule = typeof Notifications

const mocks = vi.hoisted(() => ({
  notifyError: vi.fn(),
  reconnectGateway: vi.fn()
}))

vi.mock('@/store/gateway-reconnect', () => ({ reconnectGateway: mocks.reconnectGateway }))
vi.mock('@/store/notifications', async importOriginal => ({
  ...(await importOriginal<NotificationsModule>()),
  notifyError: mocks.notifyError
}))

describe('reloadAppAndBackend (⌘R)', () => {
  const recycleBackend = vi.fn()

  beforeEach(() => {
    recycleBackend.mockReset().mockResolvedValue({ ok: true })
    mocks.reconnectGateway.mockReset().mockResolvedValue(undefined)
    mocks.notifyError.mockReset()
    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = { recycleBackend }
    $connection.set(null)
    $activeGatewayProfile.set('default')
  })

  afterEach(() => {
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  })

  it('restarts the local backend for the profile in view, then reloads the window', async () => {
    const order: string[] = []
    recycleBackend.mockImplementation(async () => {
      order.push('recycle')

      return { ok: true }
    })

    await reloadAppAndBackend(() => order.push('reload'))

    expect(recycleBackend).toHaveBeenCalledWith('default')
    expect(order).toEqual(['recycle', 'reload'])
    expect(mocks.reconnectGateway).not.toHaveBeenCalled()
  })

  it('only reconnects a remote gateway (not ours to restart) before reloading', async () => {
    $connection.set({ mode: 'remote' } as never)
    const reload = vi.fn()

    await reloadAppAndBackend(reload)

    expect(mocks.reconnectGateway).toHaveBeenCalledOnce()
    expect(recycleBackend).not.toHaveBeenCalled()
    expect(reload).toHaveBeenCalledOnce()
  })

  it('still reloads the window when the restart fails, and reports the failure', async () => {
    recycleBackend.mockRejectedValue(new Error('boom'))
    const reload = vi.fn()

    await reloadAppAndBackend(reload)

    expect(mocks.notifyError).toHaveBeenCalledOnce()
    expect(reload).toHaveBeenCalledOnce()
  })

  it('ignores a second ⌘R while the first restart is still running', async () => {
    let finish: () => void = () => {}
    recycleBackend.mockImplementation(() => new Promise(resolve => (finish = () => resolve({ ok: true }))))
    const reload = vi.fn()

    const first = reloadAppAndBackend(reload)
    await reloadAppAndBackend(reload)
    finish()
    await first

    expect(recycleBackend).toHaveBeenCalledOnce()
    expect(reload).toHaveBeenCalledOnce()
  })
})
