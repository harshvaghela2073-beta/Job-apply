import type { Template, TemplateInput, TemplateKind } from '../api/types'

/** Kinds that have a subject line (emails, and LinkedIn messages sent as InMails). */
export const usesSubject = (kind: TemplateKind) => kind !== 'answer'

/** Form state for adding or editing a template. */
export interface TemplateDraft {
  kind: TemplateKind
  question: string
  answer: string
  subject: string
}

export const emptyDraft = (kind: TemplateKind): TemplateDraft => ({ kind, question: '', answer: '', subject: '' })

export function draftFromTemplate(t: Template): TemplateDraft {
  // An unanswered template holds a placeholder like "TBD"; start from an empty box instead.
  return { kind: t.kind, question: t.question, answer: t.is_answered ? t.answer : '', subject: t.subject ?? '' }
}

/** Request body for a draft. Kinds without a subject line always send `subject: null`. */
export function toInput(d: TemplateDraft): TemplateInput {
  const subject = d.subject.trim()
  return {
    kind: d.kind,
    question: d.question.trim(),
    answer: d.answer.trim(),
    subject: usesSubject(d.kind) && subject ? subject : null,
  }
}

export const sameInput = (a: TemplateInput, b: TemplateInput) =>
  a.kind === b.kind && a.question === b.question && a.answer === b.answer && (a.subject ?? null) === (b.subject ?? null)

/** Case-insensitive match on question/name, text and subject. */
export function matchesSearch(t: Template, search: string): boolean {
  if (!search) return true
  const s = search.toLowerCase()
  return (
    t.question.toLowerCase().includes(s) ||
    t.answer.toLowerCase().includes(s) ||
    (t.subject ?? '').toLowerCase().includes(s)
  )
}
