import { useStore } from '@nanostores/react'
import { useEffect, useMemo } from 'react'
import type * as React from 'react'

import type { SessionInfo } from '@/hermes'
import { exitProjectScope } from '@/store/project-scope'
import { $activeProjectId, refreshWorktrees } from '@/store/projects'
import { $workingSessionIds } from '@/store/session-states'

import {
  EnteredProjectContent,
  enteredProjectLiveRows,
  excludeProjectSessions,
  overlayLiveLanes,
  type SidebarProjectTree,
  type SidebarWorkspaceTree,
  useRepoWorktreeMap
} from './projects'
import { ProjectGroupMenu } from './projects/presentation-group-menu'
import { MachineHeading } from './projects/presentation-group-row'
import { groupProjects, presentationGroupOf } from './projects/presentation-groups'
import { ProjectContextMenu, ProjectMenu } from './projects/project-menu'
import { StartWorkButton } from './projects/workspace-header'
import { SidebarLoadErrorState, SidebarSessionSkeletons } from './section-states'
import { useEnteredProjectSessions } from './use-entered-project-sessions'

export function enteredPresentation(nodes: SidebarProjectTree[] = [], scope: string, windowOwner?: string | null) {
  const group = presentationGroupOf(groupProjects(nodes, windowOwner ?? undefined), scope)
  const overview = group?.primary ?? nodes.find(node => node.id === scope)
  const members = group && group.members.length > 1 ? group.members : undefined

  return { overview, members, grouped: Boolean(members) }
}

export function enteredContentExists(
  content: SidebarProjectTree | undefined,
  members: SidebarProjectTree[] | undefined
) {
  return Boolean(members?.length || (content?.sessionCount ?? 0) > 0)
}

export interface EnteredMemberOptions {
  gatewayReady: boolean
  projectTree: SidebarProjectTree[]
  scope: string
  grouped: boolean
  orderRepos: (repos: SidebarWorkspaceTree[]) => SidebarWorkspaceTree[]
  isHiddenFromProjects: (session: SessionInfo) => boolean
  agentSessions: SessionInfo[]
  owners: Parameters<typeof enteredProjectLiveRows>[2]
  removedSessionIds: ReadonlySet<string>
  projectOwners: Parameters<typeof overlayLiveLanes>[3]
  projects: NonNullable<Parameters<typeof overlayLiveLanes>[4]>['explicitProjects']
  knownProjects: NonNullable<Parameters<typeof overlayLiveLanes>[4]>['known']
  ownerKey: NonNullable<Parameters<typeof overlayLiveLanes>[4]>['ownerKey']
  showAllProfiles: boolean
}

export function useEnteredMember(
  overviewEnteredProject: SidebarProjectTree | undefined,
  options: EnteredMemberOptions
) {
  const {
    gatewayReady,
    projectTree,
    scope,
    grouped,
    orderRepos,
    isHiddenFromProjects,
    agentSessions,
    owners,
    removedSessionIds,
    projectOwners,
    projects,
    knownProjects,
    ownerKey,
    showAllProfiles
  } = options

  const enteredProjectId = overviewEnteredProject?.id

  // Entering a project lazily hydrates its full lanes (repo -> lane -> sessions)
  // from the backend — same grouping/ids as the overview, just with rows.
  const {
    project: enteredProjectTree,
    failed: projectLoadFailed,
    loading: projectLoading,
    retry: retryProject
  } = useEnteredProjectSessions(
    enteredProjectId,
    gatewayReady,
    projectTree,
    scope,
    grouped ? { supersedable: false } : undefined
  )

  // Prefer the hydrated tree; fall back to the overview node (empty lanes) while
  // the drill-in fetch is in flight, so the header/structure render immediately.
  const enteredProject = useMemo<SidebarProjectTree | undefined>(() => {
    if (!overviewEnteredProject) {
      return undefined
    }

    const hydrated =
      enteredProjectTree && enteredProjectTree.id === overviewEnteredProject.id
        ? enteredProjectTree
        : overviewEnteredProject

    // The live-session overlay (creates/evictions) is applied per-repo in
    // RepoFlatSection, AFTER the visual git-worktree lanes are merged in (so
    // out-of-tree worktrees can be placed). Here we just order the snapshot and
    // drop pinned rows — the hydrated lanes come straight from the backend, so
    // they haven't been through projectModel's filter.
    // The label comes from the overview node either way — that's the model's
    // presentation copy (Home is translated there), not the raw payload's.
    return excludeProjectSessions(
      {
        ...hydrated,
        label: overviewEnteredProject.label,
        repos: orderRepos(hydrated.repos)
      },
      isHiddenFromProjects
    )
  }, [overviewEnteredProject, enteredProjectTree, orderRepos, isHiddenFromProjects])

  const enteredProjectOverlaySessions = useMemo(
    () => enteredProjectLiveRows(overviewEnteredProject, agentSessions, owners),
    [agentSessions, overviewEnteredProject, owners]
  )

  // Overlay live `$sessions` onto the entered project so a just-created session
  // (which the backend snapshot hasn't folded in yet) counts as content and
  // renders immediately. Also carry over the overview's current preview rows:
  // its project tree and the separately hydrated drill-in can resolve at
  // different times, but a row visible in the overview must not disappear on
  // entry. The backend seeds each project folder as an (empty) repo, so the
  // overlay always has a lane to place a missing in-project session into.
  const enteredProjectContent = useMemo(
    () =>
      enteredProject
        ? overlayLiveLanes(enteredProject, enteredProjectOverlaySessions, removedSessionIds, projectOwners, {
            explicitProjects: projects,
            known: knownProjects,
            ownerKey
          })
        : undefined,
    [enteredProject, enteredProjectOverlaySessions, removedSessionIds, projectOwners, projects, knownProjects, ownerKey]
  )

  const scopedRepoPaths = useMemo(
    () =>
      enteredProject ? enteredProject.repos.map(repo => repo.path).filter((path): path is string => Boolean(path)) : [],
    [enteredProject]
  )

  // git worktree list is a VISUAL-only enhancer (empty lanes); never membership.
  const inEnteredProject = Boolean(enteredProject && !showAllProfiles)
  const [scopedRepoWorktrees] = useRepoWorktreeMap(scopedRepoPaths, inEnteredProject, enteredProject)

  // Re-probe worktree lanes on out-of-band git changes the renderer can't see.
  // A turn can `git worktree add/remove` in the terminal (e.g. you ask Hermes to
  // "remove that worktree"), and the window never blurs during an in-app chat,
  // so nothing would otherwise re-run the visual probe. Re-sync when a working
  // session settles (its turn finished) or the window refocuses (an external
  // terminal may have changed things) — only while a project is entered, and
  // only the cheap per-repo `git worktree list`, never the heavy tree scan.
  //
  // Listened to rather than rendered from: a settling turn is a side effect,
  // and reading it with `useStore` repainted this whole component — every
  // section, every row — on each status edge, to run an effect that touches no
  // markup. The rows subscribe to their own status, so nothing above them needs
  // to re-render for one of them to change color.
  useEffect(() => {
    if (!inEnteredProject) {
      return
    }

    let previous = $workingSessionIds.get()

    return $workingSessionIds.listen(working => {
      // A session leaving the working set means its turn just completed.
      const aTurnSettled = previous.some(id => !working.includes(id))

      previous = working

      if (aTurnSettled) {
        refreshWorktrees()
      }
    })
  }, [inEnteredProject])

  useEffect(() => {
    if (!inEnteredProject) {
      return
    }

    const onFocus = () => refreshWorktrees()
    window.addEventListener('focus', onFocus)

    return () => window.removeEventListener('focus', onFocus)
  }, [inEnteredProject])

  return {
    project: enteredProject,
    content: enteredProjectContent,
    liveSessions: enteredProjectOverlaySessions,
    repoWorktrees: scopedRepoWorktrees,
    failed: projectLoadFailed,
    loading: projectLoading,
    retry: retryProject
  }
}

