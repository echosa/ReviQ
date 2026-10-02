/**
 * What the bulk-actions and duplicate-review tabs promise a reviewer.
 *
 * The server enforces the rules (see backend/tests/test_bulk.py); this is the
 * half a person touches: nothing is sent until a decision is confirmed, only the
 * checked records are sent, and a duplicate group with no copy to keep cannot be
 * applied.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { BulkView, DuplicatesView } from './BulkTools'
import type { DuplicateGroup, DuplicateMember, BulkPaper } from '../api/types'

const api = vi.hoisted(() => ({
  matchUndecided: vi.fn(),
  applyBulkDecisions: vi.fn(),
  getDuplicateCandidates: vi.fn(),
  resolveDuplicates: vi.fn(),
  getPapers: vi.fn(),
  getReviewers: vi.fn(),
  getInclusionCriteria: vi.fn(),
  getExclusionCriteria: vi.fn(),
}))
vi.mock('../api/client', () => ({ ...api, apiErrors: () => ['The server said no.'] }))
vi.mock('../App', () => ({ useProject: () => ({ reviewerId: 1 }) }))

const crit = (id: number, label: string) => ({ id, project_id: 1, label, description: '', phase: 'screening' })

function paper(id: number, over: Partial<BulkPaper> = {}): BulkPaper {
  return { id, citekey: `k${id}`, title: `Paper ${id}`, source: 'acm', year: 2020, ...over }
}
function member(id: number, over: Partial<DuplicateMember> = {}): DuplicateMember {
  return { ...paper(id), decision: null, criterion_label: null, ...over }
}
function group(members: DuplicateMember[], over: Partial<DuplicateGroup> = {}): DuplicateGroup {
  return { papers: members, reasons: ['Same title'], score: 1, all_excluded_as_duplicate: false, ...over }
}

function renderIn(ui: JSX.Element) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  Object.values(api).forEach(f => f.mockReset())
  api.getPapers.mockResolvedValue([])
  api.getReviewers.mockResolvedValue([{ id: 1, role: 'R1', name: 'Alice', project_id: 1 }])
  api.getInclusionCriteria.mockResolvedValue([crit(1, 'Terminology')])
  api.getExclusionCriteria.mockResolvedValue([crit(2, 'Off-topic'), crit(3, 'Duplicate')])
})

describe('bulk actions', () => {
  async function previewTitleRule(papers: BulkPaper[]) {
    api.matchUndecided.mockResolvedValue({ candidates: 50, matched: papers.length, offset: 0, limit: 200, papers })
    renderIn(<BulkView pid={1} phase="screening" />)
    fireEvent.change(await screen.findByLabelText('Title terms'), { target: { value: 'editorial, erratum' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview matches' }))
    await screen.findByText(/of 50 undecided records match/)
  }

  it('cannot preview until a rule is switched on', async () => {
    renderIn(<BulkView pid={1} phase="screening" />)
    const preview = await screen.findByRole('button', { name: 'Preview matches' })
    expect(preview).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Title terms'), { target: { value: 'editorial' } })
    expect(preview).toBeEnabled()
    expect(api.matchUndecided).not.toHaveBeenCalled()
  })

  it('sends the rules as typed and records nothing at preview time', async () => {
    await previewTitleRule([paper(1), paper(2)])
    expect(api.matchUndecided).toHaveBeenCalledWith(1, expect.objectContaining({
      reviewer_id: 1, phase: 'screening',
      rules: expect.objectContaining({ mode: 'all', title_terms: ['editorial', 'erratum'] }),
    }))
    expect(api.applyBulkDecisions).not.toHaveBeenCalled()
  })

  it('applies only the checked records, after a confirmation, with the chosen criterion', async () => {
    await previewTitleRule([paper(1), paper(2), paper(3)])
    api.applyBulkDecisions.mockResolvedValue({ applied: 2 })

    fireEvent.click(screen.getByLabelText('Include Paper 2 in this action'))     // uncheck one
    fireEvent.change(screen.getByLabelText('Criterion'), { target: { value: 'Off-topic' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply to 2 checked' }))
    expect(api.applyBulkDecisions).not.toHaveBeenCalled()                       // not yet

    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(api.applyBulkDecisions).toHaveBeenCalledTimes(1))
    const body = api.applyBulkDecisions.mock.calls[0][1]
    expect(body.phase).toBe('screening')
    expect(body.decisions.map((d: any) => [d.paper_id, d.decision, d.criterion_label]))
      .toEqual([[1, 'E', 'Off-topic'], [3, 'E', 'Off-topic']])
    expect(await screen.findByRole('status')).toHaveTextContent('Recorded Exclude for 2 records.')
  })

  it('will not apply until a criterion is chosen', async () => {
    await previewTitleRule([paper(1)])
    expect(screen.getByRole('button', { name: 'Apply to 1 checked' })).toBeDisabled()
  })

  it('shows the server\'s reason when a request is refused', async () => {
    await previewTitleRule([paper(1)])
    api.applyBulkDecisions.mockRejectedValue(new Error('409'))
    fireEvent.change(screen.getByLabelText('Criterion'), { target: { value: 'Off-topic' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply to 1 checked' }))
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The server said no.')
  })

  it('says so when nothing matches', async () => {
    api.matchUndecided.mockResolvedValue({ candidates: 12, matched: 0, offset: 0, limit: 200, papers: [] })
    renderIn(<BulkView pid={1} phase="full-text" />)
    fireEvent.change(await screen.findByLabelText('Title terms'), { target: { value: 'zzz' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview matches' }))
    expect(await screen.findByText(/No reports match/)).toBeInTheDocument()
  })
})

describe('duplicate review', () => {
  const pair = () => group([member(10), member(11)])

  async function open(groups: DuplicateGroup[]) {
    api.getDuplicateCandidates.mockResolvedValue({ total: groups.length, offset: 0, limit: 100, groups })
    renderIn(<DuplicatesView pid={1} phase="screening" />)
    await screen.findByText('Paper 10')
  }

  it('preselects the Duplicate criterion and starts with nothing to apply', async () => {
    await open([pair()])
    expect(screen.getByLabelText('Exclusion criterion')).toHaveValue('Duplicate')
    expect(screen.getByRole('button', { name: /Exclude 0 copies in 0 groups/ })).toBeDisabled()
  })

  it('"Keep only this" keeps one copy and excludes the other, then sends the group', async () => {
    await open([pair()])
    api.resolveDuplicates.mockResolvedValue({ applied: 1, changes: [{ paper_id: 11, previous: null, previous_criterion: null }] })

    const row10 = screen.getByText('Paper 10').closest('div.py-2') as HTMLElement
    fireEvent.click(within(row10).getByRole('button', { name: 'Keep only this' }))
    fireEvent.click(screen.getByRole('button', { name: /Exclude 1 copy in 1 group/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Exclude copies' }))        // confirm

    await waitFor(() => expect(api.resolveDuplicates).toHaveBeenCalledWith(1, {
      reviewer_id: 1, phase: 'screening', criterion_label: 'Duplicate',
      groups: [{ keep: [10], exclude: [11] }],
    }))
    expect(await screen.findByRole('status')).toHaveTextContent('Excluded 1 record as duplicates.')
  })

  it('does not send a group with an exclusion but no copy to keep', async () => {
    await open([pair()])
    const row11 = screen.getByText('Paper 11').closest('div.py-2') as HTMLElement
    fireEvent.click(within(row11).getByRole('button', { name: 'Exclude' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Choose a copy to keep')
    expect(screen.getByRole('button', { name: /Exclude 0 copies in 0 groups/ })).toBeDisabled()
  })

  it('does not exclude a copy that already has a decision when keeping the other', async () => {
    await open([group([member(10), member(11, { decision: 'I', criterion_label: 'Terminology' })])])
    const row10 = screen.getByText('Paper 10').closest('div.py-2') as HTMLElement
    fireEvent.click(within(row10).getByRole('button', { name: 'Keep only this' }))
    // Paper 11 was already decided, so it is left alone: nothing to exclude.
    expect(screen.getByRole('button', { name: /Exclude 0 copies in 0 groups/ })).toBeDisabled()
  })

  it('warns when every copy was already excluded as a duplicate', async () => {
    const dup = { decision: 'E', criterion_label: 'Duplicate' }
    await open([group([member(10, dup), member(11, dup)], { all_excluded_as_duplicate: true })])
    expect(screen.getByRole('alert')).toHaveTextContent('out of the review')
  })

  it('shows a copy ReviQ flagged as a duplicate, without letting it be changed', async () => {
    const dup = { decision: 'E', criterion_label: 'Duplicate' }
    await open([group([member(10, dup), member(11, { ...dup, dedup_status: 'duplicate' })],
      { all_excluded_as_duplicate: true })])
    const row11 = screen.getByText('Paper 11').closest('div.py-2') as HTMLElement
    expect(within(row11).getByText(/Flagged duplicate/)).toBeInTheDocument()
    expect(within(row11).queryByRole('button')).toBeNull()
    expect(within(screen.getByText('Paper 10').closest('div.py-2') as HTMLElement)
      .getAllByRole('button').length).toBe(3)
  })

  it('says when there is nothing to review', async () => {
    api.getDuplicateCandidates.mockResolvedValue({ total: 0, offset: 0, limit: 100, groups: [] })
    renderIn(<DuplicatesView pid={1} phase="screening" />)
    expect(await screen.findByText(/No possible duplicates need a decision/)).toBeInTheDocument()
  })
})
