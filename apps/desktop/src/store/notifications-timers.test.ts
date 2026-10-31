import { afterEach, expect, test, vi } from 'vitest'

import { $notifications, clearNotifications, dismissNotification, notify } from './notifications'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  clearNotifications()
  vi.useRealTimers()
})

test('auto-dismiss is harmless after the browser environment is torn down', () => {
  let dismiss!: () => void
  vi.spyOn(window, 'setTimeout').mockImplementation(callback => {
    dismiss = callback as () => void

    return 1 as unknown as ReturnType<typeof window.setTimeout>
  })
  const onDismiss = vi.fn()
  notify({ message: 'Plugin enabled', onDismiss })
  vi.stubGlobal('window', undefined)

  expect(() => dismiss()).not.toThrow()
  expect(onDismiss).not.toHaveBeenCalled()
})

test('auto-dismiss still removes a live toast and calls its callback once', () => {
  vi.useFakeTimers()
  const onDismiss = vi.fn()
  const id = notify({ message: 'Plugin enabled', onDismiss })

  vi.advanceTimersByTime(5_000)

  expect($notifications.get().find(item => item.id === id)).toBeUndefined()
  expect(onDismiss).toHaveBeenCalledTimes(1)
  expect(vi.getTimerCount()).toBe(0)
})

test('reset and explicit dismissal cancel pending auto-dismiss callbacks', () => {
  vi.useFakeTimers()
  const onDismiss = vi.fn()
  const id = notify({ message: 'Plugin enabled', onDismiss })
  dismissNotification(id)
  notify({ message: 'Plugin disabled', onDismiss })
  clearNotifications()

  expect(vi.getTimerCount()).toBe(0)
  vi.advanceTimersByTime(5_000)
  expect(onDismiss).toHaveBeenCalledTimes(2)
  expect($notifications.get()).toEqual([])
})
