<script setup lang="ts">
import { onMounted, onUnmounted, ref, reactive, watch, nextTick } from 'vue'
import { api } from '@/utils/api'

const providers = ref<any[]>([])
const logs = ref<any[]>([])
const keyInputs = reactive<Record<string, string>>({})
const geminiKeys = reactive<{key1:string, key2:string, key3:string}>({key1:'',key2:'',key3:''})
const geminiSaveState = ref('')
const saveState = reactive<Record<string, string>>({})
const showAdd = ref(false)
const exportJson = ref('')
const importText = ref('')
const importMsg = ref('')
const aiSuggestion = ref('')
const aiLoading = ref(false)
const budget = ref<any[]>([])
const showCustom = ref(false)
const customForm = reactive({name:'', base_url:'', model:'', api_key:''})
const customMsg = ref('')
const customTesting = ref(false)
const customTestResult = ref<any>(null)
const ollamaModels = ref<any[]>([])
const showOllama = ref(false)
const ollamaRunning = ref<boolean | null>(null)   // null = chưa scan
const ollamaDebugErr = ref('')   // raw error from backend for diagnosis
const ollamaScanning = ref(false)
const pullName = ref('')
const isPulling = ref(false)
const terminalLines = ref<string[]>([])
const terminalEl = ref<HTMLElement | null>(null)
let timer: any

function _loadKS(): Record<string,boolean> { try { return JSON.parse(sessionStorage.getItem('nb_ks')||'{}') } catch { return {} } }
const keyStatus = reactive<Record<string,boolean>>({..._loadKS()})
watch(keyStatus, ()=> sessionStorage.setItem('nb_ks', JSON.stringify({...keyStatus})), {deep:true})

const KEY_CARDS = [
  // FIX v9.40: names match actual provider names in build_provider_stack()
  {id:'gemini',label:'Google Gemini',color:'#4285f4',icon:'✦',model:'gemini-2.0-flash',url:'https://aistudio.google.com/app/apikey',ph:'AIza...',names:['gemini-2.0-flash','gemini-2.0-flash-lite'],tier:'Miễn phí',limit:'15 RPM · 1M TPD',badge:'free',multi:true},
  {id:'mistral',label:'Mistral AI',color:'#ff6f00',icon:'▲',model:'mistral-small-latest',url:'https://console.mistral.ai/',ph:'sk-...',names:['mistral-small'],tier:'Miễn phí',limit:'1 RPS · 500K TPM',badge:'free'},
  {id:'cerebras',label:'Cerebras',color:'#00c853',icon:'◆',model:'llama3.1-8b',url:'https://cloud.cerebras.ai/',ph:'csk-...',names:['cerebras-llama3.1-8b'],tier:'Miễn phí',limit:'30 RPM · 1M TPD',badge:'free'},
  {id:'openrouter',label:'OpenRouter',color:'#9c27b0',icon:'◉',model:'qwen3-coder-480b',url:'https://openrouter.ai/keys',ph:'sk-or-...',names:['openrouter-free'],tier:'Free tier',limit:'20 RPM',badge:'free'},
]
const FREE_MODELS = [
  {id:'groq',label:'Groq',color:'#f55036',icon:'⚡',model:'llama-3.3-70b',url:'https://console.groq.com/keys',ph:'gsk_...',names:[],tier:'Miễn phí',limit:'30 RPM · 14K TPD',badge:'free'},
  {id:'deepseek',label:'DeepSeek',color:'#0066ff',icon:'🔮',model:'deepseek-chat',url:'https://platform.deepseek.com/',ph:'sk-...',names:[],tier:'$0.14/1M',limit:'60 RPM',badge:'cheap'},
  {id:'fireworks',label:'Fireworks AI',color:'#ff4500',icon:'🔥',model:'llama-v3p1-70b',url:'https://fireworks.ai/api-keys',ph:'fw_...',names:[],tier:'$0.9/1M',limit:'600 RPM',badge:'fast'},
  {id:'together',label:'Together AI',color:'#00b4d8',icon:'🤝',model:'meta-llama-3.1-70b',url:'https://api.together.xyz/',ph:'sk-...',names:[],tier:'Free $5',limit:'60 RPM',badge:'free'},
]

async function fetchAll() {
  try {
    const ks = await api.get('/api/v2/config/key-status')
    if (ks.data) {
      // API responded → trust it fully (both true AND false)
      for (const [k, v] of Object.entries(ks.data)) { keyStatus[k] = !!v }
    }
  } catch {}
  try { const r = await api.get('/api/v2/providers'); if(r.data?.providers?.length>0){providers.value=r.data.providers; for(const p of r.data.providers){if(p.has_key){const c=KEY_CARDS.find(x=>x.names.includes(p.name));if(c)keyStatus[c.id]=true}}} } catch{}
  try { const lr = await api.get('/api/v2/providers/logs'); logs.value=lr.data?.logs||[] } catch{}
  await fetchBudget()
}
function hasKey(c:any):boolean{return keyStatus[c.id]===true}

// FIX v9.40: Save Gemini multi-keys — primary + 2 backup
async function saveGeminiKeys() {
  const keys = [geminiKeys.key1, geminiKeys.key2, geminiKeys.key3].map(k=>k.trim()).filter(Boolean)
  if (keys.length === 0) return
  geminiSaveState.value = 'saving'
  try {
    // Save all keys to backend
    await api.post('/api/v2/config/api-key', {
      provider: 'gemini',
      api_key: keys[0],
      backup_keys: keys.slice(1),
    }).catch(()=>{})
    // Hot-reload: update in-memory stack for ALL Gemini providers
    for (const name of ['gemini-2.0-flash', 'gemini-2.0-flash-lite']) {
      await api.post(`/api/v2/providers/${encodeURIComponent(name)}/api-key`, {
        api_key: keys[0],
      }).catch(()=>{})
    }
    keyStatus['gemini'] = true
    geminiSaveState.value = 'ok'
    geminiKeys.key1 = ''; geminiKeys.key2 = ''; geminiKeys.key3 = ''
    // Refresh UI immediately
    await fetchAll()
    setTimeout(()=>{geminiSaveState.value=''},3000)
  } catch {
    geminiSaveState.value = 'error'
    setTimeout(()=>{geminiSaveState.value=''},3000)
  }
}

