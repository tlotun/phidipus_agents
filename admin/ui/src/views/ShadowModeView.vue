<script setup lang="ts">
// ShadowModeView.vue — KILLER FEATURE: Shadow Mode full control panel
import { onMounted, ref, computed } from 'vue'
import { useShadowStore } from '@/stores/shadowStore'

const shadow = useShadowStore()
const replaySegment = ref<Record<string, unknown> | null>(null)
const showReplay = ref(false)
const replayLoading = ref(false)

onMounted(() => shadow.fetchState())

async function doReplay(segId: string) {
  replayLoading.value = true
  try {
    replaySegment.value = await shadow.replay(segId) as Record<string, unknown>
    showReplay.value = true
  } finally {
    replayLoading.value = false
  }
}

const privacyConfig = {
  low:    { label: 'Low Privacy', color: '#ff4d4d', icon: '🔴', desc: 'Ghi tất cả: màn hình, phím, file' },
  medium: { label: 'Medium',      color: '#f5c518', icon: '🟡', desc: 'Ghi màn hình + file, ẩn phím' },
  god:    { label: 'God Mode',    color: '#00d4aa', icon: '🟢', desc: 'Chỉ ghi metadata, ẩn tất cả nội dung' },
}

const currentPrivacy = computed(() => privacyConfig[shadow.privacyLevel] ?? privacyConfig.medium)
</script>

