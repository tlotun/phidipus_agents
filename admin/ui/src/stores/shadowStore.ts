// stores/shadowStore.ts — Shadow Mode state + recording buffer
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '@/utils/api'

export interface ShadowSegment {
  id: string
  goal: string
  duration_s: number
  success: boolean
  action_count: number
  started_at: number
}

export interface HeatmapPoint {
  x: number
  y: number
  weight: number
}

export const useShadowStore = defineStore('shadow', () => {
  const enabled = ref(false)
  const privacyLevel = ref<'low' | 'medium' | 'god'>('medium')
  const segments = ref<ShadowSegment[]>([])
  const heatmapPoints = ref<HeatmapPoint[]>([])
  const learnInsights = ref<unknown[]>([])
  const replayData = ref<unknown | null>(null)
  const loading = ref(false)

  async function fetchState() {
    try {
      const res = await api.get('/api/v2/shadow/state')
      enabled.value = res.data.enabled
      privacyLevel.value = res.data.privacy
      segments.value = res.data.segments ?? []
      heatmapPoints.value = res.data.heatmap_points ?? []
      learnInsights.value = res.data.learn_insights ?? []
    } catch { /* ignore */ }
  }

  async function toggle(privacy?: string) {
    loading.value = true
    try {
      const res = await api.post('/api/v2/shadow/toggle', {
        privacy: privacy ?? privacyLevel.value,
      })
      enabled.value = res.data.enabled
      privacyLevel.value = res.data.privacy
    } finally {
      loading.value = false
    }
  }

  async function replay(segmentId: string) {
    const res = await api.get(`/api/v2/shadow/replay/${segmentId}`)
    replayData.value = res.data
    return res.data
  }

  function setPrivacy(level: 'low' | 'medium' | 'god') {
    privacyLevel.value = level
    api.post('/api/v2/config/shadow', { privacy_level: level })
  }

  const successRate = computed(() => {
    if (!segments.value.length) return 0
    return Math.round(
      segments.value.filter(s => s.success).length / segments.value.length * 100
    )
  })

  const lastSegment = computed(() => segments.value[0] ?? null)

  return {
    enabled, privacyLevel, segments, heatmapPoints,
    learnInsights, replayData, loading,
    successRate, lastSegment,
    fetchState, toggle, replay, setPrivacy,
  }
})
