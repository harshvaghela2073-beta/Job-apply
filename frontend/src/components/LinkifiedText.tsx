import { Fragment } from 'react'
import { ExternalLink } from './ExternalLink'

// http(s) URLs up to the next whitespace; trailing punctuation is trimmed off below.
const URL_RE = /https?:\/\/[^\s<>"]+/gi
const TRAILING = /[.,;:!?'")\]}]+$/

/** Plain text with every http(s) URL turned into a link that opens in a new tab. */
export function LinkifiedText({ text }: { text: string }) {
  const parts: (string | { url: string })[] = []
  let last = 0
  for (const match of text.matchAll(URL_RE)) {
    const start = match.index
    const url = match[0].replace(TRAILING, '')
    if (start > last) parts.push(text.slice(last, start))
    parts.push({ url })
    last = start + url.length
  }
  if (last < text.length) parts.push(text.slice(last))

  return (
    <>
      {parts.map((part, i) =>
        typeof part === 'string' ? (
          <Fragment key={i}>{part}</Fragment>
        ) : (
          <ExternalLink key={i} href={part.url} className="break-all">
            {part.url}
          </ExternalLink>
        ),
      )}
    </>
  )
}
