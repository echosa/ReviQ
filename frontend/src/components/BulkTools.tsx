/**
 * Bulk actions and duplicate review, for the Screening and Eligibility pages.
 *
 * Both work on the active reviewer's own decisions and go through the same
 * server-side checks: only original records are offered, nothing the reviewer
 * has already decided is overwritten by a bulk action, and a duplicate group
 * can never lose every copy.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useProject } from '../App'
import {
  apiErrors, applyBulkDecisions, getDuplicateCandidates, getExclusionCriteria,
  getInclusionCriteria, getPapers, getReviewers, matchUndecided, resolveDuplicates,
} from '../api/client'
import type {
  BulkMatchResult, DuplicateGroup, DuplicateMember, ExclusionCriterion, InclusionCriterion,
} from '../api/types'
import { buildRules, criteriaFor, EMPTY_FORM, ruleCount, type RuleForm } from '../utils/bulk'
import { buildHighlighters, type Highlighters } from '../utils/highlight'
import Highlight, { MARK_STYLE } from './Highlight'
import { counted, PHASE_NOUN } from '../utils/vocabulary'
import AbstractToggle, { AbstractList } from './AbstractToggle'
import { Card, CardHeader, ConfirmDialog, DecisionBadge, EmptyState, FormField } from './ui'

export type BulkPhase = 'screening' | 'full-text'

const nounFor = (phase: BulkPhase) =>
  phase === 'screening' ? PHASE_NOUN.screening : PHASE_NOUN.eligibility

/** The active reviewer, as the NavBar selection or else R1. */
function useActiveReviewerId(pid: number): number | null {
  const { reviewerId } = useProject()
  const { data: reviewers = [] } = useQuery({
    queryKey: ['reviewers', pid],
    queryFn: () => getReviewers(pid),
  })
  return reviewerId ?? reviewers.find(r => r.role === 'R1')?.id ?? reviewers[0]?.id ?? null
}

function useCriteria(pid: number, phase: BulkPhase) {
  const { data: inclusions = [] } = useQuery({
    queryKey: ['inclusion', pid], queryFn: () => getInclusionCriteria(pid),
  })
  const { data: exclusions = [] } = useQuery({
    queryKey: ['exclusion', pid], queryFn: () => getExclusionCriteria(pid),
  })
  return {
    inclusion: criteriaFor<InclusionCriterion>(inclusions, phase),
    exclusion: criteriaFor<ExclusionCriterion>(exclusions, phase),
  }
}

/** Everything a decision can change downstream. */
function useRefreshAfterDecisions(pid: number) {
  const qc = useQueryClient()
  return () => {
    for (const key of ['papers', 'conflicts', 'kappa', 'snowballing', 'bulk-match', 'duplicates']) {
      qc.invalidateQueries({ queryKey: [key, pid] })
    }
  }
}

function Errors({ messages }: { messages: string[] }) {
  if (messages.length === 0) return null
  const shown = messages.slice(0, 8)
  return (
    <div role="alert" className="text-xs text-exclude mt-3 space-y-0.5">
      {shown.map((m, i) => <p key={i}>{m}</p>)}
      {messages.length > shown.length && <p>…and {messages.length - shown.length} more.</p>}
    </div>
  )
}

type MetaPaper = { year?: number | null; venue?: string | null; source: string; language?: string | null }

/** Year · venue · source · language, with whatever matched a rule marked. */
function Meta({ p, hl }: { p: MetaPaper; hl?: Highlighters | null }) {
  const yearHit = !!hl?.years && p.year != null && (p.year < hl.years.min || p.year > hl.years.max)
  const langHit = !!p.language && !!hl?.languages.has(p.language.toLowerCase())
  const mark = (text: string | number) => <mark style={MARK_STYLE} className="rounded-sm px-0.5">{text}</mark>
  const parts: JSX.Element[] = []
  if (p.year != null) parts.push(<span key="y">{yearHit ? mark(p.year) : p.year}</span>)
  if (p.venue) parts.push(<span key="v"><Highlight text={p.venue} pattern={hl?.venue ?? null} /></span>)
  parts.push(<span key="s">{p.source}</span>)
  if (p.language) parts.push(<span key="l">{langHit ? mark(p.language) : p.language}</span>)
  return <>{parts.flatMap((el, i) => (i === 0 ? [el] : [<span key={`d${i}`}> · </span>, el]))}</>
}

