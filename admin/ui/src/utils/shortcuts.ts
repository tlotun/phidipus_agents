// utils/shortcuts.ts — Keyboard shortcut map for SpiderHub
import { onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'

export const SHORTCUT_MAP: Record<string, string> = {
  '1': '/home',
  '2': '/chrome',
  '3': '/skill-forge',
  '4': '/shadow',
  '5': '/task-graph',
  '6': '/memory',
  '7': '/resource',
  '8': '/security',
  '9': '/god-mode',
  '?': 'help',
}

export function useKeyboardShortcuts(opts: {
  onHelp?: () => void
  onCmdK?: () => void
  onCmdEnter?: () => void
  onCmdS?: () => void
}) {
  const router = useRouter()

  function handleKey(e: KeyboardEvent) {
    // Ignore when typing in inputs
    const tag = (e.target as HTMLElement).tagName
    if (tag === 'INPUT' || tag === 'TEXTAREA') return

    const key = e.key

    // Cmd/Ctrl combos
    if (e.metaKey || e.ctrlKey) {
      if (key === 'k') { e.preventDefault(); opts.onCmdK?.() }
      if (key === 'Enter') { e.preventDefault(); opts.onCmdEnter?.() }
      if (key === 's') { e.preventDefault(); opts.onCmdS?.() }
      return
    }

    // Number nav shortcuts
    if (SHORTCUT_MAP[key]) {
      const route = SHORTCUT_MAP[key]
      if (route === 'help') { opts.onHelp?.() }
      else { router.push(route) }
    }
  }

  onMounted(() => document.addEventListener('keydown', handleKey))
  onUnmounted(() => document.removeEventListener('keydown', handleKey))
}
