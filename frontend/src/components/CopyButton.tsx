import { useEffect, useState } from 'react'
import { copyText } from '../lib/clipboard'

interface CopyButtonProps {
  text: string
  /** Button text, e.g. "Copy" or "Copy subject". */
  label?: string
  /** What is being copied, for screen readers, e.g. "answer to: What is your notice period?". */
  what?: string
  className?: string
  disabled?: boolean
  /** Shown on hover, e.g. why the button is disabled. */
  title?: string
  onCopied?: () => void
}

/** Copies `text` to the clipboard and says "Copied" for a moment. */
export function CopyButton({
  text,
  label = 'Copy',
  what,
  className = 'btn btn-sm',
  disabled,
  title,
  onCopied,
}: CopyButtonProps) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')

  useEffect(() => {
    if (state === 'idle') return
    const timer = window.setTimeout(() => setState('idle'), state === 'copied' ? 1600 : 3000)
    return () => window.clearTimeout(timer)
  }, [state])

  const onClick = async () => {
    const ok = await copyText(text)
    setState(ok ? 'copied' : 'failed')
    if (ok) onCopied?.()
  }

  return (
    <>
      <button
        type="button"
        className={`${className}${state === 'copied' ? ' btn-copied' : ''}`}
        onClick={onClick}
        disabled={disabled}
        aria-label={what ? `${label}: ${what}` : undefined}
        title={state === 'failed' ? 'The browser blocked the clipboard. Select the text and copy it by hand.' : title}
      >
        {state === 'copied' ? '✓ Copied' : state === 'failed' ? 'Copy failed' : label}
      </button>
      <span className="sr-only" role="status">
        {state === 'copied' ? 'Copied to clipboard' : state === 'failed' ? 'Copy failed' : ''}
      </span>
    </>
  )
}