async function saveKey(c:any){
  const val=(keyInputs[c.id]||'').trim();
  if(!val)return;
  saveState[c.id]='saving';
  try{
    // FIX v9.40: Hot-reload — update in-memory stack FIRST
    for(const n of(c.names||[])){
      await api.post(`/api/v2/providers/${encodeURIComponent(n)}/api-key`,{api_key:val}).catch(()=>{})
    };
    // Then persist to config.yaml
    await api.post('/api/v2/config/api-key',{provider:c.id,api_key:val}).catch(()=>{});
    // FIX v9.40: Update UI immediately
    keyStatus[c.id]=true;
    saveState[c.id]='ok';
    keyInputs[c.id]='';
    // Refresh providers list to update priority display
    await fetchAll();
    setTimeout(()=>{saveState[c.id]=''},3000)
  }catch{saveState[c.id]='error';setTimeout(()=>{saveState[c.id]=''},3000)}
}
async function moveProvider(n:string,d:'up'|'down'){try{const r=await api.post(`/api/v2/providers/${encodeURIComponent(n)}/move`,{direction:d});if(r.data?.providers?.length)providers.value=r.data.providers}catch{await fetchAll()}}
async function toggleProvider(n:string,e:boolean){await api.post(`/api/v2/providers/${encodeURIComponent(n)}/toggle`,{enabled:e}).catch(()=>{});await fetchAll()}
async function doExport(){try{const r=await api.get('/api/v2/config/export');exportJson.value=JSON.stringify(r.data?.config||{},null,2)}catch{exportJson.value='Lỗi export'}}
async function doImport(){importMsg.value='';try{const cfg=JSON.parse(importText.value);await api.post('/api/v2/config/import',{config:cfg});importMsg.value='✅ OK! Restart để áp dụng.';importText.value=''}catch(e:any){importMsg.value='❌ '+(e.message||'Lỗi')}}
function copyEx(){navigator.clipboard?.writeText(exportJson.value)}
function fmtT(ts:number){return new Date(ts*1000).toLocaleTimeString('vi-VN')}
async function askAI(){aiLoading.value=true;aiSuggestion.value='';try{const r=await api.post('/api/v2/providers/optimize');aiSuggestion.value=r.data?.suggestion||'Không có gợi ý'}catch(e:any){aiSuggestion.value='❌ Lỗi: '+(e.message||'Không kết nối được')}finally{aiLoading.value=false}}
async function fetchBudget(){try{const r=await api.get('/api/v2/providers/budget');budget.value=r.data?.budget||[]}catch{}}

async function testCustom(){
  customTesting.value=true;customTestResult.value=null
  try{const r=await api.post('/api/v2/providers/custom/test',{base_url:customForm.base_url,model:customForm.model,api_key:customForm.api_key});customTestResult.value=r.data}
  catch(e:any){customTestResult.value={ok:false,error:e.message||'Lỗi kết nối'}}
  finally{customTesting.value=false}
}
async function addCustom(){
  if(!customForm.name||!customForm.base_url||!customForm.model){customMsg.value='❌ Điền đủ Name, URL, Model';return}
  try{await api.post('/api/v2/providers/custom/add',{...customForm});customMsg.value='✅ Đã thêm '+customForm.name;customForm.name='';customForm.base_url='';customForm.model='';customForm.api_key='';customTestResult.value=null;await fetchAll()}
  catch(e:any){customMsg.value='❌ '+(e.message||'Lỗi')}
  setTimeout(()=>{customMsg.value=''},4000)
}
async function removeCustom(name:string){
  if(!confirm(`Xóa provider "${name}"?`))return
  await api.post('/api/v2/providers/custom/remove',{name}).catch(()=>{})
  await fetchAll()
}
async function fetchOllama(){
  ollamaScanning.value = true
  ollamaDebugErr.value = ''
  try {
    const r = await api.get('/api/v2/ollama/models')
    ollamaModels.value = r.data?.models || []
    ollamaRunning.value = r.data?.ollama_running === false ? false : true
    // Capture debug info (e.g. "HTTP failed — used CLI fallback")
    const dbg = r.data?.debug
    if (Array.isArray(dbg) && dbg.length > 0) {
      ollamaDebugErr.value = dbg.join(' | ')
    } else if (r.data?.error) {
      ollamaDebugErr.value = r.data.error
    }
  } catch (e: any) {
    // axios failed → backend unreachable, don't know Ollama status
    ollamaRunning.value = null
    ollamaDebugErr.value = `Không kết nối được backend: ${e.message}`
  } finally {
    ollamaScanning.value = false
  }
}

async function toggleOllama(){
  showOllama.value = !showOllama.value
  // Auto-scan the first time user opens the panel
  if (showOllama.value && ollamaRunning.value === null) {
    await fetchOllama()
  }
}

async function pullModel(){
  const name = pullName.value.trim()
  if (!name || isPulling.value) return
  isPulling.value = true
  terminalLines.value = []
  const BASE = (import.meta as any).env?.VITE_API_BASE ?? 'http://127.0.0.1:8912'
  const url = `${BASE}/api/v2/ollama/pull-stream?name=${encodeURIComponent(name)}`
  try {
    const resp = await fetch(url)
    if (!resp.ok || !resp.body) {
      terminalLines.value.push(`❌ HTTP ${resp.status}`)
      isPulling.value = false; return
    }
    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const parts = buf.split('\n\n'); buf = parts.pop() ?? ''
      for (const block of parts) {
        for (const line of block.split('\n')) {
          if (line.startsWith('data: ')) {
            const msg = line.slice(6)
            if (msg === '__DONE__') {
              isPulling.value = false
              await fetchOllama(); pullName.value = ''; return
            }
            terminalLines.value.push(msg)
            await nextTick()
            if (terminalEl.value) terminalEl.value.scrollTop = terminalEl.value.scrollHeight
          }
        }
      }
    }
  } catch (e: any) {
    terminalLines.value.push(`❌ Lỗi: ${e.message}`)
  } finally {
    isPulling.value = false
  }
}
async function addOllamaToStack(name:string){
  try{await api.post('/api/v2/ollama/add-to-stack',{name});await fetchAll();await fetchOllama()}catch{}
}

