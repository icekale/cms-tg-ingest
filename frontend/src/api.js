async function request(path, options = {}) {
  const response = await fetch(`/api/v1/${path}`, {
    headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}) },
    ...options,
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(payload.error || payload.message || `请求失败 (${response.status})`)
  return payload
}

export const api = {
  overview: () => request('overview'),
  tasks: () => request('tasks'),
  task: (id) => request(`tasks/${id}`),
  health: () => request('health'),
  quality: () => request('quality'),
  hdhive: () => request('hdhive'),
  setDefaultMode: (mode) => request('settings/strm-mode', { method: 'POST', body: JSON.stringify({ mode }) }),
  setTaskMode: (id, mode) => request(`tasks/${id}/strm-mode`, { method: 'POST', body: JSON.stringify({ mode }) }),
}
