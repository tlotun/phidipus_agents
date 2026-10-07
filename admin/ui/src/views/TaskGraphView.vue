<script setup lang="ts">
// TaskGraphView.vue — v9.21 Enhanced Task Graph with Recovery Visualization
// Shows: node-based DAG, self-healing branches, dynamic replan paths, live status
import { onMounted, onUnmounted, ref, computed } from 'vue'
import { api } from '@/utils/api'
import { useWsStore } from '@/stores/wsStore'

const ws = useWsStore()
const graphEl = ref<HTMLElement | null>(null)
const graphData = ref<{ nodes: any[]; edges: any[] }>({ nodes: [], edges: [] })
const selectedNode = ref<any>(null)
const taskStats = ref({ total: 0, done: 0, failed: 0, healed: 0, replanned: 0 })
let cy: any = null
let refreshTimer: ReturnType<typeof setInterval>

// Status → color mapping (matches new design system)
const STATUS_COLORS: Record<string, string> = {
  done:      '#10b981',
  running:   '#f59e0b',
  failed:    '#ef4444',
  pending:   '#4b5066',
  skipped:   '#2a2e3a',
  healed:    '#8b5cf6',
  replanned: '#06b6d4',
  // FIX BUG#D: workflow nodes from WorkflowLibrary have type="workflow" and status="workflow"
  // (status field added by API fix). Map both so fallback colour is green not grey.
  workflow:  '#10b981',
  active:    '#10b981',
}

// Status → border glow
const STATUS_GLOW: Record<string, string> = {
  running:   'rgba(245,158,11,0.4)',
  failed:    'rgba(239,68,68,0.4)',
  healed:    'rgba(139,92,246,0.4)',
  replanned: 'rgba(6,182,212,0.4)',
}

async function fetchGraph() {
  try {
    const res = await api.get('/api/v2/task/graph')
    graphData.value = res.data || { nodes: [], edges: [] }
    updateStats()
    await renderGraph()
  } catch { /* ignore */ }
}

function updateStats() {
  // FIX BUG#B: API wraps nodes as {data:{...}}; access .data.status not .status directly
  const nodes = graphData.value.nodes
  const flat = nodes.map((n: any) => n.data ?? n)   // handle both wrapped and legacy flat
  taskStats.value = {
    total:     flat.length,
    done:      flat.filter((n: any) => n.status === 'done').length,
    failed:    flat.filter((n: any) => n.status === 'failed').length,
    healed:    flat.filter((n: any) => n.status === 'healed').length,
    replanned: flat.filter((n: any) => n.status === 'replanned').length,
  }
}

