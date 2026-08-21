<script setup>
import { computed, onMounted, ref } from 'vue'
import { NButton, NCard, NDataTable, NForm, NFormItem, NInput, NSpace, NSwitch, NTag, useMessage } from 'naive-ui'
import { api } from '../api'

const message = useMessage()
const data = ref({ subscriptions: [], account: null, schedule: {}, background_job: null })
const settings = ref({ enabled: true, time: '01:30', timezone: 'Asia/Shanghai' })
const filterDraft = ref({})
const createDraft = ref({ url: '', tmdb_id: '', title: '' })
const creating = ref(false)
const HIGHLIGHT_STATUSES = new Set(['unparsed', 'pending_confirmation', 'failed', 'unlocking', 'unlocked'])

function syncFilterDraft() {
  const next = {}
  for (const subscription of data.value.subscriptions || []) next[subscription.id] = subscription.episode_filter || ''
  filterDraft.value = next
}

async function load() {
  try {
    data.value = await api.hdhive()
    settings.value = { ...settings.value, ...(data.value.schedule || {}) }
    syncFilterDraft()
  } catch (err) { message.error(err.message) }
}

async function waitForHdhiveJob() {
  for (let i = 0; i < 20; i++) {
    await new Promise(resolve => setTimeout(resolve, 400))
    await load()
    const state = data.value.background_job?.state
    if (!state || !['queued', 'running'].includes(state)) return
  }
}

async function createSubscription() {
  const url = createDraft.value.url.trim()
  const tmdbId = createDraft.value.tmdb_id.trim()
  const title = createDraft.value.title.trim()
  const payload = url ? { url } : { tmdb_id: tmdbId, title }
  creating.value = true
  try {
    await api.hdhiveCreateSubscription(payload)
    message.success('订阅已添加')
    createDraft.value = { url: '', tmdb_id: '', title: '' }
    await load()
  } catch (err) {
    message.error(err.message)
  } finally {
    creating.value = false
  }
}

async function subscriptionAction(id, action) {
  if (action === 'delete' && !window.confirm('确认删除此订阅？')) return
  try {
    await api.hdhiveSubscriptionAction(id, action)
    message.success(action === 'check' ? '检查已提交' : '操作已提交')
    if (action === 'check') await waitForHdhiveJob()
    else await load()
  } catch (err) { message.error(err.message) }
}

async function saveFilter(subscription) {
  try {
    await api.hdhiveSubscriptionFilter(subscription.id, filterDraft.value[subscription.id] || '')
    message.success('集数过滤已保存')
    await load()
  } catch (err) { message.error(err.message) }
}

async function confirmItem(id) {
  try {
    await api.hdhiveItemConfirm(id)
    message.success('解锁已提交')
    await waitForHdhiveJob()
  } catch (err) { message.error(err.message) }
}

async function saveSettings() {
  try { await api.hdhiveSettings(settings.value); message.success('订阅设置已保存'); await load() } catch (err) { message.error(err.message) }
}

async function run() {
  try {
    await api.hdhiveRun()
    message.success('订阅检查已启动')
    await waitForHdhiveJob()
  } catch (err) { message.error(err.message) }
}

function statusLabel(status) {
  return { active: '运行中', paused: '已暂停', error: '异常', completed: '已完结' }[status] || status || '未知'
}

function statusType(status) {
  return { active: 'success', paused: 'warning', error: 'error', completed: 'success' }[status] || 'default'
}

function itemStatusLabel(status) {
  return {
    unparsed: '无法识别',
    pending_confirmation: '待确认',
    failed: '失败',
    unlocking: '解锁中',
    unlocked: '已解锁未入队',
    enqueued: '已入队',
    filtered: '已过滤',
    emby_exists: 'Emby已有',
    discovered: '已发现',
  }[status] || status || '未知'
}

function summaryText(summary) {
  if (!summary || !Object.keys(summary).length) return ''
  return [
    summary.discovered == null ? '' : `发现 ${summary.discovered}`,
    summary.enqueued == null ? '' : `入队 ${summary.enqueued}`,
    summary.emby_exists == null ? '' : `Emby已有 ${summary.emby_exists}`,
    summary.filtered == null ? '' : `过滤 ${summary.filtered}`,
    summary.unparsed == null ? '' : `无法识别 ${summary.unparsed}`,
    summary.pending_confirmation == null ? '' : `待确认 ${summary.pending_confirmation}`,
    summary.failed == null ? '' : `失败 ${summary.failed}`,
    summary.blocked == null ? '' : `阻塞 ${summary.blocked}`,
  ].filter(Boolean).join(' · ')
}

