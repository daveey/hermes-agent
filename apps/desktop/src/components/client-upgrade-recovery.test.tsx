import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'

import { hasClientUpgradeNote } from '@/store/client-upgrade-recovery'
import { $notifications, clearNotifications, dismissNotification, notify, notifyError } from '@/store/notifications'
import { stubResizeObserver } from '@/test/jsdom'

import { NotificationStack } from './notifications'

const { openUpdatesWindow } = vi.hoisted(() => ({ openUpdatesWindow: vi.fn() }))
vi.mock('@/store/updates', () => ({ openUpdatesWindow }))
vi.mock('@/lib/hermes-cloud-host', () => ({ servedByHermesCloud: () => false }))

beforeAll(stubResizeObserver)
afterEach(() => {
  cleanup()
  for (const notification of $notifications.get()) {
    dismissNotification(notification.id)
  }
  localStorage.clear()
  vi.clearAllMocks()
})

test('rendered Update button retains visible durable recovery until explicit dismissal', async () => {
  notifyError(new Error('409: client_upgrade_required'), 'Edit failed')
  expect(hasClientUpgradeNote()).toBe(true)
  render(<NotificationStack />)
  fireEvent.click(screen.getByRole('button', { name: 'Update Hermes' }))
  await waitFor(() => expect(openUpdatesWindow).toHaveBeenCalledWith('client'))
  expect(hasClientUpgradeNote()).toBe(true)
  expect($notifications.get().map(item => item.id)).toEqual(['client-upgrade-required'])
  expect(screen.getByRole('button', { name: 'Update Hermes' })).toBeTruthy()
  act(() => clearNotifications())
  expect(hasClientUpgradeNote()).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Dismiss notification' }))
  await waitFor(() => expect(hasClientUpgradeNote()).toBe(false))
  expect($notifications.get()).toEqual([])
  act(() => {
    notifyError(new Error('client_upgrade_required'), 'Edit failed again')
  })
  expect(hasClientUpgradeNote()).toBe(true)
})

test.each(['action', 'secondaryAction'] as const)('ordinary %s still dismisses and invokes callback', async slot => {
  const onClick = vi.fn()
  const onDismiss = vi.fn()
  notify({ message: 'Ordinary notice', durationMs: 0, [slot]: { label: 'Continue', onClick }, onDismiss })
  render(<NotificationStack />)
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await waitFor(() => expect(onDismiss).toHaveBeenCalledOnce())
  expect(onDismiss).toHaveBeenCalledOnce()
  expect(onClick).toHaveBeenCalledOnce()
  expect($notifications.get()).toEqual([])
})

test('secondary action can also retain an acknowledgement notice', () => {
  const onClick = vi.fn()
  const onDismiss = vi.fn()
  notify({ message: 'Standing notice', secondaryAction: { label: 'Inspect', onClick, dismiss: false }, onDismiss })
  render(<NotificationStack />)
  fireEvent.click(screen.getByRole('button', { name: 'Inspect' }))
  expect(onClick).toHaveBeenCalledOnce()
  expect(onDismiss).not.toHaveBeenCalled()
  expect($notifications.get()).toHaveLength(1)
})
