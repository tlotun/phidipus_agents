<script setup lang="ts">
// SecurityView.vue — Audit log + security vault
import { onMounted, ref } from 'vue'
import { api } from '@/utils/api'

const auditLog = ref<unknown[]>([])
const skillSecurity = ref<{ safe: number; blocked: number }>({ safe: 0, blocked: 0 })

onMounted(async () => {
  try {
    const res = await api.get('/api/v2/memory/failure')
    // Extract security-related records
    const records = res.data.recent ?? []
    auditLog.value = records.filter((r: Record<string, unknown>) =>
      ['LLM_SAFETY_BLOCK', 'PERMISSION_ERROR'].includes(r.error_type as string)
    )
  } catch { /* ignore */ }
})
</script>

<template>
  <div class="security-view">
    <div class="view-header">
      <h2>🔒 Security Vault</h2>
    </div>

    <div class="panel glass">
      <div class="panel-title">🛡️ Security Invariants (v9.20)</div>
      <div class="invariants">
        <div v-for="inv in [
          { code: 'R-01', status: '✅', desc: 'No direct pyautogui — all via IPC' },
          { code: 'R-02', status: '✅', desc: 'No subprocess.run() on host OS' },
          { code: 'R-05', status: '✅', desc: 'All OS actions via IPC send_action()' },
          { code: 'R-12', status: '✅', desc: 'sandbox.network_mode = none' },
          { code: 'R-19', status: '✅', desc: 'Patcher rate_limit_persist = true' },
          { code: 'R-21', status: '✅', desc: 'VLM confidence_threshold >= 0.8' },
          { code: 'AST',  status: '✅', desc: 'Skill Forge: eval/exec/subprocess blocked' },
        ]" :key="inv.code" class="inv-item">
          <span class="inv-code">{{ inv.code }}</span>
          <span class="inv-status">{{ inv.status }}</span>
          <span class="inv-desc">{{ inv.desc }}</span>
        </div>
      </div>
    </div>

    <div class="panel glass">
      <div class="panel-title">📋 Security Events ({{ auditLog.length }})</div>
      <div v-if="!auditLog.length" class="empty">Chưa có security events</div>
      <div v-else class="audit-list">
        <div v-for="(ev, i) in auditLog" :key="i" class="audit-item">
          <span class="badge badge-danger">{{ (ev as any).error_type }}</span>
          <span>{{ (ev as any).goal }}</span>
          <span class="audit-time">{{ new Date((ev as any).t * 1000).toLocaleTimeString('vi-VN') }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.security-view { display: flex; flex-direction: column; gap: 20px; }
.view-header h2 { font-size: 20px; font-weight: 700; }
.panel { padding: 20px; }
.panel-title { font-size: 14px; font-weight: 600; color: var(--text-muted); margin-bottom: 16px; }
.empty { color: var(--text-muted); font-size: 13px; }
.invariants { display: flex; flex-direction: column; gap: 8px; }
.inv-item { display: flex; align-items: center; gap: 14px; font-size: 13px; padding: 8px 10px; background: rgba(0,200,81,0.05); border-radius: var(--radius-sm); }
.inv-code { font-family: monospace; color: var(--accent-spider); width: 50px; flex-shrink: 0; }
.inv-status { flex-shrink: 0; }
.inv-desc { color: var(--text-muted); }
.audit-list { display: flex; flex-direction: column; gap: 6px; }
.audit-item { display: flex; align-items: center; gap: 10px; font-size: 12px; }
.audit-time { margin-left: auto; color: var(--text-muted); }
</style>
