<script setup>
import { onMounted, ref } from 'vue'
import { NButton, NCard, NForm, NFormItem, NInput, NInputNumber, NModal, NPopconfirm, NSelect, NSwitch, NText, useMessage } from 'naive-ui'
import { api } from '../api'

const message = useMessage()
const loading = ref(false)
const saving = ref(false)
const settings = ref(null)
const mode = ref('shared')
const receiveCode = ref('')
const receiveCid = ref('')
const reviewMode = ref('env')
const savingReview = ref(false)
const cms = ref(null)
const cmsSaving = ref(false)
const embyBaseUrl = ref('')
const embyApiKey = ref('')
const tmdbApiKey = ref('')
const tmdbBearerToken = ref('')
const savingEmby = ref(false)
const savingTmdb = ref(false)
const reviewOffPrompt = ref(false)

async function load() {
  loading.value = true
  try {
    settings.value = await api.settings()
    cms.value = await api.cmsVersion()
    cms.value.interval_minutes = Number.isFinite(Number(cms.value.interval_seconds))
      ? Math.round(cms.value.interval_seconds / 60)
      : 1440
    mode.value = settings.value.strm_default_mode
    reviewMode.value = settings.value.self_share_review.mode
    receiveCid.value = ''
  } catch (err) { message.error(err.message) } finally { loading.value = false }
}

async function saveMode(value) {
  saving.value = true
  try {
    await api.setDefaultMode(value)
    mode.value = value
    settings.value.strm_default_mode = value
    message.success('默认 STRM 模式已保存')
  } catch (err) { message.error(err.message) } finally { saving.value = false }
}

async function saveReceiveCode() {
  try {
    const result = await api.setOwnShareReceiveCode(receiveCode.value)
    settings.value.own_share_receive_code = result.own_share_receive_code
    receiveCode.value = ''
    message.success('自有分享访问码已保存')
  } catch (err) { message.error(err.message) }
}

async function clearReceiveCode() {
  try {
    const result = await api.clearOwnShareReceiveCode()
    settings.value.own_share_receive_code = result.own_share_receive_code
    receiveCode.value = ''
    message.success('已恢复 CMS 或环境配置')
  } catch (err) { message.error(err.message) }
}

async function saveReceiveCid() {
  try {
    const result = await api.setSelfShareReceiveCid(receiveCid.value)
    settings.value.self_share_receive_cid = result.self_share_receive_cid
    receiveCid.value = ''
    message.success('待整理目录已保存，后续任务立即使用新目录')
  } catch (err) { message.error(err.message) }
}

async function clearReceiveCid() {
  try {
    const result = await api.clearSelfShareReceiveCid()
    settings.value.self_share_receive_cid = result.self_share_receive_cid
    receiveCid.value = ''
    message.success('已恢复环境中的待整理目录')
  } catch (err) { message.error(err.message) }
}

function requestReviewMode(value) {
  if (value === 'off') {
    reviewOffPrompt.value = true
    return
  }
  return saveReviewMode(value)
}

async function confirmReviewOff() {
  reviewOffPrompt.value = false
  await saveReviewMode('off')
}

async function saveReviewMode(value) {
  savingReview.value = true
  try {
    const result = await api.setSelfShareReview(value)
    settings.value.self_share_review = result.self_share_review
    reviewMode.value = result.self_share_review.mode
    message.success('分享审核观察设置已保存')
  } catch (err) { message.error(err.message) } finally { savingReview.value = false }
}

async function saveEmbyCredentials() {
  const payload = {}
  if (embyBaseUrl.value.trim()) payload.base_url = embyBaseUrl.value.trim()
  if (embyApiKey.value.trim()) payload.api_key = embyApiKey.value.trim()
  if (!payload.base_url && !payload.api_key) {
    message.error('请至少填写 Emby 地址或 API Key 一项')
    return
  }
  savingEmby.value = true
  try {
    const result = await api.setEmbyCredentials(payload)
    settings.value.emby_credentials = result.emby_credentials
    embyBaseUrl.value = ''
    embyApiKey.value = ''
    message.success('Emby 凭据已保存并立即生效')
  } catch (err) { message.error(err.message) } finally { savingEmby.value = false }
}

async function clearEmbyCredentials() {
  try {
    const result = await api.clearEmbyCredentials()
    settings.value.emby_credentials = result.emby_credentials
    message.success('已恢复环境配置中的 Emby 凭据')
  } catch (err) { message.error(err.message) }
}

