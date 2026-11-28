import { describe, expect, it } from 'vitest'

import { appendAssistantTextPart, assistantTextPart, type ChatMessage, textPart } from '@/lib/chat-messages'

import { graftEarlySegmentDeltas, mergeSegmentSnapshotIntoLiveRow } from './current-segment'
import { appendLiveSessionProjection } from './utils'

describe('appendLiveSessionProjection canonical current segment', () => {
  it('upserts the canonical current segment without repeating narration or tools', () => {
    const messages: ChatMessage[] = [
      { id: 'u', role: 'user', parts: [textPart('prompt')] },
      {
        id: 'assistant-stream-runtime-1',
        role: 'assistant',
        pending: true,
        parts: [
          textPart('Narration'),
          { type: 'tool-call', toolCallId: 'tool-1', toolName: 'terminal', args: {} },
          textPart('Answer')
        ]
      }
    ]

    const restored = appendLiveSessionProjection(messages, {
      session_id: 'runtime-1',
      inflight: { user: '', assistant: 'Answer across a reload', streaming: true, segment_only: true }
    })

    expect(restored).toHaveLength(2)
    expect(restored[1].parts).toEqual([...messages[1].parts.slice(0, 2), assistantTextPart('Answer across a reload')])
    expect(
      appendLiveSessionProjection(restored, {
        session_id: 'runtime-1',
        inflight: { user: '', assistant: 'Answer', streaming: true, segment_only: true }
      })
    ).toEqual(restored)
  })

  it('creates a current segment stream row after durable tool commentary on cold resume', () => {
    const messages: ChatMessage[] = [
      { id: 'u', role: 'user', parts: [textPart('prompt')] },
      {
        id: 'durable',
        role: 'assistant',
        parts: [textPart('Narration'), { type: 'tool-call', toolCallId: 'tool-1', toolName: 'terminal', args: {} }]
      }
    ]

    const restored = appendLiveSessionProjection(messages, {
      session_id: 'runtime-1',
      inflight: { user: '', assistant: 'Answer across a reload', streaming: true, segment_only: true }
    })

    expect(restored.slice(0, 2)).toEqual(messages)
    expect(restored[2]).toMatchObject({
      id: 'assistant-stream-runtime-1',
      pending: true,
      parts: [assistantTextPart('Answer across a reload')]
    })
  })

  it('ignores a snapshot cut before the turn settled when the durable reply already closes with that text', () => {
    const messages: ChatMessage[] = [
      { id: 'u', role: 'user', parts: [textPart('prompt')] },
      {
        id: 'durable',
        role: 'assistant',
        parts: [
          textPart('Narration'),
          { type: 'tool-call', toolCallId: 'tool-1', toolName: 'terminal', args: {} },
          textPart('Answer across the drop')
        ]
      }
    ]

    expect(
      appendLiveSessionProjection(messages, {
        session_id: 'runtime-1',
        inflight: { user: '', assistant: 'Answer across', streaming: true, segment_only: true }
      })
    ).toEqual(messages)
  })
})

describe('a stamped current-segment snapshot merges with live text by offset', () => {
  const turn = { turnId: 'turn-1', segment: 0 }
  const tool = { type: 'tool-call', toolCallId: 'tool-1', toolName: 'terminal', args: {} } as const

  const stamped = (assistant: string, offset: number, end: number, segment = 0, turnId = 'turn-1') => ({
    session_id: 'runtime-1',
    inflight: {
      user: '',
      assistant,
      streaming: true,
      segment_only: true as const,
      segment_stamp: { turn_id: turnId, segment, offset, end }
    }
  })

  /** A warm live row whose closing text ends with stamped units [start, end). */
  const liveRow = (text: string, start: number, end: number, segment = 0): ChatMessage[] => [
    { id: 'u', role: 'user', parts: [textPart('prompt')] },
    {
      id: 'assistant-stream-runtime-1',
      role: 'assistant',
      pending: true,
      parts: [textPart('Narration'), tool, { ...textPart(text), streamSpan: { ...turn, segment, start, end } }]
    }
  ]

  const closing = (messages: ChatMessage[]) =>
    messages
      .at(-1)!
      .parts.filter(part => part.type === 'text')
      .map(part => (part.type === 'text' ? part.text : ''))
      .join('|')

  it('keeps a delta that landed past the snapshot cut instead of replacing it with the snapshot', () => {
    // The journal row held "A2 " (unclaimed); the delta [8, 13) reached the row before the resume answered.
    const restored = appendLiveSessionProjection(liveRow('A2 NEXT ', 8, 13), stamped('A2 TAIL ', 0, 8))

    expect(closing(restored)).toBe('Narration|A2 TAIL NEXT ')
    expect(restored.at(-1)!.parts.at(-1)).toMatchObject({ streamSpan: { ...turn, start: 0, end: 13 } })
  })

  it('never duplicates an overlap even when the texts differ (offsets, not text, decide)', () => {
    // Live run claims [3, 13): "XXXX NEXT " - its first 5 units overlap the snapshot's [0, 8).
    const restored = appendLiveSessionProjection(liveRow('XXXX NEXT ', 3, 13), stamped('A2 TAIL ', 0, 8))

    expect(closing(restored)).toBe('Narration|A2 TAIL NEXT ')
  })

  it('replaces live text the snapshot fully covers', () => {
    const restored = appendLiveSessionProjection(liveRow('A2 TA', 0, 5), stamped('A2 TAIL ', 0, 8))

    expect(closing(restored)).toBe('Narration|A2 TAIL ')
  })

  it('leaves a row that already streams a later segment of the same turn', () => {
    const messages = liveRow('second', 0, 6, 1)

    expect(appendLiveSessionProjection(messages, stamped('first segment', 0, 13, 0))).toBe(messages)
  })

  it('ignores a live run of another turn and takes the snapshot', () => {
    const restored = appendLiveSessionProjection(liveRow('old turn text', 0, 13), stamped('new', 0, 3, 0, 'turn-2'))

    expect(closing(restored)).toBe('Narration|new')
  })

  it('grafts early deltas from the runtime state onto a cold-built snapshot row, once', () => {
    const cold = appendLiveSessionProjection(
      [{ id: 'u', role: 'user', parts: [textPart('prompt')] }],
      stamped('A2 TAIL ', 0, 8)
    )

    const runtime: ChatMessage[] = [
      {
        id: 'assistant-stream-early',
        role: 'assistant',
        pending: true,
        parts: [{ ...textPart('TAIL NEXT '), streamSpan: { ...turn, start: 3, end: 13 } }]
      }
    ]

    const grafted = graftEarlySegmentDeltas(cold, runtime, stamped('A2 TAIL ', 0, 8).inflight)

    expect(closing(grafted)).toBe('A2 TAIL NEXT ')
    expect(graftEarlySegmentDeltas(grafted, runtime, stamped('A2 TAIL ', 0, 8).inflight)).toBe(grafted)
  })

  it('grafts nothing from unstamped live text or another segment', () => {
    const cold = appendLiveSessionProjection(
      [{ id: 'u', role: 'user', parts: [textPart('prompt')] }],
      stamped('A2 TAIL ', 0, 8)
    )
    const inflight = stamped('A2 TAIL ', 0, 8).inflight
    const unstamped: ChatMessage[] = [{ id: 'x', role: 'assistant', pending: true, parts: [textPart('NEXT ')] }]

    const otherSegment: ChatMessage[] = [
      {
        id: 'x',
        role: 'assistant',
        pending: true,
        parts: [{ ...textPart('NEXT '), streamSpan: { ...turn, segment: 1, start: 0, end: 5 } }]
      }
    ]

    expect(graftEarlySegmentDeltas(cold, unstamped, inflight)).toBe(cold)
    expect(graftEarlySegmentDeltas(cold, otherSegment, inflight)).toBe(cold)
  })
})

