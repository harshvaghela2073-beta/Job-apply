import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import type { UnansweredQuestion } from '../api/types'
import { useAction } from '../hooks/useAction'
import { PlatformTag } from './Badge'
import { InlineError } from './DataState'

/** An unanswered form question with a box to answer it (adds a Template Library row). */
export function QuestionCard({ question, onAnswered }: { question: UnansweredQuestion; onAnswered: () => void }) {
  const [answer, setAnswer] = useState('')
  const submit = useAction(() => api.answerQuestion(question.id, { answer: answer.trim() }))

  const onSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    const result = await submit.run()
    // 409 means it was already answered elsewhere: refresh so it drops off the list.
    if (result.ok || (result.error instanceof ApiError && result.error.status === 409)) onAnswered()
  }

  const inputId = `answer-${question.id}`
  return (
    <form className="question-card" onSubmit={onSubmit}>
      <label htmlFor={inputId} className="question-text">
        {question.question}
      </label>
      {question.platform && (
        <div className="job-card-meta">
          <PlatformTag platform={question.platform} />
        </div>
      )}
      <div className="question-answer">
        <textarea
          id={inputId}
          value={answer}
          onChange={(e) => setAnswer(e.target.value)}
          rows={2}
          placeholder="Your answer…"
          disabled={submit.pending}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) e.currentTarget.form?.requestSubmit()
          }}
        />
        <button type="submit" className="btn btn-primary" disabled={submit.pending || answer.trim() === ''}>
          {submit.pending ? 'Saving…' : 'Save answer'}
        </button>
      </div>
      <InlineError message={submit.error} onDismiss={submit.clearError} />
    </form>
  )
}
