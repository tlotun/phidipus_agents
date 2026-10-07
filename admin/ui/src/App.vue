<script setup lang="ts">
// App.vue — Main SpiderHub layout (v9.21 Hacker Terminal)
import { ref, computed, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useWsStore } from '@/stores/wsStore'
import { useSpiderStore } from '@/stores/spiderStore'
import { useShadowStore } from '@/stores/shadowStore'
import { useKeyboardShortcuts } from '@/utils/shortcuts'
import { api } from '@/utils/api'

const route = useRoute()
const ws = useWsStore()
const spider = useSpiderStore()
const shadow = useShadowStore()

const sidebarOpen = ref(true)
const showHelp = ref(false)
const showCmdPalette = ref(false)
const cmdQuery = ref('')
const restarting = ref(false)

useKeyboardShortcuts({
  onHelp: () => { showHelp.value = !showHelp.value },
  onCmdK: () => { showCmdPalette.value = !showCmdPalette.value; cmdQuery.value = '' },
  onCmdEnter: () => { /* submit current god button input */ },
  onCmdS: () => { shadow.toggle() },
})

async function restartPhidipus() {
  if (!confirm('Restart Phidipus? Agent sẽ dừng và khởi động lại.')) return
  restarting.value = true
  try {
    await api.post('/api/v2/system/restart')
    // Wait then reload page
    setTimeout(() => { window.location.reload() }, 3000)
  } catch { restarting.value = false }
}

const navItems = [
  { path: '/',            icon: '🏠', label: 'Tổng quan',       key: '1' },
  { path: '/task-graph',  icon: '🕸️', label: 'Sơ đồ tác vụ',   key: '2' },
  { path: '/forensic',    icon: '🔬', label: 'Phân tích',       key: '3' },
  { path: '/skill-forge', icon: '⚡', label: 'Kỹ năng AI',     key: '4' },
  { path: '/shadow',      icon: '👁️', label: 'Chế độ ẩn',      key: '5', highlight: true },
  { path: '/chrome',      icon: '🌐', label: 'Chrome',          key: '6' },
  { path: '/memory',      icon: '🧠', label: 'Bộ nhớ',         key: '7' },
  { path: '/resource',    icon: '📊', label: 'Tài nguyên',     key: '8' },
  { path: '/providers',   icon: '🔌', label: 'Nhà cung cấp',   key: '9' },
  { path: '/telegram',    icon: '🤖', label: 'Telegram',        key: 't' },
  { path: '/ai-lab',      icon: '🧬', label: 'AI Lab',          key: 'a' },
  { path: '/security',    icon: '🔒', label: 'Bảo mật',        key: '' },
  { path: '/god-mode',    icon: '⚡', label: 'Toàn quyền',     key: '0' },
]

const currentLabel = computed(() =>
  navItems.find(n => n.path === route.path)?.label ?? 'SpiderHub'
)
</script>

