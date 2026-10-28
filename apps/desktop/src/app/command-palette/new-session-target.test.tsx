// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'
import { registerBuiltinActionRunner } from '@/lib/keybinds/plugin-actions'
import { $commandPaletteOpen } from '@/store/command-palette'

vi.mock('@/hermes', async importActual => ({
  ...(await importActual<typeof HermesApi>()),
  getHermesConfigRecord: async () => ({}),
  listAllProfileSessions: async () => ({ sessions: [], total: 0 })
}))

import { CommandPalette } from './index'

// [daveey] The palette's "New session" row is ⌘N: it lands on the saved
// new-session gateway (session.new), not whichever gateway is on screen.
describe('command palette New session', () => {
  afterEach(() => {
    act(() => $commandPaletteOpen.set(false))
    cleanup()
  })

  it('runs the session.new action', async () => {
    const runner = vi.fn(() => true)
    const unregister = registerBuiltinActionRunner(runner)

    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <CommandPalette />
        </MemoryRouter>
      </QueryClientProvider>
    )
    act(() => $commandPaletteOpen.set(true))

    const row = await waitFor(() => {
      const found = document.querySelector('[data-value$="nav-new"]')
      expect(found).not.toBeNull()

      return found!
    })

    fireEvent.click(row)

    expect(runner).toHaveBeenCalledWith('session.new')
    unregister()
  })
})
