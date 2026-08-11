<script setup>
import { onMounted, ref } from 'vue'
import { NButton, NCard, NStatistic, NSpace, useMessage } from 'naive-ui'
import { api } from '../api'

const message = useMessage()
const data = ref(null)
const loading = ref(false)
const error = ref('')

async function load(refresh = false) {
  loading.value = true
  try {
    data.value = await api.embyDashboard(refresh)
    error.value = ''
  } catch (err) {
    error.value = err.message
    data.value = null
  } finally {
    loading.value = false
  }
}
onMounted(() => load())

const statCards = [
  { key: 'movie_count', label: '电影' },
  { key: 'series_count', label: '剧集' },
  { key: 'episode_count', label: '总集数' },
  { key: 'library_count', label: '媒体库' },
]

const posterFailed = ref(new Set())
function markPosterFailed(key) {
  posterFailed.value = new Set(posterFailed.value).add(key)
}
function detailUrl(item) {
  if (!data.value?.emby_base || !item?.id) return ''
  return `${data.value.emby_base}/web/#/details?id=${item.id}`
}
function formatCount(value) {
  return Number(value ?? 0).toLocaleString()
}
function yearText(item) {
  return item?.year ? String(item.year) : ''
}
function ratingText(item) {
  return item?.rating ? item.rating.toFixed(1) : ''
}
</script>

<template>
  <div class="page-title">
    <div><h1>Emby 看板</h1><p>媒体库概览、最近入库与各库规模。</p></div>
    <n-space>
      <n-button secondary :loading="loading" @click="load(true)">刷新</n-button>
    </n-space>
  </div>

  <n-card v-if="error" type="error" class="section-card">{{ error }}</n-card>

  <template v-else-if="data">
    <n-card v-if="!data.available" class="section-card">
      <div class="media-empty">
        <template v-if="data.reason === 'emby_not_configured'">未配置 Emby：请在 .env 中设置 <code>EMBY_BASE_URL</code> 与 <code>EMBY_API_KEY</code> 后重启。</template>
        <template v-else>Emby 不可达，请检查 Emby 服务状态。</template>
      </div>
    </n-card>

    <template v-else>
      <div class="metric-grid">
        <n-card v-for="card in statCards" :key="card.key">
          <n-statistic :label="card.label" :value="formatCount(data.stats?.[card.key])" />
        </n-card>
      </div>

      <n-card title="我的媒体库" class="section-card">
        <div v-if="data.libraries?.length" class="media-wall">
          <a
            v-for="lib in data.libraries"
            :key="lib.name"
            class="media-card"
            :href="data.emby_base || undefined"
            target="_blank"
            rel="noopener"
          >
            <div class="media-poster">
              <img
                v-if="lib.poster_url && !posterFailed.has('lib:' + lib.name)"
                :src="lib.poster_url"
                alt=""
                loading="lazy"
                @error="markPosterFailed('lib:' + lib.name)"
              >
              <div v-else class="media-poster-fallback">{{ (lib.name || '?').slice(0, 2) }}</div>
            </div>
            <span class="media-card-title">{{ lib.name }}</span>
            <span class="media-card-sub">{{ formatCount(lib.count) }} 部</span>
          </a>
        </div>
        <div v-else class="media-empty">暂无媒体库数据。</div>
      </n-card>

      <n-card title="最近入库" class="section-card">
        <div v-if="data.recent?.length" class="media-wall">
          <a
            v-for="item in data.recent"
            :key="item.id"
            class="media-card"
            :href="detailUrl(item)"
            target="_blank"
            rel="noopener"
          >
            <div class="media-poster">
              <img
                v-if="item.poster_url && !posterFailed.has('item:' + item.id)"
                :src="item.poster_url"
                alt=""
                loading="lazy"
                @error="markPosterFailed('item:' + item.id)"
              >
              <div v-else class="media-poster-fallback">{{ (item.name || '?').slice(0, 2) }}</div>
              <span v-if="ratingText(item)" class="media-score">{{ ratingText(item) }}</span>
            </div>
            <span class="media-card-title">{{ item.name }}</span>
            <span class="media-card-sub">
              <template v-if="yearText(item)">{{ yearText(item) }}</template>
              <template v-if="item.genres?.length"> · {{ item.genres.join(' / ') }}</template>
            </span>
          </a>
        </div>
        <div v-else class="media-empty">暂无最近入库条目。</div>
      </n-card>
    </template>
  </template>

  <n-card v-else class="section-card">
    <div class="media-wall">
      <div v-for="index in 6" :key="index" class="media-skeleton">
        <div class="media-skeleton-poster" />
        <div class="media-skeleton-line short" />
        <div class="media-skeleton-line" />
      </div>
    </div>
  </n-card>
</template>
