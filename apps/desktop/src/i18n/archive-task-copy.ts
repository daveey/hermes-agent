/** Copy shared by the archive dialog's authoritative Kanban projection. */
export interface ArchiveTaskCopy {
  archiveLinkedTasksLabel: string
  archiveLinkedTasksValue: (open: number, blocked: number) => string
  archiveLinkedTasksUnknown: string
}

export const enArchiveTasks: ArchiveTaskCopy = {
  archiveLinkedTasksLabel: 'Kanban tasks',
  archiveLinkedTasksValue: (open, blocked) =>
    [open ? `${open} open` : '', blocked ? `${blocked} blocked` : ''].filter(Boolean).join(' · '),
  archiveLinkedTasksUnknown: 'Task status unavailable; work may still be unfinished.'
}

export const deArchiveTasks: ArchiveTaskCopy = {
  archiveLinkedTasksLabel: 'Kanban-Aufgaben',
  archiveLinkedTasksValue: (open, blocked) =>
    [open ? `${open} offen` : '', blocked ? `${blocked} blockiert` : ''].filter(Boolean).join(' · '),
  archiveLinkedTasksUnknown: 'Aufgabenstatus nicht verfügbar; die Arbeit ist möglicherweise noch nicht abgeschlossen.'
}

export const esArchiveTasks: ArchiveTaskCopy = {
  archiveLinkedTasksLabel: 'Tareas de Kanban',
  archiveLinkedTasksValue: (open, blocked) =>
    [open ? `${open} abiertas` : '', blocked ? `${blocked} bloqueadas` : ''].filter(Boolean).join(' · '),
  archiveLinkedTasksUnknown: 'Estado de las tareas no disponible; puede que el trabajo siga sin terminar.'
}

export const frArchiveTasks: ArchiveTaskCopy = {
  archiveLinkedTasksLabel: 'Tâches Kanban',
  archiveLinkedTasksValue: (open, blocked) =>
    [open ? `${open} ouvertes` : '', blocked ? `${blocked} bloquées` : ''].filter(Boolean).join(' · '),
  archiveLinkedTasksUnknown: 'État des tâches indisponible ; le travail peut être encore inachevé.'
}

export const jaArchiveTasks: ArchiveTaskCopy = {
  archiveLinkedTasksLabel: 'Kanban タスク',
  archiveLinkedTasksValue: (open, blocked) =>
    [open ? `未完了 ${open}` : '', blocked ? `ブロック中 ${blocked}` : ''].filter(Boolean).join(' · '),
  archiveLinkedTasksUnknown: 'タスクの状態を取得できません。作業が未完了の可能性があります。'
}

export const ruArchiveTasks: ArchiveTaskCopy = {
  archiveLinkedTasksLabel: 'Задачи Kanban',
  archiveLinkedTasksValue: (open, blocked) =>
    [open ? `открыто: ${open}` : '', blocked ? `заблокировано: ${blocked}` : ''].filter(Boolean).join(' · '),
  archiveLinkedTasksUnknown: 'Статус задач недоступен; работа может быть ещё не завершена.'
}

export const zhArchiveTasks: ArchiveTaskCopy = {
  archiveLinkedTasksLabel: '看板任务',
  archiveLinkedTasksValue: (open, blocked) =>
    [open ? `${open} 个未完成` : '', blocked ? `${blocked} 个受阻` : ''].filter(Boolean).join(' · '),
  archiveLinkedTasksUnknown: '无法获取任务状态；工作可能尚未完成。'
}
