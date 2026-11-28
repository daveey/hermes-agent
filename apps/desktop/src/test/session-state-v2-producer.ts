import { execFileSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

/**
 * The checkout's test interpreter, resolved like vitest.hermes-python.ts (which only the
 * electron project runs): a PATH python lacks the application dependencies.
 */
function fixturePython(root: string): string {
  const explicit = process.env.HERMES_SESSION_FIXTURE_PYTHON || process.env.HERMES_PYTHON

  if (explicit) {
    return explicit
  }

  return execFileSync(
    'bash',
    [join(root, 'scripts', 'run-in-hermes-env'), 'sh', '-c', 'printf %s "$__HERMES_TEST_PYTHON"'],
    { cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'] }
  )
}

/** One bounded producer invocation per suite; never manufacture status envelopes. */
export function produceSessionStateV2() {
  const root = resolve(import.meta.dirname, '../../../..')
  const dir = mkdtempSync(join(tmpdir(), 'desktop-session-state-v2-'))
  // Outside pytest's home isolation: the producer must never touch the invoking user's home.
  const home = join(dir, 'home')
  mkdirSync(home)

  try {
    const output = join(dir, 'producer.json')
    execFileSync(fixturePython(root), ['-m', 'tests.hermes_state.session_state_v2_fixtures', '--output', output], {
      cwd: root,
      timeout: 60_000,
      env: { ...process.env, HERMES_HOME: join(home, '.hermes'), HOME: home, PYTHONDONTWRITEBYTECODE: '1' }
    })

    return JSON.parse(readFileSync(output, 'utf8'))
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}
