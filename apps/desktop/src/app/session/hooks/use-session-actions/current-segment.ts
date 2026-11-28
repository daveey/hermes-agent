import {
  appendAssistantTextPart,
  assistantTextPart,
  type ChatMessage,
  type ChatMessagePart,
  closingStreamRun,
  sameStreamSegment,
  type StreamSpan
} from '@/lib/chat-messages'
import type { SessionResumeResult } from '@/types/hermes'

type SegmentInflight = NonNullable<SessionResumeResult['inflight']>

/** The prose after a message's last tool/reasoning boundary. */
function closingText(message: ChatMessage): string {
  const boundary = message.parts.findLastIndex(part => part.type !== 'text') + 1

  return message.parts
    .slice(boundary)
    .map(part => (part.type === 'text' ? part.text : ''))
    .join('')
}

/** The snapshot's span in the stamped stream, or null for a snapshot that carries no identity. */
function snapshotSpan(inflight: SegmentInflight): null | StreamSpan {
  const stamp = inflight.segment_stamp
  const ok = (value: unknown) => typeof value === 'number' && Number.isInteger(value) && value >= 0

  if (!stamp || typeof stamp.turn_id !== 'string' || !stamp.turn_id || !ok(stamp.segment) || !ok(stamp.offset)) {
    return null
  }

  return ok(stamp.end) && stamp.end >= stamp.offset
    ? { turnId: stamp.turn_id, segment: stamp.segment, start: stamp.offset, end: stamp.end }
    : null
}

/** The snapshot as the closing text part of a live row (claimed by its span when stamped). */
function snapshotParts(text: string, span: null | StreamSpan): ChatMessagePart[] {
  if (!text) {
    return []
  }

  return span ? appendAssistantTextPart([], text, undefined, span) : [assistantTextPart(text)]
}

/** A live row's closing text: the snapshot, plus what `live`'s stamped run streamed past it. */
function withLiveRun(parts: ChatMessagePart[], live: ReturnType<typeof closingStreamRun>): ChatMessagePart[] {
  // appendAssistantTextPart admits by offset: units the snapshot already holds add nothing.
  return live ? appendAssistantTextPart(parts, live.text, undefined, live.span) : parts
}

/** Canonical snapshots describe just the current segment, not the legacy flat turn dump. */
export function appendCurrentSegment(
  messages: ChatMessage[],
  sessionId: string,
  inflight: SegmentInflight
): ChatMessage[] {
  const text = inflight.assistant ?? ''
  const span = snapshotSpan(inflight)
  const last = messages.at(-1)
  const current = last?.role === 'assistant' && last.pending ? last : null

  if (!current) {
    // A snapshot cut before the turn settled can reach a view that already
    // holds the committed reply (the history read came after message.complete):
    // the durable row's closing prose is that segment, so appending it again
    // would show the answer twice.
    if (text && last?.role === 'assistant' && closingText(last).startsWith(text)) {
      return messages
    }

    return [
      ...messages,
      {
        id: `assistant-stream-${sessionId}`,
        role: 'assistant',
        parts: snapshotParts(text, span),
        pending: true
      }
    ]
  }

  // A tool/reasoning boundary seals the prose before it. Replace only the
  // cumulative trailing text, retaining narration, tools and their identity.
  const boundary = current.parts.findLastIndex(part => part.type !== 'text') + 1
  const live = closingStreamRun(current)

  if (span) {
    // A later segment of the same turn already streams here: the snapshot is behind it.
    if (live && live.span.turnId === span.turnId && live.span.segment > span.segment) {
      return messages
    }

    // Deltas delivered after the snapshot's cut but before this reconcile sit
    // past `span.end` in the row; keep them by offset instead of guessing from text.
    const sameSegment = live && sameStreamSegment(live.span, span) ? live : null

    // The row holds stamped text before the snapshot's start (the backend
    // restarted its span past a gap in its own stream): the snapshot is only a
    // tail, so it lands on the row by offset instead of replacing that text.
    if (sameSegment && sameSegment.span.start < span.start) {
      const closing = current.parts.slice(boundary)
      const extended = appendAssistantTextPart(closing, text, undefined, span)

      return extended === closing
        ? messages
        : [...messages.slice(0, -1), { ...current, parts: [...current.parts.slice(0, boundary), ...extended] }]
    }

    const parts = [...current.parts.slice(0, boundary), ...withLiveRun(snapshotParts(text, span), sameSegment)]

    return [...messages.slice(0, -1), { ...current, parts }]
  }

  // No stamp (an emitter without segment identity): a live delta may already
  // have extended the snapshot while resume awaited.
  if (closingText(current).startsWith(text)) {
    return messages
  }

  return [
    ...messages.slice(0, -1),
    {
      ...current,
      parts: [...current.parts.slice(0, boundary), ...snapshotParts(text, null)]
    }
  ]
}

