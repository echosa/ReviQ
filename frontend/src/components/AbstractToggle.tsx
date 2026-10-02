/**
 * An expand/close abstract for a row in any list of records.
 *
 * Rows are usually clickable (they open a decision or scoring dialog), so
 * clicks here stop at the toggle instead of reaching the row.
 */
import { useState } from 'react'
import { hasMatch } from '../utils/highlight'
import Highlight from './Highlight'

export default function AbstractToggle({ abstract, highlight = null }: {
  abstract?: string | null
  /** Terms to mark inside the abstract; the toggle says when it holds one. */
  highlight?: RegExp | null
}) {
  const [open, setOpen] = useState(false)
  const text = abstract?.trim()

  if (!text) {
    return <p className="text-2xs text-ink-muted italic mt-1">No abstract</p>
  }

  return (
    <div className="mt-1" onClick={e => e.stopPropagation()}>
      <button type="button" aria-expanded={open}
        className="text-2xs text-info hover:underline"
        onClick={() => setOpen(o => !o)}>
        {open ? 'Hide abstract' : 'Show abstract'}
      </button>
      {!open && hasMatch(text, highlight) && (
        <span className="text-2xs text-ink-muted ml-2">· contains a match</span>
      )}
      {open && (
        <p className="text-xs text-ink-light leading-relaxed mt-1 max-h-48 overflow-y-auto">
          <Highlight text={text} pattern={highlight} />
        </p>
      )}
    </div>
  )
}
