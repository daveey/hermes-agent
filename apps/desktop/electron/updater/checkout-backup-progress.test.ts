import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

import { expect, it } from 'vitest'

import { updateProgressLogLine } from './checkout'
import { readDesktopUpdateConfig } from './checkout-source'

const GIB = 1024 ** 3

it('logs a backup’s start, end and check but not its twice-a-second ticks', () => {
  const copy = (doneBytes: number, percent: number) => ({
    stage: 'backup',
    message: `Backing up state.db (copy): ${doneBytes / GIB} of 4 GB`,
    percent,
    backup: { phase: 'copy' as const, doneBytes, totalBytes: 4 * GIB, method: 'copy' as const }
  })

  expect(updateProgressLogLine(copy(0, 0))).toBe('[updates] backup: Backing up state.db (copy): 0 of 4 GB')
  expect(updateProgressLogLine(copy(GIB, 25))).toBe('')
  expect(updateProgressLogLine(copy(4 * GIB, 100))).toBe('[updates] backup: Backing up state.db (copy): 4 of 4 GB')
  expect(
    updateProgressLogLine({
      stage: 'backup',
      message: 'Checking the state.db backup…',
      percent: null,
      backup: { phase: 'check', doneBytes: 4 * GIB, totalBytes: 4 * GIB, method: 'copy' }
    })
  ).toBe('[updates] backup: Checking the state.db backup…')
  expect(updateProgressLogLine({ stage: 'error', message: '', error: 'boom', percent: null })).toBe(
    '[updates] error: boom'
  )
})

it('reads the Desktop update branch, falling back to the default', () => {
  const dir: string = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-update-config-'))
  const config: string = path.join(dir, 'desktop-update.json')

  try {
    expect(readDesktopUpdateConfig(config, 'main')).toEqual({ branch: 'main', branchExplicit: false })
    fs.writeFileSync(config, JSON.stringify({ branch: ' feature/x ' }))
    expect(readDesktopUpdateConfig(config, 'main')).toEqual({ branch: 'feature/x', branchExplicit: true })
    fs.writeFileSync(config, JSON.stringify({ branch: '' }))
    expect(readDesktopUpdateConfig(config, 'main')).toEqual({ branch: 'main', branchExplicit: false })
    fs.writeFileSync(config, '{not json')
    expect(readDesktopUpdateConfig(config, 'stable')).toEqual({ branch: 'stable', branchExplicit: false })
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})
