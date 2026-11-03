import { renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { isMacPlatform } from '@/lib/platform'
import { $selectedStoredSessionId } from '@/store/session'

import { useKeybinds } from './use-keybinds'

const mocks = vi.hoisted(() => ({ toggleSessionBlocked: vi.fn() }))

vi.mock('@/store/session-blocked', async importOriginal => ({
  ...(await importOriginal<typeof SessionBlocked>()),
  toggleSessionBlocked: mocks.toggleSessionBlocked
}))

vi.mock('@/themes/context', () => ({
  useTheme: () => ({ resolvedMode: 'light', setMode: vi.fn() })
}))

import type * as SessionBlocked from '@/store/session-blocked'

const deps = {
  archiveSelectedSession: vi.fn(),
  openNewSessionTab: vi.fn(),
  requestGateway: vi.fn(),
  startFreshSession: vi.fn(),
  toggleCommandCenter: vi.fn(),
  toggleSelectedPin: vi.fn()
}

function pressModB(target: EventTarget): KeyboardEvent {
  const event = new KeyboardEvent('keydown', {
    bubbles: true,
    cancelable: true,
    code: 'KeyB',
    ctrlKey: !isMacPlatform(),
    key: 'b',
    metaKey: isMacPlatform()
  })

  target.dispatchEvent(event)

  return event
}

describe('⌘B toggles the current session blocked', () => {
  let unmount: (() => void) | undefined

  beforeEach(() => {
    $selectedStoredSessionId.set('s1')
    unmount = renderHook(() => useKeybinds(deps), {
      wrapper: ({ children }: { children: ReactNode }) => <MemoryRouter>{children}</MemoryRouter>
    }).unmount
  })

  afterEach(() => {
    unmount?.()
    document.body.innerHTML = ''
    mocks.toggleSessionBlocked.mockReset()
  })

  it('fires from the page body', () => {
    const event = pressModB(document.body)

    expect(mocks.toggleSessionBlocked).toHaveBeenCalledWith('s1')
    expect(event.defaultPrevented).toBe(true)
  })

  it('fires while typing in the composer', () => {
    const editor = document.createElement('div')
    editor.contentEditable = 'true'
    document.body.append(editor)
    editor.focus()

    pressModB(editor)

    expect(mocks.toggleSessionBlocked).toHaveBeenCalledWith('s1')
  })
})
