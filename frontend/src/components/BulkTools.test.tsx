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

  it('marks what made a record match, in its title and its abstract', async () => {
    api.matchUndecided.mockResolvedValue({
      candidates: 5, matched: 1, offset: 0, limit: 200,
      papers: [paper(1, { title: 'Editorial: the year in review', venue: 'ACM Workshop', language: 'fr',
        abstract: 'We study verbal irony in tweets.' })],
    })
    renderIn(<BulkView pid={1} phase="screening" />)
    fireEvent.change(await screen.findByLabelText('Title terms'), { target: { value: 'editorial' } })
    fireEvent.change(screen.getByLabelText('Venue terms'), { target: { value: 'workshop' } })
    fireEvent.change(screen.getByLabelText('Language codes'), { target: { value: 'fr' } })
    fireEvent.change(screen.getByLabelText('Text terms'), { target: { value: 'iron' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview matches' }))
    await screen.findByText(/of 5 undecided records match/)

    const marked = () => [...document.querySelectorAll('mark')].map(m => m.textContent)
    expect(marked()).toEqual(['Editorial', 'Workshop', 'fr'])
    // The abstract is closed but says it holds a match; opening it marks the term.
    expect(screen.getByText(/contains a match/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Show abstract' }))
    expect(marked()).toEqual(['Editorial', 'Workshop', 'fr', 'iron'])
  })

  it('keeps the highlights of the preview shown while the form is edited', async () => {
    api.matchUndecided.mockResolvedValue({
      candidates: 5, matched: 1, offset: 0, limit: 200,
      papers: [paper(1, { title: 'Editorial and erratum' })],
    })
    renderIn(<BulkView pid={1} phase="screening" />)
    const terms = await screen.findByLabelText('Title terms')
    fireEvent.change(terms, { target: { value: 'editorial' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview matches' }))
    await screen.findByText(/of 5 undecided records match/)
    fireEvent.change(terms, { target: { value: 'erratum' } })       // edited, not previewed
    expect([...document.querySelectorAll('mark')].map(m => m.textContent)).toEqual(['Editorial'])
  })

  it('searches only undecided records unless others are added, and says what it searched', async () => {
    api.matchUndecided.mockResolvedValue({ candidates: 40, matched: 1, offset: 0, limit: 200, papers: [paper(1)] })
    renderIn(<BulkView pid={1} phase="screening" />)
    fireEvent.change(await screen.findByLabelText('Title terms'), { target: { value: 'editorial' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview matches' }))
    await screen.findByText(/of 40 undecided records match/)
    expect(api.matchUndecided).toHaveBeenLastCalledWith(1, expect.objectContaining({ statuses: ['undecided'] }))

    fireEvent.click(screen.getByLabelText('Excluded'))
    fireEvent.click(screen.getByLabelText('Included'))
    fireEvent.click(screen.getByRole('button', { name: 'Preview matches' }))
    await screen.findByText(/of 40 undecided, included and excluded records match/)
    expect(api.matchUndecided).toHaveBeenLastCalledWith(1, expect.objectContaining({ statuses: ['undecided', 'I', 'E'] }))
  })

  it('cannot preview with no status chosen', async () => {
    renderIn(<BulkView pid={1} phase="screening" />)
    fireEvent.change(await screen.findByLabelText('Title terms'), { target: { value: 'editorial' } })
    fireEvent.click(screen.getByLabelText('Undecided'))
    expect(screen.getByRole('button', { name: 'Preview matches' })).toBeDisabled()
    expect(screen.getByText('Choose which records to search.')).toBeInTheDocument()
  })

  it('shows your current decision on a record that has one', async () => {
    api.matchUndecided.mockResolvedValue({
      candidates: 5, matched: 2, offset: 0, limit: 200,
      papers: [paper(1, { decision: 'I', criterion_label: 'Terminology' }), paper(2)],
    })
    renderIn(<BulkView pid={1} phase="screening" />)
    fireEvent.change(await screen.findByLabelText('Title terms'), { target: { value: 'paper' } })
    fireEvent.click(screen.getByLabelText('Included'))
    fireEvent.click(screen.getByRole('button', { name: 'Preview matches' }))
    await screen.findByText(/of 5 undecided and included records match/)
    expect(screen.getAllByText('Your decision:')).toHaveLength(1)      // only Paper 1 has one
    expect(screen.getByText('Terminology')).toBeInTheDocument()
  })

  it('asks to replace only when a checked record already has a decision, and says so first', async () => {
    api.matchUndecided.mockResolvedValue({
      candidates: 9, matched: 3, offset: 0, limit: 200,
      papers: [paper(1, { decision: 'I', criterion_label: 'Terminology' }),
               paper(2, { decision: 'E', criterion_label: 'Off-topic' }), paper(3)],
    })
    api.applyBulkDecisions.mockResolvedValue({ applied: 3, replaced: 2, changes: [] })
    renderIn(<BulkView pid={1} phase="screening" />)
    fireEvent.change(await screen.findByLabelText('Title terms'), { target: { value: 'paper' } })
    fireEvent.click(screen.getByLabelText('Included'))
    fireEvent.click(screen.getByLabelText('Excluded'))
    fireEvent.click(screen.getByRole('button', { name: 'Preview matches' }))
    await screen.findByText(/of 9 undecided, included and excluded records match/)

    fireEvent.change(screen.getByLabelText('Criterion'), { target: { value: 'Off-topic' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply to 3 checked' }))
    expect(screen.getByText(/2 of them already have a decision from you \(1 included, 1 excluded\); applying replaces them/))
      .toBeInTheDocument()
    expect(api.applyBulkDecisions).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(api.applyBulkDecisions).toHaveBeenCalledTimes(1))
    expect(api.applyBulkDecisions.mock.calls[0][1].replace).toBe(true)
    expect(await screen.findByRole('status')).toHaveTextContent('Recorded Exclude for 3 records, replacing 2 earlier decisions.')
  })

  it('does not ask to replace when none of the checked records has a decision', async () => {
    await previewTitleRule([paper(1), paper(2)])
    api.applyBulkDecisions.mockResolvedValue({ applied: 2, replaced: 0, changes: [] })
    fireEvent.change(screen.getByLabelText('Criterion'), { target: { value: 'Off-topic' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply to 2 checked' }))
    expect(screen.getByText(/Each can still be changed/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(api.applyBulkDecisions).toHaveBeenCalled())
    expect(api.applyBulkDecisions.mock.calls[0][1].replace).toBe(false)
  })

  it('sends the non-English option as a single flag and marks any other language', async () => {
    api.matchUndecided.mockResolvedValue({
      candidates: 9, matched: 2, offset: 0, limit: 200,
      papers: [paper(1, { language: 'fr' }), paper(2, { language: 'en' })],
    })
    renderIn(<BulkView pid={1} phase="screening" />)
    fireEvent.click(await screen.findByLabelText('Language is not English'))
    const preview = screen.getByRole('button', { name: 'Preview matches' })
    expect(preview).toBeEnabled()                                   // it alone is a rule
    fireEvent.click(preview)
    await screen.findByText(/of 9 undecided records match/)
    expect(api.matchUndecided).toHaveBeenLastCalledWith(1, expect.objectContaining({
      rules: expect.objectContaining({ language_non_english: true, language_codes: [] }),
    }))
    expect([...document.querySelectorAll('mark')].map(m => m.textContent)).toEqual(['fr'])   // not 'en'
  })

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

  it('forgets the criterion after an apply, so the next batch has to choose its own', async () => {
    await previewTitleRule([paper(1)])
    api.applyBulkDecisions.mockResolvedValue({ applied: 1, replaced: 0, changes: [] })
    fireEvent.change(screen.getByLabelText('Criterion'), { target: { value: 'Off-topic' } })
    expect(screen.getByLabelText('Criterion')).toHaveValue('Off-topic')
    fireEvent.click(screen.getByRole('button', { name: 'Apply to 1 checked' }))
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(api.applyBulkDecisions).toHaveBeenCalledTimes(1))
    expect(api.applyBulkDecisions.mock.calls[0][1].decisions[0].criterion_label).toBe('Off-topic')
    await screen.findByRole('status')

    // The next preview starts with no criterion chosen, and cannot be applied until one is.
    api.matchUndecided.mockResolvedValue({ candidates: 40, matched: 1, offset: 0, limit: 200, papers: [paper(2)] })
    fireEvent.click(screen.getByRole('button', { name: 'Preview matches' }))
    await screen.findByText(/of 40 undecided records match/)
    expect(screen.getByLabelText('Criterion')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Apply to 1 checked' })).toBeDisabled()
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

  it('says what a preview found right beside the button, including when nothing matched', async () => {
    api.matchUndecided.mockResolvedValueOnce({ candidates: 1511, matched: 0, offset: 0, limit: 200, papers: [] })
    renderIn(<BulkView pid={1} phase="screening" />)
    fireEvent.change(await screen.findByLabelText('Title terms'), { target: { value: 'zzzqqq' } })
    expect(screen.queryByTestId('preview-summary')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Preview matches' }))
    expect(await screen.findByTestId('preview-summary'))
      .toHaveTextContent('Nothing matched the rules (1,511 undecided records searched).')

    api.matchUndecided.mockResolvedValueOnce({ candidates: 1511, matched: 2, offset: 0, limit: 200, papers: [paper(1), paper(2)] })
    fireEvent.click(screen.getByRole('button', { name: 'Preview matches' }))
    await waitFor(() => expect(screen.getByTestId('preview-summary')).toHaveTextContent('2 matched — see below.'))
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
    api.getDuplicateCandidates.mockResolvedValue({
      total: groups.length, offset: 0, limit: 100, groups,
      copies: groups.reduce((n, g) => n + g.papers.length, 0),
      lost: groups.filter(g => g.all_excluded_as_duplicate).length, hidden: 0, hidden_settled: 0,
    })
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

  it('shows how many groups and copies there are, and how many have lost every copy', async () => {
    const dup = { decision: 'E', criterion_label: 'Duplicate' }
    await open([
      pair(),
      group([member(20, dup), member(21, dup)], { all_excluded_as_duplicate: true }),
      group([member(30), member(31), member(32)]),
    ])
    expect(screen.getByTestId('duplicate-totals')).toHaveTextContent(
      '3 groups (7 copies) need a decision · 1 where every copy is already excluded as a duplicate')
  })

  it('says "in total" when all groups are shown, and does not mention losses when there are none', async () => {
    api.getDuplicateCandidates.mockResolvedValue({
      total: 1, offset: 0, limit: 100, copies: 2, lost: 0, hidden: 0, hidden_settled: 0, groups: [pair()],
    })
    renderIn(<DuplicatesView pid={1} phase="screening" />)
    await screen.findByText('Paper 10')
    fireEvent.change(screen.getByLabelText('Show groups'), { target: { value: 'all' } })
    await waitFor(() => expect(screen.getByTestId('duplicate-totals'))
      .toHaveTextContent(/^1 group \(2 copies\) in total$/))
  })

  it('asks the server to leave out groups that have lost every copy, and says how many it hid', async () => {
    const dup = { decision: 'E', criterion_label: 'Duplicate' }
    await open([group([member(20, dup), member(21, dup)], { all_excluded_as_duplicate: true }), pair()])
    expect(api.getDuplicateCandidates).toHaveBeenLastCalledWith(1, expect.objectContaining({ hide_lost: false }))

    api.getDuplicateCandidates.mockResolvedValue({
      total: 1, offset: 0, limit: 100, copies: 2, lost: 0, hidden: 1, hidden_settled: 0, groups: [group([member(30), member(31)])],
    })
    fireEvent.click(screen.getByLabelText(/Hide groups where every copy is already excluded/))
    await waitFor(() => expect(api.getDuplicateCandidates)
      .toHaveBeenLastCalledWith(1, expect.objectContaining({ hide_lost: true })))
    await waitFor(() => expect(screen.getByTestId('duplicate-totals'))
      .toHaveTextContent('1 group (2 copies) need a decision · 1 such group hidden'))
    expect(screen.queryByRole('alert')).toBeNull()          // the red warning went with it
  })

  it('asks the server to leave out settled groups, and says how many it hid', async () => {
    const dup = { decision: 'E', criterion_label: 'Duplicate' }
    await open([group([member(20, dup), member(21, dup), member(22)]), pair()])
    expect(api.getDuplicateCandidates).toHaveBeenLastCalledWith(1, expect.objectContaining({ hide_settled: false }))

    api.getDuplicateCandidates.mockResolvedValue({
      total: 1, offset: 0, limit: 100, copies: 2, lost: 0, hidden: 0, hidden_settled: 4, groups: [group([member(30), member(31)])],
    })
    fireEvent.click(screen.getByLabelText(/Hide groups where all but one copy is already excluded/))
    await waitFor(() => expect(api.getDuplicateCandidates)
      .toHaveBeenLastCalledWith(1, expect.objectContaining({ hide_settled: true, hide_lost: false })))
    await waitFor(() => expect(screen.getByTestId('duplicate-totals'))
      .toHaveTextContent('1 group (2 copies) need a decision · 4 settled groups hidden'))
  })

  it('says when there is nothing to review', async () => {
    api.getDuplicateCandidates.mockResolvedValue({ total: 0, offset: 0, limit: 100, copies: 0, lost: 0, hidden: 0, hidden_settled: 0, groups: [] })
    renderIn(<DuplicatesView pid={1} phase="screening" />)
    expect(await screen.findByText(/No possible duplicates need a decision/)).toBeInTheDocument()
  })
})
