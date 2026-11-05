import { expect, it } from 'vitest'

import type { SessionMessage } from '@/types/hermes'

import type { QueuedPromptEntry } from './composer-queue'
import { cardRanToReply } from './owner-queue-landed'

// [daveey] The stored rows of the incident chat (hub state.db, 2026-11-03 UTC, text shortened): two sends queued
// behind a running turn at 02:50-02:51, the backend restarted at 02:54, "resume  resume" ran again at 02:56:09 and
// was answered at 02:57:45; "what are you waiting on?" was re-asked by hand at 03:01:37. The 03:02:00 user turn is
// synthetic: it cuts the re-ask's turn before a reply, to test that boundary on the real shape.
const at = (hms: string) => Date.parse(`2026-11-03T${hms}Z`) / 1000
const queued = JSON.stringify({ _queued_prompt: true })

const incident: SessionMessage[] = [
  { role: 'user', content: 'what are you waiting on?', timestamp: at('02:50:17'), display_metadata: queued },
  { role: 'user', content: 'resume  resume', timestamp: at('02:51:05'), display_metadata: queued },
  { role: 'user', content: 'resume  resume', timestamp: at('02:56:09') },
  { role: 'assistant', content: '', timestamp: at('02:56:29') },
  { role: 'tool', content: '{"ok": true}', timestamp: at('02:56:30') },
  { role: 'assistant', content: 'Resumed. The placement repair has published PR #734', timestamp: at('02:57:45') },
  { role: 'user', content: 'what are you waiting on??', timestamp: at('03:01:37') },
  {
    role: 'user',
    content: '[Message from another Hermes session]',
    timestamp: at('03:01:46'),
    display_metadata: queued
  },
  { role: 'assistant', content: '', timestamp: at('03:01:57') },
  { role: 'user', content: 'unrelated next turn', timestamp: at('03:02:00') },
  { role: 'assistant', content: 'The immediate blocker is an older gateway', timestamp: at('03:03:16') }
]

const card = (text: string, queuedAtS: number, extra: Partial<QueuedPromptEntry> = {}): QueuedPromptEntry => ({
  attachments: [],
  id: 'lost-turn',
  queuedAt: queuedAtS * 1000,
  serverStatus: 'unknown',
  text,
  ...extra
})

it('a card whose words ran again after the restart and were answered in that turn settles', () => {
  expect(cardRanToReply(card('resume  resume', at('02:52:00')), incident)).toBe(true)
  // Newest-first pages (the REST read's `order=latest`) read the same.
  expect(cardRanToReply(card('resume resume', at('02:52:00')), [...incident].reverse())).toBe(true)
})

it('a send stored at queue time whose turn the next user turn replaced stays unsettled', () => {
  // 02:50:17's own turn never answered it: the next turn started at 02:56:09. The 03:01:37 re-ask is not the same
  // words' turn either: it was cut by another user turn before any reply.
  expect(cardRanToReply(card('what are you waiting on?', at('02:50:00')), incident)).toBe(false)
})

it('a user row stored while the turn ran (queued prompt) rides that turn instead of ending it', () => {
  const rows: SessionMessage[] = [
    { role: 'user', content: 'check the hub', timestamp: 100 },
    { role: 'user', content: 'also the mini', timestamp: 101, display_metadata: { _queued_prompt: true } as never },
    { role: 'assistant', content: 'Both checked.', timestamp: 102 }
  ]

  expect(cardRanToReply(card('check the hub', 90), rows)).toBe(true)
})

it('the owner-stamped submission id matches even when the words were rewritten', () => {
  const rows: SessionMessage[] = [
    {
      role: 'user',
      content: 'expanded skill body',
      timestamp: 100,
      display_metadata: JSON.stringify({ submission_id: 'sub-1' })
    },
    { role: 'assistant', content: 'Done.', timestamp: 101 }
  ]

  expect(cardRanToReply(card('/skill run', 90, { submissionId: 'sub-1' }), rows)).toBe(true)
  expect(cardRanToReply(card('/skill run', 90, { submissionId: 'sub-2' }), rows)).toBe(false)
})
