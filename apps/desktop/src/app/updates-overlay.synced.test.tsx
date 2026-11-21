import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { I18nProvider } from '@/i18n/context'
import { en } from '@/i18n/en'
import { $updateApply, $updateOverlayOpen, $updateOverlayTarget, resetUpdateApplyState } from '@/store/updates'

import { UpdatesOverlay } from './updates-overlay'

afterEach((): void => {
  cleanup()
  $updateOverlayOpen.set(false)
  resetUpdateApplyState()
})

// [daveey] Update handed to hermes-sync: the dialog says so and closes, instead of "Restarting…" or a failure.
it('shows the hermes-sync hand-off and closes on Done', async (): Promise<void> => {
  const copy = en.shell.statusbar.forceUpdate
  $updateOverlayTarget.set('client')
  $updateOverlayOpen.set(true)
  $updateApply.set({
    applying: false,
    stage: 'synced',
    message: copy.syncedApplying,
    percent: null,
    error: null,
    log: []
  } as never)
  await act(async (): Promise<void> => {
    render(
      <I18nProvider configClient={null} initialLocale="en">
        <UpdatesOverlay />
      </I18nProvider>
    )
  })
  expect(screen.getByText(copy.syncedTitle)).toBeTruthy()
  expect(screen.getByText(copy.syncedApplying)).toBeTruthy()
  fireEvent.click(screen.getByText(en.updates.done))
  expect($updateOverlayOpen.get()).toBe(false)
  expect($updateApply.get().stage).toBe('idle')
})
