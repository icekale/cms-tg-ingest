<script setup>
import { computed, onMounted, ref } from 'vue'
import { NButton, NCard, NPopconfirm, NSpace, NStatistic, NTag, useMessage } from 'naive-ui'
import { api } from '../api'
import { displayTaskTitle } from '../taskView'

const data = ref(null)
const error = ref('')
const loading = ref(false)
const message = useMessage()
const modeLabels = { shared: '自有分享', direct: '直链', source_shared: '原始分享' }
async function load() {
  loading.value = true
  try { data.value = await api.overview(); error.value = '' } catch (err) { error.value = err.message } finally { loading.value = false }
}
async function clearHistory() {
  try {
    const result = await api.clearHistory()
    message.success(`已清理 ${result.cleared || 0} 条历史记录`)
    await load()
  } catch (err) { message.error(err.message) }
}
onMounted(load)

const TMDB_IMAGE_BASE = 'https://image.tmdb.org/t/p/w500'
const KIND_LABELS = { movie: '电影', tv: '剧集', category: '' }

function mediaItems() {
  return (data.value?.tasks?.items || []).filter((task) => {
    return task.status === 'succeeded' || task.status === 'completed'
  })
}

function posterUrl(task) {
  const path = task.metadata?.poster_path
  return path ? `${TMDB_IMAGE_BASE}${path}` : ''
}

function categoryLabel(task) {
  const kind = task.metadata?.type || task.category
  return KIND_LABELS[kind] || task.category || ''
}

function meta(task) {
  return task.metadata || {}
}

function year(task) {
  const release = meta(task).release_date
  const match = String(release || '').match(/^(\d{4})/)
  return match ? match[1] : ''
}

function rating(task) {
  const value = Number(meta(task).vote_average)
  return Number.isFinite(value) && value > 0 ? value.toFixed(1) : ''
}

function episodes(task) {
  const match = String(displayTaskTitle(task)).match(/S(\d{1,2})E(\d{1,3})/i)
  if (!match) return ''
  return `第${parseInt(match[1], 10)}季 第${parseInt(match[2], 10)}集`
}

function genres(task) {
  const list = meta(task).genres
  return Array.isArray(list) ? list.slice(0, 2).map(String) : []
}

function formatChip(task) {
  // A cheap quality hint straight from the file name (e.g. "4K", "HEVC").
  const markers = ['4K', '1080P', 'HEVC', 'HDR', 'DTS', 'TRUEHD', 'EAC3']
  const name = String(task.title || task.metadata?.share_name || '').toUpperCase()
  return markers.find((marker) => name.includes(marker)) || ''
}

function dotClass(status) {
  if (status === 'succeeded' || status === 'completed') return 'is-success'
  if (status === 'failed' || status === 'error') return 'is-danger'
  if (status === 'needs_action' || status === 'attention') return 'is-warning'
  return ''
}

function fallbackText(task) {
  const title = displayTaskTitle(task)
  return title.length > 1 ? title.slice(0, 2) : title
}

const posterFailed = ref(new Set())
function markPosterFailed(taskId) {
  posterFailed.value = new Set(posterFailed.value).add(taskId)
}
const visibleItems = computed(() => mediaItems().slice(0, 12))
</script>

<template>
  <div class="page-title"><div><h1>运行概览</h1><p>把当前队列、风险和下一步操作放在一个页面。</p></div><n-space><n-popconfirm @positive-click="clearHistory"><template #trigger><n-button secondary>清理历史</n-button></template>确认清理已完成历史任务？</n-popconfirm><n-button secondary @click="load">刷新</n-button></n-space></div>
  <n-card v-if="error" type="error" class="section-card">{{ error }}</n-card>
  <template v-else-if="data">
    <n-card title="最近入库" class="section-card">
      <div v-if="!loading && visibleItems.length" class="media-wall">
        <router-link v-for="task in visibleItems" :key="task.id" class="media-card" :to="`/tasks/${task.id}`">
          <div class="media-poster">
            <img
              v-if="posterUrl(task) && !posterFailed.has(task.id)"
              :src="posterUrl(task)"
              alt=""
              loading="lazy"
              @error="markPosterFailed(task.id)"
            >
            <div v-else class="media-poster-fallback">{{ fallbackText(task) }}</div>
            <span v-if="rating(task)" class="media-score">{{ rating(task) }}</span>
            <span class="media-status-dot" :class="dotClass(task.status)" />
          </div>
          <span class="media-card-title">{{ displayTaskTitle(task) }}</span>
          <span class="media-card-sub">
            <template v-if="episodes(task)">{{ episodes(task) }} · </template>
            <template v-if="year(task)">{{ year(task) }}</template>
          </span>
          <span class="media-chips">
            <span v-if="categoryLabel(task)" class="chip is-kind">{{ categoryLabel(task) }}</span>
            <span v-for="genre in genres(task)" :key="genre" class="chip">{{ genre }}</span>
            <span v-if="formatChip(task)" class="chip">{{ formatChip(task) }}</span>
          </span>
        </router-link>
      </div>
      <div v-else-if="loading" class="media-wall">
        <div v-for="index in 6" :key="index" class="media-skeleton">
          <div class="media-skeleton-poster" />
          <div class="media-skeleton-line short" />
          <div class="media-skeleton-line" />
        </div>
      </div>
      <div v-else class="media-empty">暂无最近入库的媒体，完成任务后这里会展示海报墙。</div>
    </n-card>

    <div class="metric-grid">
      <n-card><n-statistic label="活跃任务" :value="data.health.pending_count + data.health.running_count" /></n-card>
      <n-card><n-statistic label="需处理" :value="data.health.problem_count" /></n-card>
      <n-card><n-statistic label="锁等待" :value="data.health.lock_wait_count" /></n-card>
      <n-card><n-statistic label="默认 STRM" :value="modeLabels[data.strm_default_mode] || data.strm_default_mode" /></n-card>
    </div>
    <n-card title="当前队列" class="section-card">
      <n-space vertical>
        <div v-for="task in data.tasks.items.slice(0, 8)" :key="task.id">
          <router-link class="task-link" :to="`/tasks/${task.id}`">#{{ task.id }} {{ displayTaskTitle(task) }}</router-link>
          <span class="muted"> · {{ task.stage }} · </span><n-tag size="small">{{ task.status }}</n-tag>
        </div>
        <span v-if="!data.tasks.items.length" class="muted">暂无任务</span>
      </n-space>
    </n-card>
  </template>
</template>
