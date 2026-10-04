/**
 * Is snowballing still finding things?
 *
 * Snowballing has no natural end: every included paper has references and
 * citations, and more similar work keeps turning up. This lays out the numbers a
 * decision to stop can rest on, so it is made from evidence and not from a
 * feeling that there might be more:
 *
 *   yield      full-text includes per new record. A falling yield means
 *              each round is working harder for less.
 *   re-found   of the papers an iteration found that the project already had,
 *              the share that had been included. A rising share means the
 *              citation neighbourhood is closing: new searches keep landing on
 *              papers already in the review.
 *   core       the share of full-text includes whose title mentions your core
 *              terms: a check that what is being included is not drifting.
 */
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { getSnowballingYield } from '../api/client'
import type { SnowballYield } from '../api/types'
import { Card, CardHeader } from './ui'

const TERMS_KEY = 'reviq_snowball_core_terms'
const DEFAULT_TERMS = 'sarcas, iron, satir'

function readTerms(): string {
  try { return localStorage.getItem(TERMS_KEY) ?? DEFAULT_TERMS } catch { return DEFAULT_TERMS }
}

const pct = (v: number | null) => (v === null ? '—' : `${v}%`)
const n = (v: number) => v.toLocaleString()

function Row({ r, hasCore }: { r: SnowballYield; hasCore: boolean }) {
  const fullTextPending = r.yield_pct === null
  return (
    <tr className="border-b border-rule last:border-0">
      <td className="py-2 pr-2 font-medium text-ink">
        {r.iteration_number} <span className="text-2xs font-normal text-ink-muted capitalize">{r.iteration_type}</span>
      </td>
      <td className="py-2 text-right tabular-nums">{n(r.retrieved)}</td>
      <td className="py-2 text-right tabular-nums">
        {n(r.duplicates)} <span className="text-2xs text-ink-muted">({pct(r.duplicate_pct)})</span>
      </td>
      <td className="py-2 text-right tabular-nums">{n(r.screening_included)}</td>
      <td className="py-2 text-right tabular-nums">{n(r.fulltext_included)}
        {fullTextPending && r.screening_included > 0 &&
          <span className="text-2xs text-ink-muted" title="Full-text assessment has not started"> (pending)</span>}
      </td>
      <td className="py-2 text-right tabular-nums font-semibold">{fullTextPending ? '—' : pct(r.yield_pct)}</td>
      <td className="py-2 text-right tabular-nums">
        {r.refound_known === 0 ? '—' : <>{pct(r.refound_included_pct)}{' '}
          <span className="text-2xs text-ink-muted">({n(r.refound_included)} of {n(r.refound_known)})</span></>}
      </td>
      {hasCore && (
        <td className="py-2 text-right tabular-nums">
          {r.core_in_title_pct === null || r.fulltext_included === 0 ? '—' : <>{pct(r.core_in_title_pct)}{' '}
            <span className="text-2xs text-ink-muted">({n(r.core_in_title ?? 0)} of {n(r.fulltext_included)})</span></>}
        </td>
      )}
    </tr>
  )
}

export default function YieldPanel({ pid }: { pid: number }) {
  const [typed, setTyped] = useState(readTerms)
  const [terms, setTerms] = useState(typed)            // what the numbers use; changes on Enter or blur
  const apply = () => {
    setTerms(typed)
    try { localStorage.setItem(TERMS_KEY, typed) } catch { /* not remembered */ }
  }

  const { data: rows = [], isLoading, isError } = useQuery({
    queryKey: ['snowballing', pid, 'yield', terms],
    queryFn: () => getSnowballingYield(pid, terms),
  })

  if (isLoading || (!isError && rows.length === 0)) return null
  const hasCore = terms.trim() !== ''

  return (
    <Card>
      <CardHeader title="Yield and saturation" />
      <p className="text-xs text-ink-muted mb-3">
        Is snowballing still finding things? A <strong>falling yield</strong> and a <strong>rising share of
        re-found papers you had already included</strong> both mean the search is running out. Decide your stopping
        rule before the next round, then let these numbers apply it.
      </p>

      {isError ? (
        <p role="alert" className="text-xs text-exclude">Could not load the yield figures.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-ink-muted uppercase tracking-label border-b border-rule">
                <th className="text-left pb-2 font-semibold">Iteration</th>
                <th className="text-right pb-2 font-semibold">Records</th>
                <th className="text-right pb-2 font-semibold">Duplicates</th>
                <th className="text-right pb-2 font-semibold leading-tight">Through<br />screening</th>
                <th className="text-right pb-2 font-semibold leading-tight">Through<br />full-text</th>
                <th className="text-right pb-2 font-semibold" title="Full-text includes per new (non-duplicate) record">Yield</th>
                <th className="text-right pb-2 font-semibold leading-tight"
                  title="Of the papers this iteration found that the project already had, how many had passed screening">
                  Re-found that<br />you had included</th>
                {hasCore && <th className="text-right pb-2 font-semibold leading-tight"
                  title="Full-text includes whose title mentions a core term">Core in<br />title</th>}
              </tr>
            </thead>
            <tbody>{rows.map(r => <Row key={r.iteration_number} r={r} hasCore={hasCore} />)}</tbody>
          </table>
        </div>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-2 text-xs text-ink-muted">
        <label htmlFor="core-terms">Core terms</label>
        <input id="core-terms" className="input !w-64 !py-1" aria-label="Core terms" value={typed}
          onChange={e => setTyped(e.target.value)} onBlur={apply}
          onKeyDown={e => { if (e.key === 'Enter') apply() }} />
        <span>Comma-separated, matched at the start of a word. They show whether included work is drifting off topic.</span>
      </div>
    </Card>
  )
}