onMounted(async()=>{await fetchAll();timer=setInterval(fetchAll,15000)})
onUnmounted(()=>clearInterval(timer))
</script>

<template>
<div class="pv">
  <div class="pv-h">
    <h2>Nhà cung cấp LLM</h2>
    <button class="bs bo" @click="showAdd=!showAdd">＋ Thêm model</button>
    <button class="bs bo" @click="showCustom=!showCustom">🔧 Custom API</button>
    <button class="bs bo" :class="showOllama ? 'btn-active' : ''" @click="toggleOllama">🦙 Ollama</button>
    <button class="bs bo" @click="doExport">📦 Export</button>
  </div>

  <!-- Add Free Model -->
  <div v-if="showAdd" class="sec glass" style="border-color:rgba(34,197,94,.3)">
    <div class="st">＋ Thêm nhà cung cấp miễn phí</div>
    <div class="sd">Bấm link lấy key, dán vào ô, bấm Lưu.</div>
    <div class="kg">
      <div v-for="m in FREE_MODELS" :key="m.id" class="kc" :class="hasKey(m)?'kc-ok':'kc-no'">
        <div class="kt"><span class="ki" :style="`background:${m.color}`">{{m.icon}}</span><div class="kn"><b>{{m.label}}</b><span class="km">{{m.model}}</span></div><span class="badge" :class="'b-'+m.badge">{{m.tier}}</span></div>
        <div class="kl2">{{m.limit}}</div>
        <div class="kr"><input v-model="keyInputs[m.id]" type="password" :placeholder="m.ph" @keydown.enter="saveKey(m)"/><button class="ks" :disabled="!keyInputs[m.id]?.trim()" @click="saveKey(m)">Lưu</button></div>
        <a :href="m.url" target="_blank" class="kl">Lấy key →</a>
      </div>
    </div>
  </div>

  <!-- Export -->

  <!-- Custom Provider Form -->
  <div v-if="showCustom" class="sec glass" style="border-color:rgba(167,139,250,.3)">
    <div class="st">🔧 Thêm Custom API (OpenAI-compatible)</div>
    <div class="sd">Nhập URL endpoint + model + API key. Hỗ trợ: Groq, DeepSeek, Together, LMStudio, vLLM, bất kỳ OpenAI-compatible API.</div>
    <div class="cf">
      <div class="cf-row">
        <label>Tên hiển thị</label>
        <input v-model="customForm.name" class="ci" placeholder="vd: groq-llama3" />
      </div>
      <div class="cf-row">
        <label>Base URL</label>
        <input v-model="customForm.base_url" class="ci" placeholder="https://api.groq.com/openai/v1" />
      </div>
      <div class="cf-row">
        <label>Model</label>
        <input v-model="customForm.model" class="ci" placeholder="llama-3.3-70b-versatile" />
      </div>
      <div class="cf-row">
        <label>API Key</label>
        <input v-model="customForm.api_key" type="password" class="ci" placeholder="gsk_..." />
      </div>
    </div>
    <div style="display:flex;gap:6px;margin-top:10px;align-items:center;flex-wrap:wrap">
      <button class="bs bo" :disabled="customTesting||!customForm.base_url||!customForm.model" @click="testCustom">{{customTesting?'⏳ Testing...':'🔍 Test kết nối'}}</button>
      <button class="bs bg" :disabled="!customForm.name||!customForm.base_url||!customForm.model" @click="addCustom">➕ Thêm vào Stack</button>
      <span v-if="customMsg" style="font-size:11px">{{customMsg}}</span>
    </div>
    <div v-if="customTestResult" class="ct-result" :class="customTestResult.ok?'ct-ok':'ct-err'">
      <span v-if="customTestResult.ok">✅ Kết nối OK — {{customTestResult.latency_ms}}ms</span>
      <span v-else>❌ Lỗi: {{customTestResult.error}}</span>
    </div>
    <div style="margin-top:10px;font-size:10px;color:var(--text-muted)">
      <b>URL mẫu:</b> Groq: <code>https://api.groq.com/openai/v1</code> · DeepSeek: <code>https://api.deepseek.com/v1</code> · Together: <code>https://api.together.xyz/v1</code> · Fireworks: <code>https://api.fireworks.ai/inference/v1</code> · Local vLLM: <code>http://localhost:8000/v1</code>
    </div>
  </div>

  <!-- ══════════════════════════════════════════════════════════════ -->
  <!-- 🦙 OLLAMA LOCAL MODELS — toggle by button in header          -->
  <!-- ══════════════════════════════════════════════════════════════ -->
  <div v-if="showOllama" class="sec glass ollama-wrap">
    <!-- Header row -->
    <div class="ollama-hdr">
      <div class="ollama-title">
        <span class="ollama-icon">🦙</span>
        <span class="st" style="margin:0">Ollama — Model trên máy</span>
        <!-- Status pill -->
        <span v-if="ollamaRunning === true" class="ol-pill ol-ok">● Online</span>
        <span v-else-if="ollamaRunning === false" class="ol-pill ol-err">● Offline</span>
        <span v-else-if="ollamaScanning" class="ol-pill ol-idle">⏳ Scanning...</span>
        <span v-else class="ol-pill ol-idle">○ Bấm Scan</span>
        <span v-if="ollamaModels.length > 0" class="ol-count">{{ollamaModels.length}} model</span>
      </div>
      <button class="bs bo ol-scan-btn"
              :disabled="ollamaScanning"
              @click="fetchOllama">
        {{ ollamaScanning ? '⏳ Đang scan...' : '🔍 Scan models' }}
      </button>
    </div>

    <!-- Offline notice (confirmed by backend) -->
    <div v-if="ollamaRunning === false" class="ol-offline">
      <div>⚠️ Ollama chưa chạy — mở ứng dụng Ollama hoặc chạy <code>ollama serve</code> rồi bấm Scan lại.</div>
      <div style="margin-top:4px;font-size:9px;opacity:.65">💡 Thấy lỗi "address already in use"? = Ollama đang chạy rồi, bấm Scan thêm 1 lần.</div>
      <div v-if="ollamaDebugErr" class="ol-debug-err">🔍 {{ ollamaDebugErr }}</div>
    </div>

    <!-- Null state: backend unreachable or first open -->
    <div v-else-if="ollamaRunning === null && !ollamaScanning && ollamaModels.length === 0" class="ol-hint">
      <div v-if="ollamaDebugErr" class="ol-debug-err">⚠️ {{ ollamaDebugErr }}</div>
      <div v-else>Bấm <b>🔍 Scan models</b> để kiểm tra Ollama và liệt kê các model đã tải về.</div>
    </div>

    <!-- Empty after scan -->
    <div v-else-if="ollamaRunning === true && ollamaModels.length === 0" class="ol-empty">
      Chưa có model nào. Tải model đầu tiên bằng ô bên dưới (vd: <code>qwen3:4b</code>).
    </div>

    <!-- Model grid -->
    <div v-if="ollamaModels.length > 0" class="om-grid">
      <div v-for="m in ollamaModels" :key="m.name"
           class="om-card" :class="m.in_stack ? 'om-active' : ''">
        <div class="om-top">
          <span class="om-name">{{ m.name }}</span>
          <span class="om-size">{{ m.size_gb }} GB</span>
        </div>
        <div class="om-meta">
          <span v-if="m.params" class="om-tag">{{ m.params }}</span>
          <span v-if="m.family" class="om-tag">{{ m.family }}</span>
          <span v-if="m.quantization" class="om-tag">{{ m.quantization }}</span>
        </div>
        <div class="om-actions">
          <button v-if="!m.in_stack" class="bs bg om-add-btn"
                  @click="addOllamaToStack(m.name)">＋ Stack</button>
          <span v-else class="om-in-stack">✓ Trong Stack</span>
        </div>
      </div>
    </div>

    <!-- Divider -->
    <div class="ol-divider"></div>

    <!-- Pull section -->
    <div class="ol-pull-wrap">
      <div class="ol-pull-label">📥 Tải model mới</div>
      <div class="ol-pull-row">
        <input v-model="pullName" class="ci ol-pull-input"
               placeholder="vd: qwen3:4b-instruct, llama3.2:3b, gemma3:4b, phi4:latest..."
               :disabled="isPulling"
               @keydown.enter="pullModel" />
        <button class="bs ol-pull-btn"
                :class="isPulling ? 'ol-pulling' : 'bg'"
                :disabled="!pullName.trim() || isPulling"
                @click="pullModel">
          {{ isPulling ? '⏳ Đang tải...' : '📥 Tải xuống' }}
        </button>
        <button v-if="isPulling" class="bs bo" style="font-size:9px"
                @click="isPulling=false">✕ Hủy</button>
      </div>

      <!-- Format hint -->
      <div class="ol-format-hint">
        <span>📌 Format Ollama:</span>
        <code>model:tag</code> — vd: <code>qwen3:8b</code>, <code>llama3.2:3b</code>
        &nbsp;·&nbsp;
        <span>HuggingFace GGUF:</span>
        <code>hf.co/username/repo-name</code>
        &nbsp;·&nbsp;
        <a href="https://ollama.com/search" target="_blank" class="ol-search-link">🔍 Tìm model Ollama</a>
      </div>

      <!-- Popular model chips -->
      <div class="ol-chips">
        <span class="ol-chip-label">Gợi ý:</span>
        <button v-for="chip in ['qwen3:4b-instruct','qwen3:8b','llama3.2:3b','gemma3:4b','phi4:latest','deepseek-r1:8b','mistral:7b']"
                :key="chip" class="ol-chip"
                :disabled="isPulling"
                @click="pullName = chip">{{ chip }}</button>
      </div>

      <!-- Terminal output -->
      <div v-if="terminalLines.length > 0 || isPulling" class="ol-terminal" ref="terminalEl">
        <div class="ol-term-bar">
          <span>terminal — ollama pull {{ isPulling ? pullName || '...' : (terminalLines[0] || '').replace('🦙 Bắt đầu tải ','').replace('...','') }}</span>
          <span v-if="isPulling" class="ol-blink">▌</span>
          <button v-else class="ol-term-clear" @click="terminalLines=[]">✕ Xóa</button>
        </div>
        <div class="ol-term-body">
          <div v-for="(line, i) in terminalLines" :key="i"
               class="ol-term-line"
               :class="{
                 'ol-term-err':  line.startsWith('❌'),
                 'ol-term-ok':   line.startsWith('✅'),
                 'ol-term-warn': line.startsWith('⚠️') || line.startsWith('💡'),
                 'ol-term-sep':  line.startsWith('─'),
                 'ol-term-info': line.startsWith('→') || line.startsWith('   →'),
                 'ol-term-prog': line.includes('█') || line.includes('░'),
                 'ol-term-conn': line.startsWith('🔗'),
                 'ol-term-spin': line.startsWith('⏳'),
               }">
            <!-- Make http links clickable -->
            <template v-if="line.includes('http')">
              <span v-for="(part, pi) in line.split(/(https?:\/\/\S+)/)" :key="pi">
                <a v-if="part.startsWith('http')" :href="part" target="_blank" class="ol-link">{{ part }}</a>
                <span v-else>{{ part }}</span>
              </span>
            </template>
            <template v-else>{{ line }}</template>
          </div>
          <div v-if="isPulling && terminalLines.length === 0" class="ol-term-wait">
            ⏳ Đang kết nối với Ollama...
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Export -->
  <div v-if="exportJson" class="sec glass">
    <div class="st">📦 Export Config</div>
    <textarea class="ta" readonly :value="exportJson"></textarea>
    <div style="display:flex;gap:6px;margin-top:6px"><button class="bs bg" @click="copyEx">📋 Copy</button><button class="bs bo" @click="exportJson=''">Đóng</button></div>
  </div>

  <!-- API Keys -->
  <div class="sec glass">
    <div class="st">API Keys</div>
    <div class="sd">Dán key → Lưu. 🟢 = có key. 🔴 = chưa có.</div>
    <div class="kg">
      <!-- FIX v9.40: Gemini multi-key card (separate) -->
      <div class="kc gemini-multi" :class="hasKey(KEY_CARDS[0])?'kc-ok':'kc-no'">
        <div class="kt"><span class="ki" style="background:#4285f4">✦</span><div class="kn"><b>Google Gemini</b><span class="km">gemini-2.0-flash</span></div><span class="dot" :class="hasKey(KEY_CARDS[0])?'dg':'dr'"></span></div>
        <div class="kl2"><span class="badge b-free">Miễn phí</span> 15 RPM · 1M TPD · <b>Multi-key fallback</b></div>
        <div class="gm-keys">
          <div class="gm-row"><span class="gm-label">🔑 Key chính</span><input v-model="geminiKeys.key1" type="password" placeholder="AIza... (bắt buộc)" @keydown.enter="saveGeminiKeys"/></div>
          <div class="gm-row"><span class="gm-label">🔑 Key dự phòng 1</span><input v-model="geminiKeys.key2" type="password" placeholder="AIza... (tuỳ chọn)" @keydown.enter="saveGeminiKeys"/></div>
          <div class="gm-row"><span class="gm-label">🔑 Key dự phòng 2</span><input v-model="geminiKeys.key3" type="password" placeholder="AIza... (tuỳ chọn)" @keydown.enter="saveGeminiKeys"/></div>
        </div>
        <div style="display:flex;gap:6px;align-items:center;margin-top:6px">
          <button class="ks" :class="{'so':geminiSaveState==='ok','se':geminiSaveState==='error'}" :disabled="!geminiKeys.key1.trim()||geminiSaveState==='saving'" @click="saveGeminiKeys">{{geminiSaveState==='saving'?'...':geminiSaveState==='ok'?'✓ Đã lưu':'Lưu tất cả'}}</button>
          <span style="font-size:9px;color:var(--text-muted)">Key lỗi → tự chuyển sang key tiếp theo</span>
        </div>
        <a href="https://aistudio.google.com/app/apikey" target="_blank" class="kl">Lấy key miễn phí →</a>
      </div>
      <!-- Other providers (non-gemini) -->
      <div v-for="c in KEY_CARDS.filter(x=>x.id!=='gemini')" :key="c.id" class="kc" :class="hasKey(c)?'kc-ok':'kc-no'">
        <div class="kt"><span class="ki" :style="`background:${c.color}`">{{c.icon}}</span><div class="kn"><b>{{c.label}}</b><span class="km">{{c.model}}</span></div><span class="dot" :class="hasKey(c)?'dg':'dr'"></span></div>
        <div class="kl2"><span class="badge" :class="'b-'+c.badge">{{c.tier}}</span> {{c.limit}}</div>
        <div class="kr"><input v-model="keyInputs[c.id]" type="password" :placeholder="c.ph" @keydown.enter="saveKey(c)"/><button class="ks" :class="{'so':saveState[c.id]==='ok','se':saveState[c.id]==='error'}" :disabled="!keyInputs[c.id]?.trim()||saveState[c.id]==='saving'" @click="saveKey(c)">{{saveState[c.id]==='saving'?'...':saveState[c.id]==='ok'?'✓':'Lưu'}}</button></div>
        <a :href="c.url" target="_blank" class="kl">Lấy key miễn phí →</a>
      </div>
    </div>
  </div>

  <!-- Stack -->
  <div v-if="providers.length>0">
    <div class="st" style="margin-bottom:6px">Thứ tự ưu tiên — bấm ▲▼ để đổi</div>
    <div class="shdr"><span style="width:28px"></span><span style="width:28px"></span><span style="width:12px"></span><span style="flex:1">Provider</span><span class="sh">Latency</span><span class="sh">Success</span><span class="sh">Calls</span><span style="width:32px"></span></div>
    <div v-for="(p,i) in providers" :key="p.name" class="sr glass" :class="{'so2':!p.enabled}">
      <span class="sp" :class="{'sp0':i===0}">P{{i}}</span>
      <div class="sa"><button class="ab" :disabled="i===0" @click="moveProvider(p.name,'up')">▲</button><button class="ab" :disabled="i===providers.length-1" @click="moveProvider(p.name,'down')">▼</button></div>
      <span class="dot" :class="p.has_key&&p.enabled?'dg':!p.enabled?'dd':'dr'"></span>
      <div class="si"><span class="sn">{{p.name}}</span></div>
      <span class="sv" :class="(p.avg_latency_ms||0)<500?'tg':(p.avg_latency_ms||0)<2000?'ty':'tr'">{{p.avg_latency_ms||0}}ms</span>
      <span class="sv" :class="(p.success_rate||1)>=0.8?'tg':'tr'">{{((p.success_rate||1)*100).toFixed(0)}}%</span>
      <span class="sv">{{p.total_calls||0}}</span>
      <label class="tg2"><input type="checkbox" :checked="p.enabled" @change="toggleProvider(p.name,!p.enabled)"><span class="tt"></span></label>
      <button v-if="p.kind==='custom'" class="bs br" @click="removeCustom(p.name)" title="Xóa">✕</button>
    </div>
  </div>

  <!-- Flow -->
  <div class="sec glass fb" v-if="providers.length>0">
    <b>Fallback:</b>
    <template v-for="(p,i) in providers.filter(x=>x.enabled)" :key="p.name"><span class="fc" :class="p.has_key?'fo':'ff'">{{p.name}}</span><span v-if="i<providers.filter(x=>x.enabled).length-1" class="fa">→</span></template>
  </div>

  <!-- AI Assistant -->
  <div class="sec glass ai-box">
    <div class="st">🤖 AI Assistant — Tối ưu Fallback</div>
    <div class="sd">Dùng Gemini phân tích stack và gợi ý tối ưu.</div>
    <button class="bs bg" :disabled="aiLoading" @click="askAI">{{aiLoading?'⏳ Đang phân tích...':'🤖 Tối ưu cho tôi'}}</button>
    <div v-if="aiSuggestion" class="ai-result" v-html="aiSuggestion.replace(/\n/g,'<br>')"></div>
  </div>

  <!-- Budget Guard -->
  <div class="sec glass" v-if="budget.length>0">
    <div class="st">💰 Budget Guard — Theo dõi Quota</div>
    <div class="bg-grid">
      <div v-for="b in budget" :key="b.name" class="bg-card">
        <div class="bg-top"><span class="bg-name">{{b.name}}</span><span class="badge" :class="b.tier==='free'?'b-free':b.tier==='free_credit'?'b-free':'b-cheap'">{{b.tier}}</span></div>
        <div class="bg-bar"><div class="bg-fill" :style="`width:${Math.min(b.usage_pct,100)}%`" :class="b.usage_pct>80?'bg-danger':b.usage_pct>50?'bg-warn':''"></div></div>
        <div class="bg-info">
          <span>{{b.total_calls}} calls</span>
          <span>{{b.rpm_limit}} RPM</span>
          <span v-if="b.est_cost_usd>0">${{b.est_cost_usd.toFixed(4)}}</span>
          <span v-if="b.warning" class="bg-w">{{b.warning}}</span>
        </div>
      </div>
    </div>
  </div>

  <!-- Logs -->
  <div class="sec glass" v-if="logs.length>0">
    <div class="st">📋 Log gần nhất</div>
    <div class="ll2">
      <div v-for="(l,i) in logs" :key="i" class="lr" :class="l.success?'':'lr-e'">
        <span class="lt">{{fmtT(l.ts)}}</span>
        <span class="lp">{{l.provider}}</span>
        <span :class="l.success?'tg':'tr'">{{l.success?'✓':'✗'}}</span>
        <span class="lm">{{l.latency_ms}}ms</span>
        <span class="le" v-if="l.error">{{l.error}}</span>
      </div>
    </div>
  </div>

  <!-- Import -->
  <div class="sec glass">
    <div class="st">📥 Import Config</div>
    <textarea class="ta" v-model="importText" placeholder="Paste JSON config..."></textarea>
    <div style="display:flex;gap:6px;margin-top:6px;align-items:center"><button class="bs bg" :disabled="!importText.trim()" @click="doImport">Import</button><span v-if="importMsg" style="font-size:11px">{{importMsg}}</span></div>
  </div>

  <div v-if="providers.length===0" style="padding:16px;text-align:center;color:var(--text-muted)">Đang tải...</div>
