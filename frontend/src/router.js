import { createRouter, createWebHistory } from 'vue-router'
import Overview from './views/Overview.vue'
import Tasks from './views/Tasks.vue'
import TaskDetail from './views/TaskDetail.vue'
import Quality from './views/Quality.vue'
import Health from './views/Health.vue'
import Hdhive from './views/Hdhive.vue'

export default createRouter({
  history: createWebHistory('/app/'),
  routes: [
    { path: '/', redirect: '/overview' },
    { path: '/overview', component: Overview },
    { path: '/tasks', component: Tasks },
    { path: '/tasks/:id', component: TaskDetail },
    { path: '/quality', component: Quality },
    { path: '/health', component: Health },
    { path: '/hdhive', component: Hdhive },
  ],
})
