<script setup>
import { computed, h, onMounted, ref } from 'vue'
import { NButton, NCard, NDataTable, NSpace, NTag, useMessage } from 'naive-ui'
import { RouterLink } from 'vue-router'
import { api } from '../api'
import { mergeQualityRows, qualityActionLabel, qualityRiskType, qualityStatusLabel } from '../qualityView'
import { displayTaskTitle } from '../taskView'

const message = useMessage()
const payload = ref({ items: [], rule_counts: {}, manual_count: 0, cooldown_count: 0, automation: null })
const runs = ref({ items: [], trend: [] })
const loading = ref(false)
const busyAction = ref('')
const settings = ref({ enabled: false, time: '02:50', timezone: 'Asia/Shanghai', max_tasks: 50, check_limit: 3 })

const issues = computed(() => mergeQualityRows(payload.value.items))
const ruleCounts = computed(() => Object.entries(payload.value.rule_counts || {}))

async function load() {
  loading.value = true
  try {
    const data = await api.quality()
    payload.value = data
    if (data.automation) settings.value = { ...settings.value, ...data.automation }
    try {
      runs.value = await api.qualityRuns()
    } catch (err) {
      runs.value = { items: [], trend: [] }
    }
  } catch (err) {
    message.error(err.message)
  } finally {
    loading.value = false
  }
}

async function fix() {
  loading.value = true
  try {
    const result = await api.qualityFix()
    message.success(`已入队 ${result.fixed || 0} 个修复任务`)
    await load()
  } catch (err) {
    message.error(err.message)
  } finally {
    loading.value = false
  }
}

async function run() {
  try {
    await api.qualityRun()
    message.success('巡检已启动')
    await load()
  } catch (err) {
    message.error(err.message)
  }
}

async function saveSettings() {
  try {
    await api.qualitySettings(settings.value)
    message.success('巡检设置已保存')
    await load()
  } catch (err) {
    message.error(err.message)
  }
}

async function reset() {
  try {
    await api.qualityReset()
    message.success('已恢复默认设置')
    await load()
  } catch (err) {
    message.error(err.message)
  }
}

function confirmAction(action) {
  const prompts = {
    execute: '将任务重新入队执行，确定继续？',
    reprocess: '将任务从头重跑，确定继续？',
    snooze: '将该问题暂缓 24 小时，确定继续？',
    ignore: '忽略该质量问题后，自动巡检不会再处理它，确定继续？',
    resume: '恢复该问题的规则评估，确定继续？',
  }
  return window.confirm(prompts[action] || '确认执行该质量操作？')
}

async function runQualityAction(row, action) {
  if (!confirmAction(action)) return
  const key = `${row.task_id}:${action}`
  busyAction.value = key
  try {
    const result = await api.qualityAction(action, {
      task_id: row.task_id,
      rule_id: row.rule_id,
      rule_version: row.rule_version,
      action,
      actor: 'web-ui',
    })
    const labels = { queued: '已入队', snoozed: '已暂缓', ignored: '已忽略', resumed: '已恢复评估' }
    message.success(labels[result.status] || '质量操作已完成')
    await load()
  } catch (err) {
    message.error(err.message)
    await load()
  } finally {
    busyAction.value = ''
  }
}

function taskCell(row) {
  const title = displayTaskTitle(row)
  if (!row.title) return h('span', `#${row.task_id} ${title}`)
  return h(RouterLink, { class: 'task-link', to: `/tasks/${row.task_id}` }, { default: () => `#${row.task_id} ${title}` })
}

function actionCell(row) {
  const actions = ['execute', 'reprocess', 'snooze', 'ignore', 'resume']
    .filter((action) => (row.available_actions || []).includes(action))
  return h(NSpace, { size: 6 }, {
    default: () => actions.map((action) => h(NButton, {
      size: 'small',
      type: action === 'ignore' ? 'error' : action === 'execute' || action === 'reprocess' ? 'warning' : 'default',
      secondary: action !== 'ignore',
      loading: busyAction.value === `${row.task_id}:${action}`,
      onClick: () => runQualityAction(row, action),
    }, { default: () => qualityActionLabel(action) })),
  })
}

