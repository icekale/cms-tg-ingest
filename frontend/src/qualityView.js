const ACTION_LABELS = {
  execute: '执行重跑',
  reprocess: '人工重跑',
  snooze: '暂缓 24 小时',
  ignore: '忽略',
  resume: '恢复评估',
}

const RISK_TYPES = {
  critical: 'error',
  high: 'error',
  medium: 'warning',
  low: 'success',
  none: 'default',
}

const STATUS_LABELS = {
  open: '待处理',
  manual_required: '需要人工',
  snoozed: '已暂缓',
  ignored: '已忽略',
  archived: '已归档',
}

function uniqueValues(values) {
  return [...new Set((Array.isArray(values) ? values : []).map((value) => String(value || '').trim()).filter(Boolean))]
}

export function mergeQualityRows(items) {
  const merged = new Map()
  for (const item of Array.isArray(items) ? items : []) {
    const key = `${item.task_id}:${item.rule_id || 'manual_required'}`
    const current = merged.get(key)
    if (!current) {
      merged.set(key, {
        ...item,
        issue_codes: uniqueValues([...(item.issue_codes || []), item.code]),
        evidence: uniqueValues(item.evidence),
        available_actions: uniqueValues(item.available_actions),
        issue_count: 1,
      })
      continue
    }
    current.issue_codes = uniqueValues([...current.issue_codes, ...(item.issue_codes || []), item.code])
    current.evidence = uniqueValues([...current.evidence, ...(item.evidence || [])])
    current.available_actions = uniqueValues([...current.available_actions, ...(item.available_actions || [])])
    current.issue_count += 1
  }
  return [...merged.values()]
}

export const QUALITY_FIX_CONFIRM = '将按规则批量修复当前可自动处理的问题，入库任务会改状态。确定继续？'

export const QUALITY_ACTION_PROMPTS = {
  execute: '将任务重新入队执行，确定继续？',
  reprocess: '将任务从头重跑，确定继续？',
  snooze: '将该问题暂缓 24 小时，确定继续？',
  ignore: '忽略该质量问题后，自动巡检不会再处理它，确定继续？',
  resume: '恢复该问题的规则评估，确定继续？',
}

export const QUALITY_CLEANUP_CONFIRM = '将删除所选失效 STRM 文件，播放可能会中断。确定删除？'

export function qualityActionLabel(action) {
  return ACTION_LABELS[action] || action
}

export function qualityActionPrompt(action) {
  return QUALITY_ACTION_PROMPTS[action] || '确认执行该质量操作？'
}

export function confirmQualityBatchFix(ask = globalThis.confirm) {
  return Boolean(ask(QUALITY_FIX_CONFIRM))
}

export function qualityRiskType(risk) {
  return RISK_TYPES[String(risk || '').toLowerCase()] || 'default'
}

export function qualityStatusLabel(status) {
  return STATUS_LABELS[String(status || '').toLowerCase()] || status || '待处理'
}
