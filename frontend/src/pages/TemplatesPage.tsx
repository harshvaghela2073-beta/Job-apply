import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { TEMPLATE_KINDS, type Template, type TemplateInput, type TemplateKind } from '../api/types'
import { Collapsible } from '../components/Collapsible'
import { EmptyState, QueryBoundary } from '../components/DataState'
import { PageHeader, RefreshButton } from '../components/PageHeader'
import { QuestionCard } from '../components/QuestionCard'
import { SearchPriority } from '../components/SearchPriority'
import { TemplateCard } from '../components/TemplateCard'
import { TemplateForm } from '../components/TemplateForm'
import { useAction } from '../hooks/useAction'
import { useCounts } from '../hooks/useCounts'
import { useQuery } from '../hooks/useQuery'
import { useSectionOpen, type SectionOpen } from '../hooks/useSectionOpen'
import { TEMPLATE_KIND_LABEL, TEMPLATE_KIND_SINGULAR } from '../lib/format'
import { emptyDraft, matchesSearch } from '../lib/templates'
import '../templates-profile.css'

const EMPTY_TITLE: Record<TemplateKind, string> = {
  answer: 'No application answers yet.',
  linkedin_message: 'No LinkedIn messages yet.',
  email: 'No email templates yet.',
}

function AddTemplate({ kind, onAdded, onCancel }: { kind: TemplateKind; onAdded: (t: Template) => void; onCancel: () => void }) {
  const create = useAction((input: TemplateInput) => api.createTemplate(input))
  return (
    <TemplateForm
      initial={emptyDraft(kind)}
      action={create}
      submitLabel="Add template"
      pendingLabel="Adding…"
      onDone={onAdded}
      onCancel={onCancel}
    />
  )
}

export function TemplatesPage() {
  const { refresh: refreshCounts } = useCounts()
  const questions = useQuery('questions:unanswered', (signal) => api.unansweredQuestions(signal))
  // All kinds in one request: the lists are small, and it gives the per-kind counts for free.
  const templates = useQuery('templates', (signal) => api.templates(undefined, signal))
  const [adding, setAdding] = useState<TemplateKind | null>(null)
  const [search, setSearch] = useState('')
  const [onlyUnanswered, setOnlyUnanswered] = useState(false)

  // The page owns the groups' open state so it can open the group a template was saved to.
  const groups: Record<TemplateKind, SectionOpen> = {
    answer: useSectionOpen('templates-answer'),
    linkedin_message: useSectionOpen('templates-linkedin_message'),
    email: useSectionOpen('templates-email'),
  }
  const reveal = (kind: TemplateKind) => groups[kind].setOpen(true)

  // After the add form closes, focus goes back to that group's Add button.
  const pageRef = useRef<HTMLElement>(null)
  const pendingFocus = useRef<string | null>(null)
  useEffect(() => {
    const selector = pendingFocus.current
    if (!selector) return
    pendingFocus.current = null
    pageRef.current?.querySelector<HTMLElement>(selector)?.focus()
  })

  const reloadAll = () => {
    questions.reload()
    templates.reload()
    refreshCounts()
  }
  const reloadTemplates = () => {
    templates.reload()
    refreshCounts()
  }
  const onTemplateChanged = (saved?: Template) => {
    if (saved) reveal(saved.kind)
    reloadTemplates()
  }
  const startAdd = (kind: TemplateKind) => {
    setAdding(kind)
    reveal(kind)
  }
  const stopAdd = (kind: TemplateKind) => {
    setAdding(null)
    pendingFocus.current = `[data-add-kind="${kind}"]`
  }

  const query = search.trim()
  const filtering = query !== '' || onlyUnanswered
  const unansweredTemplates = templates.data?.items.filter((t) => !t.is_answered).length ?? 0
  const questionCount = questions.data?.total ?? 0

  return (
    <main className="page" ref={pageRef}>
      <PageHeader
        title="Template Library"
        actions={<RefreshButton onClick={reloadAll} loading={questions.loading || templates.loading} />}
      />

      <div className="tl-stack">
        <SearchPriority />

        <Collapsible
          id="unanswered-questions"
          title="Unanswered questions"
          meta={questionCount > 0 && <span className="count-bubble">{questionCount}</span>}
          className={questionCount > 0 ? 'panel-attention' : undefined}
        >
          <QueryBoundary
            query={questions}
            loadingLabel="Loading questions…"
            isEmpty={(d) => d.items.length === 0}
            empty={<EmptyState title="No unanswered questions." />}
          >
            {(d) => (
              <div className="question-list">
                {d.items.map((q) => (
                  <QuestionCard
                    key={q.id}
                    question={q}
                    onAnswered={() => {
                      reveal('answer')
                      reloadAll()
                    }}
                  />
                ))}
              </div>
            )}
          </QueryBoundary>
        </Collapsible>

        <div className="tl-toolbar">
          <input
            type="search"
            className="search"
            placeholder="Search templates"
            aria-label="Search templates"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <label className="check">
            <input type="checkbox" checked={onlyUnanswered} onChange={(e) => setOnlyUnanswered(e.target.checked)} />
            Unanswered only{unansweredTemplates ? ` (${unansweredTemplates})` : ''}
          </label>
        </div>

        <QueryBoundary query={templates} loadingLabel="Loading templates…">
          {(d) => (
            <div className="tl-stack">
              {TEMPLATE_KINDS.map((kind) => {
                const ofKind = d.items.filter((t) => t.kind === kind)
                const rows = ofKind.filter((t) => (!onlyUnanswered || !t.is_answered) && matchesSearch(t, query))
                return (
                  <Collapsible
                    key={kind}
                    id={`templates-${kind}`}
                    state={groups[kind]}
                    title={TEMPLATE_KIND_LABEL[kind]}
                    meta={
                      <span className="cl-count">
                        {filtering ? `${rows.length} of ${ofKind.length}` : ofKind.length}
                      </span>
                    }
                    actions={
                      <button
                        type="button"
                        className="btn btn-sm"
                        data-add-kind={kind}
                        aria-label={`Add ${TEMPLATE_KIND_SINGULAR[kind].toLowerCase()}`}
                        onClick={() => startAdd(kind)}
                        disabled={adding === kind}
                      >
                        + Add
                      </button>
                    }
                  >
                    {adding === kind && (
                      <AddTemplate
                        kind={kind}
                        onAdded={(t) => {
                          stopAdd(kind)
                          reveal(t.kind)
                          reloadTemplates()
                        }}
                        onCancel={() => stopAdd(kind)}
                      />
                    )}
                    {ofKind.length === 0 ? (
                      adding !== kind && <EmptyState title={EMPTY_TITLE[kind]} />
                    ) : rows.length === 0 ? (
                      <EmptyState title="No matches." />
                    ) : (
                      <div className="template-list">
                        {rows.map((t) => (
                          <TemplateCard key={`${t.id}:${t.updated_at}`} template={t} onChanged={onTemplateChanged} />
                        ))}
                      </div>
                    )}
                  </Collapsible>
                )
              })}
            </div>
          )}
        </QueryBoundary>
      </div>
    </main>
  )
}
