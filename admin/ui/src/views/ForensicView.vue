<script setup lang="ts">
// ForensicView.vue — v9.21 Forensic Replay: Debug agent decisions step-by-step
// Shows: timeline slider + screenshot + LLM thought + action + DOM diagnostics + healing path
import { onMounted, ref, computed, watch } from 'vue'
import { api } from '@/utils/api'

interface Step {
  id: string
  index: number
  timestamp: string
  status: 'done' | 'failed' | 'healed' | 'replanned' | 'skipped'
  action: string
  description: string
  thought: string
  observation: string
  error: string
  duration_ms: number
  screenshot_url: string
  dom_diagnostics: {
    page_type: string
    has_errors: boolean
    error_messages: string[]
    has_modal: boolean
    modal_text: string
    available_buttons: string[]
    url: string
  } | null
  healing: {
    strategy: string
    source: string
    confidence: number
    error_type: string
  } | null
  replan: {
    explanation: string
    new_steps: number
  } | null
}

interface TaskEpisode {
  task_id: string
  goal: string
  started_at: string
  finished_at: string
  success: boolean
  total_steps: number
  steps: Step[]
}

const episodes = ref<TaskEpisode[]>([])
const selectedEpisode = ref<TaskEpisode | null>(null)
const currentStepIndex = ref(0)
const playing = ref(false)
const playSpeed = ref(2000) // ms between steps
const loading = ref(false)
let playTimer: ReturnType<typeof setInterval> | null = null

// Current step
const currentStep = computed(() => {
  if (!selectedEpisode.value) return null
  return selectedEpisode.value.steps[currentStepIndex.value] || null
})

const totalSteps = computed(() => selectedEpisode.value?.steps.length || 0)

// Fetch recent task episodes for replay
async function fetchEpisodes() {
  loading.value = true
  try {
    const res = await api.get('/api/v2/forensic/episodes')
    episodes.value = res.data?.episodes || []
    // Auto-select most recent
    if (episodes.value.length && !selectedEpisode.value) {
      selectEpisode(episodes.value[0])
    }
  } catch {
    // Fallback: try shadow mode segments
    try {
      const res = await api.get('/api/v2/shadow/segments')
      episodes.value = (res.data?.segments || []).map((s: any) => ({
        task_id: s.id,
        goal: s.goal || 'Unknown',
        started_at: s.started_at,
        finished_at: s.finished_at,
        success: s.success,
        total_steps: s.steps?.length || 0,
        steps: (s.steps || []).map((st: any, i: number) => ({
          id: st.id || `step_${i}`,
          index: i,
          timestamp: st.timestamp || '',
          status: st.status || 'done',
          action: st.action || '',
          description: st.description || '',
          thought: st.thought || '',
          observation: st.observation || '',
          error: st.error || '',
          duration_ms: st.duration_ms || 0,
          screenshot_url: st.screenshot_url || '',
          dom_diagnostics: st.dom || null,
          healing: st.healing || null,
          replan: st.replan || null,
        })),
      }))
    } catch { /* ignore */ }
  } finally {
    loading.value = false
  }
}

function selectEpisode(ep: TaskEpisode) {
  stopPlay()
  selectedEpisode.value = ep
  currentStepIndex.value = 0
}

// Playback controls
function startPlay() {
  if (!selectedEpisode.value) return
  playing.value = true
  playTimer = setInterval(() => {
    if (currentStepIndex.value < totalSteps.value - 1) {
      currentStepIndex.value++
    } else {
      stopPlay()
    }
  }, playSpeed.value)
}

function stopPlay() {
  playing.value = false
  if (playTimer) { clearInterval(playTimer); playTimer = null }
}

function stepForward() {
  if (currentStepIndex.value < totalSteps.value - 1) currentStepIndex.value++
}
function stepBack() {
  if (currentStepIndex.value > 0) currentStepIndex.value--
}

// Status icon
function statusIcon(status: string): string {
  const map: Record<string, string> = {
    done: '✓', failed: '✕', healed: '⚕', replanned: '↻', skipped: '→',
  }
  return map[status] || '?'
}
function statusClass(status: string): string {
  const map: Record<string, string> = {
    done: 'badge-success', failed: 'badge-danger', healed: 'badge-healed',
    replanned: 'badge-replan', skipped: 'badge-spider',
  }
  return map[status] || ''
}

onMounted(fetchEpisodes)
</script>

