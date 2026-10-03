import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import AbstractToggle, { AbstractList } from './AbstractToggle'

describe('AbstractToggle', () => {
  it('starts closed, opens on click, and closes again', () => {
    render(<AbstractToggle abstract="We study verbal irony." />)
    expect(screen.queryByText('We study verbal irony.')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Show abstract' }))
    expect(screen.getByText('We study verbal irony.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Hide abstract' })).toHaveAttribute('aria-expanded', 'true')

    fireEvent.click(screen.getByRole('button', { name: 'Hide abstract' }))
    expect(screen.queryByText('We study verbal irony.')).toBeNull()
  })

  it('does not trigger the row it sits in', () => {
    const onRow = vi.fn()
    render(<div onClick={onRow}><AbstractToggle abstract="Text" /></div>)
    fireEvent.click(screen.getByRole('button', { name: 'Show abstract' }))
    fireEvent.click(screen.getByRole('button', { name: 'Hide abstract' }))
    expect(onRow).not.toHaveBeenCalled()
  })

  it('says so when there is no abstract, with nothing to open', () => {
    render(<AbstractToggle abstract="  " />)
    expect(screen.getByText('No abstract')).toBeInTheDocument()
    expect(screen.queryByRole('button')).toBeNull()
  })
})

describe('AbstractList', () => {
  const list = (abstracts: (string | null)[]) => (
    <AbstractList>
      {abstracts.map((a, i) => <AbstractToggle key={i} abstract={a} />)}
    </AbstractList>
  )

  it('shows every abstract, then hides every abstract', () => {
    render(list(['First text', 'Second text', null]))
    fireEvent.click(screen.getByRole('button', { name: 'Show all abstracts' }))
    expect(screen.getByText('First text')).toBeInTheDocument()
    expect(screen.getByText('Second text')).toBeInTheDocument()
    expect(screen.getByText('No abstract')).toBeInTheDocument()       // nothing to open, still said

    fireEvent.click(screen.getByRole('button', { name: 'Hide all abstracts' }))
    expect(screen.queryByText('First text')).toBeNull()
    expect(screen.queryByText('Second text')).toBeNull()
  })

  it('lets a row be toggled alone, and the buttons still reach it afterwards', () => {
    render(list(['First text', 'Second text']))
    fireEvent.click(screen.getByRole('button', { name: 'Show all abstracts' }))
    fireEvent.click(screen.getAllByRole('button', { name: 'Hide abstract' })[0])   // close one
    expect(screen.queryByText('First text')).toBeNull()
    expect(screen.getByText('Second text')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Show all abstracts' }))    // same command again
    expect(screen.getByText('First text')).toBeInTheDocument()
  })

  it('starts a row that appears later closed, even after "show all"', () => {
    const { rerender } = render(list(['First text']))
    fireEvent.click(screen.getByRole('button', { name: 'Show all abstracts' }))
    rerender(list(['First text', 'Late text']))
    expect(screen.getByText('First text')).toBeInTheDocument()
    expect(screen.queryByText('Late text')).toBeNull()
  })

  it('stays out of the way of a row outside any list', () => {
    render(<AbstractToggle abstract="Alone" />)
    expect(screen.queryByRole('button', { name: 'Show all abstracts' })).toBeNull()
  })
})

describe('Google Scholar link', () => {
  it('opens a Scholar search for the record in a new tab', () => {
    render(<AbstractToggle abstract="Text" title="Irony Detection Using Transformers for Social Media" authors="Smith, Jane" />)
    const link = screen.getByRole('link', { name: /Google Scholar/ })
    expect(link).toHaveAttribute('href',
      'https://scholar.google.com/scholar?q=' + encodeURIComponent('Irony Detection Using Transformers for Social Media'))
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'))
  })

  it('is there when the record has no abstract', () => {
    render(<AbstractToggle abstract={null} title="Indirect Speech Acts" authors="Searle, John" />)
    expect(screen.getByText('No abstract')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Google Scholar/ })).toHaveAttribute('href',
      'https://scholar.google.com/scholar?q=' + encodeURIComponent('Indirect Speech Acts Searle'))
  })

  it('does not trigger the row it sits in', () => {
    const onRow = vi.fn()
    render(<div onClick={onRow}><AbstractToggle abstract="Text" title="A title of five words" /></div>)
    const link = screen.getByRole('link', { name: /Google Scholar/ })
    link.addEventListener('click', e => e.preventDefault())        // keep jsdom from navigating
    fireEvent.click(link)
    expect(onRow).not.toHaveBeenCalled()
  })

  it('is left out when no title is given', () => {
    render(<AbstractToggle abstract="Text" />)
    expect(screen.queryByRole('link')).toBeNull()
  })
})
