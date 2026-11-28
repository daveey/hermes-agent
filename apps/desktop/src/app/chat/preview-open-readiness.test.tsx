import { act, cleanup, render, waitFor } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { group } from '@/components/pane-shell/tree/model'
import { LayoutTreeRoot } from '@/components/pane-shell/tree/renderer'
import { declareDefaultTree, treePanesWithPrefix, watchContributedPanes } from '@/components/pane-shell/tree/store'
import { registry } from '@/contrib/registry'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $previewTabs, $visiblePreviewTabs, setPreviewScope } from '@/store/preview'
import { $selectedStoredSessionId } from '@/store/session'
import { $sessionStates, recordSessionEventScope } from '@/store/session-states'
import { stubMenuDomApis } from '@/test/jsdom'

import {
  handleServerRequest,
  type ServerRequestContext
} from '../session/hooks/use-message-stream/gateway-event/server-requests'

import { watchPreviewTiles } from './preview-tile'

// The REAL PreviewTilePane lifecycle: the pane creates a <webview>, registers
// its reader/runner at once, and the guest only becomes usable at dom-ready.
// An acknowledgement before that is a lie the agent's next read trips over.
vi.mock('./right-rail/real-profile-consent-dialog', () => ({
  RealProfileConsentDialog: () => null
}))

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const disconnected: Element[] = []
const originalRemove = Element.prototype.remove
const attachedGuests = new Map<number, Guest>()
const activityLeases = new Map<number, Guest>()
let nextActivityToken = 0
let previousDesktop: Window['hermesDesktop']

beforeEach(() => {
  disconnected.length = 0
  attachedGuests.clear()
  activityLeases.clear()
  previousDesktop = window.hermesDesktop
  window.hermesDesktop = {
    ...previousDesktop,
    beginPreviewGuestActivity: async id => {
      const guest = attachedGuests.get(id)

      if (!guest) {
        throw new Error(`Unknown preview guest ${id}`)
      }
      const token = ++nextActivityToken
      activityLeases.set(token, guest)

      return token
    },
    executePreviewGuestScript: async (token, code) => {
      const guest = activityLeases.get(token)

      if (!guest?.executeJavaScript) {
        throw new Error(`Unknown preview activity ${token}`)
      }

      return guest.executeJavaScript(code)
    },
    endPreviewGuestActivity: async token => {
      activityLeases.delete(token)
    }
  } as Window['hermesDesktop']
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  vi.stubGlobal('CSS', { ...globalThis.CSS, escape: (value: string) => value })
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
    window.setTimeout(() => callback(Date.now()), 0)
  )
  vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))

  Element.prototype.remove = function remove(this: Element) {
    if (this.tagName === 'WEBVIEW') {
      disconnected.push(this)
    }

    originalRemove.call(this)
  }

  stubMenuDomApis()
})

afterEach(() => {
  cleanup()
  Element.prototype.remove = originalRemove
  window.hermesDesktop = previousDesktop
  expect(activityLeases.size).toBe(0)
  vi.unstubAllGlobals()
})

let seq = 0

function setupTree() {
  registry.register({
    area: 'panes',
    data: { placement: 'main', uncloseable: true },
    id: 'workspace',
    render: () => null,
    title: 'workspace'
  })
  declareDefaultTree(group(['workspace'], { active: 'workspace', id: 'grp-main' }))
  watchContributedPanes()
  watchPreviewTiles()
}

function deps(active: string) {
  return {
    activeSessionIdRef: { current: active },
    sessionInterrupted: () => false,
    updateSessionState: vi.fn(),
    upsertToolCall: vi.fn(),
    sessionStateByRuntimeIdRef: { current: new Map() }
  } as unknown as ServerRequestContext['deps']
}

function send(method: string, params: Record<string, unknown>, active: string, profile = 'default') {
  const respond = vi.fn()
  seq += 1
  handleServerRequest(
    { id: `${method}-${seq}`, method, profile, params, respond, fail: vi.fn(), decline: vi.fn() },
    deps(active),
    active
  )

  return respond
}

type Guest = HTMLElement & { executeJavaScript?: (code: string) => Promise<unknown> }

const guests = (root: ParentNode) => new Set(root.querySelectorAll<Guest>('webview'))

/** The webview the open just created (earlier tests' parked guests persist in
 *  the module-level stores). */
async function newGuest(root: ParentNode, before: Set<Guest>): Promise<Guest> {
  return waitFor(() => {
    const fresh = [...guests(root)].filter(guest => !before.has(guest))
    expect(fresh).toHaveLength(1)

    return fresh[0]!
  })
}

const value = (respond: ReturnType<typeof vi.fn>) => JSON.parse(respond.mock.calls[0]![0].value)

/** Make a webview behave like an attached Electron guest showing `text`. */
function attach(webview: Guest, text: string) {
  const id = attachedGuests.size + 1
  attachedGuests.set(id, webview)
  webview.executeJavaScript = async (code: string) =>
    code.includes('innerText') ? text : JSON.stringify({ success: true, elements: [{ ref: 'e1', text: 'Continue' }] })
  Object.assign(webview, { getWebContentsId: () => id, getTitle: () => 'Page', getURL: () => 'https://a.example/' })
  webview.dispatchEvent(new Event('dom-ready'))
}

