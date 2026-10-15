import { initials } from '../lib/profile'

/** Circle with the person's initials ("?" when there is no name). Decorative: label the parent. */
export function Avatar({ name, size = 'md' }: { name: string | null | undefined; size?: 'sm' | 'md' | 'lg' }) {
  return (
    <span className={`avatar avatar-${size}`} aria-hidden="true">
      {initials(name)}
    </span>
  )
}
