import { act, render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'

vi.mock('@/api/client', () => ({ ambientOwnerConnectionId: () => 'hub' }))
vi.mock('@/store/session', () => ({ getSessionOwnerHint: () => undefined }))
import { holdRestartingSessionResume } from '@/store/session-resume-restart'

import { GatewayRestartNotice } from './gateway-restart-notice'

it('paints Hub restarting while that owner/session waits, then clears without losing the draft', () => {
  const releaseOther = holdRestartingSessionResume(JSON.stringify(['other', 'chat']))
  render(
    <>
      <input defaultValue="retained draft" />
      <GatewayRestartNotice sessionId="chat" />
    </>
  )
  expect(screen.queryByRole('status')).toBeNull()
  let release!: () => void
  act(() => {
    release = holdRestartingSessionResume(JSON.stringify(['hub', 'chat']))
  })
  expect(screen.getByRole('status').textContent).toBe('Hub restarting…')
  expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('retained draft')
  act(() => {
    release()
    releaseOther()
  })
  expect(screen.queryByRole('status')).toBeNull()
})
