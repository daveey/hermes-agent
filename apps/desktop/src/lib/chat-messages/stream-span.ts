/**
 * Where a streamed text part sits in its turn's stamped reply
 * (tui_gateway/stream_segments.py: every message.delta carries `turn_id`,
 * `segment` and the UTF-16 `offset` of its text inside that segment).
 *
 * A part's `streamSpan` claims that its LAST `end - start` UTF-16 code units
 * (of the raw stream text, `mediaSource ?? text`) are offsets [start, end) of
 * that segment. Text before them (an in-flight journal row from the window
 * before a reload, an unstamped emitter) is not claimed. Two copies of the same
 * stream (a resume snapshot and the deltas that reached the renderer before
 * the resume answer, or a replayed chunk) are merged by these offsets, never
 * by comparing their text.
 */

import type { ChatMessage, ChatMessagePart } from './types'

export interface StreamSpan {
  turnId: string
  segment: number
  /** UTF-16 offset of the claimed run's first unit inside `segment`. */
  start: number
  /** UTF-16 offset just past the claimed run. */
  end: number
}

export const sameStreamSegment = (a: StreamSpan, b: StreamSpan) => a.turnId === b.turnId && a.segment === b.segment

/** The part of `text` (claimed as `span`) not already covered by `held`, and the claim after appending it. */
export function admitStreamText(
  held: StreamSpan | undefined,
  span: StreamSpan,
  text: string
): { span: StreamSpan; text: string } {
  if (!held || !sameStreamSegment(held, span)) {
    return { span, text }
  }

  if (span.end <= held.end) {
    return { span: held, text: '' }
  }

  // A gap (frames lost) keeps the whole text and claims only it: the units
  // between the two runs were never applied here.
  if (span.start > held.end) {
    return { span, text }
  }

  return { span: { ...held, end: span.end }, text: text.slice(held.end - span.start) }
}

/** Raw stream text of a text part (the source behind rendered MEDIA cards). */
const rawText = (part: Extract<ChatMessagePart, { type: 'text' }>) => part.mediaSource ?? part.text

/** The stamped run a message's closing prose (after its last tool/reasoning boundary) ends with, or null. */
export function closingStreamRun(message: ChatMessage): { span: StreamSpan; text: string } | null {
  const boundary = message.parts.findLastIndex(part => part.type !== 'text') + 1

  for (let index = message.parts.length - 1; index >= boundary; index -= 1) {
    const part = message.parts[index]

    if (part.type !== 'text') {
      continue
    }

    const span = part.streamSpan
    const raw = rawText(part)

    if (!span || span.end < span.start || span.end - span.start > raw.length) {
      return null
    }

    return { span, text: raw.slice(raw.length - (span.end - span.start)) }
  }

  return null
}
