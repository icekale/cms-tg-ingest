import { createRouter, createWebHistory } from 'vue-router'

export default createRouter({
  history: createWebHistory('/app/'),
  routes: [
    { path: '/', redirect: '/overview' },
    { path: '/overview', component: () => import('./views/Overview.vue') },
    { path: '/tasks', component: () => import('./views/Tasks.vue') },
    { path: '/tasks/:id', component: () => import('./views/TaskDetail.vue') },
    { path: '/quality', component: () => import('./views/Quality.vue') },
    { path: '/emby-board', component: () => import('./views/EmbyBoard.vue') },
    { path: '/health', component: () => import('./views/Health.vue') },
    { path: '/hdhive', component: () => import('./views/Hdhive.vue') },
    { path: '/logs', component: () => import('./views/Logs.vue') },
    { path: '/settings', component: () => import('./views/Settings.vue') },
  ],
})
