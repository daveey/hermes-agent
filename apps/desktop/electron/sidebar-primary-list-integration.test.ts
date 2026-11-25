import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

import ts from 'typescript'
import { test } from 'vitest'

import * as routing from './profile-session-routing'
import * as ownerRoute from './session-owner-list-route'
import { fetchSidebarPrimaryListBatch } from './sidebar-primary-list-batch'

// Execute the production main-process handlers without booting Electron.
const source = ts.createSourceFile('main.ts', readFileSync(new URL('./main.ts', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true)
const names = ['interceptSessionRequestForRemote', 'fetchProfilesSessionSlice', 'mergeRemoteProfileSessions']
const functions = source.statements.filter(n => ts.isFunctionDeclaration(n) && names.includes(n.name?.text ?? ''))
assert.equal(functions.length, names.length)
const code = ts.transpileModule(functions.map(n => n.getText(source)).join('\n'), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

test('production sidebar intercept and merge retain parity while eliminating primary slice reads', async () => {
  const query = 'recents_profile=all&recents_limit=2&cron_limit=2&messaging_limit=2&recents_exclude=cron&messaging_exclude=cli,cron'
  const slice = (kind: string) => ({ sessions: [{ id: kind, profile: 'default', last_active: 10 }], total: 1, profile_totals: { default: 1 }, errors: [] })
  const bases = { recents: slice('chat'), cron: slice('cron'), messaging: slice('telegram') }

  const run = async (batchSupported: boolean) => {
    const reads: string[] = []

    const context = vm.createContext({
      ...routing, ...ownerRoute, fetchSidebarPrimaryListBatch, URLSearchParams,
      configuredRemoteProfileNames: () => [],
      pooledRegistrySessionSources: async () => [{ connectionId: 'hub', reusesPrimary: true, kind: 'ssh', backends: [{ descriptor: {}, profile: 'default' }], installId: 'primary' }],
      globalRemoteActive: () => true,
      readDesktopConnectionsRegistry: () => ({ primary: 'hub' }),
      profileHasRemoteOverride: () => false,
      primaryBackendInstallId: async () => 'primary',
      rowsOf: (value: any) => value.sessions || [],
      getJsonForBackend: async () => assert.fail('reused primary must not read registry'),
      fetchJsonForProfile: async (_profile: unknown, path: string) => {
        reads.push(path)

        if (path.includes('/sidebar?')) {return batchSupported ? { list_slices_version: 1, ...structuredClone(bases) } : {}}
        const sp = new URL(path, 'http://test').searchParams

        return structuredClone(sp.get('source') === 'cron' ? bases.cron : sp.get('exclude_sources') === 'cli,cron' ? bases.messaging : bases.recents)
      }
    })

    vm.runInContext(code, context)
    const result = await vm.runInContext(`interceptSessionRequestForRemote({path: '/api/profiles/sessions/sidebar?${query}'})`, context)

    return { result: JSON.parse(JSON.stringify(result)), reads }
  }

  const batched = await run(true)
  const legacy = await run(false)
  assert.deepEqual(batched.result, legacy.result)
  assert.equal(batched.reads.length, 1)
  assert.equal(legacy.reads.length, 4) // unsupported batch followed by the three original slices
  assert.equal(batched.result.recents.sessions[0].connection_id, undefined) // unpinned primary stays bare
})
