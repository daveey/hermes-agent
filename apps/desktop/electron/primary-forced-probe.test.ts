import fs from 'node:fs'

import ts from 'typescript'
import { expect, test, vi } from 'vitest'

import { reuseMatchingPrimarySshBackend } from './connection-registry'
import { ensureProbedPrimaryBackend } from './primary-remote-revalidation'

function setup() {
  const descriptor = {
    mode: 'remote',
    remoteKind: 'ssh',
    baseUrl: 'http://127.0.0.1:12345',
    ssh: { effectiveConfigFingerprint: 'fixture' }
  }

  const ensureBackend = vi.fn(async () => descriptor)
  const revalidatePrimaryRemoteBackend = vi.fn(async () => ({ ok: true, rebuilt: false }))

  const bindings = {
    readDesktopConnectionsRegistry: () => ({ primary: 'hub', connections: [{ id: 'hub', kind: 'ssh', label: 'Hub' }] }),
    registryDialConnectionId: (id: string) => id,
    registryReconnectService: { assertCanDispatch() {} },
    PROFILE_NAME_RE: /^[a-zA-Z0-9_-]+$/,
    remoteWarmSet: { noteDial() {} },
    managedSshConfig: () => ({}),
    effectiveSshConfigFingerprint: async () => 'fixture',
    reuseMatchingPrimarySshBackend,
    ensureProbedPrimaryBackend,
    ensureBackend,
    revalidatePrimaryRemoteBackend
  }

  const source = ts.createSourceFile(
    'main.ts',
    fs.readFileSync(new URL('./main.ts', import.meta.url), 'utf8'),
    ts.ScriptTarget.Latest,
    true
  )

  const fn = source.statements.find(n => ts.isFunctionDeclaration(n) && n.name?.text === 'ensureRegistryBackend')!

  const code = ts.transpileModule(fn.getText(source) + '\nreturn ensureRegistryBackend', {
    compilerOptions: { target: ts.ScriptTarget.ES2022 }
  }).outputText

  const ensure = new Function(...Object.keys(bindings), code)(...Object.values(bindings))

  return { descriptor, ensure, ensureBackend, revalidatePrimaryRemoteBackend }
}

test('registry forced probe reaches the matched shared primary instead of returning its unprobed descriptor', async () => {
  const f = setup()
  const result = await f.ensure('hub', 'default', '', { forceProbe: true })
  expect(f.revalidatePrimaryRemoteBackend).toHaveBeenCalledTimes(1)
  expect(result.baseUrl).toBe(f.descriptor.baseUrl)
})

test.each([{}, { passive: true, forceProbe: true }])(
  'ordinary/passive shared-primary ensure does not probe: %j',
  async opts => {
    const f = setup()
    expect((await f.ensure('hub', 'default', '', opts)).baseUrl).toBe(f.descriptor.baseUrl)
    expect(f.revalidatePrimaryRemoteBackend).not.toHaveBeenCalled()
    expect(f.ensureBackend).toHaveBeenCalledTimes(1)
  }
)
