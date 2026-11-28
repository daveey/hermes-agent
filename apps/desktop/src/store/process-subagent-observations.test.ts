import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { projectExecutionObservation } from '@/lib/execution-observations'
import type { SessionInfo } from '@/types/hermes'

import {
  $backgroundRunningSessionIds,
  $backgroundStatusBySession,
  reconcileBackgroundProcesses
} from './composer-status'
import { rowExecution } from './execution-observation-row'
import { gatewayRouteState } from './gateway'
import { $kanbanObservationTick } from './kanban-observation'
import {
  $liveWorkRoster,
  failLiveWorkRosterSource,
  rosterLiveIds,
  rosterRowEvidence,
  setLiveWorkRosterSource
} from './live-work-roster'
import { $sessions } from './session'
import { $delegatingSessionIds, $executionRowKeys } from './session-dot-state'
import { $subagentsBySession, reconcileSubagentSnapshot, upsertSubagent } from './subagents'

// Real source adapters, not a second table of expected observations. No production reads or clocks.
const records = JSON.parse(
  execFileSync(
    'python3',
    [
      '-c',
      `
import json
from hermes_cli.turn_process_execution_observations import process, registry_subagent as subagent
class Child:
    session_id = "child-a"
    def get_activity_summary(self): return {"last_activity_ts": 1000}
p=process({"id":"proc-a", "pid":71, "host_start_time":81, "observed_at":1000, "source":"process.registry"}, gateway="edge", profile="work", session="owner", now=1001)
s=subagent({"subagent_id":"child-a", "delegation_id":"batch-a", "owner_agent_session_id":"owner", "started_at":900, "status":"running", "agent":Child()}, gateway="edge", profile="work", now=1001)
print(json.dumps([p,s]))
`
    ],
    { cwd: resolve(process.cwd(), '../..'), encoding: 'utf8' }
  )
) as Record<string, unknown>[]

const row = { id: 'owner', title: 'owner', profile: 'work' } as SessionInfo

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  $liveWorkRoster.set({})
  $sessions.set([])
  $backgroundStatusBySession.set({})
  $subagentsBySession.set({})
})

