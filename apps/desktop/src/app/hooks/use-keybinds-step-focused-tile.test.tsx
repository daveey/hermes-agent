import { renderHook } from '@testing-library/react'
import { atom } from 'nanostores'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { isMacPlatform } from '@/lib/platform'
import { $selectedStoredSessionId } from '@/store/session'
import type * as SessionFocus from '@/store/session-focus'
import type * as SidebarOrder from '@/store/sidebar-visible-order'

import { useKeybinds } from './use-keybinds'

type SessionFocusModule = typeof SessionFocus
type SidebarOrderModule = typeof SidebarOrder

const mocks = vi.hoisted(() => ({
  focused: null as null | { get: () => null | string; set: (v: null | string) => void },
  nextSidebarSessionId: vi.fn((): null | string => null)
}))

vi.mock('@/store/session-focus', async importOriginal => {
  const real = await importOriginal<SessionFocusModule>()
  const focused = atom<null | string>(null)
  mocks.focused = focused

  return { ...real, $focusedStoredSessionId: focused }
})

vi.mock('@/store/sidebar-visible-order', async importOriginal => ({
  ...(await importOriginal<SidebarOrderModule>()),
  nextSidebarSessionId: mocks.nextSidebarSessionId
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

function press(key: string, code: string): KeyboardEvent {
  const event = new KeyboardEvent('keydown', {
    bubbles: true,
    cancelable: true,
    code,
    ctrlKey: !isMacPlatform(),
    key,
    metaKey: isMacPlatform()
  })

  window.document.body.dispatchEvent(event)

  return event
}

describe('⌘] / ⌘[ step from the session the user is looking at', () => {
  let unmount: (() => void) | undefined

  beforeEach(() => {
    mocks.nextSidebarSessionId.mockClear()
    unmount = renderHook(() => useKeybinds(deps), {
      wrapper: ({ children }: { children: ReactNode }) => <MemoryRouter>{children}</MemoryRouter>
    }).unmount
  })

  afterEach(() => {
    unmount?.()
    $selectedStoredSessionId.set(null)
    mocks.focused?.set(null)
  })

  it('steps from the focused tile, not the main session behind it', () => {
    $selectedStoredSessionId.set('main-session')
    mocks.focused?.set('tile-session')

    expect(press(']', 'BracketRight').defaultPrevented).toBe(true)
    expect(mocks.nextSidebarSessionId).toHaveBeenLastCalledWith('tile-session', 1, false)

    press('[', 'BracketLeft')
    expect(mocks.nextSidebarSessionId).toHaveBeenLastCalledWith('tile-session', -1, false)
  })

  it('steps from the main session when main is focused', () => {
    $selectedStoredSessionId.set('main-session')
    mocks.focused?.set('main-session')

    press(']', 'BracketRight')
    expect(mocks.nextSidebarSessionId).toHaveBeenLastCalledWith('main-session', 1, false)
  })
})
