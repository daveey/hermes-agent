// @vitest-environment jsdom
import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react'
import type { ThreadMessageLike } from '@assistant-ui/react'
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { mainComposerScope } from '@/store/composer'

import { RICH_INPUT_SLOT } from './rich-editor'
import type { ChatBarProps, ChatBarState } from './types'

import { ChatBar } from './index'

const state: ChatBarState = {
  model: { canSwitch: false, model: '', provider: '' },
  tools: { enabled: false, label: '' },
  voice: { enabled: false, active: false }
}

function Harness({ busy, onCancel }: Pick<ChatBarProps, 'busy' | 'onCancel'>) {
  const runtime = useExternalStoreRuntime({
    convertMessage: (message: ThreadMessageLike) => message,
    isRunning: busy,
    messages: [] as ThreadMessageLike[],
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <MemoryRouter>
        <I18nProvider configClient={null} initialLocale="en">
          <ChatBar
            busy={busy}
            disabled={false}
            gateway={null}
            onCancel={onCancel}
            onSubmit={() => true}
            state={state}
          />
        </I18nProvider>
      </MemoryRouter>
    </AssistantRuntimeProvider>
  )
}

afterEach(() => {
  cleanup()
  mainComposerScope.clear()
})

it('Escape inside the real composer editor forwards the esc origin exactly once', async () => {
  const onCancel = vi.fn()
  const { container } = render(<Harness busy onCancel={onCancel} />)
  const editor = container.querySelector<HTMLElement>(`[data-slot="${RICH_INPUT_SLOT}"]`)!
  editor.tabIndex = 0
  await act(async () => {
    editor.focus()
    fireEvent.keyDown(editor, { key: 'Escape', code: 'Escape' })
  })
  expect(onCancel).toHaveBeenCalledExactlyOnceWith('esc')
})

it('Escape inside an idle composer does not cancel a turn', async () => {
  const onCancel = vi.fn()
  const { container } = render(<Harness busy={false} onCancel={onCancel} />)
  const editor = container.querySelector<HTMLElement>(`[data-slot="${RICH_INPUT_SLOT}"]`)!
  editor.tabIndex = 0
  await act(async () => {
    editor.focus()
    fireEvent.keyDown(editor, { key: 'Escape', code: 'Escape' })
  })
  expect(onCancel).not.toHaveBeenCalled()
})
