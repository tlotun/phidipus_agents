// utils/api.ts — Axios + WebSocket wrapper
import axios from 'axios'

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8912',
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

api.interceptors.response.use(
  (r) => r,
  (err) => {
    console.error('[SpiderHub API]', err.message)
    return Promise.reject(err)
  }
)