describe('process/subagent source to sidebar consumers', () => {
  it('merges the actual SQLite and registry child producers as one sidebar execution', () => {
    const sample = JSON.parse(
      execFileSync('python3', ['tests/fixtures/subagent_profile_parity.py'], {
        cwd: resolve(process.cwd(), '../..'),
        encoding: 'utf8',
        env: { ...process.env, PYTHONPATH: resolve(process.cwd(), '../..') }
      })
    )

    const stamped = sample.row as SessionInfo

    expect(stamped.execution_observations).toEqual([sample.registry])
    vi.useFakeTimers()
    vi.setSystemTime(sample.now * 1000)
    $sessions.set([stamped])
    setLiveWorkRosterSource(
      gatewayRouteState(null, 'work').key,
      [{ session_id: 'owner', subagents: 1, background: 0, execution_observations: [sample.registry] }],
      sample.now * 1000,
      ['owner']
    )
    expect(rowExecution(stamped, sample.now)).toMatchObject({ live: 1, unknown: 0 })
    expect(rowExecution(stamped, 1181)).toMatchObject({ live: 0, unknown: 1 })
  })

  it('counts one non-launch-profile process once across real row, RPC and roster producers', () => {
    const sample = JSON.parse(
      execFileSync('python3', ['tests/fixtures/process_profile_parity.py'], {
        cwd: resolve(process.cwd(), '../..'),
        encoding: 'utf8',
        env: { ...process.env, PYTHONPATH: resolve(process.cwd(), '../..') }
      })
    )

    vi.useFakeTimers()
    vi.setSystemTime(sample.now * 1000)
    const stamped = sample.row as SessionInfo
    $sessions.set([stamped])
    setLiveWorkRosterSource(gatewayRouteState(null, 'work').key, sample.roster, sample.now * 1000, ['owner'])
    expect(rowExecution(stamped, sample.now)).toMatchObject({ live: 1, unknown: 0 })
    reconcileBackgroundProcesses('owner', sample.processes)
    expect(rowExecution(stamped, sample.now)).toMatchObject({ live: 1, unknown: 0 })
    expect(stamped.background_active).toBe(1)
  })

  it('dedupes derived ages but preserves real same-time conflicts and profile isolation', () => {
    const current = records[0]
    const stamped: SessionInfo = {
      ...row,
      execution_observations: [
        { ...current, age_seconds: 0 },
        { ...current, age_seconds: 1 }
      ]
    }
    expect(rowExecution(stamped, 1001)).toMatchObject({ live: 1, unknown: 0 })
    stamped.execution_observations = [current, { ...current, state: 'queued' }]
    expect(rowExecution(stamped, 1001)).toMatchObject({ live: 0, unknown: 1 })
    stamped.execution_observations = [
      current,
      { ...current, identity: { ...(current.identity as object), profile: 'default' } }
    ]
    expect(rowExecution(stamped, 1001)).toMatchObject({ live: 2, unknown: 0 })
  })

  it('owner-qualified roster reaches row projection; failed reads retain unknown, including initial failure', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1001000)
    $sessions.set([row])
    const source = gatewayRouteState(null, 'work').key
    setLiveWorkRosterSource(
      source,
      [{ session_id: 'owner', background: 1, subagents: 1, execution_observations: [records[0]] }],
      1001000,
      ['owner']
    )
    expect(rowExecution(row, 1001)).toMatchObject({ live: 1, unknown: 1 })
    failLiveWorkRosterSource(source)
    expect(rowExecution(row, 1001)).toMatchObject({ live: 0, unknown: 2 })
    expect(rosterLiveIds($liveWorkRoster.get(), 'background', new Map(), [])).toEqual([])
    $liveWorkRoster.set({})
    failLiveWorkRosterSource(source, ['owner'])
    expect(rowExecution(row, 1001)).toMatchObject({ live: 0, unknown: 1 })
    setLiveWorkRosterSource(source, [
      { session_id: 'owner', background: 1, subagents: 0, execution_observations: [records[0]] }
    ])
    expect(rowExecution(row, 1001)).toMatchObject({ live: 1, unknown: 0 })
  })

  it('a failed roster read keeps the outstanding human request of its cached record', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1001000)
    $sessions.set([row])
    const source = gatewayRouteState(null, 'work').key
    const ask = {
      action: 'Approve',
      why_human: 'owner',
      interaction: 'approval',
      scope: 'fixture',
      evidence: 'fixture'
    }
    setLiveWorkRosterSource(
      source,
      [
        {
          session_id: 'owner',
          background: 1,
          subagents: 0,
          execution_observations: [{ ...records[0], human_request: ask }]
        }
      ],
      1001000,
      ['owner']
    )
    expect(rowExecution(row, 1001)).toMatchObject({ live: 1, unknown: 0, outstanding: 1 })
    failLiveWorkRosterSource(source, ['owner'])
    expect(rowExecution(row, 1001)).toMatchObject({ live: 0, unknown: 1, outstanding: 1 })
  })

  it('preserves roster arrays and exposes missing source beside fresh running on initial mixed version', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1001000)
    setLiveWorkRosterSource('ambient', [
      { session_id: 'owner', background: 1, subagents: 1, execution_observations: [records[0]] }
    ])
    expect($liveWorkRoster.get().ambient.sessions.owner.execution_observations?.[0]).toBe(records[0])
    const evidence = rosterRowEvidence($liveWorkRoster.get(), 'ambient', ['owner'])
    expect(evidence.missing).toBe(1)
    expect(projectExecutionObservation(evidence.records[0], 1001).state).toBe('running')
    expect(rosterLiveIds($liveWorkRoster.get(), 'background', new Map(), [])).toContain('owner')
    expect(rosterLiveIds($liveWorkRoster.get(), 'subagents', new Map(), [])).not.toContain('owner')
  })

  it('legacy counts never assert running and owner route collisions never import evidence', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1001000)
    setLiveWorkRosterSource('one', [{ session_id: 'owner', background: 5, subagents: 2 }])
    setLiveWorkRosterSource('two', [
      { session_id: 'owner', background: 1, subagents: 0, execution_observations: records }
    ])
    expect(rosterLiveIds({ one: $liveWorkRoster.get().one }, 'background', new Map(), [])).toEqual([])
    expect(rosterRowEvidence($liveWorkRoster.get(), 'one', ['owner'])).toEqual({ records: [], missing: 7 })
    expect(rosterRowEvidence($liveWorkRoster.get(), 'unread-owner', ['owner'])).toEqual({ records: [], missing: 0 })
  })

  it('RPC snapshot arrays reach row projection and expire on the existing tick without another fetch', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1001000)
    $sessions.set([row])
    reconcileBackgroundProcesses('owner', [
      { session_id: 'proc-a', status: 'running', execution_observations: [records[0]] }
    ])
    reconcileSubagentSnapshot('owner', [
      { subagent_id: 'child-a', status: 'running', started_at: 900, execution_observations: [records[1]] }
    ])
    expect($backgroundStatusBySession.get().owner[0].executionObservations?.[0]).toBe(records[0])
    expect($subagentsBySession.get().owner[0].executionObservations?.[0]).toBe(records[1])
    expect(rowExecution(row, 1001)).toMatchObject({ live: 2, unknown: 0 })
    const sameRow = { ...row, execution_observations: records }
    expect(rowExecution(sameRow, 1001)).toMatchObject({ live: 2, unknown: 0 })
    upsertSubagent('owner', { subagent_id: 'child-a', status: 'running', tool_name: 'terminal' })
    expect($subagentsBySession.get().owner[0].executionObservations?.[0]).toBe(records[1])
    expect($backgroundRunningSessionIds.get()).toContain('owner')
    expect($delegatingSessionIds.get()).toContain('owner')
    const off = $executionRowKeys.subscribe(() => {})
    vi.setSystemTime(1180000)
    $kanbanObservationTick.set($kanbanObservationTick.get() + 1)
    expect(rowExecution(row, 1180)).toMatchObject({ live: 0, unknown: 2 })
    expect($backgroundRunningSessionIds.get()).not.toContain('owner')
    expect($delegatingSessionIds.get()).not.toContain('owner')
    expect($executionRowKeys.get().owner).toBe('0/0/0/2/0')
    off()
  })

  it('terminal events do not resurrect a prior positive source snapshot', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1001000)
    reconcileSubagentSnapshot('owner', [
      { subagent_id: 'child-a', status: 'running', execution_observations: [records[1]] }
    ])
    expect($delegatingSessionIds.get()).toContain('owner')
    upsertSubagent('owner', { subagent_id: 'child-a', status: 'completed' }, true, 'subagent.complete')
    expect($delegatingSessionIds.get()).not.toContain('owner')
  })
})
