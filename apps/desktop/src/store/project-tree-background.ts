import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { hermesApi } from '@/hermes'
import { requestGatewayForAgent } from '@/store/gateway'

export interface ProjectTreePayload {
  projects: SidebarProjectTree[]
  active_id: null | string
  scoped_session_ids: string[]
}

// Background fanout reads all profiles on a gateway, with an older-gateway RPC
// fallback. This device is observational only: an absent local backend is not
// a reason to start one on a remote-only client.
export async function fetchRemoteProjectTree(
  connectionId: string,
  profile: string,
  previewLimit: number,
  timeoutMs: number
): Promise<ProjectTreePayload> {
  try {
    return await hermesApi<ProjectTreePayload>({
      connectionId,
      path: `/api/profiles/projects/tree?preview_limit=${previewLimit}`,
      ...(connectionId === 'local' ? { passive: true } : {}),
      timeoutMs
    })
  } catch (error) {
    if (connectionId === 'local') {
      throw error
    }

    return requestGatewayForAgent<ProjectTreePayload>(
      connectionId,
      profile,
      'projects.tree',
      { preview_limit: previewLimit },
      timeoutMs
    )
  }
}
