import { type ChatMessage, textPart } from '@/lib/chat-messages'
import type { SessionResumeResult } from '@/types/hermes'

/** Both reconnect projections must carry the owner's hold, not just its text. */
export function queuedProjectionMessage(
  sessionId: string,
  queued: NonNullable<SessionResumeResult['queued']>
): ChatMessage {
  return {
    id: `user-queued-${sessionId}`,
    role: 'user',
    parts: [textPart(queued.user?.trim() ?? '')],
    queued_reason: queued.queued_reason ?? undefined,
    queued_started_at: queued.queued_started_at
  }
}