function formatTime(value) {
  if (!value) return '-'
  return new Date(Number(value) * 1000).toLocaleString()
}

const pendingItems = computed(() => data.value.subscriptions.flatMap(subscription => (subscription.items || []).filter(item => item.status === 'pending_confirmation').map(item => ({ ...item, subscriptionTitle: subscription.title }))))
const diagnosticItems = computed(() => data.value.subscriptions.flatMap(subscription => (subscription.items || []).filter(item => HIGHLIGHT_STATUSES.has(item.status)).map(item => ({ ...item, subscriptionTitle: subscription.title }))))
const unlockedItems = computed(() => data.value.subscriptions.flatMap(subscription => (subscription.items || []).filter(item => item.status === 'enqueued').map(item => ({ ...item, subscriptionTitle: subscription.title }))))
const diagnosticColumns = [
  { title: '剧集', key: 'subscriptionTitle' },
  { title: '集数', key: 'episode_key' },
  { title: '资源', key: 'title' },
  { title: '状态', key: 'status', render: row => itemStatusLabel(row.status) },
  { title: '原因', key: 'reason', render: row => row.skip_reason || row.last_error || '-' },
  { title: '任务', key: 'task_id' },
]
const unlockedColumns = [
  { title: '剧集', key: 'subscriptionTitle' },
  { title: '集数', key: 'episode_key' },
  { title: '资源', key: 'title' },
  { title: '积分', key: 'unlock_points_spent' },
  { title: '解锁时间', key: 'unlocked_at', render: row => formatTime(row.unlocked_at) },
  { title: '任务', key: 'task_id' },
]
onMounted(load)
</script>

