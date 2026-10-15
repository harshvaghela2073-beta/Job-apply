import { useCallback, useEffect, useId, useRef, useState, type FocusEvent } from 'react'
import { useLocation } from 'react-router-dom'

/**
 * Open/close behaviour shared by the top-bar popovers (profile card, platform connections):
 * focus moves into the card when it opens; Escape closes it and returns focus to the button;
 * a click/tap outside, a route change, or keyboard focus leaving the wrapper closes it.
 *
 * Put `wrapRef` and `onBlur` on the wrapper, `buttonRef` on the toggle button, and
 * `popRef` + `id={popId}` + `tabIndex={-1}` on the card.
 */
export function usePopover() {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const popRef = useRef<HTMLDivElement>(null)
  const popId = useId()

  const close = useCallback((returnFocus: boolean) => {
    setOpen(false)
    if (returnFocus) buttonRef.current?.focus()
  }, [])

  /** Moves focus to the card itself, e.g. after the focused button inside it disappears. */
  const focusPopover = useCallback(() => popRef.current?.focus(), [])

  // Move focus into the card when it opens.
  useEffect(() => {
    if (open) popRef.current?.focus()
  }, [open])

  // Close on Escape and on a click/tap outside.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        close(true)
      }
    }
    const onPointer = (e: PointerEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) close(false)
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('pointerdown', onPointer)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('pointerdown', onPointer)
    }
  }, [open, close])

  // Close when the route changes (adjusting state during render, as React recommends).
  const { pathname } = useLocation()
  const [shownPath, setShownPath] = useState(pathname)
  if (shownPath !== pathname) {
    setShownPath(pathname)
    setOpen(false)
  }

  // Close when keyboard focus moves to something else on the page (not when the window loses focus).
  const onBlur = (e: FocusEvent<HTMLDivElement>) => {
    const next = e.relatedTarget
    if (open && next instanceof Node && !e.currentTarget.contains(next)) setOpen(false)
  }

  return { open, setOpen, close, focusPopover, wrapRef, buttonRef, popRef, popId, onBlur }
}
