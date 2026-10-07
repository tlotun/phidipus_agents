<script setup lang="ts">
// TelegramView.vue — v9.21 Telegram Bot Configuration
import { onMounted, ref, computed } from 'vue'
import { api } from '@/utils/api'

const tokenInput = ref('')
const adminInput = ref('')
const showToken = ref(false)
const saving = ref(false)
const testing = ref(false)
const testResult = ref<{ok:boolean,bot_username?:string,bot_name?:string,error?:string}|null>(null)

// ★ sessionStorage: persist telegram status across tab switches
function _loadTg(): {token: boolean, admins: number[], masked: string} {
  try { return JSON.parse(sessionStorage.getItem('nb_tg') || '{"token":false,"admins":[],"masked":""}') } catch { return {token:false,admins:[],masked:""} }
}
const _tg = _loadTg()

// Current config from server
const tokenSet = ref(_tg.token)
const tokenMasked = ref(_tg.masked)
const adminIds = ref<number[]>(_tg.admins)
const botUsername = ref('')
const botName = ref('')
const saveMsg = ref('')

function _saveTg() {
  sessionStorage.setItem('nb_tg', JSON.stringify({token: tokenSet.value, admins: adminIds.value, masked: tokenMasked.value}))
}

async function fetchConfig() {
  try {
    const res = await api.get('/api/v2/telegram/config')
    // Trust API response fully
    tokenSet.value = !!res.data.token_set
    if (res.data.token_masked) tokenMasked.value = res.data.token_masked
    adminIds.value = res.data.admin_ids || []
    if (adminIds.value.length > 0) adminInput.value = adminIds.value.join(', ')
    if (res.data.bot_username) botUsername.value = res.data.bot_username
    if (res.data.bot_name) botName.value = res.data.bot_name
    _saveTg()
  } catch { /* ignore */ }
}

async function saveToken() {
  if (!tokenInput.value.trim()) return
  saving.value = true; saveMsg.value = ''
  try {
    const res = await api.post('/api/v2/telegram/token', { token: tokenInput.value.trim() })
    if (res.data.ok) {
      saveMsg.value = '✅ Token saved!'
      tokenSet.value = true
      tokenMasked.value = res.data.token_masked || '****'
      tokenInput.value = ''
      _saveTg()
      // Auto-test
      await testToken()
      await fetchConfig()
    }
  } catch (e: any) {
    saveMsg.value = '❌ ' + (e?.response?.data?.detail || e.message)
  } finally { saving.value = false }
}

async function saveAdmins() {
  saving.value = true; saveMsg.value = ''
  try {
    const ids = adminInput.value.split(/[,\s]+/).filter(Boolean).map(Number).filter(n => !isNaN(n) && n > 0)
    if (!ids.length) { saveMsg.value = '❌ Nhập ít nhất 1 Admin ID'; saving.value = false; return }
    const res = await api.post('/api/v2/telegram/admins', { admin_ids: ids })
    if (res.data.ok) {
      saveMsg.value = '✅ Admin IDs saved!'
      adminIds.value = res.data.admin_ids || ids
      _saveTg()
      await fetchConfig()
    }
  } catch (e: any) {
    saveMsg.value = '❌ ' + (e?.response?.data?.detail || e.message)
  } finally { saving.value = false }
}

async function testToken() {
  testing.value = true; testResult.value = null
  try {
    const res = await api.post('/api/v2/telegram/test')
    testResult.value = res.data
    if (res.data.ok) {
      botUsername.value = res.data.bot_username
      botName.value = res.data.bot_name
    }
  } catch (e: any) {
    testResult.value = { ok: false, error: e.message }
  } finally { testing.value = false }
}

const statusColor = computed(() => tokenSet.value && adminIds.value.length > 0 ? 'var(--success)' : 'var(--danger)')
const tokenDot = computed(() => tokenSet.value ? 'dot-ok' : 'dot-err')
const adminDot = computed(() => adminIds.value.length > 0 ? 'dot-ok' : 'dot-err')

onMounted(async () => {
  await fetchConfig()
  // Auto-test if token exists
  if (tokenSet.value) await testToken()
})
</script>

