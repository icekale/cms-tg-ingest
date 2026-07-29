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
