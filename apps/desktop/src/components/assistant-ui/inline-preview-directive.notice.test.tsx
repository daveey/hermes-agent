/**
 * desktop-13785ebc41: a rating page posted every rating in one intent over
 * the length cap. The frame refused it and told only the page, which ignored
 * the ack, so the user kept rating while nothing reached the agent. A refused
 * intent the user can act on must show under the frame itself.
 */

import { act, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, describe, expect, it, vi } from 'vitest'

const readText = vi.hoisted(() => vi.fn())
const submit = vi.hoisted(() => vi.fn(() => true))

vi.mock('@/lib/desktop-fs', async importOriginal => ({
  ...(await importOriginal<object>()),
  readDesktopFileText: readText
}))

vi.mock('@/app/chat/session-view', () => ({ useSessionView: () => ({ $cwd: atom('/tmp') }) }))
vi.mock('@/app/chat/composer/focus', () => ({ requestComposerSubmit: submit }))
vi.mock('@/hooks/use-theme-epoch', () => ({ useThemeEpoch: () => 0 }))
vi.mock('@/components/ui/preview-document-frame', () => ({
  PreviewDocumentFrame: (props: { srcDoc: string; title: string }) => (
    <iframe data-testid="frame" srcDoc={props.srcDoc} title={props.title} />
  )
}))

import { InlinePreviewDirective, MAX_INTENT_LENGTH } from './inline-preview-directive'

async function mountWidget() {
  readText.mockResolvedValue({ byteSize: 40, path: '/tmp/w.html', text: '<html><body><b>w</b></body></html>' })
  render(<InlinePreviewDirective attrs={{ file: '/tmp/w.html' }} streaming={false} />)
  const frame = await screen.findByTestId('frame')
  const token = /var t="([a-z0-9]+)"/.exec(frame.getAttribute('srcdoc') ?? '')?.[1]

  expect(token).toBeTruthy()

  return (prompt: string) =>
    act(() => {
      window.dispatchEvent(
        new MessageEvent('message', { data: { id: 1, prompt, token, type: 'hermes-inline-preview-intent' } })
      )
    })
}

describe('InlinePreviewDirective refused-intent notice', () => {
  afterEach(() => {
    readText.mockReset()
    submit.mockClear()
  })

  it('shows a notice under the frame when an intent is refused as too long', async () => {
    const send = await mountWidget()

    send('x'.repeat(MAX_INTENT_LENGTH + 1))

    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('not sent'))
    expect(submit).not.toHaveBeenCalled()
  })

  it('shows no notice for an accepted intent', async () => {
    const send = await mountWidget()

    send('rate 12 4')

    expect(submit).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('status')).toBeNull()
  })
})