<template>
  <div class="tg-view">
    <div class="view-header">
      <h2>Telegram Bot</h2>
      <div class="header-status">
        <span class="status-dot" :class="tokenDot"></span>
        <span class="status-label">{{ tokenSet.value && adminIds.length > 0 ? 'Đã kết nối' : tokenSet ? 'Chưa đủ config' : 'Chưa cấu hình' }}</span>
      </div>
    </div>

    <!-- Status overview -->
    <div class="status-cards">
      <div class="status-card glass" :class="tokenSet ? 'sc-ok' : 'sc-err'">
        <span class="sc-dot" :class="tokenDot"></span>
        <span class="sc-label">Bot Token</span>
        <span class="sc-val">{{ tokenSet ? '✓ Đã thêm' : '✗ Chưa có' }}</span>
      </div>
      <div class="status-card glass" :class="adminIds.length > 0 ? 'sc-ok' : 'sc-err'">
        <span class="sc-dot" :class="adminDot"></span>
        <span class="sc-label">Admin IDs</span>
        <span class="sc-val">{{ adminIds.length > 0 ? `✓ ${adminIds.length} admin` : '✗ Chưa có' }}</span>
      </div>
    </div>

    <!-- Bot info (if connected) -->
    <div v-if="botUsername" class="bot-info glass">
      <span class="bot-avatar">🤖</span>
      <div>
        <div class="bot-name">{{ botName || 'Phidipus Bot' }}</div>
        <div class="bot-user mono text-xs">@{{ botUsername }}</div>
      </div>
      <a :href="`https://t.me/${botUsername}`" target="_blank" class="open-link">Mở Telegram →</a>
    </div>

    <!-- Step 1: Bot Token -->
    <div class="config-card glass">
      <div class="card-header">
        <span class="step-num">1</span>
        <div>
          <div class="card-title">Bot Token</div>
          <div class="card-desc">Tạo bot tại <a href="https://t.me/BotFather" target="_blank">@BotFather</a> → /newbot → copy token</div>
        </div>
      </div>

      <div v-if="tokenSet" class="current-val">
        <span class="text-xs text-muted">Current:</span>
        <span class="mono">{{ tokenMasked }}</span>
        <button class="btn-ghost btn-sm" @click="testToken" :disabled="testing">
          {{ testing ? 'Đang kiểm tra...' : 'Kiểm tra kết nối' }}
        </button>
      </div>

      <div v-if="testResult" class="test-result" :class="testResult.ok ? 'test-ok' : 'test-err'">
        <span v-if="testResult.ok">✅ Bot đã kết nối: @{{ testResult.bot_username }} ({{ testResult.bot_name }})</span>
        <span v-else>❌ {{ testResult.error }}</span>
      </div>

      <div class="input-row">
        <div class="input-wrap">
          <input
            v-model="tokenInput"
            :type="showToken ? 'text' : 'password'"
            class="tg-input"
            placeholder="123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
            @keydown.enter="saveToken"
          />
          <button class="eye-btn" @click="showToken = !showToken">{{ showToken ? '🙈' : '👁' }}</button>
        </div>
        <button class="save-btn" :disabled="!tokenInput.trim() || saving" @click="saveToken">
          {{ saving ? '...' : tokenSet ? 'Cập nhật Token' : 'Lưu Token' }}
        </button>
      </div>
    </div>

    <!-- Step 2: Admin IDs -->
    <div class="config-card glass">
      <div class="card-header">
        <span class="step-num">2</span>
        <div>
          <div class="card-title">Admin Chat IDs</div>
          <div class="card-desc">
            Chỉ các ID này mới được gửi lệnh cho bot.
            <a href="https://t.me/userinfobot" target="_blank">@userinfobot</a> để lấy ID của bạn.
          </div>
        </div>
      </div>

      <div v-if="adminIds.length" class="current-val">
        <span class="text-xs text-muted">Current:</span>
        <span v-for="id in adminIds" :key="id" class="id-chip mono">{{ id }}</span>
      </div>

      <div class="input-row">
        <input
          v-model="adminInput"
          class="tg-input"
          placeholder="123456789, 987654321"
          @keydown.enter="saveAdmins"
        />
        <button class="save-btn" :disabled="!adminInput.trim() || saving" @click="saveAdmins">
          {{ saving ? '...' : 'Lưu IDs' }}
        </button>
      </div>
    </div>

    <!-- Step 3: Instructions -->
    <div class="config-card glass">
      <div class="card-header">
        <span class="step-num">3</span>
        <div>
          <div class="card-title">Kích hoạt</div>
          <div class="card-desc">Sau khi save token + admin ID, restart Phidipus để kết nối bot.</div>
        </div>
      </div>
      <div class="instructions">
        <div class="inst-line"><span class="mono">Ctrl+C</span> → dừng Phidipus</div>
        <div class="inst-line"><span class="mono">./Phidipus_Agent.command</span> → chạy lại</div>
        <div class="inst-line">Mở Telegram → gửi <span class="mono">/start</span> cho bot</div>
      </div>
    </div>

    <!-- Status message -->
    <div v-if="saveMsg" class="save-msg" :class="saveMsg.startsWith('✅') ? 'msg-ok' : 'msg-err'">
      {{ saveMsg }}
    </div>
  </div>