/**
 * Carry the stamped deltas that reached `liveMessages` (the runtime's live
 * state) before a resume answered onto the snapshot row of `messages`.
 *
 * The backend cuts the snapshot when it subscribes the viewer, so frames
 * after that cut can be applied while the resume RPC is still in flight. A
 * cold resume rebuilds the transcript from the snapshot and the stored page;
 * without this the row restarts at the snapshot and those deltas are gone
 * until the turn completes. Only the same turn and segment merge, by offset.
 */
export function graftEarlySegmentDeltas(
  messages: ChatMessage[],
  liveMessages: ChatMessage[],
  inflight: SessionResumeResult['inflight']
): ChatMessage[] {
  const span = inflight?.segment_only ? snapshotSpan(inflight) : null
  const target = messages.at(-1)

  if (!span || target?.role !== 'assistant' || !target.pending) {
    return messages
  }

  const held = closingStreamRun(target)
  const liveRow = liveMessages.findLast(message => message.role === 'assistant' && message.pending)
  const live = liveRow && liveRow !== target ? closingStreamRun(liveRow) : null

  if (!held || !sameStreamSegment(held.span, span) || !live || !sameStreamSegment(live.span, span)) {
    return messages
  }

  const boundary = target.parts.findLastIndex(part => part.type !== 'text') + 1
  const closing = target.parts.slice(boundary)
  const parts = withLiveRun(closing, live)

  return parts === closing
    ? messages
    : [...messages.slice(0, -1), { ...target, parts: [...target.parts.slice(0, boundary), ...parts] }]
}

/**
 * Merge a stamped snapshot under the live row it was cut from, after a warm
 * activation adopted the runtime's latest row wholesale.
 *
 * The warm path reconciles the snapshot onto the cache it read before the
 * activate RPC, then overlays the rows that changed while it was in flight
 * (identity-based). A stamped delta past the snapshot's cut that reached the
 * row during the RPC makes that row the overlay winner, and it never held the
 * snapshot's range. Only a pending row whose closing stamped run is in the
 * snapshot's segment and does not already cover the snapshot's span merges.
 */
export function mergeSegmentSnapshotIntoLiveRow(
  messages: ChatMessage[],
  sessionId: string,
  inflight: SessionResumeResult['inflight']
): ChatMessage[] {
  const span = inflight?.segment_only ? snapshotSpan(inflight) : null
  const target = messages.at(-1)

  if (!inflight || !span || target?.role !== 'assistant' || !target.pending) {
    return messages
  }

  const held = closingStreamRun(target)

  if (!held || !sameStreamSegment(held.span, span) || (held.span.start <= span.start && held.span.end >= span.end)) {
    return messages
  }

  return appendCurrentSegment(messages, sessionId, inflight)
}

export interface ProjectionCorrections {
  entries: { text: string; offset: number | undefined }[]
  texts: string[]
  offsetsUsable: boolean
}

/** Mid-turn redirect corrections of a live projection, with the assistant-text
 *  offset at which each was accepted (older gateways omit the offsets). */
export function projectionCorrections(projection: Pick<SessionResumeResult, 'inflight'>): ProjectionCorrections {
  const rawCorrections = projection.inflight?.corrections ?? []
  const rawOffsets = projection.inflight?.correction_offsets

  const inflightCorrectionEntries = rawCorrections
    .map((correction, index) => ({ text: correction?.trim() ?? '', offset: rawOffsets?.[index] }))
    .filter(entry => entry.text)

  const inflightCorrections = inflightCorrectionEntries.map(entry => entry.text)

  const correctionOffsetsUsable =
    inflightCorrectionEntries.length > 0 &&
    inflightCorrectionEntries.every(entry => typeof entry.offset === 'number' && entry.offset >= 0)

  return { entries: inflightCorrectionEntries, texts: inflightCorrections, offsetsUsable: correctionOffsetsUsable }
}
