<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NButton, NCard, NInput, NSelect, NSpace, NTag, useMessage } from 'naive-ui'
import {
  createLogStreamController,
  prependLog,
  preservedScrollTop,
} from '../logView'

const message = useMessage()
const entries = ref([])
const connectionState = ref('connecting')
const filterType = ref('main')
const lineLimit = ref(1000)
const keywordDraft = ref('')
const keyword = ref('')
const loggerDraft = ref('')
const logger = ref('')
const logViewport = ref(null)
const visibleCount = ref(2000)
const filterOptions = [
  { label: '重要', value: 'main' },
  { label: '错误', value: 'ERROR' },
  { label: '全部', value: 'all' },
]
const lineOptions = [1000, 2000, 5000].map((value) => ({ label: `${value} 行`, value }))
const statusLabel = computed(() => ({ connecting: '连接中', connected: '已连接', failed: '连接失败' }[connectionState.value]))
const statusType = computed(() => ({ connecting: 'warning', connected: 'success', failed: 'error' }[connectionState.value]))
let reconnectTimer
let disposed = false

function currentFilters() {
  return { filterType: filterType.value, lines: lineLimit.value, keyword: keyword.value, logger: logger.value }
}

const visibleEntries = computed(() => entries.value.slice(0, visibleCount.value))

const controller = createLogStreamController({
  onOpen: () => { connectionState.value = 'connected' },
  onError: () => { connectionState.value = 'failed' },
  onSnapshot: async (rows) => {
    entries.value = rows.slice(0, lineLimit.value)
    await nextTick()
    if (logViewport.value) logViewport.value.scrollTop = 0
  },
  onLog: async (entry) => {
    const viewport = logViewport.value
    const readingOlder = Boolean(viewport && viewport.scrollTop > 24)
    const previousTop = viewport?.scrollTop || 0
    const previousHeight = viewport?.scrollHeight || 0
    const anchor = readingOlder ? viewport?.firstElementChild : null
    const previousAnchorTop = anchor?.offsetTop
    entries.value = prependLog(entries.value, entry, lineLimit.value)
    await nextTick()
    if (logViewport.value) {
      const anchorOffsetDelta = anchor?.isConnected && Number.isFinite(previousAnchorTop)
        ? anchor.offsetTop - previousAnchorTop
        : undefined
      logViewport.value.scrollTop = preservedScrollTop(
        readingOlder,
        previousTop,
        previousHeight,
        logViewport.value.scrollHeight,
        anchorOffsetDelta,
      )
    }
  },
  onGap: (payload) => {
    if (disposed) return
    const dropped = Number.isFinite(payload?.dropped) ? payload.dropped : 0
    message.warning(dropped > 0 ? `日志更新过快，可能丢失 ${dropped} 行，正在重新获取快照` : '日志更新过快，正在重新获取快照')
    controller.close()
    clearTimeout(reconnectTimer)
    reconnectTimer = setTimeout(reconnect, 500)
  },
})

function reconnect() {
  if (disposed) return
  clearTimeout(reconnectTimer)
  connectionState.value = 'connecting'
  controller.connect(currentFilters())
}
function applyKeyword() {
  keyword.value = keywordDraft.value.trim().slice(0, 100)
  keywordDraft.value = keyword.value
  logger.value = loggerDraft.value.trim().slice(0, 100)
  loggerDraft.value = logger.value
  visibleCount.value = 2000
  reconnect()
}
function loadMore() {
  visibleCount.value = Math.min(entries.value.length, visibleCount.value + 1000)
}
function clearVisibleLogs() {
  entries.value = []
  message.success('已清空当前页面，磁盘日志未删除')
}
function levelClass(level) {
  return `log-level-${String(level || 'info').toLowerCase()}`
}

watch([filterType, lineLimit], reconnect)
onMounted(reconnect)
onBeforeUnmount(() => {
  disposed = true
  clearTimeout(reconnectTimer)
  controller.close()
})
</script>

<template>
  <div class="page-title">
    <div><h1>实时日志</h1><p>查看本程序最近和实时输出，不包含 CMS 自身日志。</p></div>
    <div class="page-actions"><n-tag :type="statusType">{{ statusLabel }}</n-tag></div>
  </div>
  <n-card>
    <div class="log-toolbar">
      <n-select v-model:value="filterType" filterable :input-props="{ 'aria-label': '日志级别' }" :options="filterOptions" style="width: 120px" />
      <n-select v-model:value="lineLimit" filterable :input-props="{ 'aria-label': '日志行数' }" :options="lineOptions" style="width: 120px" />
      <n-input v-model:value="keywordDraft" aria-label="日志关键字" maxlength="100" clearable placeholder="关键字" style="max-width: 280px" @keyup.enter="applyKeyword" />
      <n-input v-model:value="loggerDraft" aria-label="日志来源" maxlength="100" clearable placeholder="来源(如 task_runner)" style="max-width: 220px" @keyup.enter="applyKeyword" />
      <n-button secondary @click="applyKeyword">筛选</n-button>
      <n-button secondary @click="reconnect">重连</n-button>
      <n-button secondary @click="clearVisibleLogs">清空</n-button>
    </div>
    <div ref="logViewport" class="log-viewport" role="log" tabindex="0" aria-label="实时日志输出">
      <pre v-for="entry in visibleEntries" :key="entry.id" class="log-entry" :class="levelClass(entry.level)">{{ entry.text }}</pre>
      <div v-if="!visibleEntries.length" class="log-empty">当前页面暂无日志</div>
    </div>
    <n-space v-if="entries.length > visibleCount" class="log-toolbar">
      <n-button secondary @click="loadMore">加载更早日志（当前 {{ visibleCount }}/{{ entries.length }}）</n-button>
    </n-space>
  </n-card>
</template>