</template>

<style scoped>
.tg-view{display:flex;flex-direction:column;gap:16px}
.view-header{display:flex;align-items:center;gap:12px}
.view-header h2{font-size:18px;font-weight:700}
.header-status{display:flex;align-items:center;gap:8px;margin-left:8px}
.status-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.dot-ok{background:var(--success);box-shadow:0 0 6px rgba(16,185,129,.4)}
.dot-err{background:var(--danger);box-shadow:0 0 6px rgba(239,68,68,.4)}
.status-label{font-size:12px;color:var(--text-muted)}

.status-cards{display:flex;gap:12px}
.status-card{display:flex;align-items:center;gap:10px;padding:12px 18px;flex:1}
.sc-ok{border-color:rgba(16,185,129,.2)!important}
.sc-err{border-color:rgba(239,68,68,.2)!important}
.sc-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.sc-label{font-size:13px;font-weight:500}
.sc-val{margin-left:auto;font-size:12px;font-weight:600}
.sc-ok .sc-val{color:var(--success)}
.sc-err .sc-val{color:var(--danger)}

.badge{border:1px solid;border-radius:100px;padding:3px 12px;font-size:11px;font-weight:600}

.bot-info{display:flex;align-items:center;gap:12px;padding:16px}
.bot-avatar{font-size:32px}
.bot-name{font-size:15px;font-weight:600}
.bot-user{color:var(--text-muted)}
.open-link{margin-left:auto;font-size:12px;color:var(--accent-info);text-decoration:none}
.open-link:hover{text-decoration:underline}

.config-card{padding:20px}
.card-header{display:flex;align-items:flex-start;gap:14px;margin-bottom:16px}
.step-num{width:28px;height:28px;border-radius:50%;background:var(--accent-spider);color:#000;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:700;flex-shrink:0}
.card-title{font-size:15px;font-weight:600}
.card-desc{font-size:12px;color:var(--text-muted);margin-top:2px;line-height:1.5}
.card-desc a{color:var(--accent-info);text-decoration:none}
.card-desc a:hover{text-decoration:underline}

.current-val{display:flex;align-items:center;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.id-chip{background:rgba(255,255,255,0.06);border:1px solid var(--border-glass);border-radius:var(--radius-xs);padding:2px 10px;font-size:12px}

.input-row{display:flex;gap:8px}
.input-wrap{flex:1;position:relative;display:flex}
.tg-input{flex:1;background:var(--bg-primary);border:1px solid var(--border-glass);border-radius:var(--radius-sm);color:var(--text-primary);padding:10px 40px 10px 14px;font-size:13px;font-family:var(--font-mono);transition:border-color .15s}
.tg-input:focus{outline:none;border-color:var(--accent-spider)}
.tg-input::placeholder{color:var(--text-muted)}
.eye-btn{position:absolute;right:4px;top:50%;transform:translateY(-50%);background:none;border:none;cursor:pointer;font-size:16px;padding:4px 6px;opacity:.5}
.eye-btn:hover{opacity:1}

.save-btn{background:var(--accent-spider);color:#000;border:none;border-radius:var(--radius-sm);padding:10px 20px;font-size:13px;font-weight:600;cursor:pointer;transition:all .15s;white-space:nowrap}
.save-btn:hover{background:#d49b0e}
.save-btn:disabled{opacity:.3;cursor:not-allowed}

.test-result{padding:8px 12px;border-radius:var(--radius-xs);font-size:12px;margin-bottom:12px}
.test-ok{background:rgba(16,185,129,.08);color:var(--success)}
.test-err{background:rgba(239,68,68,.08);color:var(--danger)}

.instructions{display:flex;flex-direction:column;gap:6px}
.inst-line{font-size:13px;color:var(--text-secondary)}
.inst-line .mono{background:rgba(255,255,255,.06);padding:2px 8px;border-radius:4px;font-size:12px}

.save-msg{padding:10px 16px;border-radius:var(--radius-sm);font-size:13px;text-align:center}
.msg-ok{background:rgba(16,185,129,.08);color:var(--success)}
.msg-err{background:rgba(239,68,68,.08);color:var(--danger)}

.btn-ghost{background:none;border:1px solid var(--border-glass);color:var(--text-secondary);border-radius:var(--radius-xs);cursor:pointer;transition:all .15s}
.btn-ghost:hover{border-color:var(--text-muted);color:var(--text-primary)}
.btn-sm{padding:4px 10px;font-size:11px}
</style>
