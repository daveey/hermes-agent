import { TaskDrawer } from './drawer'
import type { OpenTaskRequest } from './open-task'

interface BoardTaskDrawerProps {
  columns: string[]
  task: OpenTaskRequest | null
  board: string
  onChange: (task: OpenTaskRequest | null) => void
}

/** Keep the complete owner route in drawer navigation and remount identity. */
export function BoardTaskDrawer({ columns, task, board, onChange }: BoardTaskDrawerProps) {
  return (
    <TaskDrawer
      columns={columns}
      id={task?.taskId ?? null}
      key={JSON.stringify(task)}
      onClose={() => onChange(null)}
      onOpen={(id, owner) => onChange({ taskId: id, board: owner?.board ?? board, owner })}
      owner={task?.owner}
    />
  )
}
