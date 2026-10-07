// stores/spiderStore.ts — Global SpiderHub state (task, chrome, resource, forge)
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '@/utils/api'

export interface ChromeProfile {
  name: string
  directory: string
  status: string
}

export interface ProviderInfo {
  name: string
  kind: string
  temperature: number
  has_key: boolean
  success_rate: number
}

export const useSpiderStore = defineStore('spider', () => {
  // Task
  const taskGoal = ref('')
  const taskRunning = ref(false)
  const taskHistory = ref<{ goal: string; success: boolean; ts: number }[]>([])

  // Chrome profiles
  const chromeProfiles = ref<ChromeProfile[]>([])
  const chromeTotal = ref(0)

  // Resource
  const ramPct = ref(0)
  const cpuPct = ref(0)
  const diskGb = ref(0)
  const resourceStatus = ref('healthy')

  // Skill Forge
  const forgeProviders = ref<ProviderInfo[]>([])
  const forgeCachedSkills = ref(0)
  const forgeSafetyEvents = ref<unknown[]>([])

  // Workflow library
  const workflowStats = ref({ plans: 0, templates: 0, promoted: 0 })

  // Failure memory
  const failureRecords = ref<unknown[]>([])

  // Skill registry
  const skillRegistry = ref<unknown[]>([])

  // Loading states
  const loadingChromeProfiles = ref(false)
  const loadingForge = ref(false)

  // ── Actions ──────────────────────────────────────────────────

  async function fetchChromeProfiles() {
    loadingChromeProfiles.value = true
    try {
      const res = await api.get('/api/v2/chrome/profiles/status')
      chromeProfiles.value = res.data.profiles ?? []
      chromeTotal.value = res.data.total ?? 0
    } finally {
      loadingChromeProfiles.value = false
    }
  }

  async function fetchForgeRadar() {
    loadingForge.value = true
    try {
      const res = await api.get('/api/v2/skill/forge/radar')
      forgeProviders.value = res.data.providers ?? []
      forgeCachedSkills.value = res.data.cached_skills ?? 0
      forgeSafetyEvents.value = res.data.safety_events ?? []
    } finally {
      loadingForge.value = false
    }
  }

  async function fetchWorkflowLibrary() {
    const res = await api.get('/api/v2/workflow/library')
    const stats = res.data.stats ?? {}
    workflowStats.value = {
      plans: stats.plan_cache_entries ?? 0,
      templates: stats.workflow_templates ?? 0,
      promoted: stats.plan_cache_promoted ?? 0,
    }
    return res.data
  }

  async function fetchFailureMemory() {
    const res = await api.get('/api/v2/memory/failure')
    failureRecords.value = res.data.recent ?? []
    return res.data
  }

  async function fetchSkillRegistry() {
    const res = await api.get('/api/v2/skill/registry')
    skillRegistry.value = res.data.skills ?? []
    return res.data
  }

  async function runTask(goal: string) {
    if (!goal.trim()) return
    taskRunning.value = true
    taskGoal.value = goal
    try {
      await api.post('/api/v2/task/run', { goal })
    } finally {
      // Running state cleared by WS task_done event
    }
  }

  async function godReset() {
    const res = await api.post('/api/v2/system/god-reset', { confirm: true })
    taskHistory.value = []
    return res.data
  }

  // Update resource from WS heartbeat
  function updateResource(data: Record<string, unknown>) {
    const r = data.resource as Record<string, number> | undefined
    if (r) {
      ramPct.value = r.ram_pct ?? 0
      cpuPct.value = r.cpu_pct ?? 0
      diskGb.value = r.disk_free_gb ?? 0
    }
  }

  const resourceHealthy = computed(() => ramPct.value < 70 && cpuPct.value < 80)
  const hasSafetyEvents = computed(() => forgeSafetyEvents.value.length > 0)

  return {
    taskGoal, taskRunning, taskHistory,
    chromeProfiles, chromeTotal, loadingChromeProfiles,
    forgeProviders, forgeCachedSkills, forgeSafetyEvents, loadingForge,
    workflowStats, failureRecords, skillRegistry,
    ramPct, cpuPct, diskGb, resourceStatus,
    resourceHealthy, hasSafetyEvents,
    fetchChromeProfiles, fetchForgeRadar, fetchWorkflowLibrary,
    fetchFailureMemory, fetchSkillRegistry,
    runTask, godReset, updateResource,
  }
})
