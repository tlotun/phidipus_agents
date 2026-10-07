<script setup lang="ts">
// ChromeView.vue — Chrome Swarm: 107 profiles grid + launch
import { onMounted, ref, computed } from 'vue'
import { useSpiderStore } from '@/stores/spiderStore'
import { api } from '@/utils/api'

const spider = useSpiderStore()
const search = ref('')
const launching = ref<string | null>(null)
const launchResult = ref('')

onMounted(() => spider.fetchChromeProfiles())

const filtered = computed(() =>
  spider.chromeProfiles.filter(p =>
    !search.value || p.name.toLowerCase().includes(search.value.toLowerCase())
  )
)

async function launchProfile(name: string) {
  launching.value = name
  launchResult.value = ''
  try {
    const profile = spider.chromeProfiles.find(p => p.name === name)
    await api.post('/api/v2/chrome/profiles/open', {
      name: name,
      directory: profile?.directory || ''
    })
    launchResult.value = `✅ Đang mở ${name}`
  } catch {
    launchResult.value = `❌ Lỗi mở ${name}`
  } finally {
    setTimeout(() => { launching.value = null; launchResult.value = '' }, 2500)
  }
}

async function launchMultiple(names: string[]) {
  for (const name of names) {
    const profile = spider.chromeProfiles.find(p => p.name === name)
    await api.post('/api/v2/chrome/profiles/open', {
      name: name,
      directory: profile?.directory || ''
    }).catch(() => {})
    // Small delay between launches to avoid overwhelming
    await new Promise(r => setTimeout(r, 500))
  }
}
</script>

<template>
  <div class="chrome-view">
    <div class="view-header">
      <h2>🌐 Chrome Swarm</h2>
      <span class="badge badge-spider">{{ spider.chromeTotal }} profiles</span>
      <div style="flex:1"></div>
      <input v-model="search" class="spider-input" style="width:240px" placeholder="🔍 Tìm profile..." />
    </div>

    <div v-if="launchResult" class="launch-toast glass">{{ launchResult }}</div>

    <div v-if="spider.loadingChromeProfiles" class="loading-state">⏳ Đang tải profiles...</div>
    <div v-else-if="!spider.chromeProfiles.length" class="empty glass">
      Chưa có Chrome profiles. Cấu hình AppScanner trong config.yaml.
    </div>

    <div v-else class="profile-grid">
      <div
        v-for="profile in filtered"
        :key="profile.name"
        class="profile-card glass"
        :class="{ launching: launching === profile.name }"
      >
        <div class="profile-avatar">{{ profile.name[0]?.toUpperCase() }}</div>
        <div class="profile-name">{{ profile.name }}</div>
        <div class="profile-dir">{{ profile.directory }}</div>
        <div class="profile-status">
          <span class="badge badge-success">{{ profile.status }}</span>
        </div>
        <button
          class="launch-btn btn-spider"
          :disabled="launching === profile.name"
          @click="launchProfile(profile.name)"
        >
          <span v-if="launching === profile.name">⏳</span>
          <span v-else>▶ Open</span>
        </button>
      </div>
    </div>

    <div v-if="filtered.length === 0 && search" class="empty glass">
      Không tìm thấy profile "{{ search }}"
    </div>
  </div>
</template>

<style scoped>
.chrome-view { display: flex; flex-direction: column; gap: 20px; }
.view-header { display: flex; align-items: center; gap: 12px; }
.view-header h2 { font-size: 20px; font-weight: 700; }
.loading-state, .empty { padding: 40px; text-align: center; color: var(--text-muted); font-size: 14px; }
.launch-toast {
  padding: 12px 20px; border-radius: var(--radius-sm);
  color: var(--text-primary); font-size: 13px; font-weight: 600;
}
.profile-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 14px;
}
.profile-card {
  padding: 16px 14px;
  display: flex; flex-direction: column; align-items: center; gap: 8px;
  text-align: center; transition: all 0.2s; cursor: default;
}
.profile-card:hover { transform: translateY(-2px); border-color: rgba(245,197,24,0.3); }
.profile-card.launching { opacity: 0.7; }
.profile-avatar {
  width: 44px; height: 44px; border-radius: 50%;
  background: linear-gradient(135deg, var(--accent-spider), var(--accent-honey));
  color: #000; font-weight: 800; font-size: 18px;
  display: flex; align-items: center; justify-content: center;
}
.profile-name { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.profile-dir { font-size: 10px; color: var(--text-muted); }
.profile-status { margin: 2px 0; }
.launch-btn { width: 100%; padding: 6px; font-size: 12px; margin-top: 4px; }
</style>
