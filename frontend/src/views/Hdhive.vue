<script setup>
import { computed, onMounted, ref } from 'vue'
import { NButton, NCard, NDataTable, NSpace, NTag, useMessage } from 'naive-ui'
import { api } from '../api'

const message = useMessage()
const data = ref({ subscriptions: [], account: null, schedule: {} })
const settings = ref({ enabled: true, time: '01:30', timezone: 'Asia/Shanghai' })

async function load() {
  try {
    data.value = await api.hdhive()
    settings.value = { ...settings.value, ...(data.value.schedule || {}) }
  } catch (err) { message.error(err.message) }
}

async function subscriptionAction(id, action) {
  if (action === 'delete' && !window.confirm('确认删除此订阅？')) return
  try { await api.hdhiveSubscriptionAction(id, action); message.success('操作已提交'); await load() } catch (err) { message.error(err.message) }
}

async function confirmItem(id) {
  try { await api.hdhiveItemConfirm(id); message.success('解锁已提交'); await load() } catch (err) { message.error(err.message) }
}

async function saveSettings() {
  try { await api.hdhiveSettings(settings.value); message.success('订阅设置已保存'); await load() } catch (err) { message.error(err.message) }
}

async function run() {
  try { await api.hdhiveRun(); message.success('订阅检查已启动') } catch (err) { message.error(err.message) }
}

const pendingItems = computed(() => data.value.subscriptions.flatMap(subscription => (subscription.items || []).filter(item => item.status === 'pending_confirmation').map(item => ({ ...item, subscriptionTitle: subscription.title }))))
const unlockedItems = computed(() => data.value.subscriptions.flatMap(subscription => (subscription.items || []).filter(item => item.status === 'enqueued').map(item => ({ ...item, subscriptionTitle: subscription.title }))))
const unlockedColumns = [
  { title: '剧集', key: 'subscriptionTitle' },
  { title: '集数', key: 'episode_key' },
  { title: '资源', key: 'title' },
  { title: '积分', key: 'unlock_points_spent' },
  { title: '解锁时间', key: 'unlocked_at' },
  { title: '任务', key: 'task_id' },
]
onMounted(load)
</script>

<template>
  <div class="page-title"><div><h1>HDHive 订阅</h1><p>管理订阅、确认解锁并查看积分和时间。</p></div><n-space><n-button secondary @click="run">立即检查 run</n-button><n-button secondary @click="load">刷新</n-button></n-space></div>
  <n-card v-if="data.account" title="账号状态"><n-space><n-tag type="success">{{ data.account.nickname || '已授权' }}</n-tag><span>积分：{{ data.account.points }}</span><span>免费次数：{{ data.account.weekly_free_quota_unlimited ? '无限' : data.account.weekly_free_quota_remaining }}</span></n-space></n-card>
  <n-card title="自动检查" class="section-card"><n-space><label>启用 <input v-model="settings.enabled" type="checkbox"></label><label>时间 <input v-model="settings.time" size="5"></label><label>时区 <input v-model="settings.timezone" size="18"></label><n-button @click="saveSettings">保存 settings</n-button></n-space><p class="muted">状态：{{ data.schedule.status || 'idle' }}，下次：{{ data.schedule.next_run_at || '-' }}</p></n-card>
  <n-card title="当前订阅" class="section-card"><div v-for="subscription in data.subscriptions" :key="subscription.id" class="subscription-row"><div><strong>#{{ subscription.id }} {{ subscription.title }}</strong><div class="muted">TMDB {{ subscription.tmdb_id }} · {{ subscription.status }} · {{ (subscription.items || []).length }} 个资源</div></div><n-space><n-button v-if="subscription.status === 'active'" secondary @click="subscriptionAction(subscription.id, 'pause')">暂停 pause</n-button><n-button v-else secondary @click="subscriptionAction(subscription.id, 'resume')">恢复 resume</n-button><n-button secondary @click="subscriptionAction(subscription.id, 'check')">检查 check</n-button><n-button type="error" secondary @click="subscriptionAction(subscription.id, 'delete')">删除 delete</n-button></n-space></div><div v-if="!data.subscriptions.length" class="muted">暂无订阅</div></n-card>
  <n-card title="待确认资源" class="section-card"><div v-for="item in pendingItems" :key="item.id" class="subscription-row"><span>{{ item.subscriptionTitle }} · {{ item.episode_key }} · {{ item.title || item.resource_slug }} · {{ item.unlock_points ?? '未知' }} 积分</span><n-button type="primary" @click="confirmItem(item.id)">确认 confirm</n-button></div><div v-if="!pendingItems.length" class="muted">暂无待确认资源</div></n-card>
  <n-card title="解锁记录"><n-data-table :columns="unlockedColumns" :data="unlockedItems" :pagination="{ pageSize: 20 }" /></n-card>
</template>