async function saveTmdbCredentials() {
  const payload = {}
  if (tmdbApiKey.value.trim()) payload.api_key = tmdbApiKey.value.trim()
  if (tmdbBearerToken.value.trim()) payload.bearer_token = tmdbBearerToken.value.trim()
  if (!payload.api_key && !payload.bearer_token) {
    message.error('请至少填写 TMDB API Key 或 Bearer Token 一项')
    return
  }
  savingTmdb.value = true
  try {
    const result = await api.setTmdbCredentials(payload)
    settings.value.tmdb_credentials = result.tmdb_credentials
    tmdbApiKey.value = ''
    tmdbBearerToken.value = ''
    message.success('TMDB 凭据已保存并立即生效')
  } catch (err) { message.error(err.message) } finally { savingTmdb.value = false }
}

async function clearTmdbCredentials() {
  try {
    const result = await api.clearTmdbCredentials()
    settings.value.tmdb_credentials = result.tmdb_credentials
    message.success('已恢复环境配置中的 TMDB 凭据')
  } catch (err) { message.error(err.message) }
}

async function saveCmsVersion() {
  cmsSaving.value = true
  try {
    const minutes = Number(cms.value.interval_minutes)
    const intervalMinutes = Number.isFinite(minutes) && minutes > 0 ? minutes : 1440
    cms.value = await api.saveCmsVersion({
      enabled: cms.value.enabled,
      interval_seconds: Math.round(intervalMinutes * 60),
      image: cms.value.image,
      container: cms.value.container,
      docker_socket: cms.value.docker_socket,
      auto_pull: cms.value.auto_pull,
    })
    cms.value.interval_minutes = Number.isFinite(Number(cms.value.interval_seconds))
      ? Math.round(cms.value.interval_seconds / 60)
      : 1440
    message.success('CMS 版本更新设置已保存')
  } catch (err) { message.error(err.message) } finally { cmsSaving.value = false }
}

async function resetCmsVersion() {
  try {
    cms.value = await api.resetCmsVersion()
    cms.value.interval_minutes = Number.isFinite(Number(cms.value.interval_seconds))
      ? Math.round(cms.value.interval_seconds / 60)
      : 1440
    message.success('已恢复环境默认设置')
  } catch (err) { message.error(err.message) }
}

async function checkCmsVersion() {
  cmsSaving.value = true
  try {
    cms.value = await api.cmsVersionCheck()
    cms.value.interval_minutes = Number.isFinite(Number(cms.value.interval_seconds))
      ? Math.round(cms.value.interval_seconds / 60)
      : 1440
    if (!cms.value.current_version) {
      message.success('未获取到 CMS 本地版本')
    } else if (cms.value.update_ready) {
      message.success(`检测到新版本：${cms.value.current_version}`)
    } else if (cms.value.update_available) {
      message.success(`发现远程新版本：${cms.value.remote_version}（当前 ${cms.value.current_version}）`)
    } else if (cms.value.remote_version) {
      message.success(`当前 ${cms.value.current_version}，远程 ${cms.value.remote_version}，无更新`)
    } else {
      message.success('当前已是最新版本')
    }
  } catch (err) { message.error(err.message) } finally { cmsSaving.value = false }
}

async function pullCmsImage() {
  cmsSaving.value = true
  try {
    cms.value = await api.cmsVersionPull()
    cms.value.interval_minutes = Number.isFinite(Number(cms.value.interval_seconds))
      ? Math.round(cms.value.interval_seconds / 60)
      : 1440
    if (cms.value.pull_result === 'pulled') {
      message.success('镜像已拉取，尚未切换容器')
    } else {
      message.error(`镜像拉取失败：${cms.value.pull_result || '未知错误'}`)
    }
  } catch (err) { message.error(err.message) } finally { cmsSaving.value = false }
}

async function waitForCmsJob() {
  for (let i = 0; i < 60; i++) {
    await new Promise(resolve => setTimeout(resolve, 2000))
    cms.value = await api.cmsVersion()
    cms.value.interval_minutes = Number.isFinite(Number(cms.value.interval_seconds))
      ? Math.round(cms.value.interval_seconds / 60)
      : 1440
    const state = cms.value.background_job?.state
    if (!state || !['queued', 'running'].includes(state)) return
  }
}

async function upgradeCms() {
  cmsSaving.value = true
  try {
    await api.cmsVersionUpgrade()
    message.success('升级已提交')
    await waitForCmsJob()
    if (cms.value.upgrade_status === 'succeeded') {
      message.success(cms.value.message || 'CMS 已升级')
    } else if (cms.value.upgrade_status === 'failed') {
      message.error(cms.value.upgrade_error || cms.value.message || 'CMS 升级失败')
    }
  } catch (err) { message.error(err.message) } finally { cmsSaving.value = false }
}