// ── Bulk actions ──────────────────────────────────────────────────────────────

export function BulkView({ pid, phase }: { pid: number; phase: BulkPhase }) {
  const noun = nounFor(phase)
  const reviewerId = useActiveReviewerId(pid)
  const criteria = useCriteria(pid, phase)
  const refresh = useRefreshAfterDecisions(pid)

  const [form, setForm] = useState<RuleForm>(EMPTY_FORM)
  const [result, setResult] = useState<BulkMatchResult | null>(null)
  // Highlights follow the rules the shown preview was made with, not the form
  // as it is edited afterwards.
  const [shown, setShown] = useState<Highlighters | null>(null)
  const [checked, setChecked] = useState<Set<number>>(new Set())
  const [decision, setDecision] = useState<'I' | 'E'>('E')
  const [criterion, setCriterion] = useState('')
  const [rationale, setRationale] = useState('')
  const [confirming, setConfirming] = useState(false)
  const [errors, setErrors] = useState<string[]>([])
  const [done, setDone] = useState<string | null>(null)

  // The sources this phase's records came from, for the scope filter.
  const { data: papers = [] } = useQuery({
    queryKey: ['papers', pid, phase], queryFn: () => getPapers(pid, { phase }),
  })
  const sources = [...new Set(papers.filter(p => p.dedup_status === 'original').map(p => p.source))].sort()

  const set = <K extends keyof RuleForm>(k: K, v: RuleForm[K]) => setForm(f => ({ ...f, [k]: v }))

  const preview = useMutation({
    mutationFn: () => {
      const rules = buildRules(form)
      return matchUndecided(pid, { reviewer_id: reviewerId!, phase, rules, limit: 200 })
        .then(r => ({ r, hl: buildHighlighters(rules) }))
    },
    onSuccess: ({ r, hl }) => {
      setResult(r); setShown(hl); setChecked(new Set(r.papers.map(p => p.id))); setErrors([])
    },
    onError: e => { setResult(null); setErrors(apiErrors(e)) },
  })

  const apply = useMutation({
    mutationFn: () => applyBulkDecisions(pid, {
      reviewer_id: reviewerId!, phase,
      decisions: [...checked].map(id => ({
        paper_id: id, decision, criterion_label: criterion || undefined,
        rationale: rationale || undefined,
      })),
    }),
    onSuccess: r => {
      setDone(`Recorded ${decision === 'E' ? 'Exclude' : 'Include'} for ${counted(r.applied, noun)}.`)
      setResult(null); setChecked(new Set()); setConfirming(false); setErrors([])
      refresh()
    },
    onError: e => { setConfirming(false); setErrors(apiErrors(e)) },
  })

  if (reviewerId === null) {
    return <EmptyState icon="—" message="Select a reviewer in the top bar first." />
  }

  const list = decision === 'I' ? criteria.inclusion : criteria.exclusion
  const needsCriterion = list.length > 0
  const canApply = checked.size > 0 && (!needsCriterion || criterion !== '')
  const rules = ruleCount(form)

  const toggle = (id: number) => setChecked(prev => {
    const next = new Set(prev)
    next.has(id) ? next.delete(id) : next.add(id)
    return next
  })

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader title="Match rules" />
        <p className="text-xs text-ink-muted mb-4">
          Finds {noun.many} you have not decided yet. Leave a rule empty to switch it off.
          Nothing is recorded until you apply a decision to the matches.
        </p>

        <FormField label="A record must satisfy">
          <select className="select" aria-label="Match mode" value={form.mode}
            onChange={e => set('mode', e.target.value as RuleForm['mode'])}>
            <option value="all">all of the rules below</option>
            <option value="any">any of the rules below</option>
          </select>
        </FormField>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-4">
          <FormField label="Language code is" hint="ISO codes, comma-separated: fr, de, es, zh">
            <input className="input" aria-label="Language codes" value={form.languageCodes}
              onChange={e => set('languageCodes', e.target.value)} />
            <label className="flex items-center gap-2 text-xs text-ink-muted mt-2">
              <input type="checkbox" checked={form.languageBlank}
                onChange={e => set('languageBlank', e.target.checked)} />
              Language is not known
            </label>
          </FormField>

          <FormField label="Title contains" hint="Comma-separated, not case-sensitive">
            <input className="input" aria-label="Title terms" value={form.titleTerms}
              placeholder="editorial, erratum, retracted"
              onChange={e => set('titleTerms', e.target.value)} />
          </FormField>

          <FormField label="Year is outside" hint="Records with no year are not matched">
            <div className="flex items-center gap-2">
              <input className="input" aria-label="Earliest year" placeholder="from" value={form.yearMin}
                onChange={e => set('yearMin', e.target.value)} />
              <span className="text-ink-muted">–</span>
              <input className="input" aria-label="Latest year" placeholder="to" value={form.yearMax}
                onChange={e => set('yearMax', e.target.value)} />
            </div>
          </FormField>

          <FormField label="Venue contains" hint="Comma-separated, not case-sensitive">
            <input className="input" aria-label="Venue terms" value={form.venueTerms}
              placeholder="workshop, poster"
              onChange={e => set('venueTerms', e.target.value)} />
          </FormField>

          <FormField label="Title or abstract contains"
            hint="Comma-separated. Matches the start of a word: “iron” finds “irony”, not “environment”">
            <input className="input" aria-label="Text terms" value={form.textTerms}
              placeholder="sarcas, irony, metaphor"
              onChange={e => set('textTerms', e.target.value)} />
          </FormField>

          <FormField label="Title and abstract contain none of"
            hint="Comma-separated, same word-start matching. Records with any of these are left out">
            <input className="input" aria-label="Excluded text terms" value={form.textExcludes}
              placeholder="sarcas, irony, humor"
              onChange={e => set('textExcludes', e.target.value)} />
          </FormField>

          <FormField label="Abstract">
            <select className="select" aria-label="Abstract" value={form.abstract}
              onChange={e => set('abstract', e.target.value as RuleForm['abstract'])}>
              <option value="">— not a rule —</option>
              <option value="missing">is missing</option>
              <option value="present">is present</option>
            </select>
          </FormField>

          <FormField label="Only from" hint="Limits the search; it is not one of the rules">
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {sources.length === 0 && <span className="text-xs text-ink-muted">—</span>}
              {sources.map(s => (
                <label key={s} className="flex items-center gap-1.5 text-xs text-ink">
                  <input type="checkbox" checked={form.sources.includes(s)}
                    onChange={e => set('sources', e.target.checked
                      ? [...form.sources, s] : form.sources.filter(x => x !== s))} />
                  {s}
                </label>
              ))}
            </div>
          </FormField>
        </div>

        <div className="flex items-center gap-3">
          <button className="btn-primary" disabled={rules === 0 || preview.isPending}
            onClick={() => { setDone(null); preview.mutate() }}>
            {preview.isPending ? 'Searching…' : 'Preview matches'}
          </button>
          {rules === 0 && <span className="text-xs text-ink-muted">Turn on at least one rule.</span>}
        </div>
      </Card>

      {done && <p role="status" className="text-sm text-include">{done}</p>}
      <Errors messages={errors} />

      {result && result.matched === 0 && (
        <EmptyState icon="—" message={`No ${noun.many} match. ${result.candidates.toLocaleString()} undecided were searched.`} />
      )}

      {result && result.matched > 0 && (
        <Card>
          <CardHeader
            title={`${result.matched.toLocaleString()} of ${result.candidates.toLocaleString()} undecided ${noun.many} match`} />
          {result.matched > result.papers.length && (
            <p className="text-xs text-ink-muted mb-3">
              Showing the first {result.papers.length}. Apply to these, then preview again for the rest.
            </p>
          )}

          <div className="flex flex-wrap items-end gap-3 mb-4 p-3 bg-paper rounded-card border border-rule">
            <div>
              <label className="block text-2xs font-semibold text-ink-muted uppercase tracking-label mb-1">Decision</label>
              <select className="select" aria-label="Decision" value={decision}
                onChange={e => { setDecision(e.target.value as 'I' | 'E'); setCriterion('') }}>
                <option value="E">Exclude</option>
                <option value="I">Include</option>
              </select>
            </div>
            {needsCriterion && (
              <div>
                <label className="block text-2xs font-semibold text-ink-muted uppercase tracking-label mb-1">Criterion</label>
                <select className="select" aria-label="Criterion" value={criterion}
                  onChange={e => setCriterion(e.target.value)}>
                  <option value="">— choose —</option>
                  {list.map(c => <option key={c.id} value={c.label}>{c.label}</option>)}
                </select>
              </div>
            )}
            <div className="flex-1 min-w-[12rem]">
              <label className="block text-2xs font-semibold text-ink-muted uppercase tracking-label mb-1">Rationale (optional)</label>
              <input className="input" aria-label="Rationale" value={rationale}
                onChange={e => setRationale(e.target.value)} />
            </div>
            <button className="btn-primary" disabled={!canApply || apply.isPending}
              onClick={() => setConfirming(true)}>
              Apply to {checked.size} checked
            </button>
          </div>

          <div className="flex gap-3 mb-2 text-xs">
            <button className="btn-ghost btn-sm" onClick={() => setChecked(new Set(result.papers.map(p => p.id)))}>Check all</button>
            <button className="btn-ghost btn-sm" onClick={() => setChecked(new Set())}>Uncheck all</button>
          </div>

          <AbstractList>
          <div className="paper-list">
            {result.papers.map(p => (
              <label key={p.id} className="paper-row gap-3">
                <input type="checkbox" className="mt-1" checked={checked.has(p.id)}
                  onChange={() => toggle(p.id)} aria-label={`Include ${p.title} in this action`} />
                <div className="min-w-0">
                  <div className="text-sm text-ink"><Highlight text={p.title} pattern={shown?.title ?? null} /></div>
                  <div className="text-xs text-ink-muted"><Meta p={p} hl={shown} /></div>
                  <AbstractToggle abstract={p.abstract} highlight={shown?.abstract ?? null} />
                </div>
              </label>
            ))}
          </div>
          </AbstractList>
        </Card>
      )}

      {confirming && (
        <ConfirmDialog
          confirmLabel="Apply"
          message={`${decision === 'E' ? 'Exclude' : 'Include'} ${counted(checked.size, noun)}${
            criterion ? ` as "${criterion}"` : ''}? Each can still be changed from the list afterwards.`}
          onConfirm={() => apply.mutate()}
          onCancel={() => setConfirming(false)}
        />
      )}
    </div>
  )
}

