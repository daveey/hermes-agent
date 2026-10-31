import { afterEach, expect, test, vi } from 'vitest'

import { requestSessionSearchFocus, SESSION_SEARCH_FOCUS_EVENT } from './layout'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

test('queued session-search focus tolerates browser teardown', () => {
  let focus!: () => void
  vi.spyOn(window, 'setTimeout').mockImplementation(callback => {
    focus = callback as () => void

    return 1 as unknown as ReturnType<typeof window.setTimeout>
  })
  requestSessionSearchFocus()
  vi.stubGlobal('window', undefined)

  expect(() => focus()).not.toThrow()
})

test('queued session-search focus still dispatches in a live browser', () => {
  vi.useFakeTimers()
  const listener = vi.fn()
  window.addEventListener(SESSION_SEARCH_FOCUS_EVENT, listener)

  try {
    requestSessionSearchFocus()
    vi.runOnlyPendingTimers()
    expect(listener).toHaveBeenCalledTimes(1)
  } finally {
    window.removeEventListener(SESSION_SEARCH_FOCUS_EVENT, listener)
    vi.useRealTimers()
  }
})