</div>
</template>

<style scoped>
.pv{display:flex;flex-direction:column;gap:12px}
.pv-h{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.pv-h h2{font-size:18px;font-weight:700}
.bs{padding:4px 10px;border-radius:3px;font-size:10px;font-weight:600;cursor:pointer;border:none}
.bg{background:#22c55e;color:#000}.bo{background:none;border:1px solid var(--border-glass);color:var(--text-secondary)}.bo:hover{border-color:#22c55e;color:#22c55e}
.sec{padding:0}.sec.glass{padding:14px}.st{font-size:13px;font-weight:700;margin-bottom:4px}.sd{font-size:10px;color:var(--text-muted);margin-bottom:10px}
.kg{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:8px}
.kc{background:var(--bg-tertiary);border:1px solid var(--border-glass);border-radius:6px;padding:10px}.kc-ok{border-color:rgba(34,197,94,.3)}.kc-no{border-style:dashed;opacity:.8}
.kt{display:flex;align-items:center;gap:8px;margin-bottom:4px}.ki{width:26px;height:26px;border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:13px;color:#fff;flex-shrink:0}
.kn{flex:1;min-width:0}.kn b{font-size:11px;display:block}.km{font-size:9px;color:var(--text-muted);font-family:var(--font-mono)}
.kl2{font-size:9px;color:var(--text-muted);margin-bottom:6px;display:flex;align-items:center;gap:4px}
.badge{font-size:8px;padding:1px 5px;border-radius:2px;font-weight:700}.b-free{background:rgba(34,197,94,.12);color:#22c55e}.b-cheap{background:rgba(56,189,248,.12);color:#38bdf8}.b-fast{background:rgba(234,179,8,.12);color:#eab308}
.dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}.dg{background:#22c55e;box-shadow:0 0 5px rgba(34,197,94,.5)}.dr{background:#ef4444;box-shadow:0 0 5px rgba(239,68,68,.4)}.dd{background:#52525b}
.kr{display:flex;gap:4px;margin-bottom:4px}.kr input{flex:1;background:var(--bg-primary);border:1px solid var(--border-glass);border-radius:3px;color:var(--text-primary);padding:5px 7px;font-size:10px;font-family:var(--font-mono)}.kr input:focus{outline:none;border-color:#22c55e}
.ks{background:#22c55e;color:#000;border:none;border-radius:3px;padding:5px 10px;font-size:10px;font-weight:600;cursor:pointer;min-width:40px}.ks:disabled{opacity:.3}.so{background:#22c55e}.se{background:#ef4444;color:#fff}
.kl{font-size:9px;color:var(--accent-info);opacity:.7}
.shdr{display:flex;align-items:center;gap:8px;padding:2px 14px;font-size:9px;color:var(--text-muted);font-weight:600}.sh{min-width:55px;text-align:right}
.sr{display:flex;align-items:center;gap:8px;padding:6px 14px;margin-bottom:2px}.so2{opacity:.3}
.sp{font-size:10px;color:var(--text-muted);width:20px;text-align:center;font-family:var(--font-mono)}.sp0{color:#22c55e;font-weight:700}
.sa{display:flex;flex-direction:column;gap:1px}.ab{background:none;border:1px solid var(--border-glass);color:var(--text-muted);width:18px;height:13px;font-size:7px;cursor:pointer;border-radius:2px;display:flex;align-items:center;justify-content:center;padding:0}.ab:hover:not(:disabled){color:#22c55e;border-color:#22c55e}.ab:disabled{opacity:.15}
.si{flex:1;min-width:0}.sn{font-size:11px;font-weight:500}
.sv{font-size:9px;font-family:var(--font-mono);min-width:55px;text-align:right}
.tg{color:#22c55e}.ty{color:#eab308}.tr{color:#ef4444}
.tg2{position:relative;display:inline-block;width:26px;height:13px;flex-shrink:0}.tg2 input{display:none}.tt{position:absolute;inset:0;background:var(--bg-tertiary);border:1px solid var(--border-glass);border-radius:7px;cursor:pointer;transition:.2s}.tt::after{content:'';position:absolute;left:2px;top:2px;width:7px;height:7px;background:var(--text-muted);border-radius:50%;transition:transform .2s}.tg2 input:checked+.tt{background:rgba(34,197,94,.15);border-color:#22c55e}.tg2 input:checked+.tt::after{transform:translateX(13px);background:#22c55e}
.fb{padding:8px 14px;display:flex;align-items:center;gap:4px;flex-wrap:wrap;font-size:9px;font-family:var(--font-mono)}.fc{border:1px solid var(--border-glass);border-radius:2px;padding:2px 5px}.fo{border-color:rgba(34,197,94,.2)}.ff{color:var(--text-muted)}.fa{color:var(--text-muted)}
.ll2{max-height:200px;overflow-y:auto}.lr{display:flex;align-items:center;gap:8px;padding:3px 6px;border-bottom:1px solid var(--border-glass);font-size:10px;font-family:var(--font-mono)}.lr-e{background:rgba(239,68,68,.03)}
.lt{color:var(--text-muted);min-width:60px}.lp{min-width:120px;font-weight:500}.lm{min-width:45px;text-align:right;color:var(--text-muted)}.le{color:var(--text-muted);font-size:9px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:180px}
.ta{width:100%;background:var(--bg-primary);border:1px solid var(--border-glass);border-radius:3px;color:var(--text-primary);padding:6px;font-family:var(--font-mono);font-size:10px;min-height:60px;resize:vertical}.ta:focus{outline:none;border-color:#22c55e}

/* AI Assistant */
.ai-box{border-color:rgba(167,139,250,.2)}
.ai-result{margin-top:10px;padding:10px;background:var(--bg-primary);border:1px solid var(--border-glass);border-radius:4px;font-size:11px;line-height:1.6;max-height:300px;overflow-y:auto}

/* Custom Provider Form */
.cf{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px}
.cf-row{display:flex;flex-direction:column;gap:3px}
.cf-row label{font-size:10px;font-weight:600;color:var(--text-muted)}
.ci{background:var(--bg-primary);border:1px solid var(--border-glass);border-radius:3px;color:var(--text-primary);padding:6px 8px;font-size:11px;font-family:var(--font-mono)}.ci:focus{outline:none;border-color:#22c55e}
.ci code{background:var(--bg-tertiary);padding:1px 4px;border-radius:2px;font-size:9px}
.ct-result{margin-top:8px;padding:8px;border-radius:4px;font-size:11px}
.ct-ok{background:rgba(34,197,94,.08);color:#22c55e}
.ct-err{background:rgba(239,68,68,.08);color:#ef4444}
.br{background:rgba(239,68,68,.1);color:#ef4444;border:1px solid rgba(239,68,68,.3);font-size:10px;padding:2px 6px;cursor:pointer;border-radius:2px}.br:hover{background:rgba(239,68,68,.2)}

.btn-active{border-color:#22c55e!important;color:#22c55e!important;background:rgba(34,197,94,.08)!important}
/* ── Ollama Section ─────────────────────────────────────── */
.ollama-wrap{border-color:rgba(34,197,94,.25)!important}
.ollama-hdr{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:10px;flex-wrap:wrap}
.ollama-title{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.ollama-icon{font-size:18px;line-height:1}
.ol-pill{font-size:9px;padding:2px 7px;border-radius:10px;font-weight:700;letter-spacing:.3px}
.ol-ok{background:rgba(34,197,94,.12);color:#22c55e;border:1px solid rgba(34,197,94,.3)}
.ol-err{background:rgba(239,68,68,.1);color:#ef4444;border:1px solid rgba(239,68,68,.25)}
.ol-idle{background:rgba(113,113,122,.1);color:var(--text-muted);border:1px solid var(--border-glass)}
.ol-count{font-size:10px;color:var(--text-muted);padding:2px 6px;background:var(--bg-primary);border-radius:3px;border:1px solid var(--border-glass)}
.ol-scan-btn{font-size:10px!important;padding:5px 12px!important}
.ol-offline{padding:10px 12px;background:rgba(239,68,68,.06);border:1px solid rgba(239,68,68,.2);border-radius:5px;font-size:11px;color:#ef4444;margin-bottom:10px}
.ol-offline code{background:rgba(239,68,68,.1);padding:1px 5px;border-radius:3px;font-size:10px}
.ol-hint{font-size:11px;color:var(--text-muted);padding:10px 0;margin-bottom:4px}
.ol-debug-err{margin-top:6px;padding:5px 8px;background:rgba(239,68,68,.05);border:1px solid rgba(239,68,68,.15);border-radius:3px;font-size:9px;font-family:var(--font-mono);color:#f87171;word-break:break-all;line-height:1.5}
.ol-empty{font-size:11px;color:var(--text-muted);padding:8px 0;margin-bottom:6px}
.ol-empty code{background:var(--bg-primary);padding:1px 5px;border-radius:3px;font-size:10px;color:var(--text-secondary)}

/* Model grid */
.om-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:8px;margin-bottom:12px}
.om-card{background:var(--bg-primary);border:1px solid var(--border-glass);border-radius:6px;padding:10px;transition:border-color .15s}
.om-card:hover{border-color:rgba(34,197,94,.3)}
.om-active{border-color:rgba(34,197,94,.4)!important;background:rgba(34,197,94,.04)!important}
.om-top{display:flex;justify-content:space-between;align-items:flex-start;gap:4px;margin-bottom:6px}
.om-name{font-size:11px;font-weight:600;font-family:var(--font-mono);color:var(--text-primary);word-break:break-all;line-height:1.3}
.om-size{font-size:9px;color:var(--text-muted);background:var(--bg-tertiary);padding:2px 6px;border-radius:3px;white-space:nowrap;flex-shrink:0}
.om-meta{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:7px}
.om-tag{font-size:8px;color:var(--text-muted);background:var(--bg-tertiary);padding:1px 5px;border-radius:2px;font-family:var(--font-mono)}
.om-actions{display:flex;align-items:center}
.om-add-btn{font-size:9px!important;padding:4px 10px!important}
.om-in-stack{font-size:10px;color:#22c55e;font-weight:600}

/* Divider */
.ol-divider{height:1px;background:var(--border-glass);margin:10px 0}

/* Pull section */
.ol-pull-wrap{display:flex;flex-direction:column;gap:8px}
.ol-pull-label{font-size:10px;font-weight:700;color:var(--text-secondary)}
.ol-pull-row{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.ol-pull-input{flex:1;min-width:200px;max-width:400px}
.ol-pull-btn{font-size:10px!important;padding:6px 14px!important;white-space:nowrap}
.ol-pulling{background:rgba(234,179,8,.15)!important;color:#eab308!important;border:1px solid rgba(234,179,8,.3)!important;cursor:default!important}

/* Quick-pick chips */
.ol-chips{display:flex;align-items:center;gap:5px;flex-wrap:wrap}
.ol-chip-label{font-size:9px;color:var(--text-muted);white-space:nowrap}
.ol-chip{background:var(--bg-primary);border:1px solid var(--border-glass);border-radius:3px;color:var(--text-secondary);font-size:9px;padding:2px 7px;cursor:pointer;font-family:var(--font-mono);transition:all .15s}
.ol-chip:hover:not(:disabled){border-color:#22c55e;color:#22c55e}
.ol-chip:disabled{opacity:.35;cursor:default}

/* Terminal */
.ol-terminal{background:#0a0a0a;border:1px solid rgba(34,197,94,.25);border-radius:6px;overflow:hidden;margin-top:4px;max-height:300px;display:flex;flex-direction:column}
.ol-term-bar{background:#111;padding:5px 10px;font-size:9px;font-family:var(--font-mono);color:#22c55e;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid rgba(34,197,94,.15);flex-shrink:0}
.ol-blink{animation:blink .7s step-end infinite;color:#22c55e}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
.ol-term-body{overflow-y:auto;padding:8px 10px;flex:1;display:flex;flex-direction:column;gap:1px}
.ol-term-line{font-size:10px;font-family:var(--font-mono);color:#a3a3a3;line-height:1.6;word-break:break-all}
.ol-term-err{color:#ef4444!important}
.ol-term-ok{color:#22c55e!important;font-weight:600}
.ol-term-warn{color:#eab308!important}
.ol-term-sep{color:#2d2d2d!important;letter-spacing:1px}
.ol-term-info{color:#71717a!important;padding-left:8px}
.ol-term-prog{color:#38bdf8!important;font-size:10px;letter-spacing:.3px}
.ol-term-conn{color:#a78bfa!important}
.ol-term-spin{color:#94a3b8!important}
.ol-link{color:#38bdf8;text-decoration:underline;cursor:pointer}.ol-link:hover{color:#7dd3fc}
.ol-term-wait{font-size:10px;font-family:var(--font-mono);color:#52525b;animation:pulse 1.2s ease-in-out infinite}

/* Format hint */
.ol-format-hint{font-size:9px;color:var(--text-muted);display:flex;align-items:center;flex-wrap:wrap;gap:4px;margin-top:2px}
.ol-format-hint code{background:var(--bg-primary);border:1px solid var(--border-glass);padding:1px 5px;border-radius:2px;font-size:9px;color:var(--text-secondary)}
.ol-search-link{color:var(--accent-info);text-decoration:none;font-size:9px}.ol-search-link:hover{text-decoration:underline}

/* Terminal clear button */
.ol-term-clear{background:none;border:none;color:#52525b;font-size:9px;cursor:pointer;padding:0 4px}.ol-term-clear:hover{color:#ef4444}
@keyframes pulse{0%,100%{opacity:.4}50%{opacity:1}}

/* Budget Guard */
.bg-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px}

/* FIX v9.40: Gemini multi-key card */
.gemini-multi{grid-column:1/-1}
.gm-keys{display:flex;flex-direction:column;gap:5px;margin-top:6px}
.gm-row{display:flex;align-items:center;gap:6px}
.gm-label{font-size:9px;color:var(--text-muted);min-width:100px;flex-shrink:0}
.gm-row input{flex:1;background:var(--bg-primary);border:1px solid var(--border-glass);border-radius:3px;color:var(--text-primary);padding:5px 7px;font-size:10px;font-family:var(--font-mono)}
.gm-row input:focus{outline:none;border-color:#4285f4}
.bg-card{background:var(--bg-tertiary);border:1px solid var(--border-glass);border-radius:4px;padding:8px}
.bg-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
.bg-name{font-size:10px;font-weight:600;font-family:var(--font-mono)}
.bg-bar{height:6px;background:var(--bg-primary);border-radius:3px;overflow:hidden;margin-bottom:4px}
.bg-fill{height:100%;background:#22c55e;border-radius:3px;transition:width .3s}
.bg-danger{background:#ef4444}.bg-warn{background:#eab308}
.bg-info{display:flex;gap:8px;font-size:9px;color:var(--text-muted);flex-wrap:wrap}
.bg-w{color:#eab308;font-weight:600}
</style>
