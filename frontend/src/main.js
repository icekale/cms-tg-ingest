import { createApp } from 'vue'
import { create, NButton, NCard, NConfigProvider, NDataTable, NDescriptions, NDescriptionsItem, NEmpty, NLayout, NLayoutContent, NLayoutHeader, NLayoutSider, NMenu, NMessageProvider, NSpace, NSelect, NStatistic, NTag } from 'naive-ui'
import App from './App.vue'
import router from './router'
import './styles.css'

const naive = create({ components: [NButton, NCard, NConfigProvider, NDataTable, NDescriptions, NDescriptionsItem, NEmpty, NLayout, NLayoutContent, NLayoutHeader, NLayoutSider, NMenu, NMessageProvider, NSpace, NSelect, NStatistic, NTag] })
createApp(App).use(router).use(naive).mount('#app')
