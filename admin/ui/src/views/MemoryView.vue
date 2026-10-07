<script setup lang="ts">
// MemoryView.vue — Memory Palace: Semantic + Failure + Episodic + Workflow cache
import { onMounted, ref } from 'vue'
import { useSpiderStore } from '@/stores/spiderStore'
import { api } from '@/utils/api'

const spider = useSpiderStore()
const activeTab = ref<'failure' | 'workflow' | 'semantic'>('failure')
const searchQuery = ref('')
const searchResults = ref<unknown[]>([])
const searching = ref(false)

onMounted(() => {
  spider.fetchFailureMemory()
})

async function searchSemantic() {
  if (!searchQuery.value.trim()) return
  searching.value = true
  try {
    const res = await api.post('/api/v2/memory/semantic/search', { query: searchQuery.value })
    searchResults.value = res.data.results ?? []
  } finally {
    searching.value = false
  }
}
</script>

<template>
  <div class="memory-view">
    <div class="view-header">
      <h2>🧠 Memory Palace</h2>
    </div>

    <!-- Tab switcher -->
    <div class="tab-bar glass">
      <button
        v-for="tab in [
          { key: 'failure', icon: '💔', label: 'Failure Memory' },
          { key: 'workflow', icon: '📋', label: 'Workflow Cache' },
          { key: 'semantic', icon: '🔍', label: 'Semantic Search' },
        ]"
        :key="tab.key"
        class="tab-btn"
        :class="{ active: activeTab === tab.key }"
        @click="activeTab = tab.key as typeof activeTab"
      >{{ tab.icon }} {{ tab.label }}</button>
    </div>

    <!-- Failure Memory tab -->
    <div v-if="activeTab === 'failure'" class="panel glass">
      <div class="panel-title">💔 Failure Memory — {{ spider.failureRecords.length }} recent records</div>
      <div v-if="!spider.failureRecords.length" class="empty">
        Chưa có failure records. Agent càng dùng càng học được nhiều lỗi.
      </div>
      <div class="failure-list">
        <div v-for="(rec, i) in spider.failureRecords" :key="i" class="failure-item">
          <div class="fi-header">
            <span class="badge" :class="(rec as any).success ? 'badge-success' : 'badge-danger'">
              {{ (rec as any).error_type }}
            </span>
            <span class="fi-time">{{ new Date((rec as any).t * 1000).toLocaleTimeString('vi-VN') }}</span>
          </div>
          <div class="fi-fix">Fix: {{ (rec as any).fix || '(not yet)' }}</div>
          <div class="fi-goal">Goal: {{ (rec as any).goal }}</div>
        </div>
      </div>
    </div>

    <!-- Workflow Cache tab -->
    <div v-if="activeTab === 'workflow'" class="panel glass">
      <div class="panel-title">📋 Plan Cache — Adaptive TTL</div>
      <WorkflowCacheList />
    </div>

    <!-- Semantic search tab -->
    <div v-if="activeTab === 'semantic'" class="panel glass">
      <div class="panel-title">🔍 Semantic Memory Search</div>
      <div class="search-row">
        <input v-model="searchQuery" class="spider-input" placeholder="Tìm kiến thức hệ thống..." @keydown.enter="searchSemantic" />
        <button class="btn-spider" :disabled="searching" @click="searchSemantic">
          {{ searching ? '⏳' : '🔍 Tìm' }}
        </button>
      </div>
      <div v-if="searchResults.length" class="search-results">
        <div v-for="(r, i) in searchResults" :key="i" class="search-result glass">
          <div class="sr-score">
            <span class="badge badge-teal">{{ Math.round((r as any).score * 100) }}%</span>
          </div>
          <div class="sr-text">{{ (r as any).text }}</div>
        </div>
      </div>
      <div v-else-if="!searching && searchQuery" class="empty">Không tìm thấy kết quả.</div>
    </div>
  </div>
</template>

<script lang="ts">
import { defineComponent, onMounted, ref } from 'vue'
import { api } from '@/utils/api'

const WorkflowCacheList = defineComponent({
  setup() {
    const cache = ref<unknown[]>([])
    async function fetch() {
      try {
        const res = await api.get('/api/v2/workflow/library')
        cache.value = res.data.cache ?? []
      } catch { /* ignore */ }
    }
    onMounted(fetch)
    return { cache }
  },
  template: `
    <div>
      <div v-if="!cache.length" style="color:var(--text-muted);font-size:13px">
        Chưa có plan cached. Cache tự động sau task thành công.
      </div>
      <div v-else>
        <div v-for="(e,i) in cache" :key="i" style="display:flex;align-items:center;gap:12px;padding:10px;border-bottom:1px solid rgba(255,255,255,0.05);font-size:13px">
          <span :class="e.promoted ? 'badge badge-spider' : 'badge badge-teal'" style="flex-shrink:0">TTL {{ e.ttl_days }}d{{ e.promoted ? ' ⬆' : '' }}</span>
          <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ e.goal }}</span>
          <span style="color:var(--text-muted);flex-shrink:0">{{ e.hit_count }} hits</span>
          <span style="color:var(--text-muted);flex-shrink:0">{{ Math.round(e.success_rate * 100) }}% ok</span>
        </div>
      </div>
    </div>
  `
})

export default { components: { WorkflowCacheList } }
</script>

<style scoped>
.memory-view { display: flex; flex-direction: column; gap: 20px; }
.view-header { display: flex; align-items: center; gap: 12px; }
.view-header h2 { font-size: 20px; font-weight: 700; }
.tab-bar { display: flex; gap: 4px; padding: 6px; border-radius: var(--radius); }
.tab-btn {
  padding: 8px 16px; border-radius: var(--radius-sm);
  background: none; border: none; color: var(--text-muted);
  cursor: pointer; font-size: 13px; transition: all 0.15s;
}
.tab-btn.active { background: rgba(245,197,24,0.12); color: var(--accent-spider); font-weight: 600; }
.tab-btn:hover:not(.active) { background: var(--bg-glass); color: var(--text-primary); }
.panel { padding: 20px; }
.panel-title { font-size: 14px; font-weight: 600; color: var(--text-muted); margin-bottom: 16px; }
.empty { color: var(--text-muted); font-size: 13px; padding: 20px 0; }
.failure-list { display: flex; flex-direction: column; gap: 8px; max-height: 500px; overflow-y: auto; }
.failure-item {
  padding: 12px 14px;
  background: rgba(255,255,255,0.03);
  border-radius: var(--radius-sm);
  display: flex; flex-direction: column; gap: 4px;
}
.fi-header { display: flex; align-items: center; gap: 10px; }
.fi-time { color: var(--text-muted); font-size: 11px; margin-left: auto; }
.fi-fix, .fi-goal { font-size: 12px; color: var(--text-muted); }
.fi-fix { color: var(--accent-hive); }
.search-row { display: flex; gap: 10px; margin-bottom: 16px; }
.search-results { display: flex; flex-direction: column; gap: 10px; }
.search-result { display: flex; gap: 12px; padding: 12px 14px; align-items: flex-start; }
.sr-score { flex-shrink: 0; }
.sr-text { font-size: 13px; line-height: 1.5; }
</style>
