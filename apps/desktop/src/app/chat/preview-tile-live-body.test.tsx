import { act, cleanup, render, waitFor } from '@testing-library/react'
import { createElement, useEffect } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { group } from '@/components/pane-shell/tree/model'
import { LayoutTreeRoot } from '@/components/pane-shell/tree/renderer'
import { declareDefaultTree, treePanesWithPrefix, watchContributedPanes } from '@/components/pane-shell/tree/store'
import { registry } from '@/contrib/registry'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $previewTabs, $visiblePreviewTabs } from '@/store/preview'
import { $selectedStoredSessionId } from '@/store/session'
import { $sessionStates } from '@/store/session-states'
import { stubMenuDomApis } from '@/test/jsdom'

import {
  handleServerRequest,
  type ServerRequestContext
} from '../session/hooks/use-message-stream/gateway-event/server-requests'

import { watchPreviewTiles } from './preview-tile'
import { setPreviewGuestState } from './right-rail/preview-guest-ready'
import { registerPreviewPageReader } from './right-rail/preview-reader'
import { registerPreviewScriptRunner } from './right-rail/preview-script-runner'

// A native guest has a lifetime beyond React: a remount, or any detach of its
// element, destroys the Electron guest (unsaved input, page JS state). Count
// both, on a stand-in for the pane body.
const disconnected: HTMLElement[] = []
const mounts = new Map<string, number>()

class LiveGuest extends HTMLElement {
  disconnectedCallback() {
    disconnected.push(this)
  }
}

if (!customElements.get('preview-live-guest')) {
  customElements.define('preview-live-guest', LiveGuest)
}

vi.mock('./right-rail/preview', () => ({
  PreviewTilePane: ({ tabId }: { tabId: string }) => {
    useEffect(() => {
      mounts.set(tabId, (mounts.get(tabId) ?? 0) + 1)
      // The stand-in guest is attached at once (the real pane reports dom-ready).
      setPreviewGuestState(tabId, { status: 'ready' })

      const stopRead = registerPreviewPageReader(tabId, async () => ({
        text: 'Hidden page text',
        title: 'A',
        url: 'https://a.example'
      }))

      const stopScript = registerPreviewScriptRunner(tabId, async () =>
        JSON.stringify({ success: true, elements: [{ ref: 'e1', tag: 'button', text: 'Continue' }] })
      )

      return () => {
        stopRead()
        stopScript()
      }
    }, [tabId])

    return createElement('preview-live-guest', { 'data-tab': tabId }, createElement('input', { defaultValue: '' }))
  }
}))

vi.mock('./right-rail/preview-console-store', () => ({
  forgetPreviewConsole: () => undefined
}))

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  disconnected.length = 0
  mounts.clear()
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  vi.stubGlobal('CSS', { ...globalThis.CSS, escape: (value: string) => value })
  stubMenuDomApis()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

// The behaviour a user sees: switching A -> B -> A shows A's Browser with the
// page they left — the same guest, never torn down in between — and B's drawer
// does not show it.
it('mounts a hidden-born Browser offscreen and preserves its guest on focus', async () => {
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

  $selectedStoredSessionId.set('sess-live-b')
  $sessionStates.set({ 'sess-live-a': createClientSessionState('sess-live-a') })
  const respond = vi.fn()
  await act(async () => {
    handleServerRequest(
      {
        id: 'open-hidden',
        method: 'preview.open',
        profile: 'default',
        params: { session_id: 'sess-live-a', url: 'https://a.example', label: 'A' },
        respond,
        fail: vi.fn()
      },
      {
        activeSessionIdRef: { current: 'sess-live-b' },
        sessionStateByRuntimeIdRef: { current: new Map() }
      } as ServerRequestContext['deps'],
      'sess-live-b'
    )
  })
  expect(respond).not.toHaveBeenCalled()
  const aTab = $previewTabs.get()[0]!.id

  const { container } = render(createElement(LayoutTreeRoot))
  await waitFor(() =>
    expect(respond).toHaveBeenCalledWith({
      value: JSON.stringify({ success: true, url: 'https://a.example', label: 'A' })
    })
  )

  const guestFor = (tabId: string) => container.querySelector<HTMLElement>(`preview-live-guest[data-tab="${tabId}"]`)

  const aGuest = guestFor(aTab)!
  const input = aGuest.querySelector('input')!
  input.value = 'unsaved in A'

  act(() => $selectedStoredSessionId.set('sess-live-b'))
  expect(treePanesWithPrefix('preview-tile:')).toEqual([])
  expect(aGuest.isConnected).toBe(true)
  expect(aGuest.closest('[inert]')).not.toBeNull()
  expect($visiblePreviewTabs.get()).toEqual([])

  for (const method of ['preview.read', 'preview.act']) {
    const answer = vi.fn()
    await act(async () => {
      handleServerRequest(
        {
          id: method,
          method,
          profile: 'default',
          params: { session_id: 'sess-live-a', action: 'elements' },
          respond: answer,
          fail: vi.fn(),
          decline: vi.fn()
        },
        {
          activeSessionIdRef: { current: 'sess-live-b' },
          sessionInterrupted: () => false,
          updateSessionState: vi.fn(),
          upsertToolCall: vi.fn(),
          sessionStateByRuntimeIdRef: { current: new Map() }
        } as ServerRequestContext['deps'],
        'sess-live-b'
      )
    })
    await waitFor(() => expect(answer).toHaveBeenCalled())
    const result = JSON.parse(answer.mock.calls[0]![0].value)
    expect(result).toMatchObject(
      method === 'preview.read'
        ? { text: 'Hidden page text' }
        : { success: true, elements: [{ ref: 'e1', tag: 'button', text: 'Continue' }] }
    )
  }

  act(() => $selectedStoredSessionId.set('sess-live-a'))
  expect(treePanesWithPrefix('preview-tile:')).toEqual([`preview-tile:${aTab}`])
  expect(guestFor(aTab)).toBe(aGuest)
  expect(aGuest.closest('[inert]')).toBeNull()
  expect(input.value).toBe('unsaved in A')
  expect(disconnected).not.toContain(aGuest)
  expect(mounts.get(aTab)).toBe(1)
})
