import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import type { DesktopUpdateProgress } from '@/global'
import { I18nProvider } from '@/i18n/context'
import { en } from '@/i18n/en'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  checkHermesUpdate: vi.fn().mockResolvedValue(null),
  getActionStatus: vi.fn(),
  getHermesUpdateProgress: vi.fn().mockResolvedValue({ progress: null }),
  updateHermes: vi.fn()
}))
vi.mock('@/store/notifications', () => ({ dismissNotification: vi.fn(), notify: vi.fn() }))

const updates = await import('@/store/updates')
const { UpdatesOverlay, backupSizes } = await import('./updates-overlay')

const GIB = 1024 ** 3

afterEach((): void => {
  cleanup()
  updates.stopUpdatePoller()
  updates.$updateOverlayOpen.set(false)
  updates.$updateStatus.set(null)
  updates.resetUpdateApplyState()
  Reflect.deleteProperty(window, 'hermesDesktop')
  vi.restoreAllMocks()
})

function backupEvent(backup: DesktopUpdateProgress['backup'], percent: number | null): DesktopUpdateProgress {
  return { stage: 'backup', message: 'Backing up state.db', percent, error: null, at: Date.now(), backup }
}

it('shows the backup size and a determinate bar while the pre-update backup runs, then the check', async () => {
  let emit: (payload: DesktopUpdateProgress) => void = () => {}

  window.hermesDesktop = {
    updates: {
      apply: () => new Promise(() => {}), // the update is still running
      check: vi.fn().mockResolvedValue({ supported: true, behind: 1 }),
      onProgress: (callback: (payload: DesktopUpdateProgress) => void) => {
        emit = callback

        return () => {}
      }
    }
  } as unknown as Window['hermesDesktop']
  updates.startUpdatePoller()
  updates.$updateOverlayTarget.set('client')
  updates.$updateOverlayOpen.set(true)
  void updates.applyUpdates()

  await act(async (): Promise<void> => {
    render(
      <I18nProvider configClient={null} initialLocale="en">
        <UpdatesOverlay />
      </I18nProvider>
    )
  })

  // The start: title and an indeterminate bar before any size is known.
  await act(async (): Promise<void> => {
    emit(backupEvent({ phase: 'start', doneBytes: 0, totalBytes: 0, method: null }, null))
  })

  expect(screen.getAllByText(en.updates.stages.backup!).length).toBeGreaterThan(0)
  expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBeNull()

  const total = 21.7 * GIB
  const done = 12.3 * GIB

  await act(async (): Promise<void> => {
    emit(backupEvent({ phase: 'copy', doneBytes: 1 * GIB, totalBytes: total, method: 'copy' }, (1 / 21.7) * 100))
    emit(backupEvent({ phase: 'copy', doneBytes: done, totalBytes: total, method: 'copy' }, (done / total) * 100))
  })

  expect(screen.getByText('Backing up your chat history before updating: 12.3 of 21.7 GB')).toBeTruthy()
  expect(screen.getAllByText(en.updates.stages.backup!).length).toBeGreaterThan(0)
  const bar = screen.getByRole('progressbar')
  expect(bar.getAttribute('aria-valuenow')).toBe(String(Math.round((done / total) * 100)))
  // Ticks replace each other in the log instead of filling it.
  expect(updates.$updateApply.get().log.filter(entry => entry.stage === 'backup')).toHaveLength(1)

  await act(async (): Promise<void> => {
    emit(backupEvent({ phase: 'check', doneBytes: total, totalBytes: total, method: 'copy' }, null))
  })

  expect(screen.getByText(en.updates.backupChecking)).toBeTruthy()
  expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBeNull()

  // The next stage leaves the backup view.
  await act(async (): Promise<void> => {
    emit({ stage: 'restart', message: 'Updating Hermes', percent: 100, error: null, at: Date.now() })
  })

  expect(screen.queryByText(en.updates.backupChecking)).toBeNull()
  expect(updates.$updateApply.get().backup).toBeNull()
})

it('formats both sizes in the total’s unit with one decimal', () => {
  expect(backupSizes(12.3 * GIB, 21.7 * GIB)).toEqual({ done: '12.3', total: '21.7 GB' })
  expect(backupSizes(0, 512 * 1024 ** 2)).toEqual({ done: '0.0', total: '512.0 MB' })
  expect(backupSizes(2048, 4096)).toEqual({ done: '2.0', total: '4.0 KB' })
  expect(backupSizes(10, 100)).toEqual({ done: '0.0', total: '0.1 KB' })
})
