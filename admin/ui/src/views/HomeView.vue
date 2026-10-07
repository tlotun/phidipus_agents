<script setup lang="ts">
// HomeView.vue — Bảng điều khiển — FIX v9.40: Live Terminal thay CPU/RAM
import { onMounted, onUnmounted, ref, nextTick, watch } from 'vue'
import { useWsStore } from '@/stores/wsStore'
import { useSpiderStore } from '@/stores/spiderStore'
import { useShadowStore } from '@/stores/shadowStore'
import { api } from '@/utils/api'

const ws = useWsStore()
const spider = useSpiderStore()
const shadow = useShadowStore()

const screenThumb = ref('')
const thumbLoading = ref(false)

// FIX v9.40: Live terminal — chỉ hiện task execution logs
const terminalLines = ref<{text:string, ts:string, type:string}[]>([])
const terminalEl = ref<HTMLElement | null>(null)
const terminalPaused = ref(false)
const MAX_LINES = 200

async function fetchLogs() {
  try {
    const res = await api.get('/api/v2/system/live-logs?limit=50')
    if (res.data?.logs?.length) {
      for (const log of res.data.logs) {
        addLine(log.text || log.message || '', log.type || 'info', log.ts || '')
      }
    }
  } catch {}
}

function addLine(text: string, type: string = 'info', ts: string = '') {
  if (!text) return
  // Bỏ qua CPU/RAM/heartbeat logs
  const skip = ['ResourceGuard', 'CPU ', 'RAM ', 'heartbeat', 'Disk:', 'VRAM', 'Resource Guard']
  if (skip.some(s => text.includes(s))) return
  if (!ts) {
    const now = new Date()
    ts = now.toLocaleTimeString('vi-VN', {hour:'2-digit',minute:'2-digit',second:'2-digit'})
  }
  const last = terminalLines.value[terminalLines.value.length - 1]
  if (last && last.text === text) return
  terminalLines.value.push({ text, ts, type })
  if (terminalLines.value.length > MAX_LINES) terminalLines.value.splice(0, terminalLines.value.length - MAX_LINES)
  if (!terminalPaused.value) nextTick(() => { if (terminalEl.value) terminalEl.value.scrollTop = terminalEl.value.scrollHeight })
}

function clearTerminal() { terminalLines.value = [] }

watch(() => ws.lastMessage, (msg) => {
  if (!msg) return
  const e = msg.event; const d = msg.data as Record<string, any>
  if (e === 'task_start') addLine(`🎯 Bắt đầu: ${d.goal || ''}`, 'task')
  else if (e === 'task_step') addLine(`  ${d.icon || '▸'} ${d.message || d.step || ''}`, 'step')
  else if (e === 'task_done') { const icon = d.success ? '✅' : '❌'; addLine(`${icon} Hoàn thành: ${d.goal || ''} (${d.steps || 0} bước, ${d.duration_s || 0}s)`, d.success ? 'ok' : 'err') }
  else if (e === 'task_error') addLine(`❌ Lỗi: ${d.error || d.message || ''}`, 'err')
  else if (e === 'skill_forge') addLine(`⚡ SkillForge: ${d.message || d.action || ''}`, 'forge')
  else if (e === 'route') addLine(`🧭 Route: ${d.intent || ''} → ${d.lane || ''} (${d.ms || 0}ms)`, 'route')
  else if (e === 'log' || e === 'terminal_log') { const text = d.message || d.text || ''; if (text && !text.includes('heartbeat')) addLine(text, d.level || 'info') }
})

let thumbInterval: ReturnType<typeof setInterval>
let logInterval: ReturnType<typeof setInterval>

async function refreshThumb() {
  thumbLoading.value = true
  try { const res = await api.get('/api/v2/screen/thumb?width=1280'); if (res.data.ok) screenThumb.value = `data:image/jpeg;base64,${res.data.image}` } finally { thumbLoading.value = false }
}

onMounted(async () => {
  await Promise.all([refreshThumb(), fetchLogs(), shadow.fetchState()])
  thumbInterval = setInterval(refreshThumb, 5000)
  logInterval = setInterval(fetchLogs, 3000)
})
onUnmounted(() => { clearInterval(thumbInterval); clearInterval(logInterval) })
</script>

