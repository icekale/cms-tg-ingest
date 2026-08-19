const ACTION_LABELS = {
  retry: '重试',
  emby: '查 Emby',
  restore: '恢复 STRM',
  reprocess: '从头重跑',
}

export function taskActionLabel(action) {
  return ACTION_LABELS[action] || action
}

export function displayTaskTitle(task = {}) {
  const displayTitle = typeof task.display_title === 'string' ? task.display_title.trim() : ''
  if (displayTitle) return displayTitle
  const title = typeof task.title === 'string' ? task.title.trim() : ''
  if (title) return title
  const id = task.id ?? task.task_id
  return id === undefined || id === null ? '-' : `任务 #${id}`
}

export function taskStatusLabel(status) {
  return status === 'cancelled' ? '已终止' : status
}

export function taskLifecycleState(task = {}) {
  const actions = new Set(Array.isArray(task.available_actions) ? task.available_actions : [])
  return {
    canTerminate: actions.has('terminate'),
    canDelete: actions.has('delete'),
    terminationRequested: task.termination_requested === true,
  }
}
