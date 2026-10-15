import type { ReactNode } from 'react'

/** 16px stroke icon in the current text colour. Decorative: label the button that holds it. */
function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      className="tp-icon"
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  )
}

export const ChevronDownIcon = () => (
  <Icon>
    <path d="M4 6l4 4 4-4" />
  </Icon>
)

export const ArrowUpIcon = () => (
  <Icon>
    <path d="M8 13V3M4 7l4-4 4 4" />
  </Icon>
)

export const ArrowDownIcon = () => (
  <Icon>
    <path d="M8 3v10M4 9l4 4 4-4" />
  </Icon>
)

export const CloseIcon = () => (
  <Icon>
    <path d="M4 4l8 8M12 4l-8 8" />
  </Icon>
)

export const PencilIcon = () => (
  <Icon>
    <path d="M10.5 2.5l3 3L6 13H3v-3z" />
  </Icon>
)
