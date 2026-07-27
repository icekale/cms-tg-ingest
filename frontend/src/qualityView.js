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

export function qualityActionLabel(action) {
  return ACTION_LABELS[action] || action
}

export function qualityRiskType(risk) {
  return RISK_TYPES[String(risk || '').toLowerCase()] || 'default'
}

export function qualityStatusLabel(status) {
  return STATUS_LABELS[String(status || '').toLowerCase()] || status || '待处理'
}
