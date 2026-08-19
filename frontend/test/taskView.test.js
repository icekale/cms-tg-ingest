import assert from 'node:assert/strict'
import test from 'node:test'
import { displayTaskTitle, taskActionLabel, taskLifecycleState, taskStatusLabel } from '../src/taskView.js'

test('prefers backend display title and falls back to legacy title', () => {
  assert.equal(
    displayTaskTitle({ id: 328, display_title: 'H-后天-2024-[tmdb=435]', title: 'swso9jn3wul' }),
    'H-后天-2024-[tmdb=435]',
  )
  assert.equal(displayTaskTitle({ id: 329, title: '旧版任务标题' }), '旧版任务标题')
  assert.equal(displayTaskTitle({ id: 330 }), '任务 #330')
})

test('task action labels stay Chinese and omit internal keys', () => {
  assert.equal(taskActionLabel('retry'), '重试')
  assert.equal(taskActionLabel('emby'), '查 Emby')
  assert.equal(taskActionLabel('restore'), '恢复 STRM')
  assert.equal(taskActionLabel('reprocess'), '从头重跑')
  assert.equal(taskActionLabel('retry').includes('retry'), false)
})

test('maps cancelled status without changing existing raw labels', () => {
  assert.equal(taskStatusLabel('cancelled'), '已终止')
  assert.equal(taskStatusLabel('running'), 'running')
})

test('uses backend lifecycle actions and termination flag', () => {
  assert.deepEqual(
    taskLifecycleState({ available_actions: ['terminate'], termination_requested: false }),
    { canTerminate: true, canDelete: false, terminationRequested: false },
  )
  assert.deepEqual(
    taskLifecycleState({ available_actions: [], termination_requested: true }),
    { canTerminate: false, canDelete: false, terminationRequested: true },
  )
  assert.deepEqual(
    taskLifecycleState({ available_actions: ['delete'], termination_requested: false }),
    { canTerminate: false, canDelete: true, terminationRequested: false },
  )
})
