<script setup lang="ts">
// ExportView.vue — Export/Import config and data
import { ref } from 'vue'
import { api } from '@/utils/api'

const exporting = ref(false)
const exportMsg = ref('')

async function exportData(type: string) {
  exporting.value = true
  exportMsg.value = ''
  try {
    const res = await api.get(`/api/v2/system/stats`)
    const blob = new Blob([JSON.stringify(res.data, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = `phidipus-${type}-${Date.now()}.json`; a.click()
    URL.revokeObjectURL(url)
    exportMsg.value = `✅ Đã export ${type}`
  } finally {
    exporting.value = false
  }
}
</script>

<template>
  <div class="export-view">
    <div class="view-header"><h2>📦 Export / Import</h2></div>
    <div v-if="exportMsg" class="msg glass">{{ exportMsg }}</div>
    <div class="panel glass">
      <div class="panel-title">📤 Export Data</div>
      <div class="export-btns">
        <button class="btn-teal" :disabled="exporting" @click="exportData('stats')">📊 Stats JSON</button>
        <button class="btn-teal" :disabled="exporting" @click="exportData('config')">⚙️ Config</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.export-view { display: flex; flex-direction: column; gap: 20px; }
.view-header h2 { font-size: 20px; font-weight: 700; }
.msg { padding: 12px 18px; font-size: 13px; }
.panel { padding: 20px; }
.panel-title { font-size: 14px; font-weight: 600; color: var(--text-muted); margin-bottom: 14px; }
.export-btns { display: flex; gap: 10px; flex-wrap: wrap; }
</style>