<template>
  <div class="forensic-view">

    <!-- Header -->
    <div class="view-header">
      <h2>Forensic Replay</h2>
      <span class="badge badge-info mono">{{ episodes.length }} episodes</span>
      <button class="btn-ghost" @click="fetchEpisodes">Refresh</button>
    </div>

    <div class="forensic-layout">

      <!-- Left: Episode list -->
      <div class="episode-list glass">
        <div class="panel-title">Recent Tasks</div>
        <div v-if="loading" class="text-muted text-sm" style="padding:12px">Loading...</div>
        <div v-else-if="!episodes.length" class="text-muted text-sm" style="padding:12px">
          No episodes recorded yet. Run a task to generate data.
        </div>
        <div
          v-for="ep in episodes"
          :key="ep.task_id"
          class="episode-item"
          :class="{ active: selectedEpisode?.task_id === ep.task_id }"
          @click="selectEpisode(ep)"
        >
          <div class="ep-header">
            <span class="ep-status" :class="ep.success ? 'text-success' : 'text-danger'">
              {{ ep.success ? '✓' : '✕' }}
            </span>
            <span class="ep-id mono text-xs">{{ ep.task_id }}</span>
          </div>
          <div class="ep-goal">{{ ep.goal }}</div>
          <div class="ep-meta">
            <span class="mono text-xs">{{ ep.total_steps }} steps</span>
            <span class="mono text-xs text-muted">{{ ep.started_at }}</span>
          </div>
        </div>
      </div>

      <!-- Right: Replay panel -->
      <div class="replay-panel" v-if="selectedEpisode">

        <!-- Task info bar -->
        <div class="task-bar glass">
          <div>
            <span class="mono text-xs text-muted">TASK</span>
            <span class="mono" style="margin-left:8px;color:var(--accent-spider)">{{ selectedEpisode.task_id }}</span>
          </div>
          <div class="task-goal">{{ selectedEpisode.goal }}</div>
        </div>

        <!-- Playback controls -->
        <div class="playback glass">
          <button class="ctrl-btn" @click="stepBack" :disabled="currentStepIndex <= 0">&#x25C0;</button>
          <button class="ctrl-btn play-btn" @click="playing ? stopPlay() : startPlay()">
            {{ playing ? '&#x25A0;' : '&#x25B6;' }}
          </button>
          <button class="ctrl-btn" @click="stepForward" :disabled="currentStepIndex >= totalSteps - 1">&#x25B6;</button>

          <!-- Timeline slider -->
          <div class="timeline-slider">
            <input
              type="range"
              :min="0"
              :max="Math.max(0, totalSteps - 1)"
              v-model.number="currentStepIndex"
              class="slider"
              @input="stopPlay()"
            />
            <div class="timeline-dots">
              <span
                v-for="(step, i) in selectedEpisode.steps"
                :key="i"
                class="t-dot"
                :class="{
                  active: i === currentStepIndex,
                  done: step.status === 'done',
                  failed: step.status === 'failed',
                  healed: step.status === 'healed',
                  replanned: step.status === 'replanned',
                }"
                @click="currentStepIndex = i; stopPlay()"
              ></span>
            </div>
          </div>

          <span class="step-counter mono">{{ currentStepIndex + 1 }}/{{ totalSteps }}</span>

          <!-- Speed control -->
          <select v-model.number="playSpeed" class="speed-select" @change="stopPlay()">
            <option :value="500">0.5s</option>
            <option :value="1000">1s</option>
            <option :value="2000">2s</option>
            <option :value="4000">4s</option>
          </select>
        </div>

        <!-- Step detail (main content) -->
        <div v-if="currentStep" class="step-detail">
          <div class="step-grid">

            <!-- Left column: Screenshot + DOM -->
            <div class="step-visual">
              <!-- Screenshot -->
              <div class="screenshot-wrap glass">
                <div v-if="currentStep.screenshot_url" class="screenshot">
                  <img :src="currentStep.screenshot_url" alt="Screenshot" />
                </div>
                <div v-else class="screenshot-empty">
                  <span class="text-muted">No screenshot</span>
                </div>
              </div>

              <!-- DOM diagnostics -->
              <div v-if="currentStep.dom_diagnostics" class="dom-panel glass">
                <div class="panel-title">DOM State</div>
                <div class="dom-grid">
                  <span class="dom-key">Page</span>
                  <span class="mono text-sm" :class="{
                    'text-danger': currentStep.dom_diagnostics.page_type === 'error',
                    'text-warning': currentStep.dom_diagnostics.page_type === 'login',
                  }">{{ currentStep.dom_diagnostics.page_type }}</span>

                  <span class="dom-key">URL</span>
                  <span class="mono text-xs text-muted" style="word-break:break-all">
                    {{ currentStep.dom_diagnostics.url }}
                  </span>

                  <template v-if="currentStep.dom_diagnostics.has_errors">
                    <span class="dom-key text-danger">Errors</span>
                    <span class="text-sm text-danger">
                      {{ currentStep.dom_diagnostics.error_messages.join('; ') }}
                    </span>
                  </template>

                  <template v-if="currentStep.dom_diagnostics.has_modal">
                    <span class="dom-key text-warning">Modal</span>
                    <span class="text-sm">{{ currentStep.dom_diagnostics.modal_text }}</span>
                  </template>

                  <template v-if="currentStep.dom_diagnostics.available_buttons.length">
                    <span class="dom-key">Buttons</span>
                    <div class="btn-tags">
                      <span v-for="b in currentStep.dom_diagnostics.available_buttons" :key="b" class="btn-tag mono">
                        {{ b }}
                      </span>
                    </div>
                  </template>
                </div>
              </div>
            </div>

            <!-- Right column: Thought + Action + Result -->
            <div class="step-logic">

              <!-- Step header -->
              <div class="step-header">
                <span :class="`badge ${statusClass(currentStep.status)}`">
                  {{ statusIcon(currentStep.status) }} {{ currentStep.status }}
                </span>
                <span class="mono text-xs text-muted">{{ currentStep.action }}</span>
                <span v-if="currentStep.duration_ms" class="mono text-xs text-muted">
                  {{ currentStep.duration_ms }}ms
                </span>
              </div>

              <!-- Description -->
              <div class="step-desc">{{ currentStep.description }}</div>

              <!-- LLM Thought (the "why") -->
              <div v-if="currentStep.thought" class="thought-box glass">
                <div class="thought-label">LLM Thought</div>
                <div class="thought-text">{{ currentStep.thought }}</div>
              </div>

              <!-- Action taken -->
              <div class="action-box">
                <div class="action-label">Action</div>
                <code class="action-code">{{ currentStep.action }}({{ JSON.stringify(currentStep.observation || {}).substring(0, 200) }})</code>
              </div>

              <!-- Observation / Result -->
              <div v-if="currentStep.observation" class="obs-box glass">
                <div class="obs-label">Observation</div>
                <div class="obs-text mono text-sm">{{ currentStep.observation }}</div>
              </div>

              <!-- Error (if failed) -->
              <div v-if="currentStep.error" class="error-box glass">
                <div class="error-label">Error</div>
                <div class="error-text mono text-sm">{{ currentStep.error }}</div>
              </div>

              <!-- Self-Healing info -->
              <div v-if="currentStep.healing" class="healing-box glass">
                <div class="healing-label">Self-Healing</div>
                <div class="healing-grid">
                  <span class="heal-key">Strategy</span>
                  <span class="mono text-sm">{{ currentStep.healing.strategy }}</span>
                  <span class="heal-key">Source</span>
                  <span class="badge badge-healed">{{ currentStep.healing.source }}</span>
                  <span class="heal-key">Confidence</span>
                  <span class="mono text-sm" :style="currentStep.healing.confidence >= 0.7 ? 'color:var(--success)' : 'color:var(--warning)'">
                    {{ (currentStep.healing.confidence * 100).toFixed(0) }}%
                  </span>
                </div>
              </div>

              <!-- Dynamic Replan info -->
              <div v-if="currentStep.replan" class="replan-box glass">
                <div class="replan-label">Dynamic Replan</div>
                <div class="text-sm">{{ currentStep.replan.explanation }}</div>
                <span class="badge badge-replan mono">{{ currentStep.replan.new_steps }} new steps</span>
              </div>

            </div>
          </div>
        </div>

      </div>

      <!-- No episode selected -->
      <div v-else class="replay-empty glass">
        <div class="empty-icon" style="font-size:48px;opacity:.1">&#x1F50D;</div>
        <span class="text-muted">Select a task episode to replay</span>
      </div>

    </div>
  </div>
