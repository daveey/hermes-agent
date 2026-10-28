import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { DesktopConnectionsRegistry } from '@/global'
import { $defaultProfileRoute } from '@/store/default-profile'
import { $findInPage } from '@/store/find-in-page'

import { ConnectionSwitcher } from './connection-switcher'

// Radix menus use pointer capture; jsdom does not implement it.
Element.prototype.hasPointerCapture ??= () => false
Element.prototype.setPointerCapture ??= () => undefined
Element.prototype.releasePointerCapture ??= () => undefined
Element.prototype.scrollIntoView ??= () => undefined
globalThis.ResizeObserver ??= class ResizeObserver {
  disconnect() {}
  observe() {}
  unobserve() {}
}

vi.mock('@/store/connections', () => ({
  $activeConnectionId: atom<null | string>('local'),
  $connectionsRegistry: atom<DesktopConnectionsRegistry | null>(null),
  $pendingConnectionId: atom<null | string>(null),
  selectConnection: vi.fn(async () => undefined)
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      profiles: {
        switchConnectionFailed: (name: string) => `Could not connect to ${name}`,
        switchToConnection: (name: string) => `Switch to ${name}`,
        connectGateway: 'Manage gateways…',
        failedSetDefault: 'Could not set the default profile',
        newSessionsDescription: 'Opening a session on another gateway does not change this.',
        newSessionsSet: (name: string) => `New sessions will start on ${name}`,
        newSessionsStartHere: 'New sessions start here',
        useForNewSessions: 'Use for new sessions'
      },
      settings: {
        connections: {
          noSearchResults: 'No gateways match your search.',
          searchPlaceholder: 'Search gateways…',
          kindCloud: 'Hermes Cloud',
          kindLocal: 'Local',
          kindRemote: 'Remote gateway',
          kindSsh: 'SSH',
          title: 'Registered gateways'
        }
      }
    }
  })
}))

const connectionStore = await import('@/store/connections')
const $activeConnectionId = connectionStore.$activeConnectionId as ReturnType<typeof atom<null | string>>
const $connectionsRegistry = connectionStore.$connectionsRegistry
const $pendingConnectionId = connectionStore.$pendingConnectionId
const selectConnection = vi.mocked(connectionStore.selectConnection)
const onConnect = vi.fn()

const connection = (id: string, label: string, kind: 'local' | 'remote' = 'remote') => ({
  id,
  kind,
  label,
  tokenPreview: null,
  tokenSet: false
})

const registry = (connections: ReturnType<typeof connection>[]): DesktopConnectionsRegistry => ({
  connections,
  primary: connections[0]?.id ?? 'local',
  secureTokenStorage: true,
  version: 2
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  $connectionsRegistry.set(null)
  $activeConnectionId.set('local')
  $pendingConnectionId.set(null)
  $defaultProfileRoute.set(null)
  $findInPage.set({ active: false, query: '', matchOrdinal: 0, matchCount: 0, focusRequest: 0 })
})