<template>
  <div class="page-title"><div><h1>HDHive 订阅</h1><p>添加剧集订阅，管理集数过滤、确认解锁并查看积分和时间。</p></div><n-space><n-button secondary @click="run">立即检查</n-button><n-button secondary @click="load">刷新</n-button></n-space></div>
  <n-card v-if="data.account" title="账号状态"><n-space><n-tag type="success">{{ data.account.nickname || '已授权' }}</n-tag><span>积分：{{ data.account.points }}</span><span>免费次数：{{ data.account.weekly_free_quota_unlimited ? '无限' : data.account.weekly_free_quota_remaining }}</span></n-space></n-card>
  <n-card title="自动检查" class="section-card">
    <n-form class="compact-form" label-placement="top">
      <n-form-item label="启用">
        <n-switch v-model:value="settings.enabled" />
      </n-form-item>
      <n-form-item label="时间">
        <n-input v-model:value="settings.time" aria-label="订阅检查时间" style="width: 96px" />
      </n-form-item>
      <n-form-item label="时区">
        <n-input v-model:value="settings.timezone" aria-label="订阅检查时区" style="width: 200px" />
      </n-form-item>
      <n-form-item label="操作" :show-feedback="false">
        <n-button @click="saveSettings">保存设置</n-button>
      </n-form-item>
    </n-form>
    <p class="muted">状态：{{ data.schedule.status || 'idle' }}，下次：{{ data.schedule.next_run_at || '-' }}</p>
  </n-card>
  <n-card title="添加订阅" class="section-card">
    <n-form class="compact-form" label-placement="top" @submit.prevent="createSubscription">
      <n-form-item label="HDHive 剧集链接">
        <n-input v-model:value="createDraft.url" aria-label="HDHive 剧集链接" placeholder="https://hdhive.com/tv/..." style="min-width: 280px" />
      </n-form-item>
      <n-form-item label="或 TMDB ID">
        <n-input v-model:value="createDraft.tmdb_id" aria-label="TMDB 剧集 ID" placeholder="255358" style="width: 120px" />
      </n-form-item>
      <n-form-item label="剧名">
        <n-input v-model:value="createDraft.title" aria-label="剧名" placeholder="可选" style="width: 200px" />
      </n-form-item>
      <n-form-item label="操作" :show-feedback="false">
        <n-button type="primary" :loading="creating" :disabled="!createDraft.url.trim() && !createDraft.tmdb_id.trim()" @click="createSubscription">添加订阅</n-button>
      </n-form-item>
    </n-form>
    <p class="muted">只接受剧集。粘贴 HDHive 剧集页，或填写 TMDB 剧集 ID。创建后不会立即解锁，可再点检查。</p>
  </n-card>
  <n-card title="当前订阅" class="section-card"><div v-for="subscription in data.subscriptions" :key="subscription.id" class="subscription-row"><div><strong>#{{ subscription.id }} {{ subscription.title }}</strong><div class="muted"><n-tag size="small" :type="statusType(subscription.status)">{{ statusLabel(subscription.status) }}</n-tag> · TMDB {{ subscription.tmdb_id }} · {{ (subscription.items || []).length }} 个资源</div><div v-if="subscription.episode_filter" class="muted">当前过滤：{{ subscription.episode_filter }}</div><div v-if="summaryText(subscription.last_summary)" class="muted">最近检查：{{ summaryText(subscription.last_summary) }}</div><div v-if="subscription.diagnosis && subscription.diagnosis.conclusion" class="muted">{{ subscription.diagnosis.conclusion }}</div><div v-if="subscription.diagnosis && subscription.diagnosis.reasons && subscription.diagnosis.reasons.length" class="muted">{{ subscription.diagnosis.reasons.join('；') }}</div></div><n-space vertical align="end"><n-space><n-button v-if="subscription.status === 'active'" secondary @click="subscriptionAction(subscription.id, 'pause')">暂停</n-button><n-button v-else secondary @click="subscriptionAction(subscription.id, 'resume')">恢复</n-button><n-button secondary @click="subscriptionAction(subscription.id, 'check')">检查</n-button><n-button type="error" secondary @click="subscriptionAction(subscription.id, 'delete')">删除</n-button></n-space><n-space><n-input v-model:value="filterDraft[subscription.id]" size="small" aria-label="集数过滤" placeholder="S01E01-S01E10,S02" /><n-button secondary @click="saveFilter(subscription)">设置集数过滤</n-button></n-space></n-space></div><div v-if="!data.subscriptions.length" class="muted">暂无订阅。可在上方粘贴 HDHive 剧集链接或填写 TMDB ID。</div></n-card>
  <n-card title="资源状态" class="section-card">
    <div class="desktop-table">
      <n-data-table :columns="diagnosticColumns" :data="diagnosticItems" :pagination="{ pageSize: 20 }" :scroll-x="760" />
    </div>
    <div class="mobile-cards" aria-label="资源状态">
      <article v-for="item in diagnosticItems" :key="item.id" class="issue-card">
        <strong>{{ item.subscriptionTitle }} · {{ item.episode_key }}</strong>
        <div class="issue-card-meta">{{ itemStatusLabel(item.status) }} · {{ item.skip_reason || item.last_error || item.title || '-' }}</div>
      </article>
    </div>
    <div v-if="!diagnosticItems.length" class="muted">没有需要关注的资源</div>
  </n-card>
  <n-card title="待确认资源" class="section-card"><div v-for="item in pendingItems" :key="item.id" class="subscription-row"><span>{{ item.subscriptionTitle }} · {{ item.episode_key }} · {{ item.title || item.resource_slug }} · {{ item.unlock_points ?? '未知' }} 积分</span><n-button type="primary" @click="confirmItem(item.id)">确认解锁</n-button></div><div v-if="!pendingItems.length" class="muted">暂无待确认资源</div></n-card>
  <n-card title="解锁记录">
    <div class="desktop-table">
      <n-data-table :columns="unlockedColumns" :data="unlockedItems" :pagination="{ pageSize: 20 }" :scroll-x="760" />
    </div>
    <div class="mobile-cards" aria-label="解锁记录">
      <article v-for="item in unlockedItems" :key="item.id" class="issue-card">
        <strong>{{ item.subscriptionTitle }} · {{ item.episode_key }}</strong>
        <div class="issue-card-meta">任务 {{ item.task_id || '-' }} · {{ item.unlock_points_spent ?? '-' }} 积分 · {{ formatTime(item.unlocked_at) }}</div>
      </article>
    </div>
  </n-card>
</template>
