import assert from 'node:assert/strict'
import test from 'node:test'

import { api } from '../src/api.js'

test('lifecycle requests surface backend Chinese reasons for conflicts and missing tasks', async () => {
  const originalFetch = globalThis.fetch
  const responses = [
    { status: 409, payload: { error: 'action_not_allowed', reason: '任务已经结束，无需终止' } },
    { status: 404, payload: { error: 'task_not_found', message: '任务不存在或已过期' } },
  ]
  globalThis.fetch = async () => {
    const response = responses.shift()
    return { ok: false, status: response.status, json: async () => response.payload }
  }

  try {
    await assert.rejects(api.taskAction(7, 'terminate'), { message: '任务已经结束，无需终止' })
    await assert.rejects(api.deleteTask(8), { message: '任务不存在或已过期' })
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('quality runs request targets the history endpoint', async () => {
  const originalFetch = globalThis.fetch
  let requestedPath = ''
  globalThis.fetch = async (url) => {
    requestedPath = url
    return { ok: true, status: 200, json: async () => ({ items: [], trend: [] }) }
  }

  try {
    await api.qualityRuns()
    assert.equal(requestedPath, '/api/v1/quality/runs')
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('task purge request sends ids and dry_run flag', async () => {
  const originalFetch = globalThis.fetch
  let requestedPath = ''
  let requestedBody = ''
  globalThis.fetch = async (url, options) => {
    requestedPath = url
    requestedBody = options.body
    return { ok: true, status: 200, json: async () => ({ deleted: [], rejected: [] }) }
  }

  try {
    await api.purgeTasks([7, 8], true)
    assert.equal(requestedPath, '/api/v1/tasks/purge')
    assert.deepEqual(JSON.parse(requestedBody), { ids: [7, 8], dry_run: true })
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('cms version settings save posts overrides', async () => {
  const originalFetch = globalThis.fetch
  let requestedPath = ''
  let requestedBody = ''
  globalThis.fetch = async (url, options) => {
    requestedPath = url
    requestedBody = options.body
    return { ok: true, status: 200, json: async () => ({ enabled: true, interval_seconds: 86400 }) }
  }

  try {
    await api.saveCmsVersion({ enabled: true, interval_seconds: 86400, image: 'cms:latest', auto_pull: true })
    assert.equal(requestedPath, '/api/v1/settings/cms-version')
    assert.deepEqual(JSON.parse(requestedBody), { enabled: true, interval_seconds: 86400, image: 'cms:latest', auto_pull: true })
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('unauthorized redirect to the login page rejects instead of parsing HTML as JSON', async () => {
  const originalFetch = globalThis.fetch
  // 未登录时后端 303 到 /login，fetch 跟随后 status 是 200、json() 返回 {}，
  // 之前会导致页面显示空数据而不是跳登录页。
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    redirected: true,
    url: 'http://localhost:5173/login',
    json: async () => ({}),
  })

  try {
    await assert.rejects(api.overview(), { message: '未登录，请先登录' })
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('non-login redirects are not treated as unauthenticated', async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    redirected: true,
    url: 'http://localhost:5173/api/v1/overview/',
    json: async () => ({ tasks: { items: [] } }),
  })

  try {
    const payload = await api.overview()
    assert.deepEqual(payload, { tasks: { items: [] } })
  } finally {
    globalThis.fetch = originalFetch
  }
})
