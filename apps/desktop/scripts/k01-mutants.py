#!/usr/bin/env python3
"""Offline K01 authority mutations. Never edit the input checkouts.
Usage: python3 k01-mutants.py CORE MODS OUT [comma-separated IDs]
A mutation is killed only by failed executed testcases, not collection/setup loss.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

core, mods, out = map(lambda p: Path(p).resolve(), sys.argv[1:4])
out.mkdir(parents=True, exist_ok=True)
fixture = out / 'fixture'
if fixture.exists(): shutil.rmtree(fixture)
fixture.mkdir()
paths = subprocess.check_output(['git','-C',str(core),'ls-files','-z','--cached','--others','--exclude-standard'],timeout=30).decode().split('\0')
inputs = {}
for name in filter(None,paths):
    source=core/name
    if not source.is_file(): continue
    dest=fixture/'core'/name; dest.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(source,dest)
    inputs['core/'+name]=hashlib.sha256(source.read_bytes()).hexdigest()
companion=fixture/'mods/session-tasks-plugin.js';companion.parent.mkdir(parents=True)
shutil.copy2(mods/'desktop-plugins/session-tasks/plugin.js',companion)
inputs['mods/session-tasks-plugin.js']=hashlib.sha256(companion.read_bytes()).hexdigest()
for rel in ['node_modules','apps/desktop/node_modules']:
    (fixture/'core'/rel).symlink_to(core/rel,target_is_directory=True)
(out/'input-shas.json').write_text(json.dumps(inputs,indent=2))
app=fixture/'core/apps/desktop'
identity='src/contrib/session-tasks-identity.ts'; runtime='src/sdk/runtime.ts';loader='src/contrib/runtime-loader.ts'
itest='src/contrib/session-tasks-identity.test.ts';ctest='src/contrib/session-tasks-capture.test.ts';atest='src/sdk/loaded-assets.test.ts'
mutants=[]
def add(id,path,old,new,tests): mutants.append(dict(id=id,path=path,old=old,new=new,tests=tests))
add('surface-object',identity,'descriptor?.value !== activation.surface','false',[itest])
add('surface-required',identity,'!activation.surface','false',[itest])
add('surface-names',identity,'names.length !== activation.names?.length','false',[itest])
add('surface-per-name',identity,'after !== undefined &&','',[itest])
for key in ['configurable','enumerable','writable','value','get','set']:
    add('descriptor-'+key,identity,"'"+key+"'," if key!='set' else ",'set'",'', [itest])
add('bind-synchronous',identity,'registering !== activation','false',[itest])
add('bind-once',identity,'if (called)','if (false)',[itest])
add('bind-plugin-id',identity,"input.pluginId !== 'session-tasks'",'false',[itest])
add('bind-type',identity,"typeof surface !== 'object'",'false',[itest])
add('bind-null',identity,'surface === null','false',[itest])
add('reentrant-registration',identity,'if (registering)','if (false)',[itest])
add('reentrant-bind',identity,"activation.reason === 'REENTRANT_REGISTRATION'",'false',[itest])
add('settle-sticky',identity,"activation.reason !== 'REGISTRATION_PENDING'",'false',[itest])
add('settle-dependency',identity,'input.dependencyReason ??','null ??',[ctest])
add('settle-binding',identity,"activation.surface ? 'MATCH' : 'MISSING_BINDING'","true ? 'MATCH' : 'MISSING_BINDING'",[itest])
add('activation-owner',identity,"input.pluginId === 'session-tasks'",'true',[itest])
add('invalidate',identity,'activation.reason = reason','activation.reason = activation.reason',[itest])
add('navigation-url',identity,'active.navigation !== location.href','false',[itest])
add('surface-status',identity,"active.reason === 'MATCH' && !matches",'false',[itest])
add('unload-before-disposal',loader,'if (disposers) {invalidators.get(disposers)?.()}','void disposers',[itest])
add('reload-before-await',loader,"if ($pluginRecords.get()[id]?.file === file)",'if (false)',[itest])
add('reload-no-file',loader,'if (!file) {return}','if (false) {return}',[itest])
add('navigation-document',identity,'active.document !== document','false',[itest])
add('read-not-loaded',identity,'if (!active)','if (false)',[ctest])
for event in ['pagehide','beforeunload','popstate','hashchange']:
    add('event-'+event,identity,"'"+event+"'","'disabled-"+event+"'",[itest])
add('disk-reload-before-read',loader,'if (previous)','if (false)',[ctest])
add('shim-evaluation-hook',runtime,'globalThis.__HERMES_CAPTURE_PLUGIN_SHIM__?.(import.meta.url, m);','',[ctest])
add('shim-known-only',runtime,'!shimSources.has(url) ||','',[ctest])
add('shim-capture-once',runtime,'|| captures.has(url)','',[ctest])
add('capture-idempotent-end',runtime,'if (ended)','if (false)',[ctest])
add('capture-refcount-enter',runtime,'captureUsers++ === 0','true',[ctest])
add('capture-refcount-leave',runtime,'--captureUsers === 0','true',[ctest])
add('capture-prior-descriptor',runtime,'if (priorCapture)','if (false)',[ctest])
add('capture-missing',runtime,'if (!captured)','if (false)',[ctest])
add('capture-evidence-failure',runtime,'if (evidence.reason)','if (false)',[ctest])
add('capture-reference',runtime,'namespace:captured.namespace',"namespace:(globalThis as any).__HERMES_PLUGIN_SDK__",[ctest])
add('companion-immediate-bind','@companion','ctx.bindSessionTasksDiagnostic?.(surface)','void surface',[itest])
add('companion-owned-cleanup','@companion',"Object.getOwnPropertyDescriptor(window, '__sessionTasks')?.value === surface",'true',[itest])
add('dependency-closed-set',loader,'dynamic || specifiers.some(specifier => !supported.has(specifier))','false',[itest])
add('static-import-comments',loader,r'const gap = String.raw`(?:\s|\/\*[\s\S]*?\*\/|\/\/[^\n]*\n)`',r'const gap = String.raw`\s`',[itest])
# Digest semantics, not digest-shaped strings.
add('exact-sha','src/sdk/loaded-assets.ts',"crypto.subtle.digest('SHA-256', bytes)","crypto.subtle.digest('SHA-256', new Uint8Array())",[atest])
add('static-asset-bytes','src/sdk/loaded-assets.ts','...await identifyRawBytes(await read(url))','...await identifyBytes(url)',[atest])
add('served-bom-bytes','src/sdk/loaded-assets.ts','new Uint8Array(await response.arrayBuffer())','new TextEncoder().encode(await response.text())',[atest])
add('static-closure','src/sdk/loaded-assets.ts','for (const dependency of item.imports ?? [])','for (const dependency of [])',[atest])
add('graph-limit','src/sdk/loaded-assets.ts','seen.size >= 256','false',[atest])
add('root-protocol','src/sdk/loaded-assets.ts',"!['http:', 'https:', 'file:'].includes(rootUrl.protocol)",'false',[atest])
add('root-bundled','src/sdk/loaded-assets.ts',"!rootUrl.pathname.includes('/assets/')",'false',[atest])
add('response-ok','src/sdk/loaded-assets.ts','!response.ok','false',[atest])
add('response-redirect','src/sdk/loaded-assets.ts','response.url && response.url !== url','false',[atest])
add('io-deadline','src/sdk/loaded-assets.ts','AbortSignal.timeout(5_000)','new AbortController().signal',[atest])
add('manifest-root','src/sdk/loaded-assets.ts','if (!rootKey)','if (false)',[atest])
add('graph-cycle','src/sdk/loaded-assets.ts','if (seen.has(key))','if (false)',[atest])
add('graph-entry','src/sdk/loaded-assets.ts','!item ||','',[atest])
add('asset-path-regex','src/sdk/loaded-assets.ts',r'!/^assets\/[^?#\\]+\.js$/.test(item.file)','false',[atest])
add('asset-path-traversal','src/sdk/loaded-assets.ts',"item.file.split('/').includes('..')",'false',[atest])
rtest='src/contrib/k01-review-probes.test.ts'
add('pending-owner-exists',loader,'pending && pending.file !== options.file','false',[rtest])
add('pending-owner-file',loader,'pending.file !== options.file','false',[rtest])
add('pending-unload',loader,'  pendingIdentities.delete(id)','  void id',[rtest])
add('pending-current-claim',loader,'pendingIdentities.get(plugin.id) !== claim','false',[rtest])
add('pending-release-owned',loader,'pendingIdentities.get(plugin.id) === claim','true',[rtest])
add('crypto-fail-closed',loader,"dependencyReason:'DIAGNOSTIC_IDENTITY_UNAVAILABLE'",'dependencyReason:null',[rtest])
add('crypto-load-compatibility',loader,'// Diagnostic evidence is optional for loading, never optional for MATCH.',"throw new Error('diagnostic crypto gates loading')",[rtest])
add('companion-rollback-custody','@companion','ctx.onDispose?.(dispose)\n    window.__sessionTasks = surface\n    ctx.bindSessionTasksDiagnostic?.(surface)',
    'window.__sessionTasks = surface\n    ctx.bindSessionTasksDiagnostic?.(surface)\n    ctx.onDispose?.(dispose)',[rtest])
add('companion-rollback-poll','@companion','if (timer) clearInterval(timer)','if (false) clearInterval(timer)',[rtest])
add('companion-rollback-rpc','@companion','      clearTimeout(rpcEventTimer)','      void rpcEventTimer',[rtest])
add('companion-rollback-focus','@companion','for (const stop of stops) stop?.()','void stops',[rtest])
add('companion-rollback-visibility','@companion',"document.removeEventListener('visibilitychange', onVis)",'void onVis',[rtest])
dtest='src/contrib/k01-disk-lifecycle.test.ts'
add('disk-attempt-owner',loader,'diskLoads.get(entry) === attempt','true',[dtest])
add('disk-entry-owner',loader,'disk.get(entry.file) === entry','true',[dtest])
add('disk-before-evaluation',loader,'if (!isCurrent()) {return true}\n\n    const result','if (false) {return true}\n\n    const result',[dtest])
add('disk-cancellation-not-failure',loader,'if (result.cancelled)','if (false)',[dtest])
add('disk-stale-read-error',loader,'if (!isCurrent()) {return true}\n    // An oversize','if (false) {return true}\n    // An oversize',[dtest])
add('evaluation-stale-import',loader,'if (!isCurrent()) {return {id:null, cancelled:true}}\n    const plugin','if (false) {return {id:null, cancelled:true}}\n    const plugin',[dtest])
add('evaluation-stale-identity',loader,'!== claim || !isCurrent()','!== claim || false',[dtest])
add('evaluation-stale-error',loader,'if (!isCurrent()) {return {id:null, cancelled:true}}\n    console.error','if (false) {return {id:null, cancelled:true}}\n    console.error',[dtest])
(out/'inventory.json').write_text(json.dumps(mutants,indent=2))
selected=set(sys.argv[4].split(',')) if len(sys.argv)>4 else None
results=[]
def run(id,tests):
    report=out/(id+'.json');log=out/(id+'.log')
    command=['node',str(core/'apps/desktop/node_modules/vitest/vitest.mjs'),'run','--maxWorkers=1','--no-file-parallelism','--reporter=json','--outputFile='+str(report),*tests]
    env={**os.environ,'HERMES_K01_COMPANION':str(companion),'NODE_OPTIONS':'--max-old-space-size=2048'}
    with log.open('w') as stream:
        try: code=subprocess.run(command,cwd=app,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=90,check=False).returncode
        except subprocess.TimeoutExpired: code=124
    data=json.loads(report.read_text()) if report.exists() else {}
    assertions=[test for suite in data.get('testResults',[]) for test in suite.get('assertionResults',[])]
    failed=[t for t in assertions if t.get('status')=='failed']
    verdict='PASS' if code==0 and assertions and not failed else 'KILLED' if code not in [0,124] and failed else 'INVALID'
    row=dict(id=id,command=command,exit=code,verdict=verdict,tests=len(assertions),failed=[t['fullName'] for t in failed],skipped=data.get('numPendingTests'))
    results.append(row);(out/'results.json').write_text(json.dumps(results,indent=2));print(json.dumps(row),flush=True)
    return row
baseline=run('baseline',[itest,ctest,atest,rtest,dtest])
if baseline['verdict']!='PASS' or baseline['skipped']!=0: raise SystemExit('baseline failed: no mutation evidence accepted')
for mutant in mutants:
    if selected and mutant['id'] not in selected: continue
    file=companion if mutant['path']=='@companion' else app/mutant['path']
    original=file.read_text()
    if original.count(mutant['old'])!=1:
        results.append(dict(id=mutant['id'],verdict='INVALID_ANCHOR',count=original.count(mutant['old'])));continue
    file.write_text(original.replace(mutant['old'],mutant['new'],1))
    try:
        row=run(mutant['id'],mutant['tests'])
        if row['verdict']=='PASS': row['verdict']='SURVIVED'
        row['mutated_sha256']=hashlib.sha256(file.read_bytes()).hexdigest()
    finally: file.write_text(original)
(out/'results.json').write_text(json.dumps(results,indent=2))
summary={key:sum(row['verdict']==key for row in results[1:]) for key in ['KILLED','SURVIVED','INVALID','INVALID_ANCHOR']}
print(json.dumps(summary),flush=True)
raise SystemExit(0 if not any(summary[k] for k in ['SURVIVED','INVALID','INVALID_ANCHOR']) else 1)
