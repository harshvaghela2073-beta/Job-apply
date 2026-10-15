import { useEffect, type Dispatch, type SetStateAction } from 'react'

export const PAGE_SIZE = 50

/**
 * For paged lists: if the current page comes back empty while not on the first page (e.g. every
 * item on the last page was acted on), step back one page.
 */
export function useStepBackWhenEmpty(
  itemsOnPage: number | undefined,
  offset: number,
  setOffset: Dispatch<SetStateAction<number>>,
) {
  useEffect(() => {
    if (itemsOnPage === 0 && offset > 0) setOffset((o) => Math.max(0, o - PAGE_SIZE))
  }, [itemsOnPage, offset, setOffset])
}