<template>
  <div class="spiderhub-root" :class="{ 'shadow-active': shadow.enabled }">

    <!-- Grid background via CSS body::before -->
    <div class="grid-bg" aria-hidden="true"></div>

    <!-- Sidebar -->
    <nav class="sidebar" :class="{ collapsed: !sidebarOpen }">
      <div class="sidebar-logo" @click="sidebarOpen = !sidebarOpen">
        <img class="logo-spider" src="/static/logo-phidipus.png" alt="🐝" />
        <span v-if="sidebarOpen" class="logo-text">SpiderHub</span>
      </div>

      <RouterLink
        v-for="item in navItems"
        :key="item.path"
        :to="item.path"
        class="nav-item"
        :class="{ active: route.path === item.path, highlight: item.highlight }"
      >
        <span class="nav-icon">{{ item.icon }}</span>
        <span v-if="sidebarOpen" class="nav-label">{{ item.label }}</span>
        <span v-if="sidebarOpen" class="nav-key">{{ item.key }}</span>
        <!-- Shadow Mode live indicator -->
        <span v-if="item.highlight && shadow.enabled" class="shadow-dot"></span>
      </RouterLink>

      <div class="sidebar-footer">
        <div class="ws-indicator" :class="{ connected: ws.connected }">
          <span class="ws-dot"></span>
          <span v-if="sidebarOpen">{{ ws.connected ? 'Live' : 'Offline' }}</span>
        </div>
      </div>
    </nav>

    <!-- Main content -->
    <div class="main-area">

      <!-- Topbar -->
      <header class="topbar">
        <div class="topbar-left">
          <h1 class="page-title">{{ currentLabel }}</h1>
        </div>

        <div class="topbar-center">
          <!-- God Button -->
          <div class="god-input-wrap">
            <input
              v-model="spider.taskGoal"
              class="spider-input god-input"
              placeholder="Nhập lệnh... (Enter)"
              @keydown.ctrl.enter.prevent="spider.runTask(spider.taskGoal)"
              @keydown.meta.enter.prevent="spider.runTask(spider.taskGoal)"
            />
            <button
              class="btn-spider god-btn"
              :disabled="spider.taskRunning || !spider.taskGoal.trim()"
              @click="spider.runTask(spider.taskGoal)"
            >
              <span v-if="spider.taskRunning">⏳</span>
              <span v-else>▶ Chạy</span>
            </button>
          </div>
        </div>

        <div class="topbar-right">
          <!-- Shadow toggle -->
          <button
            class="shadow-toggle-btn"
            :class="{ active: shadow.enabled }"
            @click="shadow.toggle()"
            title="Toggle Shadow Mode (Cmd+S)"
          >
            <span>👁️</span>
            <span class="shadow-label">{{ shadow.enabled ? 'ON' : 'OFF' }}</span>
          </button>

          <!-- Resource status -->
          <div class="resource-mini" :class="{ warning: spider.ramPct > 70 }">
            <span>RAM {{ spider.ramPct }}%</span>
            <span>CPU {{ spider.cpuPct }}%</span>
          </div>

          <!-- Cmd+K palette button -->
          <button class="btn-ghost" @click="showCmdPalette = true" title="Cmd+K">⌘K</button>

          <!-- Restart button -->
          <button
            class="restart-btn"
            @click="restartPhidipus"
            :disabled="restarting"
            title="Restart Phidipus"
          >
            <span v-if="restarting">🔄 Đang khởi động lại...</span>
            <span v-else>⟳ Khởi động lại</span>
          </button>
        </div>
      </header>

      <!-- Page content -->
      <main class="page-content">
        <RouterView v-slot="{ Component }">
          <Transition name="fade" mode="out-in">
            <component :is="Component" />
          </Transition>
        </RouterView>
      </main>
    </div>

    <!-- Command Palette (Cmd+K) -->
    <Transition name="fade">
      <div v-if="showCmdPalette" class="cmd-overlay" @click.self="showCmdPalette = false">
        <div class="cmd-palette glass">
          <input
            v-model="cmdQuery"
            class="spider-input"
            placeholder="Tìm tab, lệnh, profile..."
            autofocus
          />
          <div class="cmd-items">
            <RouterLink
              v-for="item in navItems.filter(n => !cmdQuery || n.label.toLowerCase().includes(cmdQuery.toLowerCase()))"
              :key="item.path"
              :to="item.path"
              class="cmd-item"
              @click="showCmdPalette = false"
            >
              <span>{{ item.icon }}</span>
              <span>{{ item.label }}</span>
              <span class="cmd-key">{{ item.key }}</span>
            </RouterLink>
          </div>
        </div>
      </div>
    </Transition>

    <!-- Help modal (?) -->
    <Transition name="fade">
      <div v-if="showHelp" class="cmd-overlay" @click.self="showHelp = false">
        <div class="glass help-modal">
          <h3>⌨️ Keyboard Shortcuts</h3>
          <div class="shortcut-grid">
            <span>1–9</span><span>Chuyển tab</span>
            <span>Cmd+K</span><span>Command Palette</span>
            <span>Enter</span><span>Chạy lệnh (God Button)</span>
            <span>Cmd+S</span><span>Toggle Shadow Mode</span>
            <span>?</span><span>Hiện help này</span>
            <span>Esc</span><span>Đóng modal</span>
          </div>
          <button class="btn-spider" @click="showHelp = false">Đóng</button>
        </div>
      </div>
    </Transition>

  </div>
</template>

<style scoped>
.spiderhub-root {
  display: flex;
  height: 100vh;
  overflow: hidden;
  position: relative;
  transition: background 0.3s;
}
.spiderhub-root.shadow-active { background: #090810; }

/* Grid bg (placeholder — actual grid in main.css body::before) */
.grid-bg { position: fixed; inset: 0; pointer-events: none; z-index: 0; }

/* Sidebar */
.sidebar {
  width: 220px;
  flex-shrink: 0;
  background: var(--bg-secondary);
  border-right: 1px solid var(--border-glass);
  display: flex;
  flex-direction: column;
  padding: 16px 12px;
  gap: 2px;
  z-index: 10;
  transition: width 0.25s;
}
.sidebar.collapsed { width: 64px; }

.sidebar-logo {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 6px 16px;
  cursor: pointer;
}
.logo-spider { width: 36px; height: 36px; object-fit: contain; border-radius: 4px; }
.logo-text { font-size: 18px; font-weight: 800; color: var(--accent-spider); }

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 10px;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  text-decoration: none;
  transition: all 0.15s;
  position: relative;
  font-size: 14px;
}
.nav-item:hover { background: rgba(34,197,94,0.05); color: var(--text-primary); }
.nav-item.active {
  background: rgba(34,197,94,0.08);
  color: var(--accent-spider);
  border-left: 3px solid var(--accent-spider);
  text-shadow: 0 0 8px rgba(34,197,94,0.3);
}
.nav-item.highlight.active {
  background: rgba(155,89,182,0.15);
  color: var(--accent-shadow);
  border-left-color: var(--accent-shadow);
}
.nav-icon { font-size: 18px; flex-shrink: 0; }
.nav-label { flex: 1; }
.nav-key {
  font-size: 10px;
  color: rgba(255,255,255,0.2);
  background: rgba(255,255,255,0.05);
  padding: 1px 5px;
  border-radius: 4px;
}
.shadow-dot {
  width: 8px; height: 8px;
  background: var(--accent-shadow);
  border-radius: 50%;
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  animation: blink 1.5s ease-in-out infinite;
}
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.3} }

