// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'
import { $commandPaletteOpen } from '@/store/command-palette'
import type * as ProjectsStore from '@/store/projects'
import { $projectTree } from '@/store/projects'
import { $selectedStoredSessionId, $sessions } from '@/store/session'
import { makeSessionInfo } from '@/test/session-info'

const moveSessionToProject = vi.fn(async () => undefined)
const goToProject = vi.fn()

vi.mock('@/hermes', async importActual => ({
  ...(await importActual<typeof HermesApi>()),
  getHermesConfigRecord: async () => ({}),
  listAllProfileSessions: async () => ({ sessions: [], total: 0 })
}))

vi.mock('@/store/projects', async importActual => ({
  ...(await importActual<typeof ProjectsStore>()),
  goToProject: (...args: unknown[]) => goToProject(...args),
  moveSessionToProject: (...args: unknown[]) => moveSessionToProject(...(args as [])),
  refreshProjectTree: async () => undefined
}))

import { CommandPalette } from './index'

const fabric = { id: '/repos/fabric', label: 'fabric', path: '/repos/fabric', repos: [], sessionCount: 0 }
const other = { id: '/repos/other', label: 'other', path: '/repos/other', repos: [], sessionCount: 0 }

const renderPalette = () =>
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <CommandPalette />
      </MemoryRouter>
    </QueryClientProvider>
  )

const typeSearch = (text: string) => {
  const input = screen.getByRole('combobox')
  fireEvent.change(input, { target: { value: text } })
}

describe('⌘P: move the current session to a typed project', () => {
  beforeEach(() => {
    $projectTree.set([fabric, other])
    $sessions.set([makeSessionInfo({ cwd: '/repos/other', id: 'chat-1', profile: 'default', title: 'My chat' })])
    $selectedStoredSessionId.set('chat-1')
  })

  afterEach(() => {
    act(() => $commandPaletteOpen.set(false))
    cleanup()
    $projectTree.set([])
    $sessions.set([])
    $selectedStoredSessionId.set(null)
    moveSessionToProject.mockClear()
    goToProject.mockClear()
  })

  it('lists "Move to project: fabric" next to the go-to row and moves the chat, not the view', async () => {
    renderPalette()
    act(() => $commandPaletteOpen.set(true))
    typeSearch('fabric')

    await screen.findAllByText(/fabric/)
    const row = document.querySelector('[data-value$="move-current-to-project-/repos/fabric"]')
    expect(row?.textContent).toBe('Move to project: fabric')
    // The plain go-to-project row is still there too.
    expect(
      document.querySelector('[data-value$="project-/repos/fabric"]:not([data-value*="move-current"])')
    ).not.toBeNull()
    fireEvent.click(row!)

    expect(moveSessionToProject).toHaveBeenCalledWith('chat-1', '/repos/fabric', 'default')
    expect(goToProject).not.toHaveBeenCalled()
  })

  it('does not offer the project the chat already lives in', async () => {
    renderPalette()
    act(() => $commandPaletteOpen.set(true))
    typeSearch('other')
    await screen.findAllByText(/other/)
    expect(document.querySelector('[data-value*="move-current-to-project-/repos/other"]')).toBeNull()
  })

  it('offers no move rows without a current session', async () => {
    $selectedStoredSessionId.set(null)
    renderPalette()
    act(() => $commandPaletteOpen.set(true))
    typeSearch('fabric')
    await screen.findAllByText(/fabric/)
    expect(document.querySelector('[data-value*="move-current-to-project-"]')).toBeNull()
  })
})