<template>
  <div class="shadow-view">

    <!-- Hero toggle -->
    <div class="shadow-hero glass" :class="{ 'shadow-glow': shadow.enabled }">
      <div class="hero-left">
        <div class="hero-icon">👁️</div>
        <div class="hero-info">
          <h2>Shadow Mode</h2>
          <p>Ghi lại tất cả hành động của agent. Replay, phân tích, học từ kinh nghiệm.</p>
          <div class="privacy-row">
            <span class="privacy-label">Privacy:</span>
            <span class="privacy-value" :style="`color: ${currentPrivacy.color}`">
              {{ currentPrivacy.icon }} {{ currentPrivacy.label }}
            </span>
            <span class="privacy-desc">— {{ currentPrivacy.desc }}</span>
          </div>
        </div>
      </div>

      <div class="hero-right">
        <!-- Privacy slider -->
        <div class="privacy-slider">
          <button
            v-for="(cfg, key) in privacyConfig"
            :key="key"
            class="priv-btn"
            :class="{ active: shadow.privacyLevel === key }"
            :style="shadow.privacyLevel === key ? `border-color: ${cfg.color}; color: ${cfg.color}` : ''"
            @click="shadow.setPrivacy(key as 'low' | 'medium' | 'god')"
          >{{ cfg.icon }} {{ cfg.label }}</button>
        </div>

        <!-- Big toggle button -->
        <button
          class="shadow-big-toggle"
          :class="{ on: shadow.enabled }"
          :disabled="shadow.loading"
          @click="shadow.toggle()"
        >
          <span class="toggle-eye">{{ shadow.enabled ? '👁️' : '🚫' }}</span>
          <span class="toggle-text">{{ shadow.enabled ? 'SHADOW ON' : 'SHADOW OFF' }}</span>
          <span class="toggle-sub">Cmd+S để toggle</span>
        </button>
      </div>
    </div>

    <!-- Stats row -->
    <div class="shadow-stats">
      <div class="stat-card">
        <div class="value">{{ shadow.segments.length }}</div>
        <div class="label">Segments Recorded</div>
      </div>
      <div class="stat-card">
        <div class="value">{{ shadow.successRate }}<span style="font-size:16px">%</span></div>
        <div class="label">Task Success Rate</div>
      </div>
      <div class="stat-card">
        <div class="value">{{ shadow.heatmapPoints.length }}</div>
        <div class="label">Heatmap Points</div>
      </div>
      <div class="stat-card">
        <div class="value">{{ shadow.learnInsights.length }}</div>
        <div class="label">Learn Insights</div>
      </div>
    </div>

    <div class="shadow-grid">

      <!-- Live timeline -->
      <div class="panel glass">
        <div class="panel-title">📺 Action Timeline</div>
        <div v-if="!shadow.segments.length" class="empty">Chưa có segment nào được ghi</div>
        <div class="timeline">
          <div
            v-for="seg in shadow.segments.slice(0, 15)"
            :key="seg.id"
            class="timeline-item"
            :class="{ success: seg.success, failed: !seg.success }"
          >
            <div class="tl-marker">{{ seg.success ? '✅' : '❌' }}</div>
            <div class="tl-content">
              <div class="tl-goal">{{ seg.goal }}</div>
              <div class="tl-meta">
                <span>{{ seg.action_count }} actions</span>
                <span>{{ seg.duration_s }}s</span>
                <span>{{ new Date(seg.started_at * 1000).toLocaleTimeString('vi-VN') }}</span>
              </div>
            </div>
            <button
              class="replay-btn"
              :disabled="replayLoading"
              @click="doReplay(seg.id)"
              title="Replay segment"
            >▶ Replay</button>
          </div>
        </div>
      </div>

      <!-- Heatmap placeholder -->
      <div class="panel glass">
        <div class="panel-title">🔥 Click Heatmap</div>
        <div class="heatmap-area" title="Vùng hay click nhất">
          <div
            v-for="(pt, i) in shadow.heatmapPoints.slice(0, 100)"
            :key="i"
            class="heat-dot"
            :style="`left:${pt.x / 34.4}%;top:${pt.y / 14.4}%;opacity:${Math.min(1, pt.weight * 0.3 + 0.2)}`"
          ></div>
          <div v-if="!shadow.heatmapPoints.length" class="empty center">
            Heatmap xuất hiện sau khi Shadow Mode ghi click (Privacy=Low)
          </div>
        </div>
      </div>

    </div>

    <!-- Learn from last 10 -->
    <div class="panel glass learn-panel" v-if="shadow.learnInsights.length">
      <div class="panel-title">🧠 Learn from Last 10 Tasks</div>
      <div v-for="(insight, i) in shadow.learnInsights" :key="i" class="insight-card">
        <template v-if="(insight as any).type === 'summary'">
          <div class="insight-row">
            <div class="insight-stat">
              <span class="is-val">{{ Math.round(((insight as any).success_rate ?? 0) * 100) }}%</span>
              <span class="is-label">Success Rate</span>
            </div>
            <div class="insight-stat">
              <span class="is-val">{{ (insight as any).avg_duration_s }}s</span>
              <span class="is-label">Avg Duration</span>
            </div>
            <div class="insight-stat">
              <span class="is-val">{{ (insight as any).tasks_analyzed }}</span>
              <span class="is-label">Tasks Analyzed</span>
            </div>
          </div>
          <div class="top-actions">
            <span class="ta-label">Top actions:</span>
            <span
              v-for="act in ((insight as any).top_actions ?? [])"
              :key="act.action"
              class="badge badge-teal"
            >{{ act.action }} ×{{ act.count }}</span>
          </div>
        </template>
      </div>
    </div>

    <!-- Replay modal -->
    <Transition name="fade">
      <div v-if="showReplay && replaySegment" class="replay-overlay" @click.self="showReplay = false">
        <div class="replay-modal glass">
          <div class="replay-header">
            <h3>▶ Replay: {{ (replaySegment as any).goal }}</h3>
            <button class="btn-ghost" @click="showReplay = false">✕ Đóng</button>
          </div>
          <div class="replay-meta">
            <span class="badge" :class="(replaySegment as any).success ? 'badge-success' : 'badge-danger'">
              {{ (replaySegment as any).success ? 'Success' : 'Failed' }}
            </span>
            <span>{{ (replaySegment as any).duration_s }}s</span>
            <span>{{ ((replaySegment as any).actions ?? []).length }} actions</span>
          </div>
          <div class="replay-actions">
            <div
              v-for="(action, i) in ((replaySegment as any).actions ?? []).slice(0, 30)"
              :key="i"
              class="replay-action"
            >
              <span class="ra-num">{{ i + 1 }}</span>
              <span class="ra-action badge badge-spider">{{ (action as any).action }}</span>
              <span class="ra-result">{{ (action as any).result ?? '' }}</span>
              <span class="ra-time">+{{ (((action as any).t ?? 0) - ((replaySegment as any).started_at ?? 0)).toFixed(1) }}s</span>
            </div>
          </div>
        </div>
      </div>
    </Transition>

  </div>
</template>

<style scoped>
.shadow-view { display: flex; flex-direction: column; gap: 24px; max-width: 1200px; }

.shadow-hero {
  padding: 28px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  transition: all 0.3s;
}
.hero-left { display: flex; gap: 20px; align-items: flex-start; flex: 1; }
.hero-icon { font-size: 48px; flex-shrink: 0; }
.hero-info h2 { font-size: 22px; font-weight: 800; color: var(--accent-shadow); }
.hero-info p { color: var(--text-muted); font-size: 13px; margin-top: 4px; max-width: 480px; line-height: 1.5; }
.privacy-row { display: flex; align-items: center; gap: 8px; margin-top: 8px; font-size: 12px; }
.privacy-label { color: var(--text-muted); }
.privacy-desc { color: var(--text-muted); }

