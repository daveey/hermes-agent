/** [daveey] Owner-routed, exact board/card-set navigation for disk plugins. */
import { atom, Loader, type PluginRestOptions, useQuery, useValue } from '@hermes/plugin-sdk'
import { useState } from 'react'

import { BoardTaskDrawer } from './board-task-drawer'
import { columnLabel, useKanban } from './i18n'
import type { OpenTaskRequest } from './open-task'
import type { KanbanBoard } from './types'

export interface CardSetRequest {
  readonly connectionId: string
  readonly cards: readonly Readonly<{ board: string; id: string }>[]
}
export const SHOW_CARDS_HOOK = '__hermesKanbanShowCards'
export const $cardSet = atom<CardSetRequest | null>(null)
type Rest = <T>(path: string, options?: PluginRestOptions) => Promise<T>

export function installCardSetHook(
  deps: { navigate: (path: string) => void },
  target: Record<string, unknown> = globalThis as unknown as Record<string, unknown>
): () => void {
  const hook = (value: unknown): boolean => {
    if (!value || typeof value !== 'object') { return false }
    const request = value as Partial<CardSetRequest>

    if (typeof request.connectionId !== 'string' || !request.connectionId.trim() || !Array.isArray(request.cards) ||
        !request.cards.every(card => card && typeof card.board === 'string' && card.board.trim() && typeof card.id === 'string' && card.id.trim())) { return false }

    // Snapshot all identities: neither later input mutations nor gateway switches retarget the filter.
    const cards = request.cards.map(({ board, id }) => Object.freeze({ board, id }))
    $cardSet.set(Object.freeze({ connectionId: request.connectionId, cards: Object.freeze(cards) }))
    deps.navigate('/kanban')

    return true
  }

  target[SHOW_CARDS_HOOK] = hook

  return () => { if (target[SHOW_CARDS_HOOK] === hook) { delete target[SHOW_CARDS_HOOK] } }
}

export function CardSetBoard({ rest }: { rest: Rest }) {
  const k = useKanban()
  const request = useValue($cardSet)
  const [task, setTask] = useState<OpenTaskRequest | null>(null)

  const { data, error } = useQuery({
    queryKey: ['kanban-card-set', request],
    enabled: request !== null,
    refetchInterval: 15000,
    queryFn: async () => {
      if (!request) { return [] }

      return Promise.all([...new Set(request.cards.map(card => card.board))].map(async board => {
        const snapshot = await rest<KanbanBoard>(`/board?board=${encodeURIComponent(board)}`, { connectionId: request.connectionId, profile: 'default' })
        const ids = new Set(request.cards.filter(card => card.board === board).map(card => card.id))

        return { board, columns: snapshot.columns.map(column => ({ ...column, tasks: column.tasks.filter(card => ids.has(card.id)) })) }
      }))
    }
  })

  if (!request) { return null }

  return <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-auto p-4" data-slot="kanban-card-set">
    <button onClick={() => $cardSet.set(null)} type="button">{k.clearCardFilter}</button>
    {error ? <div role="alert">{String(error)}</div> : !data ? <Loader /> : data.map(board =>
      <section aria-label={board.board} key={board.board}>
        <h2>{board.board}</h2>
        {board.columns.map(column => column.tasks.length ? <div key={column.name}>
          <h3>{columnLabel(k, column.name)}</h3>
          {column.tasks.map(card => <button className="block text-left" key={card.id} onClick={() => setTask({ board: board.board, taskId: card.id, owner: { board: board.board, gateway: request.connectionId, profile: 'default' } })} type="button">{card.title}</button>)}
        </div> : null)}
      </section>)}
    {task && <BoardTaskDrawer board={task.board} columns={data?.flatMap(board => board.columns.map(column => column.name)) ?? []} onChange={setTask} task={task} />}
  </div>
}
