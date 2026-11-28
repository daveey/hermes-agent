import type { SessionInfo } from '@/hermes'
import type { Translations } from '@/i18n/types'
import type { ArchiveTrace } from '@/lib/archive-trace'
import { collectArchiveLinks, sessionKanbanArchiveLinks } from '@/store/archive-guards'
import { confirm as confirmAction } from '@/store/confirm'

import { reconnectArchivePreflight } from './reconnect-archive-preflight'

export async function confirmLinkedArchive(
  storedSessionId: string,
  archived: SessionInfo | undefined,
  profile: string | undefined,
  copy: Translations['desktop'],
  skip = false,
  trace?: ArchiveTrace
): Promise<boolean> {
  if (skip) {
    return true
  }

  const linked = [
    ...sessionKanbanArchiveLinks(
      await reconnectArchivePreflight(storedSessionId, archived, profile, copy, trace),
      copy
    ),
    ...(await collectArchiveLinks({
      connectionId: archived?.connection_id,
      ids: [...new Set([storedSessionId, archived?.id, archived?._lineage_root_id].filter(Boolean) as string[])],
      profile: profile || undefined,
      session: archived,
      sessionId: storedSessionId
    }))
  ]

  if (linked.length > 0) {
    const ok = await confirmAction({
      cancelLabel: copy.archiveLinkedCancel,
      confirmLabel: copy.archiveLinkedConfirm,
      description: copy.archiveLinkedBody,
      details: linked,
      title: copy.archiveLinkedTitle(archived?.title?.trim() || '')
    })

    return ok
  }

  return true
}