describe('ConnectionSwitcher', () => {
  it('adds no source chrome for a local-only setup', () => {
    $connectionsRegistry.set(registry([connection('local', 'This device', 'local')]))
    render(<ConnectionSwitcher onConnect={onConnect} />)

    expect(screen.queryByRole('group', { name: 'Registered gateways' })).toBeNull()
  })

  it('shows a named source selector instead of profile-like gateway glyphs', () => {
    $connectionsRegistry.set(
      registry([
        connection('local', 'This device', 'local'),
        connection('homelab', 'Homelab'),
        connection('work-vps', 'Work VPS')
      ])
    )
    render(<ConnectionSwitcher onConnect={onConnect} />)

    const trigger = screen.getByRole('button', { name: 'Registered gateways: This device' })

    expect(trigger.textContent).toContain('This device')

    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Homelab' }))
    expect(selectConnection).toHaveBeenCalledWith('homelab')

    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.click(screen.getByRole('menuitem', { name: 'Manage gateways…' }))
    expect(onConnect).toHaveBeenCalledTimes(1)
    expect(selectConnection).toHaveBeenCalledTimes(1)
  })

  it('keeps source controls stable while a remote is opening', () => {
    $connectionsRegistry.set(registry([connection('local', 'This device', 'local'), connection('homelab', 'Homelab')]))
    $pendingConnectionId.set('homelab')
    render(<ConnectionSwitcher onConnect={onConnect} />)

    expect(screen.getByRole('group', { name: 'Registered gateways' }).getAttribute('aria-busy')).toBe('true')
  })

  it('keeps small gateway lists simple and naturally sorted', () => {
    $connectionsRegistry.set(
      registry([
        connection('zulu', 'Zulu'),
        connection('local', 'This device', 'local'),
        connection('studio-10', 'Studio 10'),
        connection('alpha', 'alpha'),
        connection('studio-2', 'Studio 2')
      ])
    )
    render(<ConnectionSwitcher onConnect={onConnect} />)

    const trigger = screen.getByRole('button', { name: 'Registered gateways: This device' })
    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })

    expect(screen.queryByPlaceholderText('Search gateways…')).toBeNull()
    expect(screen.getAllByRole('menuitemradio').map(item => item.textContent)).toEqual([
      'This device',
      'alpha',
      'Studio 2',
      'Studio 10',
      'Zulu'
    ])
  })

  it('adds search at eight gateways and filters stable results without moving the connect action', async () => {
    $connectionsRegistry.set(
      registry([
        connection('zulu', 'Zulu'),
        connection('local', 'This device', 'local'),
        connection('studio-10', 'Studio 10'),
        connection('alpha', 'Alpha'),
        connection('studio-2', 'Studio 2'),
        connection('work', 'Work VPS'),
        connection('homelab', 'Homelab'),
        connection('cloud', 'Cloud lab')
      ])
    )
    render(<ConnectionSwitcher onConnect={onConnect} />)

    const trigger = screen.getByRole('button', { name: 'Registered gateways: This device' })
    fireEvent.pointerDown(trigger, {
      button: 0,
      pointerType: 'mouse'
    })

    const search = screen.getByPlaceholderText('Search gateways…')
    expect(screen.getByRole('menuitem', { name: 'Manage gateways…' })).toBeTruthy()
    expect(screen.getAllByRole('menuitemradio').map(item => item.textContent)).toEqual([
      'This device',
      'Alpha',
      'Cloud lab',
      'Homelab',
      'Studio 2',
      'Studio 10',
      'Work VPS',
      'Zulu'
    ])

    fireEvent.change(search, { target: { value: 'studio 10' } })
    expect(screen.getAllByRole('menuitemradio').map(item => item.textContent)).toEqual(['Studio 10'])

    const result = screen.getByRole('menuitemradio', { name: 'Studio 10' })
    fireEvent.keyDown(search, { key: 'ArrowDown' })
    expect(globalThis.document.activeElement).toBe(result)

    $findInPage.set({ active: true, query: '', matchOrdinal: 0, matchCount: 0, focusRequest: 0 })
    result.focus()
    fireEvent.keyDown(result, { key: 'f', metaKey: true })
    expect(globalThis.document.activeElement).toBe(search)
    expect($findInPage.get().active).toBe(false)

    fireEvent.keyDown(search, { key: 'Escape' })
    expect(screen.queryByPlaceholderText('Search gateways…')).toBeNull()
    await waitFor(() => expect(globalThis.document.activeElement).toBe(trigger))

    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    expect((screen.getByPlaceholderText('Search gateways…') as HTMLInputElement).value).toBe('')

    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Studio 10' }))
    expect(selectConnection).toHaveBeenCalledWith('studio-10')

    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    expect((screen.getByPlaceholderText('Search gateways…') as HTMLInputElement).value).toBe('')
  })

  it('explains an empty large-list search', () => {
    $connectionsRegistry.set(
      registry([
        connection('local', 'This device', 'local'),
        ...Array.from({ length: 7 }, (_, index) => connection(`remote-${index}`, `Remote ${index}`))
      ])
    )
    render(<ConnectionSwitcher onConnect={onConnect} />)

    fireEvent.pointerDown(screen.getByRole('button', { name: 'Registered gateways: This device' }), {
      button: 0,
      pointerType: 'mouse'
    })
    fireEvent.change(screen.getByPlaceholderText('Search gateways…'), { target: { value: 'missing' } })

    expect(screen.getByText('No gateways match your search.')).toBeTruthy()
    expect(screen.getByRole('menuitem', { name: 'Manage gateways…' })).toBeTruthy()
  })

  // The window lifecycle owns IPC; this consumer paints its published cache.
  it('repaints the menu after the registry changes without reload', () => {
    const before = registry([connection('local', 'This device', 'local'), connection('homelab', 'Homelab')])

    const after = registry([
      connection('local', 'This device', 'local'),
      connection('homelab', 'Homelab'),
      connection('w2-probe', 'W2Probe')
    ])

    $connectionsRegistry.set(before)
    render(<ConnectionSwitcher onConnect={onConnect} />)

    const trigger = screen.getByRole('button', { name: 'Registered gateways: This device' })

    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    expect(screen.queryByRole('menuitemradio', { name: 'W2Probe' })).toBeNull()
    fireEvent.keyDown(document, { key: 'Escape' })

    act(() => $connectionsRegistry.set(after))

    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    expect(screen.getByRole('menuitemradio', { name: 'W2Probe' })).toBeTruthy()
  })

  // [daveey] Right-click a gateway → "Use for new sessions": saves the app-wide
  // new-session route without switching what is on screen.
  it('right-click saves a gateway for new sessions without switching to it', async () => {
    const setDefault = vi.fn(async (route: { connectionId: null | string; profile: string }) => route)
    window.hermesDesktop = { profile: { setDefault } } as never
    $connectionsRegistry.set(
      registry([
        connection('local', 'This device', 'local'),
        connection('mac-mini', 'mac-mini'),
        connection('hermes-hub', 'hub (EC2)')
      ])
    )
    render(<ConnectionSwitcher onConnect={onConnect} />)

    fireEvent.pointerDown(screen.getByRole('button', { name: 'Registered gateways: This device' }), {
      button: 0,
      pointerType: 'mouse'
    })
    fireEvent.contextMenu(screen.getByRole('menuitemradio', { name: 'hub (EC2)' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Use for new sessions' }))

    await waitFor(() => expect($defaultProfileRoute.get()).toEqual({ connectionId: 'hermes-hub', profile: 'default' }))
    expect(setDefault).toHaveBeenCalledWith({ connectionId: 'hermes-hub', profile: 'default' })
    expect(selectConnection).not.toHaveBeenCalled()
    expect($activeConnectionId.get()).toBe('local')

    // The saved row is marked, and its own right-click item is the checked state.
    const hubRow = screen.getByRole('menuitemradio', { name: /hub \(EC2\)/ })
    expect(hubRow.hasAttribute('data-new-session-target')).toBe(true)
    expect(screen.getByRole('menuitemradio', { name: 'mac-mini' }).hasAttribute('data-new-session-target')).toBe(false)
    fireEvent.contextMenu(hubRow)
    expect(
      (await screen.findByRole('menuitem', { name: 'New sessions start here' })).getAttribute('aria-disabled')
    ).toBe('true')
  })
})
