import { useEffect } from 'react'

import { Button } from '@/components/ui/button'
import { ErrorState } from '@/components/ui/error-state'
import { useI18n } from '@/i18n'
import type { CredentialFailureMetadata } from '@/lib/runtime-readiness'
import {
  closeManualOnboarding,
  type OnboardingContext,
  startManualOnboarding,
  startManualProviderOAuth
} from '@/store/onboarding'
import { setModelPickerOpen } from '@/store/session'

export function ExhaustedRecovery({ failure, ctx }: { failure: CredentialFailureMetadata; ctx: OnboardingContext }) {
  const { t } = useI18n()
  const copy = t.onboarding
  const provider = failure.provider || copy.exhaustedProvider

  const reset =
    typeof failure.reset_at === 'number' && Number.isFinite(failure.reset_at) ? new Date(failure.reset_at * 1000) : null

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        closeManualOnboarding()
      }
    }

    window.addEventListener('keydown', onKeyDown)

    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  return (
    <div className="grid gap-4" role="status">
      <ErrorState description={copy.exhaustedBody} title={copy.exhaustedTitle(provider)} />
      <p className="text-sm text-muted-foreground">
        {reset && !Number.isNaN(reset.getTime())
          ? copy.exhaustedReset(reset.toLocaleString())
          : copy.exhaustedResetUnknown}
      </p>
      <div className="flex flex-wrap gap-2">
        <Button
          onClick={() => {
            closeManualOnboarding()
            setModelPickerOpen(true)
          }}
        >
          {copy.exhaustedChooseModel}
        </Button>
        <Button
          onClick={() =>
            failure.provider
              ? startManualProviderOAuth(failure.provider, ctx.scope ?? ctx.profile)
              : startManualOnboarding(null, ctx.scope ?? ctx.profile)
          }
          variant="outline"
        >
          {copy.exhaustedAddAccount}
        </Button>
        <Button onClick={() => startManualOnboarding(null, ctx.scope ?? ctx.profile)} variant="text">
          {copy.pickDifferentProvider}
        </Button>
      </div>
    </div>
  )
}
