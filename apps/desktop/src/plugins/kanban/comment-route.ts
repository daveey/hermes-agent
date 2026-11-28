import { connectionForHubGateway } from '@hermes/plugin-sdk'

import type { TaskOwner } from './api'

/** A foreign-card refusal proves the first write did not happen. Never retry
 * timeouts or other uncertain failures: comments have no replay key. */
export async function commentOnOwner<T>(
  id: string,
  owner: TaskOwner | undefined,
  send: (target: TaskOwner | undefined) => Promise<T>
): Promise<T> {
  try {
    return await send(owner)
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    const prefix = `foreign_card: ${id} was created on `
    const start = message.indexOf(prefix)

    if (start < 0) {
      throw error
    }

    const ownerText = message.slice(start + prefix.length).split(' and lives there;')[0]
    const gateway = /^[a-zA-Z0-9_-]+$/.test(ownerText) ? ownerText : 'another gateway'
    const connection = gateway === 'another gateway' ? undefined : connectionForHubGateway(gateway)
    const unavailable = `This card belongs to ${gateway}. Your comment was not saved. Connect to that gateway and open the card there to comment.`

    if (!connection || !owner?.profile || !owner.board || connection === owner.gateway) {
      throw new Error(unavailable, { cause: error })
    }

    try {
      return await send({ ...owner, gateway: connection })
    } catch (cause) {
      // The owner's response can be uncertain. Do not queue or retry again.
      throw new Error(
        `Could not confirm that ${gateway} saved your comment. Open the card on that gateway and check its comments before trying again.`,
        { cause }
      )
    }
  }
}