describe('mergeSegmentSnapshotIntoLiveRow (warm activation adopted the live row)', () => {
  const span = (start: number, end: number, segment = 0) => ({ turnId: 'turn-1', segment, start, end })

  const inflight = {
    user: '',
    assistant: 'A2 TAIL ',
    streaming: true,
    segment_only: true as const,
    segment_stamp: { turn_id: 'turn-1', segment: 0, offset: 0, end: 8 }
  }

  const row = (parts: ChatMessage['parts'], pending = true): ChatMessage[] => [
    { id: 'u', role: 'user', parts: [textPart('prompt')] },
    { id: 'assistant-stream-runtime-1', role: 'assistant', pending, parts }
  ]

  const raw = (messages: ChatMessage[]) => {
    const part = messages.at(-1)!.parts.at(-1)!

    return part.type === 'text' ? (part.mediaSource ?? part.text) : ''
  }

  it('puts the snapshot under a run that starts past its cut, keeping the MEDIA source', () => {
    // The row lost [3, 8) and then took a delta past the cut that carries a MEDIA tag.
    const live = appendAssistantTextPart(
      appendAssistantTextPart([], 'A2 '),
      'MEDIA:/tmp/AI Brain/r.pdf\n',
      undefined,
      span(8, 34)
    )
    const merged = mergeSegmentSnapshotIntoLiveRow(row(live), 'runtime-1', inflight)

    expect(raw(merged)).toBe('A2 TAIL MEDIA:/tmp/AI Brain/r.pdf\n')
    expect(merged.at(-1)!.parts.at(-1)).toMatchObject({ streamSpan: span(0, 34) })
  })

  it('keeps the row text before a snapshot whose span starts past zero', () => {
    // The backend restarted its span at 8 (a gap in its own stream); the row holds [0, 8).
    const messages = row([{ ...textPart('A2 TAIL '), streamSpan: span(0, 8) }])
    const tail = { ...inflight, assistant: 'NEXT ', segment_stamp: { ...inflight.segment_stamp, offset: 8, end: 13 } }

    expect(raw(appendLiveSessionProjection(messages, { session_id: 'runtime-1', inflight: tail }))).toBe(
      'A2 TAIL NEXT '
    )
    expect(raw(mergeSegmentSnapshotIntoLiveRow(messages, 'runtime-1', tail))).toBe('A2 TAIL NEXT ')
  })

  it('leaves a row whose run already covers the snapshot, a settled row, and another segment', () => {
    const covered = row([{ ...textPart('A2 TAIL NEXT '), streamSpan: span(0, 13) }])
    const settled = row([{ ...textPart('NEXT '), streamSpan: span(8, 13) }], false)
    const later = row([{ ...textPart('second'), streamSpan: span(0, 6, 1) }])
    const unstamped = row([textPart('A2 NEXT ')])

    for (const messages of [covered, settled, later, unstamped]) {
      expect(mergeSegmentSnapshotIntoLiveRow(messages, 'runtime-1', inflight)).toBe(messages)
    }
  })
})