<template>
  <div class="dashboard">
    <div class="dash-header">
      <h2>🐝 Bảng điều khiển</h2>
      <div class="badges">
        <span class="badge" :class="ws.connected ? 'badge-ok' : 'badge-danger'">{{ ws.connected ? '● Trực tuyến' : '○ Ngoại tuyến' }}</span>
        <span v-if="shadow.enabled" class="badge badge-shadow">👁️ Shadow ON</span>
      </div>
    </div>

    <!-- Live Terminal -->
    <div class="terminal-section">
      <div class="terminal-header">
        <span class="terminal-title"><span class="terminal-dot" :class="ws.connected ? 'dot-live' : ''"></span>Live Terminal<span class="terminal-count">{{ terminalLines.length }} dòng</span></span>
        <div class="terminal-actions">
          <button class="btn-sm" :class="terminalPaused ? 'btn-warn' : ''" @click="terminalPaused = !terminalPaused">{{ terminalPaused ? '▶ Tiếp tục' : '⏸ Dừng' }}</button>
          <button class="btn-sm" @click="clearTerminal">✕ Xóa</button>
        </div>
      </div>
      <div class="terminal-body" ref="terminalEl">
        <div v-if="terminalLines.length === 0" class="terminal-empty">Đang chờ lệnh... Gõ lệnh trên Telegram để xem log tại đây.</div>
        <div v-for="(line, i) in terminalLines" :key="i" class="tl-line" :class="'tl-'+line.type">
          <span class="tl-ts">{{ line.ts }}</span>
          <span class="tl-text">{{ line.text }}</span>
        </div>
      </div>
    </div>

    <!-- 3 compact stats -->
    <div class="stat-row">
      <div class="stat-mini glass"><span class="sm-val">{{ spider.forgeCachedSkills }}</span><span class="sm-label">Kỹ năng</span></div>
      <div class="stat-mini glass"><span class="sm-val">{{ spider.workflowStats.plans }}</span><span class="sm-label">Kế hoạch</span></div>
      <div class="stat-mini glass"><span class="sm-val">{{ shadow.segments.length }}</span><span class="sm-label">Shadow</span></div>
    </div>

    <!-- Screen + History side by side -->
    <div class="bottom-grid">
      <div class="screen-section glass">
        <div class="section-title"><span>🖥️ Live Screen</span><button class="btn-ghost" @click="refreshThumb" :disabled="thumbLoading">{{ thumbLoading ? '...' : '↻' }}</button></div>
        <div class="screen-thumb-wrap">
          <img v-if="screenThumb" :src="screenThumb" class="screen-thumb" alt="screen" />
          <div v-else class="screen-placeholder"><span>{{ thumbLoading ? '📸 Đang chụp...' : '🖥️ Chưa có ảnh' }}</span></div>
        </div>
      </div>
      <div class="history-section glass">
        <div class="section-title">📋 Lịch sử tác vụ</div>
        <div v-if="!spider.taskHistory.length" class="empty-state">Chưa có tác vụ</div>
        <div class="history-list">
          <div v-for="(task, i) in spider.taskHistory.slice(0, 8)" :key="i" class="history-item">
            <span class="hist-icon">{{ task.success ? '✅' : '❌' }}</span>
            <span class="hist-goal">{{ task.goal }}</span>
            <span class="hist-time">{{ new Date(task.ts * 1000).toLocaleTimeString('vi-VN') }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dashboard{display:flex;flex-direction:column;gap:16px;max-width:1200px}
.dash-header{display:flex;align-items:center;gap:16px}.dash-header h2{font-size:20px;font-weight:700}
.badges{display:flex;gap:8px}.badge-ok{color:#22c55e}.badge-danger{color:#ef4444}

/* Terminal */
.terminal-section{background:#0a0a0a;border:1px solid rgba(34,197,94,.25);border-radius:8px;overflow:hidden}
.terminal-header{background:#111;padding:8px 12px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid rgba(34,197,94,.15)}
.terminal-title{font-size:12px;font-weight:600;font-family:var(--font-mono);color:#22c55e;display:flex;align-items:center;gap:8px}
.terminal-dot{width:8px;height:8px;border-radius:50%;background:#52525b}
.dot-live{background:#22c55e;box-shadow:0 0 6px rgba(34,197,94,.6);animation:pulse-dot 2s ease-in-out infinite}
@keyframes pulse-dot{0%,100%{opacity:1}50%{opacity:.5}}
.terminal-count{font-size:9px;color:#52525b;font-weight:400}
.terminal-actions{display:flex;gap:6px}
.btn-sm{background:none;border:1px solid rgba(255,255,255,.1);color:#71717a;font-size:9px;padding:3px 8px;border-radius:3px;cursor:pointer}.btn-sm:hover{color:#a3a3a3;border-color:rgba(255,255,255,.2)}.btn-warn{color:#eab308!important;border-color:rgba(234,179,8,.3)!important}
.terminal-body{height:300px;overflow-y:auto;padding:8px 12px;font-family:var(--font-mono);font-size:11px}
.terminal-empty{color:#3f3f46;font-style:italic;padding:40px 0;text-align:center}
.tl-line{display:flex;gap:8px;line-height:1.7;color:#a3a3a3}
.tl-ts{color:#3f3f46;min-width:60px;flex-shrink:0;font-size:10px}
.tl-text{word-break:break-word}
.tl-task{color:#f5c518!important;font-weight:600}.tl-task .tl-text{color:#f5c518}
.tl-step{color:#71717a}.tl-step .tl-text{color:#a3a3a3;padding-left:8px}
.tl-ok{color:#22c55e;font-weight:500}.tl-ok .tl-text{color:#22c55e}
.tl-err{color:#ef4444}.tl-err .tl-text{color:#ef4444}
.tl-forge{color:#a78bfa}.tl-forge .tl-text{color:#a78bfa}
.tl-route{color:#38bdf8}.tl-route .tl-text{color:#38bdf8}

/* Mini stats */
.stat-row{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.stat-mini{display:flex;align-items:center;gap:8px;padding:10px 14px}
.sm-val{font-size:20px;font-weight:700;color:var(--text-primary)}.sm-label{font-size:11px;color:var(--text-muted)}

/* Bottom grid */
.bottom-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:800px){.bottom-grid{grid-template-columns:1fr}}
.screen-section,.history-section{padding:16px}
.section-title{display:flex;align-items:center;justify-content:space-between;font-size:13px;font-weight:600;margin-bottom:12px;color:var(--text-muted)}
.screen-thumb-wrap{border-radius:6px;overflow:hidden}.screen-thumb{width:100%;height:auto;display:block}
.screen-placeholder{height:180px;display:flex;align-items:center;justify-content:center;background:rgba(255,255,255,.03);border-radius:6px;color:var(--text-muted);font-size:13px}
.empty-state{color:var(--text-muted);font-size:12px;padding:12px 0}
.history-list{display:flex;flex-direction:column;gap:6px}
.history-item{display:flex;align-items:center;gap:10px;padding:8px 10px;font-size:12px;background:var(--bg-tertiary);border-radius:4px}
.hist-icon{flex-shrink:0}.hist-goal{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.hist-time{color:var(--text-muted);font-size:10px;flex-shrink:0}
</style>
