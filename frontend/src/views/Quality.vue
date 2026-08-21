<script setup>
import { computed, h, onMounted, ref } from 'vue'
import { NButton, NCard, NCheckbox, NCheckboxGroup, NDataTable, NForm, NFormItem, NInput, NInputNumber, NModal, NSpace, NSwitch, NTag, useMessage } from 'naive-ui'
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

function rowKey(row) {
  return `${row.task_id}:${row.rule_id || 'manual'}`
}

function rowActions(row) {
  return ['execute', 'reprocess', 'snooze', 'ignore', 'resume']
    .filter((action) => (row.available_actions || []).includes(action))
}

function evidenceLines(row) {
  const evidence = row.evidence || []
  if (!evidence.length) return [row.detail || '-']
  const shown = evidence.slice(0, 3)
  const extra = evidence.length - shown.length
  return extra > 0 ? [...shown, `等 ${extra} 条`] : shown
}

function taskCell(row) {
  const title = displayTaskTitle(row)
  if (!row.title) return h('span', `#${row.task_id} ${title}`)
  return h(RouterLink, { class: 'task-link', to: `/tasks/${row.task_id}` }, { default: () => `#${row.task_id} ${title}` })
}

function actionCell(row) {
  const actions = ['execute', 'reprocess', 'snooze', 'ignore', 'resume']
    .filter((action) => (row.available_actions || []).includes(action))
  const buttons = actions.map((action) => h(NButton, {
    size: 'small',
    type: action === 'ignore' ? 'error' : action === 'execute' || action === 'reprocess' ? 'warning' : 'default',
    secondary: action !== 'ignore',
    loading: busyAction.value === `${row.task_id}:${action}`,
    onClick: () => runQualityAction(row, action),
  }, { default: () => qualityActionLabel(action) }))
  if (payload.value.cleanup_enabled) {
    buttons.push(h(NButton, {
      size: 'small',
      secondary: true,
      onClick: () => openCleanup(row),
    }, { default: () => '失效 STRM' }))
  }
  return h(NSpace, { size: 6 }, { default: () => buttons })
}

