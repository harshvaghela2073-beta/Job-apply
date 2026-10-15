/** The JOB·HUNT wordmark: a heavy "JOB" block plus "HUNT". Hidden from screen readers; name the wrapper. */
export function Wordmark() {
  return (
    <>
      <span className="brand-job" aria-hidden="true">
        JOB
      </span>
      <span className="brand-word" aria-hidden="true">
        HUNT
      </span>
    </>
  )
}
