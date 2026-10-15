import type { ReactNode } from 'react'

interface ExternalLinkProps {
  href: string | null | undefined
  children: ReactNode
  className?: string
  title?: string
  onClick?: () => void
}

/** Link that opens in a new tab. Renders nothing when there is no URL. */
export function ExternalLink({ href, children, className, title, onClick }: ExternalLinkProps) {
  if (!href) return null
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className={className}
      title={title ?? href}
      onClick={onClick}
      onAuxClick={onClick}
    >
      {children}
    </a>
  )
}
