<script setup>
import { onMounted, ref } from 'vue'
import { NButton, NCard, NInput, NSelect, NSpace, NSwitch, NText, useMessage } from 'naive-ui'
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

async function load() {
  loading.value = true
  try {
    settings.value = await api.settings()
    cms.value = await api.cmsVersion()
    cms.value.interval_minutes = Math.round(cms.value.interval_seconds / 60)
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

async function saveReviewMode(value) {
  if (value === 'off' && !window.confirm('关闭后，Emby 入库确认完成即清理 115 源文件。确定关闭分享审核观察？')) return
  savingReview.value = true
  try {
    const result = await api.setSelfShareReview(value)
    settings.value.self_share_review = result.self_share_review
    reviewMode.value = result.self_share_review.mode
    message.success('分享审核观察设置已保存')
  } catch (err) { message.error(err.message) } finally { savingReview.value = false }
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
    cms.value.interval_minutes = Math.round(cms.value.interval_seconds / 60)
    message.success('CMS 版本更新设置已保存')
  } catch (err) { message.error(err.message) } finally { cmsSaving.value = false }
}

async function resetCmsVersion() {
  try {
    cms.value = await api.resetCmsVersion()
    cms.value.interval_minutes = Math.round(cms.value.interval_seconds / 60)
    message.success('已恢复环境默认设置')
  } catch (err) { message.error(err.message) }
}

async function checkCmsVersion() {
  cmsSaving.value = true
  try {
    cms.value = await api.cmsVersionCheck()
    cms.value.interval_minutes = Math.round(cms.value.interval_seconds / 60)
    if (!cms.value.current_version) {
      message.success('未获取到 CMS 版本')
    } else if (cms.value.update_ready) {
      message.success(`检测到新版本：${cms.value.current_version}`)
    } else {
      message.success('当前已是最新版本')
    }
  } catch (err) { message.error(err.message) } finally { cmsSaving.value = false }
}

onMounted(load)
</script>

<template>
  <div class="page-title"><div><h1>设置</h1><p>统一管理新任务的默认 STRM 工作方式。</p></div><n-button secondary :loading="loading" @click="load">刷新</n-button></div>
  <n-card v-if="settings" title="STRM 模式" class="section-card">
    <n-space vertical :size="16">
      <n-select style="max-width: 320px" :value="mode" :options="settings.strm_modes" :loading="saving" @update:value="saveMode" />
      <n-text depth="3" v-if="mode === 'shared'">接收并由 CMS 整理，创建自己的永久分享后生成 STRM；Emby 入库确认后按配置清理源文件。</n-text>
      <n-text depth="3" v-if="mode === 'direct'">由 CMS 普通同步直接生成直链 STRM，不创建 115 分享。</n-text>
      <n-text depth="3" v-if="mode === 'source_shared'">直接使用收到的第三方 115 分享生成 STRM，跳过转存、整理、自有分享和 115 源文件清理。</n-text>
    </n-space>
  </n-card>
  <n-card v-if="settings" title="分享审核观察" class="section-card">
    <n-space vertical :size="12">
      <n-select style="max-width: 320px" :value="reviewMode" :options="settings.self_share_review_modes" :loading="savingReview" @update:value="saveReviewMode" />
      <n-text depth="3" v-if="reviewMode === 'ten_minutes'">分享创建后观察 10 分钟并复检一次，通过后清理 115 源文件。</n-text>
      <n-text depth="3" v-if="reviewMode === 'off'">已关闭观察；Emby 入库确认后将直接清理 115 源文件。</n-text>
      <n-text depth="3" v-if="reviewMode === 'env'">当前使用环境配置：{{ Math.round(settings.self_share_review.seconds / 60) }} 分钟。</n-text>
    </n-space>
  </n-card>
  <n-card v-if="settings" title="自有分享访问码" class="section-card">
    <n-space vertical :size="12">
      <n-text depth="3">当前：{{ settings.own_share_receive_code.masked }}（来源：{{ settings.own_share_receive_code.source }}）</n-text>
      <n-space>
        <n-input v-model:value="receiveCode" type="password" show-password-on="click" placeholder="例如 1212" style="width: 180px" />
        <n-button type="primary" :disabled="!receiveCode" @click="saveReceiveCode">保存</n-button>
        <n-button secondary @click="clearReceiveCode">使用 CMS 配置</n-button>
      </n-space>
    </n-space>
  </n-card>
  <n-card v-if="settings" title="待整理目录" class="section-card">
    <n-space vertical :size="12">
      <n-text depth="3">当前：{{ settings.self_share_receive_cid.value || '未配置' }}（来源：{{ settings.self_share_receive_cid.source }}）</n-text>
      <n-space>
        <n-input v-model:value="receiveCid" placeholder="例如 3481694068122059860" style="width: 260px" />
        <n-button type="primary" :disabled="!receiveCid" @click="saveReceiveCid">保存</n-button>
        <n-button secondary @click="clearReceiveCid">使用环境配置</n-button>
      </n-space>
      <n-text depth="3">用于 115 转存和云下载的目标目录。Web 保存值写入 TaskStore，重启后仍保留。</n-text>
    </n-space>
  </n-card>
  <n-card v-if="cms" title="CMS 版本更新" class="section-card">
    <n-space vertical :size="12">
      <n-space align="center"><n-text depth="3">启用检测</n-text><n-switch v-model:value="cms.enabled" /></n-space>
      <n-space align="center"><n-text depth="3">检查频率（分钟）</n-text><n-input v-model:value="cms.interval_minutes" type="number" min="5" style="width: 140px" /></n-space>
      <n-space align="center"><n-text depth="3">更新镜像</n-text><n-input v-model:value="cms.image" placeholder="例如 imaliang/cloud-media-sync:latest" style="width: 320px" /></n-space>
      <n-space align="center"><n-text depth="3">容器名</n-text><n-input v-model:value="cms.container" placeholder="例如 cloud-media-sync" style="width: 200px" /></n-space>
      <n-space align="center"><n-text depth="3">Docker Socket</n-text><n-input v-model:value="cms.docker_socket" placeholder="/var/run/docker.sock" style="width: 260px" /></n-space>
      <n-space align="center"><n-text depth="3">自动拉取镜像</n-text><n-switch v-model:value="cms.auto_pull" /></n-space>
      <n-text depth="3">当前版本：{{ cms.current_version || '未知' }}；上次检测：{{ cms.last_seen_version || '-' }}；{{ cms.message || '未运行检测' }}</n-text>
      <n-space>
        <n-button type="primary" :loading="cmsSaving" @click="saveCmsVersion">保存</n-button>
        <n-button secondary @click="checkCmsVersion">立即检查</n-button>
        <n-button secondary @click="resetCmsVersion">恢复环境默认</n-button>
      </n-space>
    </n-space>
  </n-card>
</template>
