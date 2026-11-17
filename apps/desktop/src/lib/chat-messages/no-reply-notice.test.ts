import { describe, expect, it } from 'vitest'

import { type ChatMessage, preserveLocalAssistantErrors, toChatMessages } from '@/lib/chat-messages'
import { withNoReplyNotice } from '@/store/session-no-reply'
import type { SessionMessage } from '@/types/hermes'

/**
 * The silent-turn check can read a running turn as ended and stamp "Hermes
 * ended this turn without a reply" on it. When the turn's reply then lands in
 * the stored transcript, the stamp was wrong: the refresh must not keep it as
 * a second bubble under the reply, nor fold it onto the reply.
 */

const at = (id: number) => 1_000 + id

const user = (id: number, text: string): SessionMessage =>
  ({ id, role: 'user', content: text, timestamp: at(id) }) as SessionMessage

const toolCall = (id: number, call: string): SessionMessage =>
  ({
    id,
    role: 'assistant',
    content: '',
    timestamp: at(id),
    tool_calls: [{ id: call, type: 'function', function: { name: 'terminal', arguments: '{"command":"echo"}' } }]
  }) as SessionMessage

const toolResult = (id: number, call: string): SessionMessage =>
  ({ id, role: 'tool', content: 'ok', tool_call_id: call, tool_name: 'terminal', timestamp: at(id) }) as SessionMessage

const answer = (id: number, text: string): SessionMessage =>
  ({ id, role: 'assistant', content: text, timestamp: at(id) }) as SessionMessage

const storedTurn = (reply: SessionMessage[]): ChatMessage[] =>
  toChatMessages([user(1, 'resume'), toolCall(2, 'call-1'), toolResult(3, 'call-1'), ...reply])

const errors = (messages: ChatMessage[]) => messages.filter(message => message.error).map(message => message.error)

describe('a no-reply notice on a turn whose reply is stored', () => {
  const stored = storedTurn([answer(4, 'Queued the next stage.')])
  const prompt = stored[0]

  it('drops the empty notice bubble', () => {
    const local = withNoReplyNotice([prompt])

    const merged = preserveLocalAssistantErrors(stored, local)

    expect(errors(merged)).toEqual([])
    expect(merged.map(message => message.id)).toEqual(stored.map(message => message.id))
  })

  it('drops a stamped live bubble that only holds the tool rows of the turn', () => {
    const liveTools = stored
      .filter(message => message.role === 'assistant')
      .flatMap(message => message.parts.filter(part => part.type !== 'text'))

    const local = withNoReplyNotice([
      prompt,
      { id: 'assistant-stream-live', role: 'assistant', parts: liveTools, pending: false }
    ])

    const merged = preserveLocalAssistantErrors(stored, local)

    expect(errors(merged)).toEqual([])
    expect(merged.map(message => message.id)).toEqual(stored.map(message => message.id))
  })

  it('drops the notice when the descriptor was lost and only the text survives', () => {
    const local = withNoReplyNotice([prompt]).map(message => {
      const { errorSurface: _dropped, ...rest } = message

      return rest
    })

    expect(errors(preserveLocalAssistantErrors(stored, local))).toEqual([])
  })
})

describe('a no-reply notice on a turn with no stored reply', () => {
  it('keeps the notice', () => {
    const stored = storedTurn([])
    const local = withNoReplyNotice([stored[0]])

    const merged = preserveLocalAssistantErrors(stored, local)

    expect(errors(merged)).toEqual(['Hermes ended this turn without a reply.'])
  })
})
