<script setup lang="ts">
// AILabView.vue — AI Lab: Train, Evaluate, Deploy AI Models
// Roadmap Item #19: Tab AI Forge trong admin panel Phidipus
import { onMounted, ref, computed, onUnmounted } from 'vue'
import { api } from '@/utils/api'

// ── State ──────────────────────────────────────────────────────
const domains = ref<any[]>([])
const selectedDomain = ref('')
const models = ref<any[]>([])
const pipelineStatus = ref<any>({ running: false, step: '', progress: 0, log: [] })
const evalResults = ref<any>(null)
const brainStatus = ref<any>({ model: '', rule_hits: 0, model_calls: 0, total: 0 })
const brainTestInput = ref('')
const brainTestResult = ref<any>(null)
const brainTesting = ref(false)
const convLogs = ref<any[]>([])
const activeSection = ref('overview')
const loading = ref(false)
const pipelineRunning = ref(false)
const pipelineStep = ref('')
const pipelineFromStep = ref('E1')
const pipelineLog = ref<string[]>([])
let pollTimer: any = null

// ── Lifecycle ──────────────────────────────────────────────────
onMounted(async () => {
  await Promise.all([
    fetchDomains(),
    fetchModels(),
    fetchBrainStatus(),
  ])
  pollTimer = setInterval(() => {
    if (pipelineRunning.value) fetchPipelineStatus()
  }, 3000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})

// ── API Calls ──────────────────────────────────────────────────
async function fetchDomains() {
  try {
    const res = await api.get('/api/v2/ailab/domains')
    domains.value = res.data.domains ?? []
    if (domains.value.length && !selectedDomain.value) {
      selectedDomain.value = domains.value[0].file
    }
  } catch { domains.value = [] }
}

async function fetchModels() {
  try {
    const res = await api.get('/api/v2/ailab/models')
    models.value = res.data.models ?? []
  } catch { models.value = [] }
}

async function fetchBrainStatus() {
  try {
    const res = await api.get('/api/v2/ailab/brain/status')
    brainStatus.value = res.data
  } catch {}
}

async function fetchPipelineStatus() {
  try {
    const res = await api.get('/api/v2/ailab/pipeline/status')
    pipelineStatus.value = res.data
    pipelineRunning.value = res.data.running ?? false
    pipelineStep.value = res.data.step ?? ''
    pipelineLog.value = res.data.log ?? []
    if (!pipelineRunning.value && evalResults.value === null) {
      fetchEvalResults()
    }
  } catch {}
}

async function fetchEvalResults() {
  try {
    const res = await api.get('/api/v2/ailab/eval/results')
    evalResults.value = res.data
  } catch { evalResults.value = null }
}

async function fetchConvLogs() {
  try {
    const res = await api.get('/api/v2/ailab/logs')
    convLogs.value = res.data.logs ?? []
  } catch { convLogs.value = [] }
}

// ── Actions ────────────────────────────────────────────────────
async function runPipeline() {
  if (!selectedDomain.value || pipelineRunning.value) return
  pipelineRunning.value = true
  pipelineLog.value = ['🚀 Starting pipeline...']
  try {
    await api.post('/api/v2/ailab/pipeline/run', {
      domain: selectedDomain.value,
      from_step: pipelineFromStep.value,
    })
  } catch (err: any) {
    pipelineLog.value.push(`❌ Error: ${err.message}`)
    pipelineRunning.value = false
  }
}

async function stopPipeline() {
  try {
    await api.post('/api/v2/ailab/pipeline/stop')
    pipelineRunning.value = false
    pipelineLog.value.push('⏹️ Pipeline stopped')
  } catch {}
}

async function testBrain() {
  if (!brainTestInput.value.trim() || brainTesting.value) return
  brainTesting.value = true
  brainTestResult.value = null
  try {
    const res = await api.post('/api/v2/ailab/brain/test', {
      input: brainTestInput.value,
    })
    brainTestResult.value = res.data
  } catch (err: any) {
    brainTestResult.value = { error: err.message }
  } finally {
    brainTesting.value = false
  }
}

async function deleteModel(name: string) {
  if (!confirm(`Xóa model ${name}?`)) return
  try {
    await api.post('/api/v2/ailab/models/delete', { name })
    await fetchModels()
  } catch {}
}

// ── Computed ───────────────────────────────────────────────────
const currentDomain = computed(() =>
  domains.value.find(d => d.file === selectedDomain.value)
)

const pipelineSteps = [
  { id: 'E1', label: 'Data Engine', icon: '📥', desc: 'YouTube + Whisper + HuggingFace' },
  { id: 'E2', label: 'Insight Extract', icon: '🔍', desc: 'Claude extract situations' },
  { id: 'E2.5', label: 'Quality Gate', icon: '🛡️', desc: 'Filter kém chất lượng' },
  { id: 'E3', label: 'Dataset Gen', icon: '📊', desc: 'ChatML JSONL + augment' },
  { id: 'E4', label: 'Train Model', icon: '🧠', desc: 'MLX LoRA fine-tune' },
  { id: 'E5', label: 'Evaluate', icon: '🧪', desc: 'OOD eval + hallucinate guard' },
  { id: 'E6', label: 'Deploy', icon: '🚀', desc: 'Ollama + RAG server' },
]

const brainModelOnline = computed(() =>
  models.value.some(m => m.name?.includes('phidipus-brain') || m.name?.includes('brain'))
)

const sections = [
  { id: 'overview', label: 'Tổng Quan', icon: '📋' },
  { id: 'pipeline', label: 'Pipeline', icon: '⚡' },
  { id: 'models', label: 'Models', icon: '🤖' },
  { id: 'brain', label: 'Brain P3', icon: '🧠' },
  { id: 'logs', label: 'Chat Logs', icon: '💬' },
]
</script>

<template>
  <div class="ailab-view">

    <!-- Header -->
    <div class="view-header">
      <h2>🧪 AI Lab</h2>
      <span class="badge badge-spider">{{ models.length }} models</span>
      <span class="badge" :class="brainModelOnline ? 'badge-success' : 'badge-danger'">
        {{ brainModelOnline ? '🟢 Brain Online' : '🔴 Brain Offline' }}
      </span>
    </div>

    <!-- Tab Nav -->
    <div class="section-nav">
      <button
        v-for="s in sections" :key="s.id"
        :class="['nav-btn', { active: activeSection === s.id }]"
        @click="activeSection = s.id; if (s.id === 'logs') fetchConvLogs(); if (s.id === 'models') fetchModels()"
      >
        {{ s.icon }} {{ s.label }}
      </button>
    </div>

    <!-- ═══════════ OVERVIEW ═══════════ -->
    <template v-if="activeSection === 'overview'">

      <!-- Domain selector -->
      <div class="panel glass">
        <div class="panel-title">🌐 Domain</div>
        <div class="domain-grid">
          <div
            v-for="d in domains" :key="d.file"
            :class="['domain-card', { selected: selectedDomain === d.file }]"
            @click="selectedDomain = d.file"
          >
            <div class="d-name">{{ d.display_name }}</div>
            <div class="d-meta">
              <span class="badge badge-teal">{{ d.model_size }}</span>
              <span class="text-muted text-xs">{{ d.language }}</span>
              <span class="text-muted text-xs">{{ d.target_samples }} samples</span>
            </div>
            <div class="d-situations text-xs text-muted">
              {{ d.situation_count }} situation types
            </div>
          </div>
          <div v-if="!domains.length" class="empty">
            Chưa có domain config. Tạo file YAML trong domains/
          </div>
        </div>
      </div>

      <!-- Quick Stats -->
      <div class="stats-grid">
        <div class="stat-card glass">
          <div class="stat-number">{{ models.length }}</div>
          <div class="stat-label">Ollama Models</div>
        </div>
        <div class="stat-card glass">
          <div class="stat-number">{{ domains.length }}</div>
          <div class="stat-label">Domains</div>
        </div>
        <div class="stat-card glass">
          <div class="stat-number">{{ brainStatus.total || 0 }}</div>
          <div class="stat-label">Brain Queries</div>
        </div>
        <div class="stat-card glass">
          <div class="stat-number">{{ brainStatus.rule_hits || 0 }}</div>
          <div class="stat-label">Rule Hits</div>
        </div>
      </div>

      <!-- Pipeline overview -->
      <div class="panel glass">
        <div class="panel-title">⚡ Pipeline Steps (E1 → E6)</div>
        <div class="pipeline-overview">
          <div v-for="(step, i) in pipelineSteps" :key="step.id" class="pipe-step">
            <div class="pipe-icon" :class="{
              'step-active': pipelineStep === step.id,
              'step-done': pipelineSteps.findIndex(s => s.id === pipelineStep) > i
            }">
              {{ step.icon }}
            </div>
            <div class="pipe-info">
              <div class="pipe-label">{{ step.id }}: {{ step.label }}</div>
              <div class="pipe-desc text-xs text-muted">{{ step.desc }}</div>
            </div>
            <div v-if="i < pipelineSteps.length - 1" class="pipe-arrow">→</div>
          </div>
        </div>
      </div>

    </template>

    <!-- ═══════════ PIPELINE ═══════════ -->
    <template v-if="activeSection === 'pipeline'">

      <div class="panel glass">
        <div class="panel-title">⚡ Chạy Pipeline</div>

        <div class="pipeline-controls">
          <div class="control-row">
            <label>Domain:</label>
            <select v-model="selectedDomain" class="spider-input">
              <option v-for="d in domains" :key="d.file" :value="d.file">
                {{ d.display_name }} ({{ d.model_size }})
              </option>
            </select>
          </div>

          <div class="control-row">
            <label>Bắt đầu từ:</label>
            <select v-model="pipelineFromStep" class="spider-input">
              <option v-for="s in pipelineSteps" :key="s.id" :value="s.id">
                {{ s.id }}: {{ s.label }}
              </option>
            </select>
          </div>

          <div class="control-row">
            <button
              v-if="!pipelineRunning"
              class="btn-spider"
              :disabled="!selectedDomain"
              @click="runPipeline"
            >
              ▶ Chạy Pipeline
            </button>
            <button v-else class="btn-danger" @click="stopPipeline">
              ⏹ Dừng
            </button>
            <span v-if="pipelineRunning" class="pipeline-badge">
              ⏳ {{ pipelineStep || 'Đang khởi động...' }}
            </span>
          </div>
        </div>
      </div>

      <!-- Pipeline Log -->
      <div class="panel glass">
        <div class="panel-title">📜 Pipeline Log</div>
        <div class="pipeline-log" ref="logContainer">
          <div v-if="!pipelineLog.length" class="empty">
            Chưa có log. Nhấn "Chạy Pipeline" để bắt đầu.
          </div>
          <div v-for="(line, i) in pipelineLog" :key="i" class="log-line mono">
            {{ line }}
          </div>
        </div>
      </div>

      <!-- Eval Results -->
      <div v-if="evalResults" class="panel glass">
        <div class="panel-title">🧪 Kết Quả Evaluation</div>
        <div class="eval-grid">
          <div class="eval-item" v-for="(val, key) in evalResults.metrics" :key="key">
            <div class="eval-metric">{{ key }}</div>
            <div class="eval-value" :class="val >= (evalResults.thresholds?.[key] || 0.7) ? 'eval-pass' : 'eval-fail'">
              {{ typeof val === 'number' ? (val * 100).toFixed(1) + '%' : val }}
            </div>
          </div>
        </div>
        <div v-if="evalResults.summary" class="eval-summary">
          {{ evalResults.summary }}
        </div>
      </div>

    </template>

    <!-- ═══════════ MODELS ═══════════ -->
    <template v-if="activeSection === 'models'">

      <div class="panel glass">
        <div class="panel-title">🤖 Ollama Models</div>
        <div v-if="!models.length" class="empty">
          Chưa có model. Chạy pipeline để train hoặc import model.
        </div>
        <table v-else class="model-table">
          <thead>
            <tr>
              <th>Model</th>
              <th>Size</th>
              <th>Modified</th>
              <th>ID</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="m in models" :key="m.name">
              <td>
                <span class="model-name">{{ m.name }}</span>
                <span v-if="m.name?.includes('brain')" class="badge badge-teal">Brain</span>
              </td>
              <td class="mono text-sm">{{ m.size }}</td>
              <td class="text-muted text-sm">{{ m.modified }}</td>
              <td class="mono text-xs text-muted">{{ m.id?.slice(0, 12) }}</td>
              <td>
                <button class="btn-ghost btn-sm" @click="deleteModel(m.name)">🗑️</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- Model Deploy Info -->
      <div class="panel glass">
        <div class="panel-title">📦 Deploy Commands</div>
        <div class="code-block mono text-sm">
          <div># Import model từ GGUF file:</div>
          <div>cd ~/phidipus-brain-export</div>
          <div>ollama create phidipus-brain-v6 -f Modelfile</div>
          <div></div>
          <div># Test model:</div>
          <div>ollama run phidipus-brain-v6 'Check email đi'</div>
        </div>
      </div>

    </template>

    <!-- ═══════════ BRAIN P3 ═══════════ -->
    <template v-if="activeSection === 'brain'">

      <!-- Brain Status -->
      <div class="panel glass">
        <div class="panel-title">🧠 Brain Pipeline Status</div>
        <div class="brain-stats">
          <div class="brain-stat">
            <div class="bs-label">Model</div>
            <div class="bs-value">{{ brainStatus.model || 'N/A' }}</div>
          </div>
          <div class="brain-stat">
            <div class="bs-label">Total Queries</div>
            <div class="bs-value">{{ brainStatus.total || 0 }}</div>
          </div>
          <div class="brain-stat">
            <div class="bs-label">Rule Hits</div>
            <div class="bs-value accent">{{ brainStatus.rule_hits || 0 }}</div>
          </div>
          <div class="brain-stat">
            <div class="bs-label">Model Calls</div>
            <div class="bs-value">{{ brainStatus.model_calls || 0 }}</div>
          </div>
          <div class="brain-stat">
            <div class="bs-label">Overrides</div>
            <div class="bs-value">{{ brainStatus.overrides || 0 }}</div>
          </div>
          <div class="brain-stat">
            <div class="bs-label">RAG Retries</div>
            <div class="bs-value">{{ brainStatus.rag_retries || 0 }}</div>
          </div>
        </div>
      </div>

      <!-- Architecture -->
      <div class="panel glass">
        <div class="panel-title">🏗️ Pipeline Architecture</div>
        <div class="arch-flow">
          <div class="arch-node node-rule">P0: Rule Layer<br><span class="text-xs">regex/keyword → 0ms</span></div>
          <div class="arch-arrow">→</div>
          <div class="arch-node node-model">P1: Model v6<br><span class="text-xs">Qwen3-4B LoRA</span></div>
          <div class="arch-arrow">→</div>
          <div class="arch-node node-override">P2: Override<br><span class="text-xs">fix mistakes</span></div>
          <div class="arch-arrow">→</div>
          <div class="arch-node node-validate">P3: Validate<br><span class="text-xs">JSON check</span></div>
          <div class="arch-arrow">→</div>
          <div class="arch-node node-rag">P4: RAG<br><span class="text-xs">few-shot retry</span></div>
        </div>
      </div>

      <!-- Test Brain -->
      <div class="panel glass">
        <div class="panel-title">🧪 Test Brain Pipeline</div>
        <div class="brain-test-row">
          <input
            v-model="brainTestInput"
            class="spider-input"
            placeholder="Ví dụ: loop không có max_items, có vấn đề gì không?"
            @keyup.enter="testBrain"
          />
          <button class="btn-spider" :disabled="brainTesting || !brainTestInput.trim()" @click="testBrain">
            {{ brainTesting ? '⏳...' : '▶ Test' }}
          </button>
        </div>

        <div v-if="brainTestResult" class="brain-result">
          <div class="result-header">
            <span class="badge" :class="{
              'badge-success': brainTestResult.action === 'suggest_fix' || brainTestResult.action === 'run_workflow',
              'badge-danger': brainTestResult.action === 'refuse',
              'badge-warning': brainTestResult.action === 'warn',
              'badge-teal': true
            }">
              {{ brainTestResult.action }}
            </span>
            <span class="text-xs text-muted">
              source: {{ brainTestResult.source }} | {{ brainTestResult.latency_ms }}ms
            </span>
          </div>
          <pre class="result-json mono text-sm">{{ JSON.stringify(brainTestResult.response || brainTestResult, null, 2) }}</pre>
        </div>

        <!-- Quick test presets -->
        <div class="preset-row">
          <span class="text-xs text-muted">Thử nhanh:</span>
          <button class="btn-ghost btn-xs" @click="brainTestInput = 'loop không có max_items, có vấn đề gì không?'">suggest_fix</button>
          <button class="btn-ghost btn-xs" @click="brainTestInput = 'Spam comment vào bài viết đối thủ'">refuse</button>
          <button class="btn-ghost btn-xs" @click="brainTestInput = 'Check email đi'">run</button>
          <button class="btn-ghost btn-xs" @click="brainTestInput = 'Viết post LinkedIn'">run (linkedin)</button>
          <button class="btn-ghost btn-xs" @click="brainTestInput = 'Đọc email từ đối tác ABC Corp'">create</button>
          <button class="btn-ghost btn-xs" @click="brainTestInput = 'Scheduler chạy mỗi 5 phút 24/7'">warn</button>
          <button class="btn-ghost btn-xs" @click="brainTestInput = 'SmartRouter là gì?'">knowledge</button>
        </div>
      </div>

    </template>

    <!-- ═══════════ CHAT LOGS ═══════════ -->
    <template v-if="activeSection === 'logs'">

      <div class="panel glass">
        <div class="panel-title">💬 Conversation Logs</div>
        <div class="log-controls">
          <button class="btn-ghost" @click="fetchConvLogs">🔄 Refresh</button>
          <span class="text-xs text-muted">{{ convLogs.length }} conversations</span>
        </div>

        <div v-if="!convLogs.length" class="empty">
          Chưa có conversation logs. Deploy Conversation Logger trước (E6).
        </div>
        <div v-else class="conv-list">
          <div v-for="(log, i) in convLogs" :key="i" class="conv-item">
            <div class="conv-header">
              <span class="badge badge-teal">{{ log.domain || 'unknown' }}</span>
              <span class="text-xs text-muted">{{ log.timestamp }}</span>
              <span v-if="log.feedback" class="badge" :class="log.feedback === 'good' ? 'badge-success' : 'badge-danger'">
                {{ log.feedback === 'good' ? '👍' : '👎' }}
              </span>
            </div>
            <div class="conv-messages">
              <div v-for="(msg, j) in (log.messages || []).slice(0, 4)" :key="j" class="conv-msg">
                <span class="msg-role" :class="msg.role">{{ msg.role === 'user' ? '👤' : '🤖' }}</span>
                <span class="msg-text text-sm">{{ msg.content?.slice(0, 120) }}{{ msg.content?.length > 120 ? '...' : '' }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

    </template>

  </div>
</template>

<style scoped>
.ailab-view { display: flex; flex-direction: column; gap: 20px; }
.view-header { display: flex; align-items: center; gap: 12px; }
.view-header h2 { font-size: 20px; font-weight: 700; }

/* Section nav */
.section-nav { display: flex; gap: 4px; padding: 4px; background: var(--bg-secondary); border-radius: var(--radius); }
.nav-btn {
  padding: 8px 16px; border: none; background: none; color: var(--text-secondary);
  border-radius: var(--radius-sm); cursor: pointer; font-size: 12px; font-weight: 500;
  transition: all .15s;
}
.nav-btn:hover { background: var(--bg-glass-hover); color: var(--text-primary); }
.nav-btn.active { background: var(--bg-glass); color: var(--accent-spider); border: 1px solid var(--border-active); }

/* Panels */
.panel { padding: 20px; }
.panel-title { font-size: 14px; font-weight: 600; color: var(--text-muted); margin-bottom: 16px; }
.empty { color: var(--text-muted); font-size: 13px; padding: 20px 0; text-align: center; }

/* Domain grid */
.domain-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
.domain-card {
  padding: 14px; border: 1px solid var(--border-glass); border-radius: var(--radius);
  cursor: pointer; transition: all .15s; background: rgba(255,255,255,0.02);
}
.domain-card:hover { border-color: var(--border-active); background: var(--bg-glass); }
.domain-card.selected { border-color: var(--accent-spider); background: rgba(34,197,94,0.08); }
.d-name { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.d-meta { display: flex; gap: 8px; align-items: center; margin-bottom: 4px; }
.d-situations { margin-top: 4px; }

/* Stats grid */
.stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.stat-card { padding: 16px; text-align: center; }
.stat-number { font-size: 28px; font-weight: 800; color: var(--accent-spider); font-family: var(--font-mono); }
.stat-label { font-size: 11px; color: var(--text-muted); margin-top: 4px; text-transform: uppercase; letter-spacing: .05em; }

/* Pipeline overview */
.pipeline-overview { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.pipe-step { display: flex; align-items: center; gap: 8px; }
.pipe-icon {
  width: 36px; height: 36px; display: flex; align-items: center; justify-content: center;
  border-radius: 50%; background: var(--bg-elevated); font-size: 16px; flex-shrink: 0;
}
.pipe-icon.step-active { background: rgba(34,197,94,0.2); box-shadow: 0 0 12px rgba(34,197,94,.3); animation: pulse-live 1.5s infinite; }
.pipe-icon.step-done { background: rgba(34,197,94,0.15); }
.pipe-info { min-width: 80px; }
.pipe-label { font-size: 11px; font-weight: 600; }
.pipe-desc { line-height: 1.3; }
.pipe-arrow { color: var(--text-muted); font-size: 14px; margin: 0 2px; }

/* Pipeline controls */
.pipeline-controls { display: flex; flex-direction: column; gap: 12px; }
.control-row { display: flex; align-items: center; gap: 12px; }
.control-row label { min-width: 100px; font-size: 12px; color: var(--text-secondary); }
.control-row select { max-width: 400px; }
.pipeline-badge { font-size: 12px; color: var(--accent-spider); animation: pulse-live 1.5s infinite; }
.btn-danger { background: var(--danger); color: white; border: none; border-radius: var(--radius-sm); padding: 6px 16px; font-size: 12px; font-weight: 600; cursor: pointer; }

/* Pipeline log */
.pipeline-log {
  max-height: 400px; overflow-y: auto; padding: 12px;
  background: var(--bg-primary); border-radius: var(--radius-sm);
  border: 1px solid var(--border-glass);
}
.log-line { font-size: 11px; line-height: 1.6; color: var(--text-secondary); }

/* Eval */
.eval-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 10px; }
.eval-item { padding: 10px; background: rgba(255,255,255,0.02); border-radius: var(--radius-sm); }
.eval-metric { font-size: 11px; color: var(--text-muted); margin-bottom: 4px; }
.eval-value { font-size: 18px; font-weight: 700; font-family: var(--font-mono); }
.eval-pass { color: var(--success); }
.eval-fail { color: var(--danger); }
.eval-summary { margin-top: 12px; font-size: 12px; color: var(--text-secondary); padding: 10px; background: rgba(255,255,255,0.02); border-radius: var(--radius-sm); }

/* Model table */
.model-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.model-table th { color: var(--text-muted); font-size: 11px; text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border-glass); text-transform: uppercase; }
.model-table td { padding: 10px; border-bottom: 1px solid rgba(255,255,255,0.04); }
.model-name { font-weight: 600; margin-right: 8px; }
.btn-sm { padding: 3px 8px; font-size: 11px; }

/* Code block */
.code-block { padding: 14px; background: var(--bg-primary); border-radius: var(--radius-sm); border: 1px solid var(--border-glass); line-height: 1.8; color: var(--text-secondary); }

/* Brain */
.brain-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.brain-stat { padding: 12px; background: rgba(255,255,255,0.02); border-radius: var(--radius-sm); }
.bs-label { font-size: 11px; color: var(--text-muted); margin-bottom: 4px; }
.bs-value { font-size: 20px; font-weight: 700; font-family: var(--font-mono); }
.bs-value.accent { color: var(--accent-spider); }

/* Architecture */
.arch-flow { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: center; padding: 16px 0; }
.arch-node {
  padding: 12px 16px; border-radius: var(--radius); text-align: center;
  font-size: 12px; font-weight: 600; min-width: 100px;
}
.node-rule { background: rgba(34,197,94,0.1); border: 1px solid rgba(34,197,94,0.3); color: var(--accent-spider); }
.node-model { background: rgba(56,189,248,0.1); border: 1px solid rgba(56,189,248,0.3); color: var(--accent-info); }
.node-override { background: rgba(234,179,8,0.1); border: 1px solid rgba(234,179,8,0.3); color: var(--warning); }
.node-validate { background: rgba(167,139,250,0.1); border: 1px solid rgba(167,139,250,0.3); color: var(--accent-shadow); }
.node-rag { background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.3); color: var(--danger); }
.arch-arrow { color: var(--text-muted); font-size: 18px; }