.sidebar-footer {
  margin-top: auto;
  padding: 8px 6px;
}
.ws-indicator {
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--text-muted);
}
.ws-indicator.connected { color: var(--success); }
.ws-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: currentColor;
}
.ws-indicator.connected .ws-dot { animation: blink 2s ease-in-out infinite; }

/* Topbar */
.main-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  z-index: 1;
}
.topbar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 24px;
  border-bottom: 1px solid var(--border-glass);
  background: var(--bg-secondary);
  flex-shrink: 0;
}
.topbar-left { flex-shrink: 0; }
.page-title { font-size: 16px; font-weight: 700; color: var(--text-primary); }
.topbar-center { flex: 1; max-width: 600px; }
.topbar-right { display: flex; align-items: center; gap: 12px; flex-shrink: 0; }

.god-input-wrap { display: flex; gap: 8px; }
.god-input { flex: 1; }
.god-btn {
  flex-shrink: 0;
  padding: 10px 20px;
}
.god-btn:disabled { opacity: 0.4; cursor: not-allowed; }

.shadow-toggle-btn {
  display: flex; align-items: center; gap: 6px;
  padding: 6px 14px;
  border-radius: var(--radius-sm);
  border: 1px solid rgba(155,89,182,0.3);
  background: rgba(155,89,182,0.1);
  color: var(--accent-shadow);
  cursor: pointer;
  font-size: 13px;
  transition: all 0.2s;
}
.shadow-toggle-btn.active {
  background: rgba(155,89,182,0.25);
  border-color: rgba(155,89,182,0.6);
  box-shadow: 0 0 12px rgba(155,89,182,0.3);
}
.shadow-label { font-weight: 700; font-size: 11px; }

.resource-mini {
  font-size: 11px;
  color: var(--text-muted);
  display: flex; flex-direction: column; gap: 2px;
}
.resource-mini.warning { color: var(--accent-spider); }

.btn-ghost {
  background: none;
  border: 1px solid var(--border-glass);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  padding: 6px 10px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.2s;
}
.btn-ghost:hover { color: var(--text-primary); border-color: rgba(255,255,255,0.2); }

.restart-btn {
  background: rgba(239,68,68,0.08);
  border: 1px solid rgba(239,68,68,0.4);
  border-radius: var(--radius-sm);
  color: #ef4444;
  padding: 6px 14px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
  display: flex;
  align-items: center;
  gap: 4px;
}
.restart-btn:hover { background: rgba(239,68,68,0.15); border-color: rgba(239,68,68,0.7); box-shadow: 0 0 12px rgba(239,68,68,0.2); }
.restart-btn:disabled { opacity: 0.5; cursor: not-allowed; }
@keyframes spin { to { transform: rotate(360deg); } }

/* Page content */
.page-content {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
  position: relative;
}

/* Command palette */
.cmd-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.6);
  backdrop-filter: blur(4px);
  z-index: 9000;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 100px;
}
.cmd-palette {
  width: 560px;
  max-height: 400px;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.cmd-items { display: flex; flex-direction: column; gap: 4px; }
.cmd-item {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  text-decoration: none;
  font-size: 14px;
}
.cmd-item:hover { background: var(--bg-glass); }
.cmd-key {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-muted);
  background: rgba(255,255,255,0.06);
  padding: 2px 6px;
  border-radius: 4px;
}

/* Help modal */
.help-modal {
  padding: 24px;
  width: 400px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.help-modal h3 { color: var(--accent-spider); font-size: 16px; }
.shortcut-grid {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 8px 24px;
  font-size: 13px;
}
.shortcut-grid span:nth-child(odd) {
  font-family: monospace;
  color: var(--accent-spider);
  background: rgba(245,197,24,0.1);
  padding: 2px 8px;
  border-radius: 4px;
}
</style>