async function renderGraph() {
  if (!graphEl.value || !graphData.value.nodes.length) return

  try {
    // Dynamic import Cytoscape
    const cytoscapeMod = await import('cytoscape')
    const Cytoscape = cytoscapeMod.default

    // Try dagre layout plugin
    try {
      const dagreMod = await import('cytoscape-dagre')
      Cytoscape.use(dagreMod.default)
    } catch { /* already registered or not available */ }

    if (cy) cy.destroy()

    // FIX BUG#A: API returns nodes already in Cytoscape {data:{...}} format.
    // Old code did `{ data: { ...n } }` which double-wrapped as {data:{data:{...}}}.
    // Now pass nodes/edges from API directly — they already have the correct shape.
    cy = Cytoscape({
      container: graphEl.value,
      elements: [
        ...graphData.value.nodes,
        ...graphData.value.edges,
      ],
      style: [
        {
          selector: 'node',
          style: {
            'background-color': (ele: any) => STATUS_COLORS[ele.data('status')] || '#4b5066',
            'label': 'data(label)',
            'color': '#e2e4e9',
            'font-size': '11px',
            'font-family': '"Inter", sans-serif',
            'font-weight': 500,
            'text-valign': 'center',
            'text-halign': 'center',
            'width': 140,
            'height': 44,
            'shape': 'roundrectangle',
            'border-width': (ele: any) => {
              const s = ele.data('status')
              return (s === 'running' || s === 'healed' || s === 'replanned') ? 2 : 1
            },
            'border-color': (ele: any) => {
              const s = ele.data('status')
              return STATUS_GLOW[s] || 'rgba(255,255,255,0.08)'
            },
            'text-wrap': 'ellipsis',
            'text-max-width': '120px',
            'overlay-opacity': 0,
          },
        },
        // Healed nodes get purple border glow
        {
          selector: 'node[status="healed"]',
          style: {
            'border-width': 2,
            'border-color': '#8b5cf6',
            'background-color': '#3b1f6e',
          },
        },
        // Replanned nodes get cyan border
        {
          selector: 'node[status="replanned"]',
          style: {
            'border-width': 2,
            'border-color': '#06b6d4',
            'background-color': '#0c3644',
          },
        },
        // Running nodes pulse
        {
          selector: 'node[status="running"]',
          style: {
            'border-width': 2,
            'border-color': '#f59e0b',
          },
        },
        // Normal edges
        {
          selector: 'edge',
          style: {
            'width': 1.5,
            'line-color': 'rgba(255,255,255,0.12)',
            'target-arrow-color': 'rgba(255,255,255,0.2)',
            'target-arrow-shape': 'triangle',
            'curve-style': 'bezier',
            'arrow-scale': 0.8,
          },
        },
        // Recovery edges (Self-Healing path)
        {
          selector: 'edge[type="recovery"]',
          style: {
            'line-color': '#8b5cf6',
            'target-arrow-color': '#8b5cf6',
            'line-style': 'dashed',
            'width': 1.5,
          },
        },
        // Replan edges
        {
          selector: 'edge[type="replan"]',
          style: {
            'line-color': '#06b6d4',
            'target-arrow-color': '#06b6d4',
            'line-style': 'dashed',
            'width': 1.5,
          },
        },
      ],
      layout: {
        name: 'dagre',
        rankDir: 'LR',
        padding: 30,
        spacingFactor: 1.2,
        nodeSep: 30,
        rankSep: 80,
      } as any,
      userZoomingEnabled: true,
      userPanningEnabled: true,
      boxSelectionEnabled: false,
      minZoom: 0.3,
      maxZoom: 2.5,
    })

    // Click handler — show node details
    cy.on('tap', 'node', (evt: any) => {
      selectedNode.value = evt.target.data()
    })
    cy.on('tap', (evt: any) => {
      if (evt.target === cy) selectedNode.value = null
    })

  } catch (err) {
    console.warn('[TaskGraph] Cytoscape render error:', err)
  }
}

// Computed for progress bar
const progressPct = computed(() => {
  if (!taskStats.value.total) return 0
  return Math.round((taskStats.value.done / taskStats.value.total) * 100)
})

// Workflow Library
const templates = ref<any[]>([])
async function fetchWorkflows() {
  try {
    const res = await api.get('/api/v2/workflow/library')
    templates.value = (res.data?.templates || []).slice(0, 10)
  } catch { /* ignore */ }
}

onMounted(async () => {
  await Promise.all([fetchGraph(), fetchWorkflows()])
  refreshTimer = setInterval(fetchGraph, 4000)
})
onUnmounted(() => {
  clearInterval(refreshTimer)
  if (cy) cy.destroy()
})
</script>

