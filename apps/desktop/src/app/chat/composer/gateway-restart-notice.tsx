import { useStore } from '@nanostores/react'

import { ambientOwnerConnectionId } from '@/api/client'
import { useI18n } from '@/i18n'
import { getSessionOwnerHint } from '@/store/session'
import { $restartingSessionResumes, sessionResumeIsRestarting } from '@/store/session-resume-restart'

import { ActionBadges } from './micro-actions'

/** A real status line, not just a placeholder hidden under a retained draft. */
export function GatewayRestartNotice({ sessionId }: { sessionId?: string | null }) {
  const pending = useStore($restartingSessionResumes)
  const { t } = useI18n()
  const connectionId = (sessionId && getSessionOwnerHint(sessionId)?.connectionId) || ambientOwnerConnectionId()

  return sessionResumeIsRestarting(sessionId, connectionId, pending) ? (
    <div className="text-xs text-muted-foreground" role="status">
      {t.composer.placeholderHubRestarting}
    </div>
  ) : null
}

export function ComposerConnectionBadges({
  sessionId,
  storedSessionId
}: {
  sessionId: string | null
  storedSessionId?: string | null
}) {
  return (
    <>
      <GatewayRestartNotice sessionId={storedSessionId ?? sessionId} />
      <ActionBadges sessionId={sessionId} />
    </>
  )
}
