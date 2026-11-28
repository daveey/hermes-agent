import type { GatewayEventName } from '@hermes/shared'
import { act, cleanup } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { graftRefreshedTailOntoBackfill } from '@/app/chat/transcript-backfill'
import { type ChatMessage, toChatMessages } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'

import { preserveLocalPendingTurnMessages } from '../use-session-actions/utils'

import { renderMessageStream } from './test-harness'

afterEach(cleanup)

it.each([false, true])('retains live tool parts across a stale background refresh (interim=%s)', async interim => {
  const sid = 'live-tools'
  const page = toChatMessages([{ id: 1, role: 'user', content: 'inspect' }])
  const states = new Map([[sid, { ...createClientSessionState(), messages: page }]])
  const stream = renderMessageStream(sid, { states })

  const send = (type: GatewayEventName, payload: Record<string, unknown> = {}) =>
    act(() => stream.handleEvent({ type, session_id: sid, payload }))

  await send('message.start')

  if (interim) {
    await send('message.delta', { text: 'Checking.' })
    await send('message.interim', { text: 'Checking.', already_streamed: true })
  }

  await send('tool.start', { tool_id: 'read', name: 'read_file', args: { path: 'README.md' } })
  await send('tool.complete', { tool_id: 'read', name: 'read_file', result: 'contents' })
  await send('tool.start', { tool_id: 'check', name: 'terminal', args: { command: 'true' } })

  const live = stream.state().messages as ChatMessage[]

  // The first tool's assistant row is persisted before its result and the
  // rest of the live bubble. Its text alone does not acknowledge those parts.
  const partialPage = toChatMessages([
    { id: 1, role: 'user', content: 'inspect' },
    {
      id: 2,
      role: 'assistant',
      content: interim ? 'Checking.' : '',
      tool_calls: [{ id: 'read', type: 'function', function: { name: 'read_file', arguments: '{"path":"README.md"}' } }]
    }
  ])

  const refresh = () => preserveLocalPendingTurnMessages(graftRefreshedTailOntoBackfill(partialPage, live), live)

  const tools = (messages: ChatMessage[]) =>
    messages.flatMap(message => message.parts.filter(part => part.type === 'tool-call'))

  expect(tools(refresh())).toEqual(tools(live))
  expect(tools(refresh()).map(part => part.toolCallId)).toEqual(['read', 'check'])
})
