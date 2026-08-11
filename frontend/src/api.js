async function request(path, options = {}) {
  const response = await fetch(`/api/v1/${path}`, {
    headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}) },
    ...options,
  })
  // 未登录时后端把请求 303 重定向到 SSR 登录页；fetch 跟随重定向后 status
  // 仍是 200，但最终 URL 已指向 /login。此时整页跳到登录页，而不是把登录页
  // HTML 当成 JSON 解析后显示空数据。
  if (response.redirected && response.url.endsWith('/login')) {
    if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
      window.location.assign('/login')
    }
    throw new Error('未登录，请先登录')
  }
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
  qualityCleanupDryRun: (taskId, checkShares = true) => request('quality/cleanup/dry-run', { method: 'POST', body: JSON.stringify({ task_id: taskId, check_shares: checkShares }) }),
  qualityCleanupRun: (taskId, paths, allowAlive = false) => request('quality/cleanup/run', { method: 'POST', body: JSON.stringify({ task_id: taskId, paths, allow_alive: allowAlive }) }),
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
  embyDashboard: (refresh = false) => request('emby/dashboard', {
    headers: refresh ? { 'X-Emby-Dashboard-Refresh': '1' } : {},
  }),
}
