<script setup lang="ts">
// SkillForgeView.vue — Skill Forge Radar + Provider Timeline + SAFETY jump highlight
import { onMounted, ref, computed } from 'vue'
import { useSpiderStore } from '@/stores/spiderStore'

const spider = useSpiderStore()
const testGoal = ref('')
const running = ref(false)
import { api } from '@/utils/api'

onMounted(() => {
  spider.fetchForgeRadar()
  spider.fetchSkillRegistry()
})

async function runForge() {
  if (!testGoal.value.trim()) return
  running.value = true
  try {
    await api.post('/api/v2/task/run', { goal: testGoal.value })
  } finally {
    running.value = false
    setTimeout(() => spider.fetchForgeRadar(), 2000)
  }
}

const providerColors: Record<string, string> = {
  gemini: '#4285f4',
  openrouter: '#7c3aed',
  ollama: '#00d4aa',
}

function providerColor(kind: string) {
  return providerColors[kind] ?? '#888'
}

// Radar chart data (ApexCharts)
const radarOptions = computed(() => ({
  chart: { type: 'radar', background: 'transparent', toolbar: { show: false } },
  theme: { mode: 'dark' },
  colors: ['#f5c518'],
  xaxis: { categories: spider.forgeProviders.map(p => p.name.split('/').pop() ?? p.name) },
  yaxis: { show: false, max: 1 },
  fill: { opacity: 0.2 },
  stroke: { width: 2 },
  markers: { size: 4 },
  tooltip: { theme: 'dark' },
}))

const radarSeries = computed(() => [{
  name: 'Success Rate',
  data: spider.forgeProviders.map(p => p.success_rate ?? 1.0),
}])
</script>

