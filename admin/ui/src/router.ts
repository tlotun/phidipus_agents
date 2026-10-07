// router.ts — SpiderHub Vue Router (14 routes)
import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  { path: '/',             component: () => import('@/views/HomeView.vue'),      meta: { tab: 1, icon: '🏠', label: 'Dashboard' } },
  { path: '/chrome',       component: () => import('@/views/ChromeView.vue'),    meta: { tab: 2, icon: '🌐', label: 'Chrome Swarm' } },
  { path: '/skill-forge',  component: () => import('@/views/SkillForgeView.vue'),meta: { tab: 3, icon: '⚡', label: 'Skill Forge' } },
  { path: '/ai-lab',       component: () => import('@/views/AILabView.vue'),     meta: { tab: 4, icon: '🧪', label: 'AI Lab' } },
  { path: '/shadow',       component: () => import('@/views/ShadowModeView.vue'),meta: { tab: 5, icon: '👁️', label: 'Shadow Mode' } },
  { path: '/task-graph',   component: () => import('@/views/TaskGraphView.vue'), meta: { tab: 6, icon: '🕸️', label: 'Task Graph' } },
  { path: '/memory',       component: () => import('@/views/MemoryView.vue'),    meta: { tab: 7, icon: '🧠', label: 'Memory' } },
  { path: '/resource',     component: () => import('@/views/ResourceView.vue'),  meta: { tab: 8, icon: '📊', label: 'Resources' } },
  { path: '/security',     component: () => import('@/views/SecurityView.vue'),  meta: { tab: 9, icon: '🔒', label: 'Security' } },
  { path: '/export',       component: () => import('@/views/ExportView.vue'),    meta: { tab: 10, icon: '📦', label: 'Export' } },
  { path: '/god-mode',     component: () => import('@/views/GodModeView.vue'),   meta: { tab: 11, icon: '⚡', label: 'God Mode' } },
  { path: '/forensic',     component: () => import('@/views/ForensicView.vue'),  meta: { tab: 12, icon: '🔬', label: 'Forensic' } },
  { path: '/providers',    component: () => import('@/views/ProvidersView.vue'), meta: { tab: 13, icon: '🔌', label: 'Providers' } },
  { path: '/telegram',     component: () => import('@/views/TelegramView.vue'),  meta: { tab: 14, icon: '🤖', label: 'Telegram' } },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