<template>
  <div class="task-view">

    <!-- Header -->
    <div class="view-header">
      <h2>Task Graph</h2>
      <div class="header-badges">
        <span class="badge badge-spider mono">{{ taskStats.total }} nodes</span>
        <span v-if="taskStats.healed" class="badge badge-healed mono">{{ taskStats.healed }} healed</span>
        <span v-if="taskStats.replanned" class="badge badge-replan mono">{{ taskStats.replanned }} replanned</span>
      </div>
      <button class="btn-ghost" @click="fetchGraph">Refresh</button>
    </div>

    <!-- Progress bar -->
    <div v-if="taskStats.total" class="progress-wrap">
      <div class="progress-bar">
        <div class="progress-fill" :style="`width:${progressPct}%`"></div>
      </div>
      <span class="progress-label mono">{{ taskStats.done }}/{{ taskStats.total }} steps ({{ progressPct }}%)</span>
    </div>

    <!-- Graph Canvas -->
    <div class="graph-panel glass">
      <div v-if="!graphData?.nodes?.length" class="graph-empty">
        <div class="empty-icon">&#x25CB;</div>
        <span>No active task graph</span>
        <span class="text-sm text-muted">Graph appears when TaskDecomposer creates a plan.</span>
      </div>
      <div ref="graphEl" class="graph-canvas"></div>
    </div>

    <!-- Legend -->
    <div class="legend glass">
      <span class="legend-item" v-for="(color, status) in STATUS_COLORS" :key="status">
        <span class="legend-dot" :style="`background:${color}`"></span>
        <span class="mono text-xs">{{ status }}</span>
      </span>
    </div>

    <!-- Node Detail Panel (slide-in) -->
    <Transition name="fade">
      <div v-if="selectedNode" class="node-detail glass">
        <div class="detail-header">
          <span class="detail-status-dot" :style="`background:${STATUS_COLORS[selectedNode.status] || '#4b5066'}`"></span>
          <h3>{{ selectedNode.label || selectedNode.id }}</h3>
          <button class="btn-ghost" @click="selectedNode = null">&times;</button>
        </div>
        <div class="detail-grid">
          <div class="detail-row">
            <span class="detail-key">Status</span>
            <span class="badge mono" :class="`badge-${selectedNode.status === 'done' ? 'success' : selectedNode.status === 'failed' ? 'danger' : selectedNode.status === 'healed' ? 'healed' : 'bee'}`">
              {{ selectedNode.status }}
            </span>
          </div>
          <div v-if="selectedNode.action" class="detail-row">
            <span class="detail-key">Action</span>
            <span class="mono text-sm">{{ selectedNode.action }}</span>
          </div>
          <div v-if="selectedNode.duration_ms" class="detail-row">
            <span class="detail-key">Duration</span>
            <span class="mono text-sm">{{ selectedNode.duration_ms }}ms</span>
          </div>
          <div v-if="selectedNode.error" class="detail-row">
            <span class="detail-key">Error</span>
            <span class="text-sm" style="color:var(--danger)">{{ selectedNode.error }}</span>
          </div>
          <div v-if="selectedNode.healing_strategy" class="detail-row">
            <span class="detail-key">Healed by</span>
            <span class="text-sm" style="color:var(--healed)">{{ selectedNode.healing_strategy }}</span>
          </div>
        </div>
      </div>
    </Transition>

    <!-- Workflow Library -->
    <div class="panel glass" v-if="templates.length">
      <div class="panel-title">Workflow Templates</div>
      <table class="data-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Score</th>
            <th>Steps</th>
            <th>Success</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in templates" :key="t.id">
            <td style="color:var(--text-primary)">{{ t.name }}</td>
            <td :style="t.score >= 0.5 ? 'color:var(--success)' : t.score >= 0.2 ? 'color:var(--warning)' : 'color:var(--danger)'">
              {{ t.score?.toFixed(2) }}
            </td>
            <td>{{ t.steps }}</td>
            <td>{{ t.success_count }}/{{ (t.success_count||0) + (t.fail_count||0) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

  </div>
</template>

<style scoped>
.task-view{display:flex;flex-direction:column;gap:16px}
.view-header{display:flex;align-items:center;gap:12px}
.view-header h2{font-size:18px;font-weight:700}
.header-badges{display:flex;gap:8px}

.progress-wrap{display:flex;align-items:center;gap:12px}
.progress-bar{flex:1;height:4px;background:var(--bg-tertiary);border-radius:2px;overflow:hidden}
.progress-fill{height:100%;background:var(--success);border-radius:2px;transition:width .5s}
.progress-label{font-size:11px;color:var(--text-muted);white-space:nowrap}

.graph-panel{padding:0;overflow:hidden;min-height:420px;position:relative}
.graph-canvas{width:100%;height:420px}
.graph-empty{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;color:var(--text-muted)}
.empty-icon{font-size:40px;opacity:.15}

.legend{display:flex;align-items:center;gap:16px;padding:10px 16px;flex-wrap:wrap}
.legend-item{display:flex;align-items:center;gap:5px}
.legend-dot{width:8px;height:8px;border-radius:50%}

.node-detail{position:fixed;right:24px;top:100px;width:320px;padding:20px;z-index:100;box-shadow:var(--shadow-elevated)}
.detail-header{display:flex;align-items:center;gap:10px;margin-bottom:16px}
.detail-header h3{flex:1;font-size:15px;font-weight:600}
.detail-status-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.detail-grid{display:flex;flex-direction:column;gap:10px}
.detail-row{display:flex;justify-content:space-between;align-items:center}
.detail-key{font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.06em}

.panel{padding:20px}
.panel-title{font-size:13px;font-weight:600;color:var(--text-muted);margin-bottom:14px;text-transform:uppercase;letter-spacing:.06em}
</style>