const columns = [
  { title: '任务', key: 'task', minWidth: 210, render: taskCell },
  { title: '规则', key: 'rule_id', minWidth: 170 },
  { title: '风险', key: 'risk_level', width: 90, render: (row) => h(NTag, { size: 'small', type: qualityRiskType(row.risk_level) }, { default: () => row.risk_level || '-' }) },
  { title: '状态', key: 'manual_status', width: 100, render: (row) => h(NTag, { size: 'small' }, { default: () => qualityStatusLabel(row.archived ? 'archived' : row.manual_status) }) },
  { title: '文件数', key: 'issue_count', width: 80, render: (row) => (row.issue_count > 1 ? `${row.issue_count} 个文件` : row.issue_count || '-') },
  { title: '尝试', key: 'attempts', width: 70 },
  { title: '问题', key: 'issue_codes', minWidth: 150, render: (row) => (row.issue_codes || []).join(', ') || '-' },
  { title: '证据', key: 'evidence', minWidth: 230, render: (row) => {
    const evidence = row.evidence || []
    if (!evidence.length) return h('div', { class: 'quality-evidence' }, row.detail || '-')
    const shown = evidence.slice(0, 3)
    const extra = evidence.length - shown.length
    return h('div', { class: 'quality-evidence' }, [...shown.map((line) => h('div', line)), ...(extra > 0 ? [h('div', { class: 'muted' }, `等 ${extra} 条`)] : [])])
  } },
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

const cleanup = ref({ show: false, taskId: 0, candidates: [], checked: [], running: false, result: null, error: '' })

async function openCleanup(row) {
  cleanup.value = { show: true, taskId: row.task_id, candidates: [], checked: [], running: false, result: null, error: '' }
  try {
    const payload = await api.qualityCleanupDryRun(row.task_id, true)
    cleanup.value.candidates = payload.candidates || []
    // Files whose share is still alive on 115 are NOT pre-checked: deleting them
    // would break playback. Only confirmed-dead shares are pre-selected.
    cleanup.value.checked = (payload.candidates || [])
      .filter((c) => c.share_state !== 'valid')
      .map((c) => c.path)
    if (payload.error) cleanup.value.error = payload.error
  } catch (err) {
    cleanup.value.error = err.message
  }
}

async function runCleanup() {
  if (!cleanup.value.checked.length) return
  cleanup.value.running = true
  cleanup.value.error = ''
  try {
    cleanup.value.result = await api.qualityCleanupRun(cleanup.value.taskId, cleanup.value.checked, false)
  } catch (err) {
    cleanup.value.error = err.message
  } finally {
    cleanup.value.running = false
  }
}

function cleanupShareTag(shareState) {
  if (shareState === 'valid') return { type: 'warning', label: '分享仍有效' }
  if (shareState === 'invalid') return { type: 'error', label: '分享已失效' }
  return { type: 'default', label: '状态未知' }
}

const CLEANUP_SKIP_REASONS = {
  not_candidate: '非候选（可能已被处理）',
  invalid_path: '路径无效',
  path_changed: '文件已变化',
  unreadable: '无法读取',
  became_direct: '已变为直链',
  share_became_live: '分享已有任务引用',
  share_still_alive: '分享在 115 仍有效（被保护）',
  unlink_failed: '删除失败',
}

function cleanupFileName(path) {
  const parts = String(path || '').split('/')
  return parts[parts.length - 1] || path
}

onMounted(load)
</script>

<template>
  <div class="page-title">
    <div><h1>质量巡检</h1><p>只读报表：同一剧集目录里指向任一仍有效自有分享的 STRM 视为健康。默认不自动重跑。</p></div>
    <n-space><n-button type="primary" @click="run">立即巡检</n-button><n-button secondary :loading="loading" @click="load">刷新</n-button></n-space>
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
    <n-form class="compact-form" label-placement="top">
      <n-form-item label="定时扫描">
        <n-switch v-model:value="settings.enabled" />
      </n-form-item>
      <n-form-item label="时间">
        <n-input v-model:value="settings.time" aria-label="巡检时间" style="width: 96px" />
      </n-form-item>
      <n-form-item label="时区">
        <n-input v-model:value="settings.timezone" aria-label="巡检时区" style="width: 200px" />
      </n-form-item>
      <n-form-item label="任务上限">
        <n-input-number v-model:value="settings.max_tasks" :min="1" style="width: 120px" />
      </n-form-item>
      <n-form-item label="115 检查上限">
        <n-input-number v-model:value="settings.check_limit" :min="1" style="width: 120px" />
      </n-form-item>
      <n-form-item label="操作" :show-feedback="false">
        <n-space>
          <n-button @click="saveSettings">保存</n-button>
          <n-button secondary @click="reset">恢复默认</n-button>
        </n-space>
      </n-form-item>
    </n-form>
    <p class="muted">启用后只扫描写报表，不会自动重跑、占任务或推 Telegram 操作按钮。自动修复请用环境变量 QUALITY_AUTO_REPAIR_ENABLED，不推荐。</p>
    <p class="muted">状态：{{ payload.automation.status }}，下次运行：{{ payload.automation.next_run_at }}</p>
    <p v-if="payload.automation.last_summary" class="muted">最近结果：扫描 {{ payload.automation.last_summary.scanned_count || 0 }}，问题 {{ payload.automation.last_summary.issue_count || 0 }}，排队 {{ payload.automation.last_summary.queued_count || 0 }}，失败 {{ payload.automation.last_summary.failed_count || 0 }}</p>
  </n-card>

  <n-card title="巡检结果">
    <div class="desktop-table">
      <n-data-table :columns="columns" :data="issues" :loading="loading" :pagination="{ pageSize: 12 }" :scroll-x="1250" />
    </div>
    <div class="mobile-cards" aria-label="巡检结果">
      <article v-for="row in issues" :key="rowKey(row)" class="issue-card">
        <router-link class="task-link" :to="`/tasks/${row.task_id}`">#{{ row.task_id }} {{ displayTaskTitle(row) }}</router-link>
        <div class="issue-card-meta">{{ row.rule_id || '人工' }} · {{ row.issue_count > 1 ? row.issue_count + ' 个文件' : '1 个文件' }}</div>
        <n-space>
          <n-tag size="small" :type="qualityRiskType(row.risk_level)">{{ row.risk_level || '-' }}</n-tag>
          <n-tag size="small">{{ qualityStatusLabel(row.archived ? 'archived' : row.manual_status) }}</n-tag>
        </n-space>
        <div class="quality-evidence">
          <div v-for="line in evidenceLines(row)" :key="line">{{ line }}</div>
        </div>
        <div class="issue-card-actions">
          <n-button
            v-for="action in rowActions(row)"
            :key="action"
            size="small"
            :type="action === 'ignore' ? 'error' : action === 'execute' || action === 'reprocess' ? 'warning' : 'default'"
            :secondary="action !== 'ignore'"
            :loading="busyAction === `${row.task_id}:${action}`"
            @click="runQualityAction(row, action)"
          >{{ qualityActionLabel(action) }}</n-button>
          <n-button v-if="payload.cleanup_enabled" size="small" secondary @click="openCleanup(row)">失效 STRM</n-button>
        </div>
      </article>
      <div v-if="!issues.length" class="muted">暂无需要处理的问题</div>
    </div>
  </n-card>

  <n-card title="近 30 天巡检趋势" class="section-card">
    <div class="desktop-table">
      <n-data-table :columns="runColumns" :data="runs.items" :pagination="{ pageSize: 10 }" :scroll-x="700" />
    </div>
    <div class="mobile-cards" aria-label="巡检趋势">
      <article v-for="run in runs.items" :key="run.run_date" class="issue-card">
        <strong>{{ run.run_date }}</strong>
        <div class="issue-card-meta">{{ run.status }} · 扫描 {{ run.scanned_count }} · 问题 {{ run.issue_count }} · 排队 {{ run.queued_count }} · 失败 {{ run.failed_count }}</div>
      </article>
      <div v-if="!runs.items.length" class="muted">暂无巡检记录</div>
    </div>
  </n-card>

  <n-modal v-model:show="cleanup.show" preset="card" title="清理失效 STRM" class="cleanup-modal">
    <template v-if="cleanup.result">
      <p>已删除 <b>{{ cleanup.result.removed?.length || 0 }}</b> 个失效 STRM，跳过 <b>{{ cleanup.result.skipped?.length || 0 }}</b> 个。</p>
      <p v-if="cleanup.result.resumed" class="muted">任务已无质量问题，已自动恢复自动评估。</p>
      <p v-if="cleanup.result.skipped?.length" class="muted">
        跳过原因：{{ cleanup.result.skipped.map((s) => `${cleanupFileName(s.path)}: ${CLEANUP_SKIP_REASONS[s.reason] || s.reason}`).join('；') }}
      </p>
      <n-space style="margin-top: 12px"><n-button type="primary" @click="cleanup.show = false; load()">关闭</n-button></n-space>
    </template>
    <template v-else-if="cleanup.error">
      <p class="muted">{{ cleanup.error }}</p>
      <n-space style="margin-top: 12px"><n-button @click="cleanup.show = false">关闭</n-button></n-space>
    </template>
    <template v-else-if="cleanup.candidates.length">
      <p class="muted">以下 STRM 引用了已无存活任务引用的分享码。分享仍在 115 的默认不勾选（删除会断链）；已失效的默认勾选。</p>
      <n-checkbox-group v-model:value="cleanup.checked" style="max-height: 320px; overflow: auto">
        <n-checkbox v-for="c in cleanup.candidates" :key="c.path" :value="c.path" :title="c.path">
          <span :class="{ 'alive-share': c.share_state === 'valid' }">{{ cleanupFileName(c.path) }}</span>
          <n-tag size="small" :type="cleanupShareTag(c.share_state).type" style="margin-left: 6px">{{ cleanupShareTag(c.share_state).label }}</n-tag>
        </n-checkbox>
      </n-checkbox-group>
      <p v-if="cleanup.candidates.some((c) => c.share_state === 'valid')" class="muted">
        提示：{{ cleanup.candidates.filter((c) => c.share_state === 'valid').length }} 个文件的分享在 115 仍有效，如需删除请在下方勾选（执行时会再次验证）。
      </p>
      <n-space style="margin-top: 12px">
        <n-button type="primary" :loading="cleanup.running" :disabled="!cleanup.checked.length" @click="runCleanup">
          删除所选（{{ cleanup.checked.length }}）
        </n-button>
        <n-button @click="cleanup.show = false">取消</n-button>
      </n-space>
    </template>
    <template v-else>
      <p class="muted">没有可清理的失效 STRM 文件。</p>
      <n-space style="margin-top: 12px"><n-button @click="cleanup.show = false">关闭</n-button></n-space>
    </template>
  </n-modal>
</template>

<style scoped>
.alive-share {
  color: var(--danger);
  font-weight: 600;
}
.cleanup-modal {
  width: min(640px, calc(100vw - 32px));
}
</style>
