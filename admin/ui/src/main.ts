// main.ts — Phidipus SpiderHub Vue 3 entry point
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import VueApexCharts from 'vue3-apexcharts'
import router from './router'
import App from './App.vue'
import './assets/main.css'

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(router)
app.use(VueApexCharts)

// Mount Vue app
app.mount('#app')

// Init WebSocket store after mount
import { useWsStore } from '@/stores/wsStore'
import { useSpiderStore } from '@/stores/spiderStore'

const wsStore = useWsStore()
const spiderStore = useSpiderStore()

const WS_URL = import.meta.env.VITE_WS_URL ?? 'ws://127.0.0.1:8912/api/v2/ws'
wsStore.connect(WS_URL)

// Sync WS heartbeat → BeeStore
wsStore.$subscribe((_m, state) => {
  if (state.lastMessage?.event === 'heartbeat') {
    spiderStore.updateResource(state.lastMessage.data as Record<string, unknown>)
  }
  if (state.lastMessage?.event === 'task_done') {
    spiderStore.taskRunning = false
    spiderStore.taskHistory.unshift({
      goal: state.lastMessage.data.goal as string ?? '',
      success: state.lastMessage.data.success as boolean ?? false,
      ts: state.lastMessage.ts,
    })
  }
})

// Hide splash screen
window.__hideSplash?.()
