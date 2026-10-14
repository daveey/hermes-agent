import { renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { isMacPlatform } from '@/lib/platform'
import type * as Connections from '@/store/connections'
import { resetBinding } from '@/store/keybinds'

import { useKeybinds } from './use-keybinds'

type ConnectionsModule = typeof Connections

const mocks = vi.hoisted(() => ({
  selectThisDeviceForNewChat: vi.fn(async () => undefined)
}))

vi.mock('@/store/connections', async importOriginal => ({
  ...(await importOriginal<ConnectionsModule>()),
  selectThisDeviceForNewChat: mocks.selectThisDeviceForNewChat
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
})
