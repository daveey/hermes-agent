// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setSessionArchived } from '@/hermes'
import { en } from '@/i18n/en'
import { $messagingSessions, $sessions, setMessagingSessions, setSessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { SessionsSettings } from './sessions-settings'

vi.mock('@/i18n', () => ({ useI18n: () => ({ t: en }) }))

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getHermesConfigRecord: vi.fn().mockResolvedValue({ config: {} }),
  setSessionArchived: vi.fn().mockResolvedValue(undefined)
}))

// The settings page reads its archived list through the store's page cursor,
// which reads the sessions API module directly — mock that seam, not the barrel.
const listAllProfileSessions = vi.hoisted(() => vi.fn())

vi.mock('@/api/sessions', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  listAllProfileSessions
}))

const selectDesktopPaths = vi.hoisted(() => vi.fn())

vi.mock('@/lib/desktop-fs', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  selectDesktopPaths
}))

const archivedMatrixSession = {
  archived: true,
  ended_at: null,
  id: 'matrix-1',
  input_tokens: 0,
  is_active: false,
  last_active: 1,
  message_count: 2,
  model: null,
  output_tokens: 0,
  preview: null,
  source: 'matrix',
  started_at: 1,
  title: 'archived room',
  tool_call_count: 0
} as SessionInfo

beforeEach(() => {
  setSessions([])
  setMessagingSessions([])
  listAllProfileSessions.mockReset().mockResolvedValue({ sessions: [archivedMatrixSession], total: 1 })
})

afterEach(() => {
  cleanup()
})

describe('SessionsSettings unarchive', () => {
  it('restores a messaging-source session into $messagingSessions, not $sessions', async () => {
    render(<SessionsSettings />)
    const button = await screen.findByRole('button', { name: en.settings.sessions.unarchive })

    await act(async () => fireEvent.click(button))

    await waitFor(() => expect(setSessionArchived).toHaveBeenCalledWith('matrix-1', false, undefined))
    expect($messagingSessions.get().map(session => session.id)).toEqual(['matrix-1'])
    expect($messagingSessions.get()[0]?.archived).toBe(false)
    expect($sessions.get()).toEqual([])
  })
})