/* Brain test */
.brain-test-row { display: flex; gap: 10px; margin-bottom: 12px; }
.brain-result { margin-top: 12px; }
.result-header { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.result-json {
  padding: 12px; background: var(--bg-primary); border-radius: var(--radius-sm);
  border: 1px solid var(--border-glass); max-height: 300px; overflow-y: auto;
  white-space: pre-wrap; color: var(--text-secondary);
}
.preset-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border-glass); }
.btn-xs { padding: 2px 8px; font-size: 10px; }

/* Conversation logs */
.log-controls { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.conv-list { display: flex; flex-direction: column; gap: 12px; }
.conv-item { padding: 14px; background: rgba(255,255,255,0.02); border-radius: var(--radius-sm); border: 1px solid var(--border-glass); }
.conv-header { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.conv-messages { display: flex; flex-direction: column; gap: 6px; }
.conv-msg { display: flex; gap: 8px; align-items: flex-start; }
.msg-role { flex-shrink: 0; }
.msg-role.user { color: var(--accent-info); }
.msg-role.assistant { color: var(--accent-spider); }
.msg-text { color: var(--text-secondary); line-height: 1.4; }

/* Badges */
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 600; }
.badge-spider { background: rgba(34,197,94,0.15); color: var(--accent-spider); }
.badge-success { background: rgba(34,197,94,0.15); color: var(--success); }
.badge-danger { background: rgba(239,68,68,0.15); color: var(--danger); }
.badge-warning { background: rgba(234,179,8,0.15); color: var(--warning); }
.badge-teal { background: rgba(34,197,94,0.1); color: var(--accent-honey); }

@media (max-width: 768px) {
  .stats-grid { grid-template-columns: repeat(2, 1fr); }
  .brain-stats { grid-template-columns: repeat(2, 1fr); }
  .domain-grid { grid-template-columns: 1fr; }
  .arch-flow { flex-direction: column; }
  .arch-arrow { transform: rotate(90deg); }
}
</style>
</template>