it('acknowledges a hidden open only once its guest is ready, then reads it', async () => {
  setupTree()
  $selectedStoredSessionId.set('ready-b')
  $sessionStates.set({ 'ready-a': createClientSessionState('ready-a') })
  const { container } = render(createElement(LayoutTreeRoot))

  const before = guests(container)
  let respond!: ReturnType<typeof vi.fn>
  await act(async () => {
    respond = send('preview.open', { session_id: 'ready-a', url: 'https://a.example', label: 'A' }, 'ready-b')
  })

  const webview = await newGuest(container, before)

  // A page never on screen still gets a real viewport: 0x0 has nothing to click.
  expect(webview.closest<HTMLElement>('[data-pane-host]')!.style.width).toContain('1280px')

  // The reader/runner are registered already, but the guest has not attached.
  await new Promise(resolve => setTimeout(resolve, 150))
  expect(respond).not.toHaveBeenCalled()

  act(() => attach(webview, 'Hidden page text'))
  await waitFor(() => expect(respond).toHaveBeenCalled())
  expect(value(respond)).toMatchObject({ success: true, url: 'https://a.example' })
  expect($visiblePreviewTabs.get()).toEqual([])

  let read!: ReturnType<typeof vi.fn>
  await act(async () => {
    read = send('preview.read', { session_id: 'ready-a' }, 'ready-b')
  })
  await waitFor(() => expect(read).toHaveBeenCalled())
  expect(value(read)).toMatchObject({ text: 'Hidden page text' })
})

it('fails a hidden open whose main-frame load fails instead of acknowledging it', async () => {
  setupTree()
  $selectedStoredSessionId.set('fail-b')
  $sessionStates.set({ 'fail-a': createClientSessionState('fail-a') })
  const { container } = render(createElement(LayoutTreeRoot))

  const before = guests(container)
  let respond!: ReturnType<typeof vi.fn>
  await act(async () => {
    respond = send('preview.open', { session_id: 'fail-a', url: 'https://down.example' }, 'fail-b')
  })

  const webview = await newGuest(container, before)

  act(() => {
    webview.dispatchEvent(
      Object.assign(new Event('did-fail-load'), {
        errorCode: -105,
        errorDescription: 'ERR_NAME_NOT_RESOLVED',
        isMainFrame: true,
        validatedURL: 'https://down.example/'
      })
    )
  })

  await waitFor(() => expect(respond).toHaveBeenCalled())
  expect(value(respond)).toMatchObject({ success: false })
  expect(value(respond).error).toContain('ERR_NAME_NOT_RESOLVED')
})

it("hosts another profile's hidden open off view and keeps the same guest when that chat is focused", async () => {
  setupTree()
  recordSessionEventScope({ connectionId: 'conn-1', profile: 'alpha', session_id: 'cross-a' })
  recordSessionEventScope({ connectionId: 'conn-1', profile: 'beta', session_id: 'cross-b' })
  setPreviewScope('beta')
  $selectedStoredSessionId.set('cross-b')
  $sessionStates.set({ 'cross-a': createClientSessionState('cross-a') })
  const betaTabsBefore = $previewTabs.get()
  const { container } = render(createElement(LayoutTreeRoot))

  const before = guests(container)
  let respond!: ReturnType<typeof vi.fn>
  await act(async () => {
    respond = send('preview.open', { session_id: 'cross-a', url: 'https://a.example', label: 'A' }, 'cross-b', 'alpha')
  })

  const webview = await newGuest(container, before)

  act(() => attach(webview, 'Alpha page text'))
  await waitFor(() => expect(respond).toHaveBeenCalled())
  expect(value(respond)).toMatchObject({ success: true })

  // Beta's rail never sees alpha's tab, and nothing in beta's drawer moved.
  expect($previewTabs.get()).toEqual(betaTabsBefore)
  expect($visiblePreviewTabs.get()).toEqual([])
  expect(webview.closest('[inert]')).not.toBeNull()

  let read!: ReturnType<typeof vi.fn>
  await act(async () => {
    read = send('preview.read', { session_id: 'cross-a' }, 'cross-b', 'alpha')
  })
  await waitFor(() => expect(read).toHaveBeenCalled())
  expect(value(read)).toMatchObject({ text: 'Alpha page text' })

  // The user focuses A: the rail re-homes onto alpha and shows the SAME guest.
  act(() => {
    setPreviewScope('alpha')
    $selectedStoredSessionId.set('cross-a')
  })
  await waitFor(() => expect(treePanesWithPrefix('preview-tile:')).toHaveLength(1))
  expect($visiblePreviewTabs.get().map(tab => tab.sessionId)).toEqual(['cross-a'])
  expect([...guests(container)].filter(guest => !before.has(guest))).toEqual([webview])
  expect(webview.isConnected).toBe(true)
  expect(disconnected).not.toContain(webview)
  expect(webview.closest('[inert]')).toBeNull()

  // And leaving alpha again keeps it parked, not destroyed.
  act(() => {
    setPreviewScope('beta')
    $selectedStoredSessionId.set('cross-b')
  })
  expect(webview.isConnected).toBe(true)
  expect(disconnected).not.toContain(webview)
})
