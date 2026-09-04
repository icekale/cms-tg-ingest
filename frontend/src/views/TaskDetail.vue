<script setup>
import { computed, onMounted, ref } from 'vue'
import { NAlert, NButton, NCard, NDescriptions, NDescriptionsItem, NModal, NPopconfirm, NSelect, NTag, NText, useMessage } from 'naive-ui'
import { useRoute } from 'vue-router'
import { api } from '../api'
import { displayTaskTitle, taskActionLabel } from '../taskView'

const route = useRoute()
const message = useMessage()
const task = ref(null)
const busyAction = ref('')
const modeOptions = [
  { label: '自有分享 STRM', value: 'shared' },
  { label: '直链 STRM', value: 'direct' },
  { label: '原始分享 STRM', value: 'source_shared' },
]
const actionSet = () => new Set(Array.isArray(task.value?.available_actions) ? task.value.available_actions : [])

async function load() {
  try { task.value = await api.task(route.params.id) } catch (err) { message.error(err.message) }
}

async function changeMode(mode) {
  try {
    task.value = (await api.setTaskMode(route.params.id, mode)).task
    message.success('任务 STRM 模式已保存')
  } catch (err) { message.error(err.message); await load() }
}

const canRetry = computed(() => actionSet().has('retry'))
const canEmby = computed(() => actionSet().has('emby'))
const canRestore = computed(() => actionSet().has('restore'))
const canReprocess = computed(() => actionSet().has('reprocess'))
const canWash = computed(() => task.value?.status === 'succeeded' && !!task.value?.tmdb_id)

async function runAction(action) {
  busyAction.value = action
  try {
    task.value = await api.taskAction(route.params.id, action)
    message.success('操作已入队')
  } catch (err) { message.error(err.message); await load() } finally { busyAction.value = '' }
}

// ---------- 洗版 ----------
const washOpen = ref(false)
const washLoading = ref(false)
const washUnlocking = ref('')
const washConfirming = ref(new Set())
const wash = ref(null) // { current, candidates }

async function openWash() {
  washOpen.value = true
  washLoading.value = true
  wash.value = null
  try {
    wash.value = await api.taskWash(route.params.id)
  } catch (err) {
    message.error(err.message)
    washOpen.value = false
  } finally { washLoading.value = false }
}

async function washUnlock(candidate) {
  // 两段式确认：积分消耗超过阈值时，后端返回 requires_confirmation；
  // 前端先把按钮切换为“确认消耗 N 积分”，再点一次才带 confirmed=true。
  if (!washConfirming.value.has(candidate.slug) && (candidate.unlock_points ?? 0) > 0) {
    washConfirming.value = new Set(washConfirming.value).add(candidate.slug)
    return
  }
  washConfirming.value = new Set([...washConfirming.value].filter(s => s !== candidate.slug))
  washUnlocking.value = candidate.slug
  try {
    const result = (await api.taskWashUnlock(route.params.id, candidate.slug, true)).wash
    if (result.enqueued) {
      message.success(`洗版任务已入队：#${result.task_id}，完成入库后将自动清理旧版本`)
      washOpen.value = false
    } else if (result.requires_confirmation) {
      washConfirming.value = new Set(washConfirming.value).add(candidate.slug)
      message.info(`该资源需要 ${result.unlock_points} 积分，请再次点击确认`)
    } else {
      message.warning('解锁成功但入队未确认，请稍后在任务列表查看')
    }
  } catch (err) { message.error(err.message) } finally { washUnlocking.value = '' }
}

function eventTime(value) { return value ? new Date(value * 1000).toLocaleString() : '-' }
onMounted(load)
</script>