onMounted(load)
</script>

<template>
  <div class="page-title"><div><h1>设置</h1><p>统一管理新任务的默认 STRM 工作方式。</p></div><div class="page-actions"><n-button secondary :loading="loading" @click="load">刷新</n-button></div></div>
  <n-card v-if="settings" title="STRM 模式" class="section-card">
    <n-form class="settings-form" label-placement="top">
      <n-form-item label="默认 STRM 模式">
        <n-select :value="mode" :options="settings.strm_modes" :loading="saving" @update:value="saveMode" />
      </n-form-item>
      <n-text depth="3" v-if="mode === 'shared'">接收并由 CMS 整理，创建自己的永久分享后生成 STRM；Emby 入库确认后按配置清理源文件。</n-text>
      <n-text depth="3" v-if="mode === 'direct'">由 CMS 普通同步直接生成直链 STRM，不创建 115 分享。</n-text>
      <n-text depth="3" v-if="mode === 'source_shared'">直接使用收到的第三方 115 分享生成 STRM，跳过转存、整理、自有分享和 115 源文件清理。</n-text>
    </n-form>
  </n-card>
  <n-card v-if="settings" title="分享审核观察" class="section-card">
    <n-form class="settings-form" label-placement="top">
      <n-form-item label="观察方式">
        <n-select :value="reviewMode" :options="settings.self_share_review_modes" :loading="savingReview" @update:value="requestReviewMode" />
      </n-form-item>
      <n-text depth="3" v-if="reviewMode === 'ten_minutes'">分享创建后观察 10 分钟并复检一次，通过后清理 115 源文件。</n-text>
      <n-text depth="3" v-if="reviewMode === 'off'">已关闭观察；Emby 入库确认后将直接清理 115 源文件。</n-text>
      <n-text depth="3" v-if="reviewMode === 'env'">当前使用环境配置：{{ Math.round(settings.self_share_review.seconds / 60) }} 分钟。</n-text>
    </n-form>
  </n-card>
  <n-card v-if="settings" title="自有分享访问码" class="section-card">
    <n-form class="settings-form" label-placement="top">
      <n-text depth="3">当前：{{ settings.own_share_receive_code.masked }}（来源：{{ settings.own_share_receive_code.source }}）</n-text>
      <n-form-item label="新访问码">
        <n-input v-model:value="receiveCode" type="password" show-password-on="click" placeholder="例如 1212" />
      </n-form-item>
      <div class="form-actions">
        <n-button type="primary" :disabled="!receiveCode" @click="saveReceiveCode">保存</n-button>
        <n-button secondary @click="clearReceiveCode">使用 CMS 配置</n-button>
      </div>
    </n-form>
  </n-card>
  <n-card v-if="settings" title="待整理目录" class="section-card">
    <n-form class="settings-form" label-placement="top">
      <n-text depth="3">当前：{{ settings.self_share_receive_cid.masked || '未配置' }}（来源：{{ settings.self_share_receive_cid.source }}）</n-text>
      <n-form-item label="待整理目录 CID">
        <n-input v-model:value="receiveCid" placeholder="例如 3481694068122059860" />
      </n-form-item>
      <div class="form-actions">
        <n-button type="primary" :disabled="!receiveCid" @click="saveReceiveCid">保存</n-button>
        <n-button secondary @click="clearReceiveCid">使用环境配置</n-button>
      </div>
      <n-text depth="3">用于 115 转存和云下载的目标目录。Web 保存值写入 TaskStore，重启后仍保留。</n-text>
    </n-form>
  </n-card>
  <n-card v-if="settings" title="Emby 凭据" class="section-card">
    <n-form class="settings-form" label-placement="top">
      <n-text depth="3">
        当前：{{ settings.emby_credentials?.base_url || '未配置' }} · API Key {{ settings.emby_credentials?.api_key || '未配置' }}
        （来源：{{ settings.emby_credentials?.source || 'unset' }}）
      </n-text>
      <n-form-item label="Emby 地址">
        <n-input v-model:value="embyBaseUrl" placeholder="例如 http://192.168.5.28:9096" />
      </n-form-item>
      <n-form-item label="Emby API Key">
        <n-input v-model:value="embyApiKey" type="password" show-password-on="click" placeholder="Emby API Key" />
      </n-form-item>
      <div class="form-actions">
        <n-button type="primary" :loading="savingEmby" :disabled="!embyBaseUrl && !embyApiKey" @click="saveEmbyCredentials">保存</n-button>
        <n-button secondary @click="clearEmbyCredentials">恢复环境配置</n-button>
      </div>
      <n-text depth="3">保存后立即生效（Emby 看板、入库确认与刷新）；只填一项时仅更新该项。留空保存不会覆盖已有值。</n-text>
    </n-form>
  </n-card>
  <n-card v-if="settings" title="TMDB 凭据" class="section-card">
    <n-form class="settings-form" label-placement="top">
      <n-text depth="3">
        当前：API Key {{ settings.tmdb_credentials?.api_key || '未配置' }} · Bearer {{ settings.tmdb_credentials?.bearer_token || '未配置' }}
        （来源：{{ settings.tmdb_credentials?.source || 'unset' }}）
      </n-text>
      <n-form-item label="TMDB API Key">
        <n-input v-model:value="tmdbApiKey" type="password" show-password-on="click" placeholder="TMDB API Key（v3）" />
      </n-form-item>
      <n-form-item label="TMDB Bearer Token">
        <n-input v-model:value="tmdbBearerToken" type="password" show-password-on="click" placeholder="可选" />
      </n-form-item>
      <div class="form-actions">
        <n-button type="primary" :loading="savingTmdb" :disabled="!tmdbApiKey && !tmdbBearerToken" @click="saveTmdbCredentials">保存</n-button>
        <n-button secondary @click="clearTmdbCredentials">恢复环境配置</n-button>
      </div>
      <n-text depth="3">用于媒体元数据刮削（海报、评分、简介）。保存后立即生效；留空保存不会覆盖已有值。</n-text>
    </n-form>
  </n-card>
  <n-card v-if="cms" title="CMS 版本更新" class="section-card">
    <n-form class="settings-form" label-placement="top">
      <n-form-item label="启用检测">
        <n-switch v-model:value="cms.enabled" />
      </n-form-item>
      <n-form-item label="检查频率（分钟）">
        <n-input-number v-model:value="cms.interval_minutes" :min="5" style="width: 160px" />
      </n-form-item>
      <n-form-item label="更新镜像">
        <n-input v-model:value="cms.image" placeholder="例如 imaliang/cloud-media-sync:latest" />
      </n-form-item>
      <n-form-item label="容器名">
        <n-input v-model:value="cms.container" placeholder="例如 cloud-media-sync" />
      </n-form-item>
      <n-form-item label="Docker Socket">
        <n-input v-model:value="cms.docker_socket" placeholder="/var/run/docker.sock" />
      </n-form-item>
      <n-form-item label="自动拉取镜像">
        <n-switch v-model:value="cms.auto_pull" />
      </n-form-item>
      <n-text depth="3">当前版本：{{ cms.current_version || '未知' }}；远程最新：{{ cms.remote_version || '未知' }}；上次检测：{{ cms.last_seen_version || '-' }}</n-text>
      <n-text depth="3" v-if="cms.update_available">发现远程新版本 {{ cms.remote_version }}（当前 {{ cms.current_version }}）。点「升级」会拉取镜像、重启 CMS 并校验 STRM 守卫，失败自动回滚。</n-text>
      <n-text depth="3" v-else-if="cms.remote_version">当前已是远程最新版本。</n-text>
      <n-text depth="3" v-else>{{ cms.message || '未运行检测' }}</n-text>
      <n-text depth="3" v-if="cms.upgrade_status">上次升级：{{ cms.upgrade_status }}{{ cms.upgrade_error ? ' · ' + cms.upgrade_error : '' }}</n-text>
      <div class="form-actions">
        <n-button type="primary" :loading="cmsSaving" @click="saveCmsVersion">保存</n-button>
        <n-button secondary @click="checkCmsVersion">立即检查</n-button>
        <n-popconfirm v-if="cms.update_available" @positive-click="upgradeCms">
          <template #trigger>
            <n-button type="primary" :loading="cmsSaving">升级</n-button>
          </template>
          将拉取新镜像并重启 CMS 容器，入库会短暂中断。确定升级？
        </n-popconfirm>
        <n-button v-if="cms.update_available" secondary :loading="cmsSaving" @click="pullCmsImage">拉取镜像</n-button>
        <n-button secondary @click="resetCmsVersion">恢复环境默认</n-button>
      </div>
    </n-form>
  </n-card>
  <n-modal
    v-model:show="reviewOffPrompt"
    preset="dialog"
    title="关闭分享审核观察"
    positive-text="关闭观察"
    negative-text="取消"
    @positive-click="confirmReviewOff"
  >
    关闭后，Emby 入库确认完成即清理 115 源文件。确定关闭？
  </n-modal>
</template>
