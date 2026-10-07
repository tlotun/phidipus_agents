// stores/wsStore.ts — WebSocket realtime store
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export interface WsMessage {
  event: string
  data: Record<string, unknown>
  ts: number
}

export const useWsStore = defineStore('ws', () => {
  const connected = ref(false)
  const lastMessage = ref<WsMessage | null>(null)
  const messages = ref<WsMessage[]>([])
  const heartbeat = ref<Record<string, unknown>>({})
  let socket: WebSocket | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null

  function connect(url: string) {
    if (socket?.readyState === WebSocket.OPEN) return

    socket = new WebSocket(url)

    socket.onopen = () => {
      connected.value = true
      console.log('[SpiderHub WS] Connected')
    }

    socket.onmessage = (ev) => {
      try {
        const msg: WsMessage = JSON.parse(ev.data)
        lastMessage.value = msg
        messages.value.unshift(msg)
        if (messages.value.length > 100) messages.value.pop()

        if (msg.event === 'heartbeat') {
          heartbeat.value = msg.data
        }
      } catch { /* ignore parse errors */ }
    }

    socket.onclose = () => {
      connected.value = false
      // Auto-reconnect after 3s
      reconnectTimer = setTimeout(() => connect(url), 3000)
    }

    socket.onerror = () => {
      connected.value = false
    }
  }

  function send(type: string, payload: Record<string, unknown> = {}) {
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type, ...payload }))
    }
  }

  function ping() { send('ping') }

  function runTask(goal: string) {
    send('run_task', { goal })
  }

  function disconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer)
    socket?.close()
    connected.value = false
  }

  const resource = computed(() => heartbeat.value?.resource as Record<string, unknown> ?? {})
  const shadowState = computed(() => heartbeat.value?.shadow as Record<string, unknown> ?? {})

  return {
    connected, lastMessage, messages, heartbeat,
    resource, shadowState,
    connect, send, ping, runTask, disconnect,
  }
})
