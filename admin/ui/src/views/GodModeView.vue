<script setup lang="ts">
// GodModeView.vue — God Mode: full agent control + reset
import { ref } from 'vue'
import { useSpiderStore } from '@/stores/spiderStore'

const spider = useSpiderStore()
const confirmReset = ref(false)
const resetDone = ref(false)
const bulkGoals = ref('')
const bulkRunning = ref(false)
import { api } from '@/utils/api'

async function doReset() {
  await spider.godReset()
  confirmReset.value = false
  resetDone.value = true
  setTimeout(() => { resetDone.value = false }, 3000)
}

async function runBulk() {
  const goals = bulkGoals.value.split('\n').map(g => g.trim()).filter(Boolean)
  if (!goals.length) return
  bulkRunning.value = true
  for (const goal of goals) {
    await api.post('/api/v2/task/run', { goal })
    await new Promise(r => setTimeout(r, 500))
  }
  bulkRunning.value = false
  bulkGoals.value = ''
}
</script>

<template>
  <div class="god-view">
    <div class="view-header">
      <h2>⚡ God Mode</h2>
      <span class="badge badge-spider">Full Control</span>
    </div>

    <div class="god-grid">
      <!-- Bulk task runner -->
      <div class="panel glass">
        <div class="panel-title">🚀 Bulk Task Runner</div>
        <p class="hint">Mỗi dòng = 1 tác vụ. Agent sẽ chạy tuần tự.</p>
        <textarea
          v-model="bulkGoals"
          class="spider-input bulk-input"
          placeholder="mở chrome profile 00Fujin&#10;tổng hợp dữ liệu tồn kho&#10;gửi báo cáo qua telegram"
          rows="6"
        ></textarea>
        <button
          class="btn-spider"
          :disabled="bulkRunning || !bulkGoals.trim()"
          @click="runBulk"
        >{{ bulkRunning ? `⏳ Đang chạy...` : '▶ Chạy Tất Cả' }}</button>
      </div>

      <!-- System reset -->
      <div class="panel glass">
        <div class="panel-title">🔄 System Reset</div>
        <div class="reset-warn">
          ⚠️ Reset sẽ xoá: Task Memory, Shadow Buffer, Task History.<br>
          Không xoá: Skill Cache, Workflow Library, Failure Memory.
        </div>
        <div v-if="resetDone" class="reset-done">✅ Reset hoàn thành</div>
        <div v-if="!confirmReset">
          <button class="btn-danger" @click="confirmReset = true">⚡ God Reset</button>
        </div>
        <div v-else class="confirm-row">
          <p>Xác nhận reset?</p>
          <button class="btn-danger" @click="doReset">✅ Xác nhận</button>
          <button class="btn-ghost" @click="confirmReset = false">Huỷ</button>
        </div>
      </div>
    </div>

    <!-- Quick shortcuts -->
    <div class="panel glass">
      <div class="panel-title">⌨️ Quick Commands</div>
      <div class="quick-grid">
        <button v-for="cmd in [
          'mở chrome profile 00Fujin',
          'kiểm tra tài nguyên hệ thống',
          'liệt kê tất cả skills đã học',
          'tóm tắt workflow library',
          'xem failure memory gần đây',
          'chạy self-healing check',
        ]" :key="cmd" class="quick-cmd" @click="spider.taskGoal = cmd">
          {{ cmd }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.god-view { display: flex; flex-direction: column; gap: 20px; }
.view-header { display: flex; align-items: center; gap: 12px; }
.view-header h2 { font-size: 20px; font-weight: 700; }
.god-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
.panel { padding: 20px; display: flex; flex-direction: column; gap: 14px; }
.panel-title { font-size: 14px; font-weight: 600; color: var(--text-muted); margin-bottom: 2px; }
.hint { font-size: 12px; color: var(--text-muted); }
.bulk-input { resize: vertical; font-family: monospace; font-size: 13px; }
.reset-warn { font-size: 13px; color: var(--text-muted); line-height: 1.6; }
.reset-done { font-size: 13px; color: var(--success); }
.confirm-row { display: flex; align-items: center; gap: 10px; }
.confirm-row p { font-size: 13px; color: var(--accent-spider); flex: 1; }
.quick-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.quick-cmd {
  padding: 10px 14px;
  background: rgba(255,255,255,0.04);
  border: 1px solid var(--border-glass);
  border-radius: var(--radius-sm);
  color: var(--text-muted); font-size: 12px;
  cursor: pointer; text-align: left; transition: all 0.15s;
}
.quick-cmd:hover { background: rgba(245,197,24,0.08); color: var(--accent-spider); border-color: rgba(245,197,24,0.3); }
</style>
