import { afterEach, expect, vi } from 'vitest'

import type { startUiPreferencesSync, UiPreferencesBridge } from './sync'

const stops = new Set<() => void>()
// Bound on waiting for the 150ms debounce to reach the bridge. A loaded host can
// fire that timer late; this is a condition wait, so an idle run never waits it out.
const ADMISSION = { timeout: 10_000 }

afterEach(() => [...stops].forEach(stop => stop()))

/** Observe real transport completion, including a seed write started by join(). */
export function observeSync(start: typeof startUiPreferencesSync, bridge: UiPreferencesBridge) {
  const read = vi.spyOn(bridge, 'read')
  const write = vi.spyOn(bridge, 'write')
  const stopSync = start(bridge)

  const stop = () => {
    if (stops.delete(stop)) {
      stopSync()
    }
  }

  stops.add(stop)

  const connected = async () => {
    const snapshot = await read.mock.results[0].value
    await Promise.all(write.mock.results.map(result => result.value))

    if (snapshot.exists || write.mock.results.length > 0) {
      await vi.waitFor(() => expect(window.localStorage.getItem('hermes:ui-preferences:generation')).toBeTruthy())
    }
  }

  const writeAfter = async (action: () => unknown) => {
    const count = write.mock.calls.length
    await action()
    // Wait for the debounce to enter the bridge, then await the actual I/O.
    // The writer may take longer than waitFor's polling budget.
    await vi.waitFor(() => expect(write.mock.calls.length).toBeGreaterThan(count), ADMISSION)
    await Promise.all(write.mock.results.slice(count).map(result => result.value))
  }

  const flushed = async () => {
    await vi.waitFor(() => expect(write.mock.calls.some(([payload]) => !payload.seed)).toBe(true), ADMISSION)
    await Promise.all(write.mock.results.map(result => result.value))
  }

  return { connected, flushed, stop, write, writeAfter }
}
