import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { type SessionView, SessionViewProvider } from '@/app/chat/session-view'
import { DropdownMenu, DropdownMenuContent } from '@/components/ui/dropdown-menu'
import { $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { ReasoningMenuPanel } from './reasoning-menu-panel'

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

afterEach(() => {
  cleanup()
  $sessions.set([])
})

// The catalog's `current` is the PROFILE default (GPT here), not this chat.
const OPTIONS = {
  model: 'gpt-6-astra',
  provider: 'openai-codex',
  providers: [
    {
      capabilities: { 'gpt-6-astra': { fast: true, reasoning: true, ultrafast: true } },
      models: ['gpt-6-astra'],
      name: 'OpenAI Codex',
      slug: 'openai-codex'
    },
    {
      capabilities: { 'claude-opus-5-5': { fast: false, reasoning: true, ultrafast: false } },
      models: ['claude-opus-5-5'],
      name: 'Anthropic',
      slug: 'anthropic'
    }
  ]
}

const unloadedView = (): SessionView => ({
  kind: 'tile',
  $awaitingResponse: atom(false),
  $busy: atom(false),
  $cwd: atom(''),
  $fast: atom(false),
  $lastVisibleIsUser: atom(false),
  $messages: atom([]),
  $messagesEmpty: atom(false),
  $model: atom(''),
  $provider: atom(''),
  $reasoningEffort: atom(''),
  $reasoningEffortPending: atom(true),
  $reasoningEffortWire: atom(''),
  $runtimeId: atom('rt-unloaded'),
  $storedId: atom('stored-opus'),
  $turnStartedAt: atom<number | null>(null)
})

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  const requestGateway = vi.fn(async (method: string) => {
    if (method === 'model.options') {
      return OPTIONS
    }

    throw new Error(`unexpected gateway method: ${method}`)
  })

  return render(
    <QueryClientProvider client={client}>
      <SessionViewProvider value={unloadedView()}>
        <DropdownMenu open>
          <DropdownMenuContent>
            <ReasoningMenuPanel onSelectModel={vi.fn()} requestGateway={requestGateway as never} />
          </DropdownMenuContent>
        </DropdownMenu>
      </SessionViewProvider>
    </QueryClientProvider>
  )
}

// t_b319a52a: with the chat's agent unloaded the effort menu fell back to the
// catalog's current and offered GPT's Thinking/Fast/Ultrafast as this chat's.
describe('ReasoningMenuPanel with a stored model and no live agent', () => {
  it("shows the stored model's (Anthropic) capabilities, not the profile default's", async () => {
    $sessions.set([
      {
        ended_at: null,
        id: 'stored-opus',
        input_tokens: 0,
        is_active: false,
        last_active: 0,
        message_count: 4,
        model: 'claude-opus-5-5',
        model_provider: 'anthropic',
        output_tokens: 0
      } as unknown as SessionInfo
    ])

    renderPanel()

    // Effort rows mount once the catalog resolves.
    // eslint-disable-next-line no-restricted-globals
    await waitFor(() => expect(document.body.textContent).toMatch(/Med/))
    expect(screen.queryByText('Ultrafast')).toBeNull()
  })
})
