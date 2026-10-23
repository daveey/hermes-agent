import { QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render as rtlRender, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// [daveey] Sidebar "Synced" section: sessions of gateways that aren't live
// right now, read from the window gateway's hermes-hub replica, greyed with a
// "synced Xm ago" hint; archive/pin/rename become hub owner requests that show
// a pending marker. Live rows stay primary.

const hub = vi.hoisted(() => ({
  calls: [] as Array<{ connectionId?: string; body?: unknown; method?: string; path: string }>,
  reachable: new Set<string>(),
  sessions: [] as unknown[]
}))

vi.mock('@/api/plugins', () => ({
  pluginRest: vi.fn(
    async (plugin: string, path: string, opts: { body?: unknown; connectionId?: string; method?: string }) => {
      if (plugin !== 'hub-sync') {
        throw new Error('unexpected plugin')
      }

      hub.calls.push({ body: opts.body, connectionId: opts.connectionId, method: opts.method, path })

      if (path === '/status') {
        if (opts.connectionId === 'local') {
          return { available: true, gateway: 'laptop' }
        }

        if (hub.reachable.has(opts.connectionId ?? '')) {
          return { available: true, gateway: opts.connectionId === 'conn-cloud' ? 'cloud' : 'mini' }
        }

        throw new Error('unreachable')
      }

      if (path.startsWith('/sessions')) {
        return { available: true, gateway: 'laptop', sessions: hub.sessions }
      }

      if (path === '/request') {
        return { id: 'hr-9', queued_locally: true, state: 'local' }
      }

      throw new Error('404')
    }
  )
}))

const openSession = vi.hoisted(() => vi.fn(async () => undefined))

vi.mock('@hermes/plugin-sdk', async importOriginal => {
  const real = await importOriginal<Record<string, unknown>>()

  return { ...real, host: { ...(real.host as object), openSession } }
})

const { $connectionsRegistry } = await import('@/store/connection-registry-state')
const { $sessions } = await import('@/store/session')
const { queryClient } = await import('@/lib/query-client')
const { _resetHubSyncForTests, rememberHubGatewayName } = await import('@/store/hub-sync')
const api = await import('./api')
const { HubSessionsSection } = await import('./sessions-section')

const render = (ui: ReactElement) => rtlRender(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>)

const now = Math.floor(Date.now() / 1000)

const rec = (gateway: string, id: string, extra: Record<string, unknown> = {}) => ({
  gateway,
  id,
  key: `default/${id}`,
  local: false,
  profile: 'default',
  stale: false,
  synced_at: now - 5 * 60,
  title: `Title ${id}`,
  version: 4,
  ...extra
})

beforeEach(() => {
  _resetHubSyncForTests()
  hub.calls = []
  hub.reachable = new Set()
  hub.sessions = []
  openSession.mockClear()
  $connectionsRegistry.set({
    connections: [
      { id: 'local', kind: 'local', label: 'Laptop' },
      { id: 'conn-mini', kind: 'ssh', label: 'Mini' },
      { id: 'conn-cloud', kind: 'remote', label: 'Cloud' }
    ],
    primary: 'local'
  } as never)
  rememberHubGatewayName('local', 'laptop')
  rememberHubGatewayName('conn-mini', 'mini')
  rememberHubGatewayName('conn-cloud', 'cloud')
  $sessions.set([])
})

afterEach(() => {
  cleanup()
  queryClient.clear()
  $connectionsRegistry.set(null)
  $sessions.set([])
})

describe('loadReplicaSidebarRows', () => {
  it('reads the replica on the window gateway and keeps only gateways that are not live', async () => {
    hub.reachable = new Set(['conn-cloud'])
    hub.sessions = [rec('laptop', 's_own', { local: true }), rec('mini', 's_mini'), rec('cloud', 's_cloud'), rec('nas', 's_nas')]

    const rows = await api.loadReplicaSidebarRows([])

    expect(rows.map((r: { gateway: string; id: string }) => `${r.gateway}:${r.id}`)).toEqual(['mini:s_mini', 'nas:s_nas'])
    expect(hub.calls.find(c => c.path.startsWith('/sessions'))?.connectionId).toBe('local')
  })

  it('treats a gateway whose rows are in the live list as live even when its status probe fails', async () => {
    hub.sessions = [rec('mini', 's_mini'), rec('mini', 's_mini2')]
    const live = [{ connection_id: 'conn-mini', id: 's_mini' }] as never

    expect(await api.loadReplicaSidebarRows(live)).toEqual([])
  })

  it('answers no rows when hub-sync is unavailable (hub down never breaks the sidebar)', async () => {
    hub.sessions = []

    expect(await api.loadReplicaSidebarRows([])).toEqual([])
  })
})

describe('HubSessionsSection', () => {
  it('renders greyed replica rows with the synced hint and nothing when empty', async () => {
    const { container } = render(<HubSessionsSection />)

    await waitFor(() => expect(hub.calls.some(c => c.path.startsWith('/sessions'))).toBe(true))
    expect(container.textContent).toBe('')
    cleanup()

    hub.sessions = [rec('mini', 's_mini')]
    queryClient.clear()
    render(<HubSessionsSection />)

    expect(await screen.findByText('Title s_mini')).toBeTruthy()
    expect(screen.getByText('synced 5m ago')).toBeTruthy()
  })

  it('archive sends a hub request with base_version and marks the row pending', async () => {
    hub.sessions = [rec('mini', 's_mini')]
    render(<HubSessionsSection />)

    await screen.findByText('Title s_mini')
    fireEvent.click(screen.getByLabelText('Archive on mini (via hub)'))

    await waitFor(() => expect(hub.calls.some(c => c.path === '/request')).toBe(true))
    const request = hub.calls.find(c => c.path === '/request')

    expect(request).toMatchObject({
      body: { action: 'archive', base_version: 4, coll: 'sessions', gateway: 'mini', key: 'default/s_mini' },
      connectionId: 'local',
      method: 'POST'
    })
    expect(await screen.findByText('pending')).toBeTruthy()
  })

  it('rename edits inline and sends the new title', async () => {
    hub.sessions = [rec('mini', 's_mini')]
    render(<HubSessionsSection />)

    await screen.findByText('Title s_mini')
    fireEvent.click(screen.getByLabelText('Rename on mini (via hub)'))
    const input = screen.getByDisplayValue('Title s_mini')
    fireEvent.change(input, { target: { value: 'Renamed' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => expect(hub.calls.some(c => c.path === '/request')).toBe(true))
    expect(hub.calls.find(c => c.path === '/request')?.body).toMatchObject({ action: 'rename', args: { title: 'Renamed' } })
  })

  it('pin on an unpinned row asks for pin; a pinned row asks for unpin', async () => {
    hub.sessions = [rec('mini', 's_a'), rec('mini', 's_b', { pinned: true })]
    render(<HubSessionsSection />)

    await screen.findByText('Title s_a')
    const pins = screen.getAllByLabelText(/^(Pin|Unpin) on mini/)
    fireEvent.click(pins[1])

    await waitFor(() => expect(hub.calls.some(c => c.path === '/request')).toBe(true))
    expect(hub.calls.find(c => c.path === '/request')?.body).toMatchObject({ action: 'unpin', key: 'default/s_b' })
  })

  it('opens a row on its own gateway through the owner route', async () => {
    hub.sessions = [rec('mini', 's_mini', { profile: 'bb', key: 'bb/s_mini' })]
    render(<HubSessionsSection />)

    fireEvent.click(await screen.findByText('Title s_mini'))

    expect(openSession).toHaveBeenCalledWith('s_mini', {
      route: { connectionId: 'conn-mini', mode: 'remote', profile: 'bb', targetProfile: 'bb' }
    })
  })
})