// ── Duplicate review ──────────────────────────────────────────────────────────

type Choice = 'keep' | 'exclude'
const PAGE = 100

export function DuplicatesView({ pid, phase }: { pid: number; phase: BulkPhase }) {
  const noun = nounFor(phase)
  const reviewerId = useActiveReviewerId(pid)
  const criteria = useCriteria(pid, phase)
  const refresh = useRefreshAfterDecisions(pid)

  const [status, setStatus] = useState<'open' | 'all'>('open')
  const [offset, setOffset] = useState(0)
  const [choices, setChoices] = useState<Record<number, Choice | undefined>>({})
  const [criterion, setCriterion] = useState('')
  const [errors, setErrors] = useState<string[]>([])
  const [done, setDone] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)

  const { data, isLoading } = useQuery({
    queryKey: ['duplicates', pid, phase, reviewerId, status, offset],
    queryFn: () => getDuplicateCandidates(pid, { reviewer_id: reviewerId!, phase, status, limit: PAGE, offset }),
    enabled: reviewerId !== null,
  })

  // "Duplicate" is nearly always the criterion; preselect it when there is one.
  const duplicateLabel = criteria.exclusion.find(c => c.label.toLowerCase() === 'duplicate')?.label
  const chosenCriterion = criterion || duplicateLabel || ''

  const groups = data?.groups ?? []
  const resolutions = groups.map(g => ({
    keep: g.papers.filter(p => choices[p.id] === 'keep').map(p => p.id),
    exclude: g.papers.filter(p => choices[p.id] === 'exclude').map(p => p.id),
  }))
  const ready = resolutions.filter(r => r.keep.length > 0 && r.exclude.length > 0)
  const toExclude = ready.reduce((n, r) => n + r.exclude.length, 0)
  const needsKeeper = (i: number) => resolutions[i].exclude.length > 0 && resolutions[i].keep.length === 0

  const resolve = useMutation({
    mutationFn: () => resolveDuplicates(pid, {
      reviewer_id: reviewerId!, phase, criterion_label: chosenCriterion, groups: ready,
    }),
    onSuccess: r => {
      const replaced = r.changes.filter(c => c.previous).length
      setDone(`Excluded ${counted(r.applied, noun)} as duplicates.` +
        (replaced ? ` ${replaced} earlier decision${replaced === 1 ? ' was' : 's were'} replaced.` : ''))
      setChoices({}); setConfirming(false); setErrors([])
      refresh()
    },
    onError: e => { setConfirming(false); setErrors(apiErrors(e)) },
  })

  if (reviewerId === null) {
    return <EmptyState icon="—" message="Select a reviewer in the top bar first." />
  }

  const setChoice = (id: number, c: Choice | undefined) =>
    setChoices(prev => ({ ...prev, [id]: prev[id] === c ? undefined : c }))

  /** Keep this copy and exclude the others that are still open. */
  const keepOnly = (g: DuplicateGroup, id: number) => setChoices(prev => {
    const next = { ...prev }
    for (const p of g.papers) {
      if (p.dedup_status && p.dedup_status !== 'original') continue
      next[p.id] = p.id === id ? 'keep' : (p.decision === null ? 'exclude' : undefined)
    }
    return next
  })

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader title="Possible duplicates" />
        <p className="text-xs text-ink-muted mb-3">
          {PHASE_NOUN.screening.Many} that look like the same work but were not caught when imported —
          for example a copy with no DOI and a venue in its title. A group is only a candidate:
          editions and chapters can share a title. Choose the copy that stays; the others are
          excluded as duplicates.
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label className="block text-2xs font-semibold text-ink-muted uppercase tracking-label mb-1">Show</label>
            <select className="select" aria-label="Show groups" value={status}
              onChange={e => { setStatus(e.target.value as 'open' | 'all'); setOffset(0); setChoices({}) }}>
              <option value="open">Groups that need a decision</option>
              <option value="all">All groups</option>
            </select>
          </div>
          <div>
            <label className="block text-2xs font-semibold text-ink-muted uppercase tracking-label mb-1">Exclude as</label>
            <select className="select" aria-label="Exclusion criterion" value={chosenCriterion}
              onChange={e => setCriterion(e.target.value)}>
              <option value="">— choose —</option>
              {criteria.exclusion.map(c => <option key={c.id} value={c.label}>{c.label}</option>)}
            </select>
          </div>
          <button className="btn-primary" disabled={ready.length === 0 || !chosenCriterion || resolve.isPending}
            onClick={() => setConfirming(true)}>
            Exclude {toExclude} {toExclude === 1 ? 'copy' : 'copies'} in {ready.length} {ready.length === 1 ? 'group' : 'groups'}
          </button>
        </div>
        {data && (
          <p className="text-xs text-ink-muted mt-3" data-testid="duplicate-totals">
            <strong className="text-ink">{data.total.toLocaleString()}</strong> {data.total === 1 ? 'group' : 'groups'}
            {' '}({data.copies.toLocaleString()} {data.copies === 1 ? 'copy' : 'copies'})
            {status === 'open' ? ' need a decision' : ' in total'}
            {data.lost > 0 && <> · <strong className="text-exclude">{data.lost.toLocaleString()}</strong> where every copy is already excluded as a duplicate</>}
          </p>
        )}
      </Card>

      {done && <p role="status" className="text-sm text-include">{done}</p>}
      <Errors messages={errors} />

      {isLoading && <p className="text-sm text-ink-muted">Looking for duplicates…</p>}
      {data && groups.length === 0 && (
        <EmptyState icon="—" message={status === 'open'
          ? `No possible duplicates need a decision.` : `No possible duplicates found.`} />
      )}

      <AbstractList>
      {groups.map((g, gi) => (
        <Card key={g.papers.map(p => p.id).join('-')}>
          <div className="flex items-center justify-between mb-3">
            <span className="text-xs text-ink-muted">
              {g.papers.length} copies · {g.reasons.join(', ')}
              {g.reasons.includes('Similar title') && ` (${Math.round(g.score * 100)}% match)`}
            </span>
          </div>

          {g.all_excluded_as_duplicate && (
            <p role="alert" className="text-xs text-exclude mb-3">
              Every copy here was excluded as a duplicate, so this work is out of the review.
              If it should stay, change one copy to Include from the {noun.many} list.
            </p>
          )}
          {needsKeeper(gi) && (
            <p role="alert" className="text-xs text-uncertain mb-3">
              Choose a copy to keep, or this group will not be applied.
            </p>
          )}

          <div className="divide-y divide-rule">
            {g.papers.map((p: DuplicateMember) => (
              <div key={p.id} className="py-2 flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="text-sm text-ink">{p.title}</div>
                  <div className="text-xs text-ink-muted">
                    <Meta p={p} />{p.doi ? ` · ${p.doi}` : ' · no DOI'} · ID {p.id}
                  </div>
                  <AbstractToggle abstract={p.abstract} />
                  {p.decision && (
                    <div className="mt-1 flex items-center gap-2 text-xs text-ink-muted">
                      <DecisionBadge decision={p.decision} />
                      {p.criterion_label}
                    </div>
                  )}
                </div>
                {p.dedup_status && p.dedup_status !== 'original' ? (
                  <span className="text-xs text-ink-muted shrink-0">Flagged duplicate — cannot be changed</span>
                ) : (
                <div className="flex gap-1 shrink-0" role="group" aria-label={`Choice for ${p.title}`}>
                  <button className={choices[p.id] === 'keep' ? 'btn-include btn-sm bg-green-50' : 'btn-include btn-sm'}
                    aria-pressed={choices[p.id] === 'keep'}
                    onClick={() => setChoice(p.id, 'keep')}>Keep</button>
                  <button className={choices[p.id] === 'exclude' ? 'btn-exclude btn-sm bg-red-50' : 'btn-exclude btn-sm'}
                    aria-pressed={choices[p.id] === 'exclude'}
                    onClick={() => setChoice(p.id, 'exclude')}>Exclude</button>
                  <button className="btn-ghost btn-sm" title="Keep this copy, exclude the undecided others"
                    onClick={() => keepOnly(g, p.id)}>Keep only this</button>
                </div>
                )}
              </div>
            ))}
          </div>
        </Card>
      ))}
      </AbstractList>

      {data && data.total > PAGE && (
        <div className="flex items-center gap-3 text-xs text-ink-muted">
          <button className="btn-secondary btn-sm" disabled={offset === 0}
            onClick={() => { setOffset(Math.max(0, offset - PAGE)); setChoices({}) }}>Previous</button>
          <span>{offset + 1}–{Math.min(offset + PAGE, data.total)} of {data.total}</span>
          <button className="btn-secondary btn-sm" disabled={offset + PAGE >= data.total}
            onClick={() => { setOffset(offset + PAGE); setChoices({}) }}>Next</button>
        </div>
      )}

      {confirming && (
        <ConfirmDialog
          confirmLabel="Exclude copies"
          message={`Exclude ${toExclude} ${toExclude === 1 ? 'copy' : 'copies'} as "${chosenCriterion}"? ` +
            `The kept copies are left as they are. Any earlier decision on an excluded copy is replaced.`}
          onConfirm={() => resolve.mutate()}
          onCancel={() => setConfirming(false)}
        />
      )}
    </div>
  )
}