describe('SessionsSettings archived list paging', () => {
  const archived = (id: string): SessionInfo => ({ ...archivedMatrixSession, id, title: `chat ${id}` })

  // 2026-11-17: an archive of 16,899 rows was read to the end before any row was listed.
  it('lists the first page of a deep archive and reads the next on request', async () => {
    listAllProfileSessions
      .mockReset()
      .mockResolvedValueOnce({ sessions: [archived('a')], total: 16_899 })
      .mockResolvedValueOnce({ sessions: [archived('b')], total: 16_899 })

    render(<SessionsSettings />)

    expect(await screen.findByText('chat a')).toBeTruthy()
    expect(listAllProfileSessions).toHaveBeenCalledTimes(1)

    await act(async () => fireEvent.click(screen.getByRole('button', { name: en.sidebar.loadMore })))

    expect(await screen.findByText('chat b')).toBeTruthy()
    expect(screen.getByText('chat a')).toBeTruthy()
    expect(listAllProfileSessions).toHaveBeenCalledTimes(2)
  })

  it('reads again from the top after an unarchive, so the row that moved up is not skipped', async () => {
    const server = [archived('a'), archived('b'), archived('c')]

    // A one-row "page": the mock serves the row at the asked offset.
    listAllProfileSessions.mockReset().mockImplementation(async (...args: unknown[]) => ({
      sessions: server.slice((args[6] as number) / 500, (args[6] as number) / 500 + 1),
      total: server.length
    }))
    vi.mocked(setSessionArchived).mockImplementation(async id => {
      server.splice(
        server.findIndex(row => row.id === id),
        1
      )

      return { ok: true }
    })

    render(<SessionsSettings />)
    await screen.findByText('chat a')

    await act(async () => fireEvent.click(screen.getByRole('button', { name: en.settings.sessions.unarchive })))
    await waitFor(() => expect(screen.queryByText('chat a')).toBeNull())

    await act(async () => fireEvent.click(screen.getByRole('button', { name: en.sidebar.loadMore })))

    // b moved to offset 0; the old cursor's next page (offset 500) would show only c.
    expect(await screen.findByText('chat b')).toBeTruthy()
    expect(screen.getByText('chat c')).toBeTruthy()
    expect(screen.queryByText('chat a')).toBeNull()
  })

  it('reads again when a row is unarchived while a load-more is out', async () => {
    const server = [archived('a'), archived('b'), archived('c')]
    let release!: () => void

    listAllProfileSessions.mockReset().mockImplementation(async (...args: unknown[]) => ({
      sessions: server.slice((args[6] as number) / 500, (args[6] as number) / 500 + 1),
      total: server.length
    }))
    vi.mocked(setSessionArchived).mockImplementation(async id => {
      server.splice(
        server.findIndex(row => row.id === id),
        1
      )

      return { ok: true }
    })

    render(<SessionsSettings />)
    await screen.findByText('chat a')

    // The second page is held while 'a' is unarchived underneath it.
    listAllProfileSessions.mockImplementationOnce(async () => {
      const rows = server.slice(1, 2)

      await new Promise<void>(resolve => (release = resolve))

      return { sessions: rows, total: 3 }
    })
    await act(async () => fireEvent.click(screen.getByRole('button', { name: en.sidebar.loadMore })))
    await act(async () => fireEvent.click(screen.getByRole('button', { name: en.settings.sessions.unarchive })))
    await act(async () => release())

    // Without the re-read the list is just 'b' with 'c' still offered; it must hold both.
    expect(await screen.findByText('chat c')).toBeTruthy()
    expect(screen.getByText('chat b')).toBeTruthy()
    expect(screen.queryByText('chat a')).toBeNull()
  })

  it("keeps another profile's row with the same id after one is unarchived", async () => {
    const mine = { ...archived('same'), profile: 'default', title: 'mine' }
    const theirs = { ...archived('same'), profile: 'work', title: 'theirs' }
    const server = [mine, theirs, archived('z')]

    listAllProfileSessions.mockReset().mockImplementation(async (...args: unknown[]) => ({
      sessions: (args[6] as number) === 0 ? server.slice(0, 2) : server.slice(2),
      total: 500 + server.length
    }))
    vi.mocked(setSessionArchived).mockImplementation(async (_id, _archived, profile) => {
      server.splice(
        server.findIndex(row => row.profile === profile),
        1
      )

      return { ok: true }
    })

    const { container } = render(<SessionsSettings />)
    await screen.findByText('mine')

    const row = container.querySelector('#archived-session-same') as HTMLElement

    const unarchive = Array.from(row.querySelectorAll('button')).find(button =>
      button.textContent?.includes(en.settings.sessions.unarchive)
    ) as HTMLElement

    await act(async () => fireEvent.click(unarchive))
    await act(async () => fireEvent.click(screen.getByRole('button', { name: en.sidebar.loadMore })))

    expect(await screen.findByText('theirs')).toBeTruthy()
  })

  it('lists automated archived rows too, whatever the sidebar preference', async () => {
    render(<SessionsSettings />)

    await screen.findByText('archived room')

    expect(listAllProfileSessions).toHaveBeenCalledWith(500, 0, 'only', 'recent', 'all', {}, 0)
  })

  it('offers no load-more when the first page is the whole archive', async () => {
    render(<SessionsSettings />)

    await screen.findByText('archived room')

    expect(screen.queryByRole('button', { name: en.sidebar.loadMore })).toBeNull()
  })

  it('leaves out a live pinned chat the server back-filled onto the page', async () => {
    listAllProfileSessions.mockReset().mockResolvedValue({
      sessions: [archived('a'), { ...archived('pin'), archived: false, pinned: true }],
      total: 1
    })

    render(<SessionsSettings />)

    await screen.findByText('chat a')

    expect(screen.queryByText('chat pin')).toBeNull()
  })
})

describe('SessionsSettings default directory in a browser tab', () => {
  afterEach(() => {
    delete document.documentElement.dataset.hermesDesktopHost
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('picks through the server folder browser instead of the native dialog', async () => {
    document.documentElement.dataset.hermesDesktopHost = 'browser'

    const settings = {
      getDefaultProjectDir: vi.fn().mockResolvedValue({ defaultLabel: 'Server workspace', dir: null, resolvedCwd: '' }),
      pickDefaultProjectDir: vi.fn().mockResolvedValue({ canceled: true, dir: null }),
      setDefaultProjectDir: vi.fn(async (dir: null | string) => ({ dir }))
    }

    Object.assign(window, { hermesDesktop: { settings } })
    selectDesktopPaths.mockReset().mockResolvedValue(['/srv/work'])

    render(<SessionsSettings subpage="default-directory" />)
    const button = await screen.findByRole('button', { name: en.common.choose })

    await act(async () => fireEvent.click(button))

    await waitFor(() => expect(settings.setDefaultProjectDir).toHaveBeenCalledWith('/srv/work'))
    expect(selectDesktopPaths).toHaveBeenCalledWith(expect.objectContaining({ directories: true }))
    expect(settings.pickDefaultProjectDir).not.toHaveBeenCalled()
  })
})