const columns = [
  { title: '任务', key: 'task', minWidth: 210, render: taskCell },
  { title: '规则', key: 'rule_id', minWidth: 170 },
  { title: '风险', key: 'risk_level', width: 90, render: (row) => h(NTag, { size: 'small', type: qualityRiskType(row.risk_level) }, { default: () => row.risk_level || '-' }) },
  { title: '状态', key: 'manual_status', width: 100, render: (row) => h(NTag, { size: 'small' }, { default: () => qualityStatusLabel(row.archived ? 'archived' : row.manual_status) }) },
  { title: '尝试', key: 'attempts', width: 70 },
  { title: '问题', key: 'issue_codes', minWidth: 150, render: (row) => (row.issue_codes || []).join(', ') || '-' },
  { title: '证据', key: 'evidence', minWidth: 230, render: (row) => h('div', { class: 'quality-evidence' }, (row.evidence || []).join('\n') || row.detail || '-') },
  { title: '操作', key: 'actions', minWidth: 180, render: actionCell },
]

const runColumns = [
  { title: '日期', key: 'run_date', width: 120 },
  { title: '状态', key: 'status', width: 100 },
  { title: '扫描任务', key: 'scanned_count', width: 100 },
  { title: '问题', key: 'issue_count', width: 80 },
  { title: '排队', key: 'queued_count', width: 80 },
  { title: '失败', key: 'failed_count', width: 80 },
]

onMounted(load)
</script>

<template>
  <div class="page-title">
    <div><h1>质量巡检</h1><p>查看规则判定和安全证据，人工操作会经过后端状态校验。</p></div>
    <n-space><n-button type="primary" :loading="loading" @click="fix">批量修复</n-button><n-button secondary @click="run">立即巡检</n-button><n-button secondary :loading="loading" @click="load">刷新</n-button></n-space>
  </div>

  <div class="metric-grid quality-metrics">
    <div class="stat-card"><div class="stat-label">发现问题</div><div class="stat-value">{{ payload.count || 0 }}</div></div>
    <div class="stat-card"><div class="stat-label">需要人工</div><div class="stat-value">{{ payload.manual_count || 0 }}</div></div>
    <div class="stat-card"><div class="stat-label">冷却中</div><div class="stat-value">{{ payload.cooldown_count || 0 }}</div></div>
    <div class="stat-card"><div class="stat-label">规则类型</div><div class="stat-value">{{ ruleCounts.length }}</div></div>
  </div>

  <n-card v-if="ruleCounts.length" title="规则分布" class="section-card">
    <n-space><n-tag v-for="([rule, count]) in ruleCounts" :key="rule" size="small">{{ rule }} · {{ count }}</n-tag></n-space>
  </n-card>

  <n-card v-if="payload.automation" title="自动巡检设置" class="section-card">
    <n-space align="center"><label>启用 <input v-model="settings.enabled" type="checkbox"></label><label>时间 <input v-model="settings.time" size="5"></label><label>时区 <input v-model="settings.timezone" size="18"></label><label>任务上限 <input v-model.number="settings.max_tasks" type="number" min="1"></label><label>115 检查上限 <input v-model.number="settings.check_limit" type="number" min="1"></label><n-button @click="saveSettings">保存</n-button><n-button secondary @click="reset">恢复默认</n-button></n-space>
    <p class="muted">状态：{{ payload.automation.status }}，下次运行：{{ payload.automation.next_run_at }}</p>
    <p v-if="payload.automation.last_summary" class="muted">最近结果：扫描 {{ payload.automation.last_summary.scanned_count || 0 }}，问题 {{ payload.automation.last_summary.issue_count || 0 }}，排队 {{ payload.automation.last_summary.queued_count || 0 }}，失败 {{ payload.automation.last_summary.failed_count || 0 }}</p>
  </n-card>

  <n-card title="人工处理队列">
    <n-data-table :columns="columns" :data="issues" :loading="loading" :pagination="{ pageSize: 12 }" :scroll-x="1250" />
  </n-card>

  <n-card title="近 30 天巡检趋势" class="section-card">
    <n-data-table :columns="runColumns" :data="runs.items" :pagination="{ pageSize: 10 }" :scroll-x="700" />
  </n-card>
</template>
