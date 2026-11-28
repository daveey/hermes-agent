import { type GatewayEventMap, registryBackendScopeKey } from '@hermes/shared'

import { toChatMessages } from '@/lib/chat-messages'
import { knownOwnerForSession } from '@/store/session-states'

import type { GatewayEventContext } from './types'

function isCommittedUserRow(row: GatewayEventMap['message.admitted']): boolean {
  return (
    Number.isSafeInteger(row.user_row_id) &&
    row.user_row_id > 0 &&
    Boolean(row.message_uid) &&
    typeof row.text === 'string' &&
    Number.isFinite(row.timestamp)
  )
}

/** Merge only the committed user row: a busy stream still owns all assistant parts. */
export function handleAdmittedMessageEvent(ctx: GatewayEventContext): boolean {
  const { event, deps, explicitSid } = ctx

  if (event.type !== 'message.admitted') {
    return false
  }

  const row = event.payload as GatewayEventMap['message.admitted'] | undefined
  const current = deps.sessionStateByRuntimeIdRef.current.get(explicitSid)
  const owner = knownOwnerForSession(explicitSid)
  const profile = typeof owner === 'string' ? owner : (owner?.targetProfile ?? owner?.profile)
  const sourceProfile = typeof owner === 'string' ? owner : owner?.profile
  const connectionId = typeof owner === 'object' ? owner?.connectionId : undefined

  // Never infer the target from the foreground. A background tile can receive
  // this event, but only its already-bound runtime and owner may consume it.
  if (
    !explicitSid ||
    !current ||
    !owner ||
    !row ||
    current.storedSessionId !== row.stored_session_id ||
    profile !== row.profile ||
    registryBackendScopeKey(connectionId ?? null, sourceProfile ?? null) !==
      registryBackendScopeKey(event.connectionId ?? null, event.profile ?? null) ||
    !isCommittedUserRow(row)
  ) {
    return true
  }

  const id = `user-admitted-${row.message_uid}`

  deps.updateSessionState(explicitSid, state => {
    if (
      state.storedSessionId !== row.stored_session_id ||
      state.messages.some(message => message.id === id || message.rowId === row.user_row_id)
    ) {
      return state
    }

    const message = {
      ...toChatMessages([{ role: 'user', content: row.text, row_id: row.user_row_id, timestamp: row.timestamp }])[0],
      id
    }

    // Replay may arrive after a delta; insert before that output, never append
    // a new user boundary after it or replace the transcript with an old page.
    const nextIndex = state.messages.findIndex(existing =>
      existing.rowId !== undefined
        ? existing.rowId > row.user_row_id
        : existing.timestamp !== undefined && existing.timestamp >= row.timestamp
    )

    const index = nextIndex < 0 ? state.messages.length : nextIndex

    return { ...state, messages: [...state.messages.slice(0, index), message, ...state.messages.slice(index)] }
  })

  return true
}
