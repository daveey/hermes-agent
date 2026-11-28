import { expect, it } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'

import { retainLiveTurnTools } from './live-turn-tools'

const user: ChatMessage = { id: 'prompt', rowId: 1, role: 'user', parts: [{ type: 'text', text: 'inspect' }] }

const tool = {
  type: 'tool-call' as const,
  toolCallId: 'call',
  toolName: 'terminal',
  args: {},
  result: 'failed',
  isError: true
}

const live: ChatMessage = { id: 'assistant-stream-1', role: 'assistant', pending: true, parts: [tool] }
const shell: ChatMessage = { id: 'stored', role: 'assistant', rowId: 2, parts: [{ ...tool, result: undefined }] }

it('keeps tool failures and call identity; repeated refresh is idempotent', () => {
  const once = retainLiveTurnTools([user, shell], [user, live])
  expect(once[1].parts).toEqual([tool])
  expect(retainLiveTurnTools(once, [user, live])).toEqual(once)
})

it('honors a newer terminal result from the gateway', () => {
  const terminal = { ...shell, parts: [{ ...tool, result: 'new result' }] }
  expect(retainLiveTurnTools([user, terminal], [user, live])[1].parts).toEqual(terminal.parts)
})

it.each([
  [user, { ...live, pending: false }],
  [{ ...user, rowId: 99, id: 'different', parts: user.parts }, live]
])('does not resurrect tools from settled or unrelated turns', (...previous) => {
  const next = [user, shell]
  expect(retainLiveTurnTools(next, previous)).toBe(next)
})

it('a later prompt closes ownership even with identical text', () => {
  const next = [user, shell, { ...user, id: 'next', rowId: 3 }]
  expect(retainLiveTurnTools(next, [user, live])).toBe(next)
})

it('uses the reconciled optimistic twin, not the text of a newer identical prompt', () => {
  const optimistic = { ...user, id: 'user-submission', rowId: undefined }
  const next = [user, shell]
  expect(retainLiveTurnTools(next, [optimistic, live], user)[1].parts).toEqual([tool])
  const later = [...next, { ...user, id: 'new-prompt', rowId: 3 }]
  expect(retainLiveTurnTools(later, [optimistic, live], user)).toBe(later)
})

it('does not duplicate a tool already present in both a fold and live segment', () => {
  const next = retainLiveTurnTools([user, shell, live], [user, live])
  expect(next.flatMap(message => message.parts.filter(part => part.type === 'tool-call'))).toEqual([tool])
})