<template>
  <div class="forge-view">
    <div class="view-header">
      <h2>⚡ Skill Forge</h2>
      <span class="badge badge-spider">{{ spider.forgeCachedSkills }} cached skills</span>
      <span v-if="spider.hasSafetyEvents" class="badge badge-danger">⚠️ SAFETY events</span>
    </div>

    <div class="forge-grid">

      <!-- Provider stack -->
      <div class="panel glass">
        <div class="panel-title">🔌 Provider Fallback Stack</div>
        <div class="provider-stack">
          <div
            v-for="(p, i) in spider.forgeProviders"
            :key="p.name"
            class="provider-item"
            :style="`border-left-color: ${providerColor(p.kind)}`"
          >
            <div class="p-rank">P{{ i }}</div>
            <div class="p-info">
              <div class="p-name">{{ p.name }}</div>
              <div class="p-meta">
                <span class="badge" :style="`background:${providerColor(p.kind)}22; color:${providerColor(p.kind)}`">
                  {{ p.kind }}
                </span>
                <span class="p-temp">temp={{ p.temperature }}</span>
                <span v-if="!p.has_key && p.kind !== 'ollama'" class="badge badge-danger">⚠ no key</span>
                <span v-else class="badge badge-success">✓ ready</span>
              </div>
            </div>
            <div class="p-rate">{{ Math.round((p.success_rate ?? 1) * 100) }}%</div>
          </div>

          <!-- SAFETY jump annotation -->
          <div v-if="spider.hasSafetyEvents" class="safety-annotation">
            <span class="badge badge-danger">🛡️ SAFETY block detected</span>
            <span class="safety-desc">Gemini → nhảy thẳng OpenRouter Qwen3-480B</span>
          </div>
        </div>
      </div>

      <!-- Radar chart -->
      <div class="panel glass">
        <div class="panel-title">📡 Provider Radar</div>
        <div v-if="spider.forgeProviders.length >= 3">
          <apexchart
            type="radar"
            height="280"
            :options="radarOptions"
            :series="radarSeries"
          />
        </div>
        <div v-else class="empty">Cần ≥3 providers để hiện radar</div>
      </div>

    </div>

    <!-- Skill registry table -->
    <div class="panel glass">
      <div class="panel-title">📚 Skill Registry</div>
      <div v-if="!spider.skillRegistry.length" class="empty">Chưa có skill nào được lưu</div>
      <table v-else class="skill-table">
        <thead>
          <tr>
            <th>Name</th><th>Source</th><th>Reliability</th>
            <th>Uses</th><th>Version</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="skill in spider.skillRegistry" :key="(skill as any).id">
            <td>{{ (skill as any).name }}</td>
            <td><span class="badge badge-teal">{{ (skill as any).source }}</span></td>
            <td>
              <span :class="(skill as any).reliability >= 0.7 ? 'badge-success' : 'badge-danger'" class="badge">
                {{ Math.round((skill as any).reliability * 100) }}%
              </span>
            </td>
            <td>{{ (skill as any).usage_count }}</td>
            <td>v{{ (skill as any).version }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Test forge -->
    <div class="panel glass">
      <div class="panel-title">🧪 Test Skill Forge</div>
      <div class="forge-test-row">
        <input v-model="testGoal" class="spider-input" placeholder="Ví dụ: tổng hợp giá bóng đèn từ file tồn kho" />
        <button class="btn-spider" :disabled="running || !testGoal.trim()" @click="runForge">
          {{ running ? '⏳ Running...' : '▶ Forge' }}
        </button>
      </div>
      <p class="forge-hint">Skill được tạo bởi Gemini → fallback OpenRouter → Ollama nếu lỗi</p>
    </div>

    <!-- SAFETY events log -->
    <div v-if="spider.forgeSafetyEvents.length" class="panel glass">
      <div class="panel-title">🛡️ SAFETY Events (Jump Log)</div>
      <div class="safety-list">
        <div v-for="(ev, i) in spider.forgeSafetyEvents" :key="i" class="safety-event">
          <span class="badge badge-danger">SAFETY</span>
          <span>{{ (ev as any).provider }}</span>
          <span class="ev-goal">{{ (ev as any).goal }}</span>
          <span class="ev-time">{{ new Date((ev as any).t * 1000).toLocaleTimeString('vi-VN') }}</span>
        </div>
      </div>
    </div>

  </div>
</template>

<style scoped>
.forge-view { display: flex; flex-direction: column; gap: 20px; }
.view-header { display: flex; align-items: center; gap: 12px; }
.view-header h2 { font-size: 20px; font-weight: 700; }
.forge-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
.panel { padding: 20px; }
.panel-title { font-size: 14px; font-weight: 600; color: var(--text-muted); margin-bottom: 16px; }
.empty { color: var(--text-muted); font-size: 13px; padding: 20px 0; }

.provider-stack { display: flex; flex-direction: column; gap: 10px; }
.provider-item {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 14px;
  border-left: 3px solid var(--border-glass);
  background: rgba(255,255,255,0.03);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
}
.p-rank { font-size: 11px; font-weight: 800; color: var(--text-muted); width: 22px; flex-shrink: 0; }
.p-info { flex: 1; }
.p-name { font-size: 13px; font-weight: 600; }
.p-meta { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
.p-temp { font-size: 11px; color: var(--text-muted); }
.p-rate { font-size: 18px; font-weight: 800; color: var(--accent-spider); }
.safety-annotation {
  margin-top: 8px;
  padding: 10px 14px;
  background: rgba(255,77,77,0.08);
  border: 1px solid rgba(255,77,77,0.2);
  border-radius: var(--radius-sm);
  display: flex; align-items: center; gap: 10px; font-size: 12px;
}
.safety-desc { color: var(--text-muted); }

.skill-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.skill-table th { color: var(--text-muted); font-size: 11px; text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border-glass); }
.skill-table td { padding: 8px 10px; border-bottom: 1px solid rgba(255,255,255,0.04); }

.forge-test-row { display: flex; gap: 10px; }
.forge-hint { font-size: 11px; color: var(--text-muted); margin-top: 8px; }

.safety-list { display: flex; flex-direction: column; gap: 8px; }
.safety-event { display: flex; align-items: center; gap: 10px; font-size: 12px; }
.ev-goal { flex: 1; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ev-time { color: var(--text-muted); flex-shrink: 0; }
</style>
