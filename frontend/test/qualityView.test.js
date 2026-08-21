import assert from 'node:assert/strict'
import test from 'node:test'

import {
  QUALITY_CLEANUP_CONFIRM,
  QUALITY_FIX_CONFIRM,
  confirmQualityBatchFix,
  mergeQualityRows,
  qualityActionLabel,
  qualityActionPrompt,
  qualityRiskType,
  qualityStatusLabel,
} from '../src/qualityView.js'

test('mergeQualityRows keeps one actionable row per task and rule', () => {
  const rows = mergeQualityRows([
    {
      task_id: 12,
      title: '任务',
      rule_id: 'strm_mode_mismatch',
      rule_version: '1',
      code: 'direct_strm',
      message: '直链',
      evidence: ['a.strm'],
      issue_codes: ['direct_strm'],
      available_actions: ['execute', 'snooze'],
    },
    {
      task_id: 12,
      title: '任务',
      rule_id: 'strm_mode_mismatch',
      rule_version: '1',
      code: 'unexpected_strm',
      message: '异常',
      evidence: ['b.strm'],
      issue_codes: ['unexpected_strm'],
      available_actions: ['execute', 'snooze'],
    },
  ])

  assert.equal(rows.length, 1)
  assert.deepEqual(rows[0].issue_codes, ['direct_strm', 'unexpected_strm'])
  assert.deepEqual(rows[0].evidence, ['a.strm', 'b.strm'])
  assert.equal(rows[0].issue_count, 2)
})

test('batch fix asks for confirmation before changing tasks', () => {
  assert.match(QUALITY_FIX_CONFIRM, /批量修复/)
  assert.equal(confirmQualityBatchFix(() => true), true)
  assert.equal(confirmQualityBatchFix(() => false), false)
})

test('quality action prompts name the consequence before changing a task', () => {
  assert.match(qualityActionPrompt('reprocess'), /从头重跑/)
  assert.match(qualityActionPrompt('ignore'), /不会再处理/)
  assert.match(QUALITY_CLEANUP_CONFIRM, /删除所选失效 STRM/)
})

test('quality labels map backend state to readable UI values', () => {
  assert.equal(qualityActionLabel('reprocess'), '人工重跑')
  assert.equal(qualityRiskType('critical'), 'error')
  assert.equal(qualityRiskType('unknown'), 'default')
  assert.equal(qualityStatusLabel('manual_required'), '需要人工')
  assert.equal(qualityStatusLabel('snoozed'), '已暂缓')
})
