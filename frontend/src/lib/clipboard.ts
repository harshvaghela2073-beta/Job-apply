/**
 * Copies text to the clipboard. Uses the async Clipboard API when available (secure contexts),
 * and falls back to a hidden textarea + `execCommand('copy')` (e.g. plain http on a LAN address).
 * Resolves to false when both fail, so the caller can say so.
 */
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Permission denied or document not focused: try the fallback.
    }
  }
  return legacyCopy(text)
}

function legacyCopy(text: string): boolean {
  const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.setAttribute('aria-hidden', 'true')
  Object.assign(area.style, { position: 'fixed', top: '0', left: '0', opacity: '0', pointerEvents: 'none' })
  document.body.appendChild(area)
  area.select()
  let ok = false
  try {
    ok = document.execCommand('copy')
  } catch {
    ok = false
  } finally {
    area.remove()
    previous?.focus()
  }
  return ok
}
