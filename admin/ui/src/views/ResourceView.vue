<script setup lang="ts">
// ResourceView.vue — Resource monitoring with flame chart
import { onMounted, onUnmounted, ref, computed } from 'vue'
import { useSpiderStore } from '@/stores/spiderStore'
import { api } from '@/utils/api'

const spider = useSpiderStore()
const history = ref<{ ram_pct: number; cpu_pct: number; t: number }[]>([])
let timer: ReturnType<typeof setInterval>

async function fetchHistory() {
  const res = await api.get('/api/v2/resource/history')
  history.value = res.data.history ?? []
}

onMounted(async () => {
  await fetchHistory()
  timer = setInterval(fetchHistory, 3000)
})
onUnmounted(() => clearInterval(timer))

const ramChartOptions = computed(() => ({
  chart: { type: 'area', height: 180, background: 'transparent', toolbar: { show: false }, animations: { enabled: false } },
  theme: { mode: 'dark' },
  colors: ['#f5c518'],
  stroke: { curve: 'smooth', width: 2 },
  fill: { type: 'gradient', gradient: { shadeIntensity: 1, opacityFrom: 0.4, opacityTo: 0.0 } },
  xaxis: { categories: history.value.map(h => new Date(h.t * 1000).toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' })), labels: { rotate: -30, style: { colors: '#666', fontSize: '10px' } } },
  yaxis: { min: 0, max: 100, labels: { style: { colors: '#666' } } },
  grid: { borderColor: 'rgba(255,255,255,0.05)' },
  tooltip: { theme: 'dark' },
}))

const cpuChartOptions = computed(() => ({
  ...ramChartOptions.value,
  colors: ['#00d4aa'],
}))

const ramSeries = computed(() => [{ name: 'RAM %', data: history.value.map(h => h.ram_pct) }])
const cpuSeries = computed(() => [{ name: 'CPU %', data: history.value.map(h => h.cpu_pct) }])
</script>

<template>
  <div class="resource-view">
    <div class="view-header">
      <h2>📊 Resources</h2>
      <span class="badge" :class="spider.resourceHealthy ? 'badge-success' : 'badge-danger'">
        {{ spider.resourceHealthy ? '● Healthy' : '⚠ Under Pressure' }}
      </span>
    </div>

    <!-- Current stats -->
    <div class="stat-grid">
      <div class="stat-card" :class="{ 'stat-warn': spider.ramPct > 70 }">
        <div class="value">{{ spider.ramPct }}<span class="unit">%</span></div>
        <div class="label">RAM Usage</div>
        <div class="gauge"><div class="gauge-fill" :style="`width:${spider.ramPct}%`"></div></div>
      </div>
      <div class="stat-card" :class="{ 'stat-warn': spider.cpuPct > 80 }">
        <div class="value">{{ spider.cpuPct }}<span class="unit">%</span></div>
        <div class="label">CPU Usage</div>
        <div class="gauge"><div class="gauge-fill cpu" :style="`width:${spider.cpuPct}%`"></div></div>
      </div>
      <div class="stat-card">
        <div class="value">{{ spider.diskGb }}<span class="unit">GB</span></div>
        <div class="label">Disk Free</div>
      </div>
    </div>

    <!-- Flame charts -->
    <div class="chart-grid">
      <div class="panel glass">
        <div class="panel-title">🔥 RAM History (5 min)</div>
        <apexchart v-if="history.length >= 3" type="area" height="180" :options="ramChartOptions" :series="ramSeries" />
        <div v-else class="empty">Đang thu thập dữ liệu...</div>
      </div>
      <div class="panel glass">
        <div class="panel-title">🔥 CPU History (5 min)</div>
        <apexchart v-if="history.length >= 3" type="area" height="180" :options="cpuChartOptions" :series="cpuSeries" />
        <div v-else class="empty">Đang thu thập dữ liệu...</div>
      </div>
    </div>

    <!-- Limits reference -->
    <div class="panel glass">
      <div class="panel-title">⚙️ Resource Guard Limits</div>
      <div class="limits-grid">
        <div class="limit-item"><span class="l-key">Max RAM</span><span class="l-val">70%</span><span class="l-desc">→ Pause new tasks</span></div>
        <div class="limit-item"><span class="l-key">Max CPU</span><span class="l-val">80%</span><span class="l-desc">→ Pause new tasks</span></div>
        <div class="limit-item"><span class="l-key">Max Windows</span><span class="l-val">20</span><span class="l-desc">→ Block more opens</span></div>
        <div class="limit-item"><span class="l-key">Chrome Profiles</span><span class="l-val">10</span><span class="l-desc">→ Concurrent limit</span></div>
        <div class="limit-item"><span class="l-key">Min Disk Free</span><span class="l-val">2 GB</span><span class="l-desc">→ Pause tasks</span></div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.resource-view { display: flex; flex-direction: column; gap: 20px; }
.view-header { display: flex; align-items: center; gap: 12px; }
.view-header h2 { font-size: 20px; font-weight: 700; }
.stat-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.stat-card .unit { font-size: 16px; color: var(--text-muted); }
.stat-card.stat-warn .value { color: var(--danger); }
.gauge { height: 4px; background: rgba(255,255,255,0.08); border-radius: 2px; margin-top: 8px; }
.gauge-fill { height: 100%; background: var(--accent-spider); border-radius: 2px; transition: width 0.5s; }
.gauge-fill.cpu { background: var(--accent-hive); }
.chart-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
.panel { padding: 20px; }
.panel-title { font-size: 14px; font-weight: 600; color: var(--text-muted); margin-bottom: 12px; }
.empty { color: var(--text-muted); font-size: 13px; padding: 20px 0; }
.limits-grid { display: flex; flex-direction: column; gap: 8px; }
.limit-item { display: flex; align-items: center; gap: 16px; font-size: 13px; }
.l-key { color: var(--text-muted); width: 130px; flex-shrink: 0; }
.l-val { color: var(--accent-spider); font-weight: 700; width: 60px; }
.l-desc { color: var(--text-muted); font-size: 12px; }
</style>
