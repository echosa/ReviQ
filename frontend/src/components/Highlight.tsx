/** `text` with the parts a pattern matched marked, for showing why a record
 *  matched. Plain text when there is no pattern. */
import { splitMatches } from '../utils/highlight'

export const MARK_STYLE = { backgroundColor: 'rgba(250, 204, 21, 0.4)', color: 'inherit' } as const

export default function Highlight({ text, pattern }: { text: string; pattern: RegExp | null }) {
  return (
    <>
      {splitMatches(text, pattern).map((p, i) =>
        p.hit
          ? <mark key={i} style={MARK_STYLE} className="rounded-sm px-0.5">{p.text}</mark>
          : <span key={i}>{p.text}</span>)}
    </>
  )
}
