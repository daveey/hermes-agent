import type { SessionInfo } from '@/hermes'
import type { Translations } from '@/i18n/types'
import { collectArchiveLinks, kanbanArchiveLinks } from '@/store/archive-guards'
import { confirm as confirmAction } from '@/store/confirm'

export async function confirmLinkedArchive(storedSessionId: string, archived: SessionInfo | undefined, profile: string | undefined, copy: Translations['desktop'], skip = false): Promise<boolean> {
  if (skip) {return true}

        const linked = [
          ...kanbanArchiveLinks(archived, {
            label: copy.archiveLinkedTasksLabel,
            value: copy.archiveLinkedTasksValue
          }),
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