<template>
  <div v-if="task" class="page-title"><div><h1>{{ displayTaskTitle(task) }}</h1><p>#{{ task.id }} · {{ task.stage }}</p></div>
    <div style="display: flex; gap: 8px; align-items: center">
      <n-tag v-if="task.quality" type="info">{{ task.quality.label }}</n-tag>
      <n-tag>{{ task.status }}</n-tag>
    </div>
  </div>
  <n-card v-if="task" title="任务详情">
    <n-alert v-if="task.completion_drift" type="warning" title="入库状态需要复核" style="margin-bottom: 18px">
      <div>{{ task.completion_drift.message }}</div>
      <div class="muted">{{ task.completion_drift.recommendation }}</div>
    </n-alert>
    <n-descriptions bordered :column="2">
      <n-descriptions-item label="STRM 模式"><n-select style="width: 170px" :value="task.strm_mode" :options="modeOptions" :input-props="{ 'aria-label': 'STRM 模式' }" @update:value="changeMode" /></n-descriptions-item>
      <n-descriptions-item label="分类">{{ task.category || '-' }}</n-descriptions-item>
      <n-descriptions-item label="为什么慢">{{ task.why_slow || '-' }}</n-descriptions-item>
      <n-descriptions-item label="阶段耗时">{{ task.stage_elapsed || '-' }}</n-descriptions-item>
      <n-descriptions-item label="115 调用">{{ task.stage_p115_calls || '-' }}</n-descriptions-item>
      <n-descriptions-item label="TMDB">{{ task.tmdb_id || '-' }}</n-descriptions-item>
    </n-descriptions>
    <div class="action-row" style="margin-top: 18px">
      <n-button v-if="canRetry" type="primary" :loading="busyAction === 'retry'" @click="runAction('retry')">{{ taskActionLabel('retry') }}</n-button>
      <n-button v-if="canEmby" :loading="busyAction === 'emby'" @click="runAction('emby')">{{ taskActionLabel('emby') }}</n-button>
      <n-popconfirm v-if="canRestore" @positive-click="runAction('restore')">
        <template #trigger>
          <n-button :loading="busyAction === 'restore'">{{ taskActionLabel('restore') }}</n-button>
        </template>
        将按当前任务恢复 STRM 文件，确定继续？
      </n-popconfirm>
      <n-popconfirm v-if="canReprocess" @positive-click="runAction('reprocess')">
        <template #trigger>
          <n-button type="warning" :loading="busyAction === 'reprocess'">{{ taskActionLabel('reprocess') }}</n-button>
        </template>
        将从头重跑该任务，已完成阶段会再走一遍，确定继续？
      </n-popconfirm>
      <n-button v-if="canWash" secondary type="info" :loading="washLoading && washOpen" @click="openWash">尝试洗版</n-button>
      <n-button secondary @click="load">刷新</n-button>
    </div>
    <n-card title="处理时间线" embedded style="margin-top: 18px">
      <div v-for="event in task.events || []" :key="event.id" class="event-row"><n-tag size="small">{{ event.stage }}</n-tag><span>{{ event.message }}</span><span class="muted">{{ eventTime(event.created_at) }}</span></div>
      <div v-if="!(task.events || []).length" class="muted">暂无事件</div>
    </n-card>
    <n-card title="错误与技术详情" embedded style="margin-top: 18px">
      <div v-if="task.error?.summary" class="error-text">{{ task.error.summary }}</div>
      <pre class="detail-text">{{ JSON.stringify(task.metadata || {}, null, 2) }}</pre>
    </n-card>
  </n-card>
  <n-card v-else>正在加载任务…</n-card>

  <n-modal v-model:show="washOpen" preset="card" title="尝试洗版" style="max-width: 760px">
    <n-text depth="3">当前版本：{{ wash?.current?.label || '未知质量' }}。只列出严格优于当前版本的资源；解锁后自动入队，入库成功会清理旧版本 STRM。</n-text>
    <div v-if="washLoading" class="muted" style="margin-top: 14px">正在搜索 HDHive 资源…</div>
    <template v-else-if="wash">
      <div v-if="!(wash.candidates || []).length" class="muted" style="margin-top: 14px">暂无更优质量的资源。</div>
      <div v-for="candidate in wash.candidates || []" :key="candidate.slug" style="display: flex; gap: 10px; align-items: center; padding: 10px 0; border-bottom: 1px solid var(--border)">
        <div style="flex: 1; min-width: 0">
          <div style="overflow-wrap: anywhere">{{ candidate.title }}</div>
          <div class="muted">
            <n-tag size="small" :type="candidate.is_upgrade ? 'success' : 'default'">{{ candidate.quality?.label || '未知质量' }}</n-tag>
            {{ candidate.pan_type.toUpperCase() }} · {{ candidate.size || '大小未知' }}
            <template v-if="candidate.unlock_points != null"> · {{ candidate.unlock_points }} 积分</template>
            <template v-if="candidate.is_unlocked"> · 已拥有</template>
          </div>
        </div>
        <n-button
          size="small"
          :type="washConfirming.has(candidate.slug) ? 'warning' : 'primary'"
          :disabled="!candidate.upgrade_eligible"
          :loading="washUnlocking === candidate.slug"
          @click="washUnlock(candidate)"
        >{{ washConfirming.has(candidate.slug) ? `确认消耗 ${candidate.unlock_points ?? 0} 积分` : '解锁入库' }}</n-button>
      </div>
    </template>
  </n-modal>
</template>
