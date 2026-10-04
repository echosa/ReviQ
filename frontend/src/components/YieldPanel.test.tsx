/**
 * The panel a decision to stop snowballing rests on: what each iteration
 * brought, and how much of it was already in the review.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import YieldPanel from './YieldPanel'
import type { SnowballYield } from '../api/types'

const { getSnowballingYield } = vi.hoisted(() => ({ getSnowballingYield: vi.fn() }))
vi.mock('../api/client', () => ({ getSnowballingYield }))

function row(over: Partial<SnowballYield> = {}): SnowballYield {
  return {
    iteration_number: 1, iteration_type: 'backward', retrieved: 3240, originals: 2725, duplicates: 515,
    duplicate_pct: 15.9, screened: 2725, screening_included: 149, fulltext_decided: 149, fulltext_included: 115,
    yield_pct: 4.2, refound_known: 70, refound_included: 18, refound_included_pct: 25.7,
    core_in_title: 108, core_in_title_pct: 93.9, ...over,
  }
}

function renderPanel(rows: SnowballYield[] | Error) {
  if (rows instanceof Error) getSnowballingYield.mockRejectedValue(rows)
  else getSnowballingYield.mockResolvedValue(rows)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><YieldPanel pid={1} /></QueryClientProvider>)
}

beforeEach(() => {
  try { localStorage.clear() } catch { /* ignore */ }
  getSnowballingYield.mockReset()
})

describe('yield panel', () => {
  it('lays out each iteration: records, duplicates, includes, yield, and what it re-found', async () => {
    renderPanel([
      row(),
      row({ iteration_number: 3, iteration_type: 'backward', retrieved: 4949, duplicates: 2153, duplicate_pct: 43.5,
            screening_included: 348, fulltext_included: 134, yield_pct: 4.8, refound_known: 1404,
            refound_included: 668, refound_included_pct: 47.6, core_in_title: 131, core_in_title_pct: 97.8 }),
    ])
    const r3 = (await screen.findByText('3')).closest('tr') as HTMLElement
    expect(within(r3).getByText('4,949')).toBeInTheDocument()
    expect(r3).toHaveTextContent('2,153 (43.5%)')
    expect(r3).toHaveTextContent('348')
    expect(r3).toHaveTextContent('4.8%')
    expect(r3).toHaveTextContent('47.6% (668 of 1,404)')
    expect(r3).toHaveTextContent('97.8% (131 of 134)')
  })

  it('does not invent a yield before full-text has begun', async () => {
    renderPanel([row({ iteration_number: 4, iteration_type: 'forward', fulltext_decided: 0, fulltext_included: 0,
                       yield_pct: null, screening_included: 12, refound_known: 0, refound_included: 0,
                       refound_included_pct: null, core_in_title: 0, core_in_title_pct: 0 })])
    const r = (await screen.findByText('4')).closest('tr') as HTMLElement
    expect(r).toHaveTextContent('(pending)')
    expect(within(r).getAllByText('—').length).toBeGreaterThanOrEqual(2)     // yield and re-found
    expect(r).not.toHaveTextContent('NaN')
  })

  it('asks the server for the core terms it starts with, and again when they change', async () => {
    renderPanel([row()])
    await screen.findByText('1')
    expect(getSnowballingYield).toHaveBeenLastCalledWith(1, 'sarcas, iron, satir')
    const input = screen.getByLabelText('Core terms')
    fireEvent.change(input, { target: { value: 'sarcas' } })
    expect(getSnowballingYield).toHaveBeenCalledTimes(1)               // nothing until Enter or leaving the field
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(getSnowballingYield).toHaveBeenLastCalledWith(1, 'sarcas'))
    expect(localStorage.getItem('reviq_snowball_core_terms')).toBe('sarcas')
  })

  it('starts from the terms it remembered', async () => {
    localStorage.setItem('reviq_snowball_core_terms', 'humor')
    renderPanel([row()])
    await screen.findByText('1')
    expect(getSnowballingYield).toHaveBeenLastCalledWith(1, 'humor')
    expect((screen.getByLabelText('Core terms') as HTMLInputElement).value).toBe('humor')
  })

  it('leaves the core column out when there are no core terms', async () => {
    renderPanel([row({ core_in_title: null, core_in_title_pct: null })])
    await screen.findByText('1')
    const input = screen.getByLabelText('Core terms')
    fireEvent.change(input, { target: { value: '' } })
    fireEvent.blur(input)
    await waitFor(() => expect(screen.queryByText(/Core in/)).toBeNull())
  })

  it('shows nothing when there are no iterations yet', async () => {
    const { container } = renderPanel([])
    await waitFor(() => expect(getSnowballingYield).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })

  it('says so when the figures cannot be loaded', async () => {
    renderPanel(new Error('boom'))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load the yield figures.')
    expect(screen.queryByRole('table')).toBeNull()
  })
})
