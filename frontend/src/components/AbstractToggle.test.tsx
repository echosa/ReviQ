import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import AbstractToggle from './AbstractToggle'

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