type MemberState = ReturnType<typeof useEnteredMember>

export function enteredPrimaryChrome(state: MemberState, grouped: boolean) {
  return { ...state, failed: state.failed && !grouped }
}

export function EnteredPresentationMenu({
  members,
  ...props
}: React.ComponentProps<typeof ProjectMenu> & { members?: SidebarProjectTree[] }) {
  if (!members) {
    return <ProjectMenu {...props} />
  }

  return (
    <ProjectGroupMenu
      group={{ key: members[0].label, primary: members[0], members, memberIds: members.map(node => node.id) }}
      onExitScope={props.onExitScope}
    />
  )
}

type BodyProps = {
  members: SidebarProjectTree[]
  primary: MemberState
  options: EnteredMemberOptions
  renderRows: (rows: SessionInfo[]) => React.ReactNode
  onNewSession?: React.ComponentProps<typeof EnteredProjectContent>['onNewSession']
  onNewSessionSplit?: React.ComponentProps<typeof EnteredProjectContent>['onNewSessionSplit']
}

function MemberHeading({ member }: { member: SidebarProjectTree }) {
  const activeId = useStore($activeProjectId)
  const isActive = activeId === member.id

  const heading = (
    <div className="flex items-center justify-between py-1 text-xs text-(--ui-text-tertiary)">
      <MachineHeading project={member} />
      <div className="flex items-center gap-1">
        <ProjectMenu isActive={isActive} onExitScope={exitProjectScope} project={member} scoped />
        {member.path && <StartWorkButton project={member} />}
      </div>
    </div>
  )

  return (
    <ProjectContextMenu isActive={isActive} onExitScope={exitProjectScope} project={member} scoped>
      {heading}
    </ProjectContextMenu>
  )
}

function MemberBody({
  member,
  state,
  ...props
}: Omit<BodyProps, 'members' | 'primary'> & {
  member: SidebarProjectTree
  state: MemberState
}) {
  return (
    <section data-entered-project-member={member.id}>
      <MemberHeading member={member} />
      {state.failed && <SidebarLoadErrorState onRetry={state.retry} />}
      {state.loading && !state.content?.repos.length && !state.content?.sessionCount && <SidebarSessionSkeletons />}
      {state.content && (
        <EnteredProjectContent
          liveSessions={state.liveSessions}
          onNewSession={props.onNewSession}
          onNewSessionSplit={props.onNewSessionSplit}
          project={state.content}
          removedSessionIds={props.options.removedSessionIds}
          renderRows={props.renderRows}
          repoWorktrees={state.repoWorktrees}
        />
      )}
    </section>
  )
}

function ExtraMember({ member, ...props }: Omit<BodyProps, 'members' | 'primary'> & { member: SidebarProjectTree }) {
  const state = useEnteredMember(member, props.options)

  return <MemberBody member={member} state={state} {...props} />
}

export function EnteredProjectMembers({ members, primary, ...props }: BodyProps) {
  return (
    <>
      {members.map((member, i) =>
        i === 0 ? (
          <MemberBody key={member.id} member={member} state={primary} {...props} />
        ) : (
          <ExtraMember key={member.id} member={member} {...props} />
        )
      )}
    </>
  )
}
