import '../run-tmp'

/** Real runner subprocess for run-tmp-reaper.gateway.unit.test.ts (no Electron needed). */
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'

import { coreAppEnv, createCoreSandbox, REPO_ROOT, writeProviderHome } from './harness'

const [mode, python] = process.argv.slice(2)

if (mode === 'reap') {
  console.log(JSON.stringify({ reaped: true }))
} else {
  const sandbox = createCoreSandbox('reattach-reaper')
  writeProviderHome(sandbox.hermesHome, 'http://127.0.0.1:1')

  const result = spawnSync(python, ['-m', 'hermes_cli.main', 'gateway', 'ensure', '--json', '--timeout', '60'], {
    cwd: REPO_ROOT,
    env: coreAppEnv(sandbox, { PYTHONPATH: REPO_ROOT }),
    encoding: 'utf8',
    timeout: 90_000
  })

  if (result.status !== 0) {
    throw new Error(`gateway ensure failed: ${result.stdout}\n${result.stderr}`)
  }

  const { pid } = JSON.parse(fs.readFileSync(`${sandbox.hermesHome}/gateway.lock`, 'utf8'))
  console.log(JSON.stringify({ pid, root: process.env.HERMES_E2E_RUN_TMP, home: sandbox.home }))
  // The parent chooses SIGKILL (no teardown/exit hook) or a normal exit without sandbox cleanup.
  process.stdin.once('data', () => process.exit(0))
  process.stdin.resume()
}
