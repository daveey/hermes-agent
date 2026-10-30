// Repo-discovery policy parsing (desktop.repo_scan_* config keys), moved out of
// ./projects.ts unchanged and re-exported there. [daveey]
export interface RepoDiscoveryPolicy {
  enabled: boolean
  roots: string[]
  exclude_paths: string[]
}

export function repoDiscoveryPolicyFromConfig(config: unknown): RepoDiscoveryPolicy {
  const desktopValue = config && typeof config === 'object' ? (config as { desktop?: unknown }).desktop : undefined

  const desktop =
    desktopValue && typeof desktopValue === 'object'
      ? (desktopValue as {
          repo_scan_enabled?: unknown
          repo_scan_exclude_paths?: unknown
          repo_scan_roots?: unknown
        })
      : {}

  return {
    enabled: desktop.repo_scan_enabled !== false,
    roots: Array.isArray(desktop.repo_scan_roots)
      ? desktop.repo_scan_roots.filter((value): value is string => typeof value === 'string')
      : [],
    exclude_paths: Array.isArray(desktop.repo_scan_exclude_paths)
      ? desktop.repo_scan_exclude_paths.filter((value): value is string => typeof value === 'string')
      : []
  }
}

export function repoDiscoveryPolicySignature(policy: RepoDiscoveryPolicy): string {
  return JSON.stringify(policy)
}
