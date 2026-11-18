import type { ClientSessionState } from '@/app/types'
import { type ChatMessage, finalizeInterruptedMessages, sealOpenToolParts } from '@/lib/chat-messages'
import { isNoReplyNotice, withoutNoReplyNotice } from '@/lib/chat-messages/no-reply'

import { isLiveTurnAwaitingEvents } from './session-states-live-turn'

export const ENDED_TURN_STORE_RETRY_MS = [2_000, 8_000, 20_000]

export function settleEndedState(state: ClientSessionState, occurredAt: number): ClientSessionState {
  if (!isLiveTurnAwaitingEvents(state)) {
    return state
  }

  const messages = sealOpenToolParts(finalizeInterruptedMessages(state.messages, state.streamId, occurredAt))

  return {
    ...state,
    awaitingResponse: false,
    busy: false,
    heartbeatSettledStreamId:
      state.streamId && messages.some(message => message.id === state.streamId) ? state.streamId : null,
    messages,
    pendingBranchGroup: null,
    streamId: null,
    turnLive: false,
    turnStartedAt: null
  }
}

type Refresh = (runtimeId: string, storedId: string) => Promise<unknown> | unknown

interface RecoveryDeps {
  getState: (runtimeId: string) => ClientSessionState | undefined
  getRefresh: () => Refresh | undefined
  referenced: (runtimeId: string, storedId: string) => boolean
  mark: (runtimeId: string) => void
  write: (runtimeId: string, update: (state: ClientSessionState) => ClientSessionState) => void
}

export function logSilentTurnCheck(line: string) {
  try {
    console.info(line)
    window.hermesDesktop?.logLine?.(line)
  } catch {
    // Logging must never change a verdict.
  }
}

function stampedTail(messages: ChatMessage[]): number | null {
  const tail = messages.findLastIndex(message => message.role === 'user' && !message.hidden)

  return messages.some((message, index) => index > tail && isNoReplyNotice(message)) ? tail : null
}

/** Recovery belongs to one prompt occurrence, never a later prompt sharing its runtime. */
export function createNoReplyRecovery(deps: RecoveryDeps) {
  const recoveries = new Map<string, object>()

  const tailPrompt = (id: string) =>
    deps.getState(id)?.messages.findLast(message => message.role === 'user' && !message.hidden)?.id

  async function readStore(runtimeId: string): Promise<boolean> {
    const refresh = deps.getRefresh()
    const storedId = deps.getState(runtimeId)?.storedSessionId

    if (!refresh || !storedId || !deps.referenced(runtimeId, storedId)) {
      return false
    }

    try {
      return (await refresh(runtimeId, storedId)) !== false
    } catch {
      return false
    }
  }

  async function recover(runtimeId: string) {
    const recovery = {}
    recoveries.set(runtimeId, recovery)
    const prompt = tailPrompt(runtimeId)
    const current = () => recoveries.get(runtimeId) === recovery && tailPrompt(runtimeId) === prompt

    try {
      // Legacy callers without a store reader retain their local terminal recovery.
      if (!deps.getRefresh()) {
        deps.mark(runtimeId)

        return
      }

      for (let attempt = 0; ; attempt += 1) {
        const applied = await readStore(runtimeId)

        if (!current()) {
          return
        }

        if (applied || attempt >= ENDED_TURN_STORE_RETRY_MS.length) {
          logSilentTurnCheck(`[silent-turn] runtime=${runtimeId} recovery store_read=${applied ? 'applied' : 'none'} attempts=${attempt + 1}`)

          // A missing store answer is not proof that running the prompt again is safe.
          if (applied) {
            deps.mark(runtimeId)
          }

          return
        }

        await new Promise(resolve => setTimeout(resolve, ENDED_TURN_STORE_RETRY_MS[attempt]))
        const state = deps.getState(runtimeId)

        if (!current() || !state || isLiveTurnAwaitingEvents(state)) {
          return
        }
      }
    } finally {
      if (recoveries.get(runtimeId) === recovery) {
        recoveries.delete(runtimeId)
      }
    }
  }

  function disprove(runtimeId: string) {
    recoveries.delete(runtimeId)

    if (stampedTail(deps.getState(runtimeId)?.messages ?? []) === null) {
      return
    }

    deps.write(runtimeId, state => {
      const tail = stampedTail(state.messages)

      if (tail === null) {
        return state
      }

      return {
        ...state,
        messages: state.messages.flatMap((message, index) => {
          if (index <= tail || !isNoReplyNotice(message)) {
            return [message]
          }

          const rest = withoutNoReplyNotice(message)

          return rest.parts.length ? [rest] : []
        })
      }
    })
    logSilentTurnCheck(`[silent-turn] runtime=${runtimeId} no_reply_notice=disproved`)
  }

  return { recover, disprove }
}
