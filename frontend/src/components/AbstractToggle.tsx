/**
 * An expand/close abstract for a row in any list of records, and the
 * show-all / hide-all buttons for the list it sits in.
 *
 * Rows are usually clickable (they open a decision or scoring dialog), so
 * clicks here stop at the toggle instead of reaching the row.
 *
 * Wrap a list in <AbstractList> to give it the buttons. Each row still
 * toggles on its own afterwards; the buttons only set every row at once.
 */
import { createContext, ReactNode, useContext, useEffect, useRef, useState } from 'react'
import { hasMatch } from '../utils/highlight'
import Highlight from './Highlight'

interface Command { open: boolean; seq: number }
const AbstractCommand = createContext<{ command: Command; send: (open: boolean) => void } | null>(null)

/** A list's rows and its show-all / hide-all buttons. */
export function AbstractList({ children }: { children: ReactNode }) {
  const [command, setCommand] = useState<Command>({ open: false, seq: 0 })
  const send = (open: boolean) => setCommand(c => ({ open, seq: c.seq + 1 }))
  return (
    <AbstractCommand.Provider value={{ command, send }}>
      <div className="flex justify-end gap-1 mb-1.5">
        <button type="button" className="btn-ghost btn-sm" onClick={() => send(true)}>Show all abstracts</button>
        <button type="button" className="btn-ghost btn-sm" onClick={() => send(false)}>Hide all abstracts</button>
      </div>
      {children}
    </AbstractCommand.Provider>
  )
}

export default function AbstractToggle({ abstract, highlight = null }: {
  abstract?: string | null
  /** Terms to mark inside the abstract; the toggle says when it holds one. */
  highlight?: RegExp | null
}) {
  const list = useContext(AbstractCommand)
  const [open, setOpen] = useState(false)
  const text = abstract?.trim()

  // Follow the list's buttons, but not the command that was already there when
  // this row appeared (a row loaded later should start closed).
  const seen = useRef(list?.command.seq ?? 0)
  useEffect(() => {
    if (list && list.command.seq !== seen.current) {
      seen.current = list.command.seq
      setOpen(list.command.open)
    }
  }, [list?.command.seq])   // eslint-disable-line react-hooks/exhaustive-deps

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
