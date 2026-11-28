import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { resolve, join } from 'node:path'

/** One bounded producer invocation per suite; never manufacture status envelopes. */
export function produceSessionStateV2() {
  const root = resolve(import.meta.dirname, '../../../..')
  const dir = mkdtempSync(join(tmpdir(), 'desktop-session-state-v2-'))
  try {
    const output = join(dir, 'producer.json')
    execFileSync(process.env.HERMES_SESSION_FIXTURE_PYTHON || 'python',
      ['-m', 'tests.hermes_state.session_state_v2_fixtures', '--output', output],
      { cwd: root, timeout: 60_000, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } })
    return JSON.parse(readFileSync(output, 'utf8'))
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}
