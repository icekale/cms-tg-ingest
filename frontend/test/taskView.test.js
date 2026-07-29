import assert from 'node:assert/strict'
import test from 'node:test'
import { taskLifecycleState, taskStatusLabel } from '../src/taskView.js'

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
