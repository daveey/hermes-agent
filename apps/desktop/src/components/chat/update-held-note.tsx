import { useI18n } from '@/i18n'

/** The backend supplies Unix seconds for the update, never the local enqueue clock. */
export function UpdateHeldNote({ startedAt }: { startedAt?: unknown }) {
  const { t } = useI18n()
  const started = typeof startedAt === 'number' && Number.isFinite(startedAt) ? new Date(startedAt * 1000) : null

  const time =
    started && Number.isFinite(started.getTime())
      ? `${String(started.getHours()).padStart(2, '0')}:${String(started.getMinutes()).padStart(2, '0')}`
      : undefined

  return <span data-slot="queue-update-note">{t.composer.queueUpdateNote(time)}</span>
}
