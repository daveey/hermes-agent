import { renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { isMacPlatform } from '@/lib/platform'
import type * as Connections from '@/store/connections'
import { $defaultProfileRoute } from '@/store/default-profile'
import { resetBinding } from '@/store/keybinds'
import { $newChatRoute, ensureGatewayAgent } from '@/store/profile'

import { useKeybinds } from './use-keybinds'

type ConnectionsModule = typeof Connections

const mocks = vi.hoisted(() => ({
  selectThisDeviceForNewChat: vi.fn(async () => undefined)
}))

vi.mock('@/store/connections', async importOriginal => ({
  ...(await importOriginal<ConnectionsModule>()),
  selectThisDeviceForNewChat: mocks.selectThisDeviceForNewChat
}))

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureGatewayAgent: vi.fn(async () => undefined)
}))

vi.mock('@/themes/context', () => ({
  useTheme: () => ({ resolvedMode: 'light', setMode: vi.fn() })
}))

const deps = {
  archiveSelectedSession: vi.fn(),
  openNewSessionTab: vi.fn(),
  requestGateway: vi.fn(),
  startFreshSession: vi.fn(),
  toggleCommandCenter: vi.fn(),
  toggleSelectedPin: vi.fn()
}

function pressN(shiftKey: boolean): KeyboardEvent {
  const event = new KeyboardEvent('keydown', {
    bubbles: true,
    cancelable: true,
    code: 'KeyN',
    ctrlKey: !isMacPlatform(),
    key: shiftKey ? 'N' : 'n',
    metaKey: isMacPlatform(),
    shiftKey
  })

  window.document.body.dispatchEvent(event)

  return event
}

describe('⌘N / ⌘⇧N new session gateway targeting', () => {
  let unmount: (() => void) | undefined

  beforeEach(() => {
    unmount = renderHook(() => useKeybinds(deps), {
      wrapper: ({ children }: { children: ReactNode }) => <MemoryRouter>{children}</MemoryRouter>
    }).unmount
  })

  afterEach(() => {
    unmount?.()
    unmount = undefined
    resetBinding('session.new')
    resetBinding('session.newSameGateway')
    deps.startFreshSession.mockReset()
    mocks.selectThisDeviceForNewChat.mockClear()
    $defaultProfileRoute.set(null)
    $newChatRoute.set(null)
    vi.mocked(ensureGatewayAgent).mockClear()
    window.history.replaceState(null, '', '/')
  })

  it('⌘N starts a new session and switches the gateway to This device', () => {
    const event = pressN(false)

    expect(event.defaultPrevented).toBe(true)
    expect(deps.startFreshSession).toHaveBeenCalledOnce()
    expect(mocks.selectThisDeviceForNewChat).toHaveBeenCalledOnce()
  })

  it('⌘⇧N starts a new session on the currently selected gateway', () => {
    const event = pressN(true)

    expect(event.defaultPrevented).toBe(true)
    expect(deps.startFreshSession).toHaveBeenCalledOnce()
    expect(mocks.selectThisDeviceForNewChat).not.toHaveBeenCalled()
  })

  it('ordinary peer same-gateway creation does not re-home to local or its boot seed', () => {
    window.history.replaceState(null, '', '/?peer=1&profile=default&connectionId=boot-owner')
    pressN(true)
    expect(deps.startFreshSession).toHaveBeenCalledOnce()
    expect(mocks.selectThisDeviceForNewChat).not.toHaveBeenCalled()
    expect(ensureGatewayAgent).not.toHaveBeenCalled()
    expect($newChatRoute.get()).toBeNull()
  })

  it('explicit profile-window creation returns to its boot owner rather than local or saved default', () => {
    window.history.replaceState(null, '', '/?peer=1&profileWindow=1&profile=ai-dev&connectionId=boot-owner')
    $defaultProfileRoute.set({ connectionId: 'saved-elsewhere', profile: 'default' })
    pressN(false)
    expect(deps.startFreshSession).toHaveBeenCalledOnce()
    expect(mocks.selectThisDeviceForNewChat).not.toHaveBeenCalled()
    expect($newChatRoute.get()).toEqual({ connectionId: 'boot-owner', profile: 'ai-dev' })
    expect(ensureGatewayAgent).toHaveBeenCalledWith('boot-owner', 'ai-dev')
  })

  // [daveey] A gateway saved with "Use for new sessions" wins over This device.
  it('⌘N starts the new session on the saved new-session gateway instead of This device', () => {
    $defaultProfileRoute.set({ connectionId: 'hermes-hub', profile: 'default' })

    const event = pressN(false)

    expect(event.defaultPrevented).toBe(true)
    expect(deps.startFreshSession).toHaveBeenCalledOnce()
    expect(mocks.selectThisDeviceForNewChat).not.toHaveBeenCalled()
    expect($newChatRoute.get()).toEqual({ connectionId: 'hermes-hub', profile: 'default' })
    expect(ensureGatewayAgent).toHaveBeenCalledWith('hermes-hub', 'default')
  })

  it('⌘⇧N still starts on the gateway on screen when a new-session gateway is saved', () => {
    $defaultProfileRoute.set({ connectionId: 'hermes-hub', profile: 'default' })

    pressN(true)

    expect(deps.startFreshSession).toHaveBeenCalledOnce()
    expect($newChatRoute.get()).toBeNull()
    expect(ensureGatewayAgent).not.toHaveBeenCalled()
  })
})