</template>

<style scoped>
.forensic-view{display:flex;flex-direction:column;gap:16px;height:100%}
.view-header{display:flex;align-items:center;gap:12px}
.view-header h2{font-size:18px;font-weight:700}

.forensic-layout{display:flex;gap:16px;flex:1;overflow:hidden}

/* Episode list */
.episode-list{width:260px;flex-shrink:0;overflow-y:auto;padding:12px}
.episode-item{padding:10px 12px;border-radius:var(--radius-sm);cursor:pointer;margin-bottom:4px;border:1px solid transparent;transition:all .15s}
.episode-item:hover{background:var(--bg-glass-hover)}
.episode-item.active{border-color:var(--accent-spider);background:rgba(229,169,16,.05)}
.ep-header{display:flex;align-items:center;gap:6px}
.ep-status{font-weight:700;font-size:14px}
.ep-goal{font-size:13px;margin-top:4px;color:var(--text-primary);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.ep-meta{display:flex;gap:8px;margin-top:4px}

/* Replay panel */
.replay-panel{flex:1;display:flex;flex-direction:column;gap:12px;overflow-y:auto}
.replay-empty{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px}

.task-bar{padding:12px 16px;display:flex;align-items:center;gap:16px}
.task-goal{font-size:14px;font-weight:500;flex:1}

/* Playback */
.playback{display:flex;align-items:center;gap:10px;padding:10px 16px}
.ctrl-btn{background:none;border:1px solid var(--border-glass);color:var(--text-secondary);width:32px;height:32px;border-radius:var(--radius-sm);cursor:pointer;font-size:12px;display:flex;align-items:center;justify-content:center;transition:all .15s}
.ctrl-btn:hover{color:var(--text-primary);border-color:var(--border-active)}
.ctrl-btn:disabled{opacity:.3;cursor:not-allowed}
.play-btn{background:var(--accent-spider);color:var(--text-inverse);border-color:var(--accent-spider);width:36px;height:36px;font-size:14px}
.play-btn:hover{background:#d49b0e}

.timeline-slider{flex:1;position:relative;display:flex;flex-direction:column;gap:4px}
.slider{width:100%;-webkit-appearance:none;height:3px;background:var(--bg-tertiary);border-radius:2px;outline:none}
.slider::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:var(--accent-spider);cursor:pointer;border:2px solid var(--bg-primary)}
.timeline-dots{display:flex;justify-content:space-between;padding:0 2px}
.t-dot{width:6px;height:6px;border-radius:50%;background:var(--text-muted);cursor:pointer;transition:all .15s}
.t-dot.active{background:var(--accent-spider);transform:scale(1.5)}
.t-dot.done{background:var(--success)}
.t-dot.failed{background:var(--danger)}
.t-dot.healed{background:var(--healed)}
.t-dot.replanned{background:var(--replanned)}

.step-counter{font-size:12px;color:var(--text-muted);white-space:nowrap}
.speed-select{background:var(--bg-tertiary);border:1px solid var(--border-glass);color:var(--text-secondary);border-radius:var(--radius-xs);padding:4px 8px;font-size:11px;font-family:var(--font-mono)}

/* Step detail */
.step-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media (max-width:900px){.step-grid{grid-template-columns:1fr}}

.step-visual{display:flex;flex-direction:column;gap:12px}
.screenshot-wrap{overflow:hidden;min-height:200px}
.screenshot img{width:100%;display:block;border-radius:var(--radius)}
.screenshot-empty{display:flex;align-items:center;justify-content:center;min-height:200px}

.dom-panel{padding:12px 16px}
.dom-grid{display:grid;grid-template-columns:80px 1fr;gap:6px 12px;font-size:12px}
.dom-key{color:var(--text-muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em;padding-top:2px}
.btn-tags{display:flex;flex-wrap:wrap;gap:4px}
.btn-tag{background:var(--bg-tertiary);border:1px solid var(--border-glass);border-radius:var(--radius-xs);padding:1px 6px;font-size:10px;color:var(--text-secondary)}

.step-logic{display:flex;flex-direction:column;gap:10px}

.step-header{display:flex;align-items:center;gap:8px}
.step-desc{font-size:14px;font-weight:500;color:var(--text-primary)}

.thought-box{padding:12px 16px;border-left:3px solid var(--accent-info)}
.thought-label{font-size:10px;color:var(--accent-info);text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px;font-weight:600}
.thought-text{font-size:13px;line-height:1.6;color:var(--text-secondary)}

.action-box{padding:0}
.action-label{font-size:10px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px}
.action-code{display:block;background:var(--bg-secondary);border:1px solid var(--border-glass);border-radius:var(--radius-sm);padding:8px 12px;font-size:12px;color:var(--accent-spider);font-family:var(--font-mono);word-break:break-all}

.obs-box{padding:10px 14px}
.obs-label{font-size:10px;color:var(--accent-hive);text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px;font-weight:600}
.obs-text{color:var(--text-secondary);max-height:120px;overflow-y:auto}

.error-box{padding:10px 14px;border-left:3px solid var(--danger)}
.error-label{font-size:10px;color:var(--danger);text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px;font-weight:600}
.error-text{color:var(--danger)}

.healing-box{padding:12px 16px;border-left:3px solid var(--healed)}
.healing-label{font-size:10px;color:var(--healed);text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px;font-weight:600}
.healing-grid{display:grid;grid-template-columns:80px 1fr;gap:4px 12px}
.heal-key{font-size:10px;color:var(--text-muted);text-transform:uppercase}

.replan-box{padding:12px 16px;border-left:3px solid var(--replanned)}
.replan-label{font-size:10px;color:var(--replanned);text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px;font-weight:600}

.panel-title{font-size:12px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:12px}
</style>
