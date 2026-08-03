async function request(path, options = {}) {
  const response = await fetch(`/api/v1/${path}`, {
    headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}) },
    ...options,
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(payload.reason || payload.message || payload.error || `请求失败 (${response.status})`)
  return payload
}

export const api = {
  overview: () => request('overview'),
  settings: () => request('settings'),
  tasks: () => request('tasks'),
  task: (id) => request(`tasks/${id}`),
  health: () => request('health'),
  quality: () => request('quality'),
  qualityRuns: () => request('quality/runs'),
  qualityAction: (action, payload) => request(`quality/action/${action}`, { method: 'POST', body: JSON.stringify(payload) }),
  hdhive: () => request('hdhive'),
  setDefaultMode: (mode) => request('settings/strm-mode', { method: 'POST', body: JSON.stringify({ mode }) }),
  setOwnShareReceiveCode: (receive_code) => request('settings/own-share-receive-code', { method: 'POST', body: JSON.stringify({ receive_code }) }),
  clearOwnShareReceiveCode: () => request('settings/own-share-receive-code', { method: 'POST', body: JSON.stringify({ clear: true }) }),
  setSelfShareReceiveCid: (receive_cid) => request('settings/self-share-receive-cid', { method: 'POST', body: JSON.stringify({ receive_cid }) }),
  clearSelfShareReceiveCid: () => request('settings/self-share-receive-cid', { method: 'POST', body: JSON.stringify({ clear: true }) }),
  setSelfShareReview: (mode) => request('settings/self-share-review', { method: 'POST', body: JSON.stringify({ mode }) }),
  setTaskMode: (id, mode) => request(`tasks/${id}/strm-mode`, { method: 'POST', body: JSON.stringify({ mode }) }),
  taskAction: (id, action) => request(`tasks/${id}/actions/${action}`, { method: 'POST' }),
  deleteTask: (id) => request(`tasks/${id}`, { method: 'DELETE' }),
  purgeTasks: (ids, dryRun = false) => request('tasks/purge', { method: 'POST', body: JSON.stringify({ ids, dry_run: dryRun }) }),
  clearHistory: () => request('history/clear', { method: 'POST' }),
  qualityFix: () => request('quality/fix', { method: 'POST' }),
  qualityRun: () => request('quality/run', { method: 'POST' }),
  qualitySettings: (settings) => request('quality/settings', { method: 'POST', body: JSON.stringify(settings) }),
  qualityReset: () => request('quality/settings/reset', { method: 'POST' }),
  hdhiveSubscriptionAction: (id, action) => request(`hdhive/subscriptions/${id}/${action}`, { method: 'POST' }),
  hdhiveSubscriptionFilter: (id, episode_filter) => request(`hdhive/subscriptions/${id}/episode-filter`, { method: 'POST', body: JSON.stringify({ episode_filter }) }),
  hdhiveItemConfirm: (id) => request(`hdhive/items/${id}/confirm`, { method: 'POST' }),
  hdhiveSettings: (settings) => request('hdhive/settings', { method: 'POST', body: JSON.stringify(settings) }),
  hdhiveRun: () => request('hdhive/run', { method: 'POST' }),
  cmsVersion: () => request('settings/cms-version'),
  saveCmsVersion: (settings) => request('settings/cms-version', { method: 'POST', body: JSON.stringify(settings) }),
  resetCmsVersion: () => request('settings/cms-version/reset', { method: 'POST' }),
  cmsVersionCheck: () => request('cms/version/check', { method: 'POST' }),
}
