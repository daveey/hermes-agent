import { cleanup } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { chatMessageText } from '@/lib/chat-messages'

import { type GatewayFrame, playFrames } from './test-harness'

// [daveey] t_01a1d2bfdf5b5260 (PR #1827 review nit 3): a core-update pause cancels a model request issued at a
// step boundary and the backend drops its output; the resumed turn re-issues the request. A streaming request has
// already painted deltas by then. The terminal frame (tui_gateway update_pause.announce_pause) carries only the
// pause notice as `text`, and the settle replaces the current response (after the last tool row) with it, so the
// dropped partial does not stay on screen beside the transcript that no longer has it.

const SID = 'paused-for-update'
const PAUSE_TEXT = 'Paused for a Hermes update; continues automatically'
const DROPPED = 'Half of an answer the update cancel'

afterEach(cleanup)

const toolRound = (id: string): GatewayFrame[] => [
  ['tool.start', { name: 'terminal', tool_id: id, args: {} }],
  ['tool.complete', { name: 'terminal', tool_id: id, result: 'ok' }]
]

const paused: GatewayFrame = ['message.complete', { text: PAUSE_TEXT, status: 'interrupted', paused_for_update: true }]

const texts = async (frames: GatewayFrame[]) =>
  (await playFrames(SID, [['message.start', {}], ...frames, paused])).map(message => chatMessageText(message).trim())

it.each([
  ['in the same bubble', [['message.delta', { text: 'Let me look.' }], ...toolRound('t')]],
  [
    'after a sealed interim',
    [
      ['message.delta', { text: 'Let me look.' }],
      ['message.interim', { text: 'Let me look.', already_streamed: true }],
      ...toolRound('t')
    ]
  ]
] as [string, GatewayFrame[]][])(
  'the cancelled request’s streamed partial %s is replaced by the pause notice',
  async (_label, before) => {
    const all = (await texts([...before, ['message.delta', { text: `\n\n${DROPPED}` }]])).join('\n')

    expect(all).not.toContain(DROPPED)
    expect(all).toContain(PAUSE_TEXT)
    expect(all).toContain('Let me look.') // the earlier response, before the tool, is kept
  }
)
