import { expect, it } from 'vitest'

import { type ChatMessage, chatMessageText, textPart } from '@/lib/chat-messages'

import { reconcileAuthoritativeChatMessages } from './resume-reconcile'

it('merges durable commentary with the warm live row before upserting the canonical segment', () => {
  const tool = { type: 'tool-call' as const, toolCallId: 'tool-1', toolName: 'terminal', args: {}, result: 'ok' }

  const durable: ChatMessage[] = [
    { id: 'u', role: 'user', parts: [textPart('prompt')] },
    { id: 'durable', role: 'assistant', parts: [textPart('Narration'), tool] }
  ]

  const cached: ChatMessage[] = [
    durable[0],
    {
      id: 'assistant-stream-original',
      role: 'assistant',
      pending: true,
      parts: [textPart('Narration'), tool, textPart('Answer')]
    }
  ]

  const restored = reconcileAuthoritativeChatMessages(durable, cached, {
    session_id: 'runtime-1',
    inflight: { user: '', assistant: 'Answer across a reload', streaming: true, segment_only: true }
  })

  expect(restored).toHaveLength(2)
  expect(restored[1].id).toBe('assistant-stream-original')
  expect(restored[1].parts).toEqual([textPart('Narration'), tool, textPart('Answer across a reload')])
  expect(
    restored
      .map(chatMessageText)
      .join('\n')
      .match(/Answer/g)
  ).toHaveLength(1)
})