.hero-right { display: flex; flex-direction: column; gap: 16px; align-items: flex-end; }
.privacy-slider { display: flex; gap: 8px; }
.priv-btn {
  padding: 6px 12px; border-radius: var(--radius-sm);
  border: 1px solid var(--border-glass);
  background: transparent; color: var(--text-muted);
  cursor: pointer; font-size: 12px; transition: all 0.2s;
}
.priv-btn.active { font-weight: 700; }
.priv-btn:hover { background: var(--bg-glass); }

.shadow-big-toggle {
  display: flex; flex-direction: column; align-items: center;
  padding: 20px 40px;
  border-radius: var(--radius);
  border: 2px solid rgba(155,89,182,0.4);
  background: rgba(155,89,182,0.1);
  cursor: pointer; transition: all 0.3s;
  color: var(--text-primary);
}
.shadow-big-toggle:hover { background: rgba(155,89,182,0.2); }
.shadow-big-toggle.on {
  background: rgba(155,89,182,0.25);
  border-color: rgba(155,89,182,0.7);
  box-shadow: 0 0 30px rgba(155,89,182,0.3);
}
.toggle-eye { font-size: 32px; }
.toggle-text { font-size: 15px; font-weight: 800; color: var(--accent-shadow); margin-top: 6px; }
.toggle-sub { font-size: 11px; color: var(--text-muted); }

.shadow-stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
.shadow-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }

.panel { padding: 20px; }
.panel-title { font-size: 14px; font-weight: 600; color: var(--text-muted); margin-bottom: 16px; }
.empty { color: var(--text-muted); font-size: 13px; }
.empty.center { text-align: center; padding: 40px 0; }

.timeline { display: flex; flex-direction: column; gap: 8px; max-height: 400px; overflow-y: auto; }
.timeline-item {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: rgba(255,255,255,0.03);
  border-left: 3px solid transparent;
  font-size: 13px;
}
.timeline-item.success { border-left-color: var(--success); }
.timeline-item.failed  { border-left-color: var(--danger); }
.tl-marker { flex-shrink: 0; }
.tl-content { flex: 1; overflow: hidden; }
.tl-goal { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tl-meta { display: flex; gap: 12px; color: var(--text-muted); font-size: 11px; margin-top: 3px; }
.replay-btn {
  flex-shrink: 0; padding: 4px 10px;
  border-radius: 4px;
  border: 1px solid rgba(245,197,24,0.3);
  background: transparent;
  color: var(--accent-spider); font-size: 11px;
  cursor: pointer;
}
.replay-btn:hover { background: rgba(245,197,24,0.1); }
.replay-btn:disabled { opacity: 0.4; cursor: not-allowed; }

.heatmap-area {
  position: relative;
  height: 320px;
  background: rgba(255,255,255,0.02);
  border-radius: var(--radius-sm);
  overflow: hidden;
}
.heat-dot {
  position: absolute;
  width: 12px; height: 12px;
  background: radial-gradient(circle, #ff4d4d, transparent);
  border-radius: 50%;
  transform: translate(-50%, -50%);
  pointer-events: none;
}

.learn-panel { padding: 20px; }
.insight-row { display: flex; gap: 32px; margin-bottom: 12px; }
.insight-stat { display: flex; flex-direction: column; gap: 2px; }
.is-val { font-size: 24px; font-weight: 800; color: var(--accent-spider); }
.is-label { font-size: 11px; color: var(--text-muted); }
.top-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ta-label { font-size: 12px; color: var(--text-muted); }

/* Replay modal */
.replay-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.7);
  backdrop-filter: blur(4px);
  z-index: 9000;
  display: flex; align-items: center; justify-content: center;
}
.replay-modal {
  width: 700px; max-height: 80vh; overflow-y: auto;
  padding: 24px;
  display: flex; flex-direction: column; gap: 16px;
}
.replay-header { display: flex; align-items: center; justify-content: space-between; }
.replay-header h3 { font-size: 16px; font-weight: 700; color: var(--accent-shadow); }
.replay-meta { display: flex; align-items: center; gap: 12px; font-size: 13px; color: var(--text-muted); }
.replay-actions { display: flex; flex-direction: column; gap: 6px; }
.replay-action {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 10px;
  background: rgba(255,255,255,0.03);
  border-radius: var(--radius-sm);
  font-size: 12px;
}
.ra-num { color: var(--text-muted); width: 24px; flex-shrink: 0; }
.ra-action { flex-shrink: 0; }
.ra-result { flex: 1; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ra-time { color: var(--text-muted); flex-shrink: 0; }
</style>
