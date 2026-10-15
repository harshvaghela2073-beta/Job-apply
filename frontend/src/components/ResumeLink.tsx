import type { ReactNode } from 'react'
import { absoluteApiUrl, getToken } from '../api/client'

/**
 * Opens the resume in a new tab. With an app login the file is fetched with the token (a plain link
 * can't send it) and shown from a local copy; without one it is an ordinary link.
 */
export function ResumeLink({
  url,
  filename,
  className,
  children,
}: {
  url: string
  filename: string
  className?: string
  children: ReactNode
}) {
  const href = absoluteApiUrl(url)

  const open = async (event: React.MouseEvent<HTMLAnchorElement>) => {
    const token = getToken()
    if (!token) return // no login: the link opens as usual
    event.preventDefault()
    const tab = window.open('', '_blank') // opened during the click, so it isn't blocked as a pop-up
    try {
      const res = await fetch(href, { headers: { Authorization: `Bearer ${token}` } })
      if (!res.ok) throw new Error(String(res.status))
      const blobUrl = URL.createObjectURL(await res.blob())
      if (tab) tab.location.href = blobUrl
      else window.location.href = blobUrl
      window.setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000)
    } catch {
      tab?.close()
    }
  }

  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className={className} title={`Open ${filename}`} onClick={open}>
      {children}
    </a>
  )
}
