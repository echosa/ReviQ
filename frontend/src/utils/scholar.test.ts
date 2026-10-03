import { describe, expect, it } from 'vitest'
import { firstAuthorSurname, scholarQuery, scholarUrl } from './scholar'

describe('firstAuthorSurname', () => {
  it('reads "Last, First; …" and "First Last and …"', () => {
    expect(firstAuthorSurname('Riloff, Ellen; Qadir, Ashequl')).toBe('Riloff')
    expect(firstAuthorSurname('Ellen Riloff and Ashequl Qadir')).toBe('Riloff')
    expect(firstAuthorSurname('Bredin, Hugh')).toBe('Bredin')
  })
  it('is null when there is nothing to read', () => {
    expect(firstAuthorSurname(null)).toBeNull()
    expect(firstAuthorSurname('  ')).toBeNull()
  })
})

describe('scholarQuery', () => {
  it('uses the title alone when it is long enough to find on its own', () => {
    expect(scholarQuery('Sarcasm as Contrast between a Positive Sentiment', 'Riloff, Ellen'))
      .toBe('Sarcasm as Contrast between a Positive Sentiment')
  })
  it('adds the first author to a short title', () => {
    expect(scholarQuery('Indirect Speech Acts', 'Searle, John')).toBe('Indirect Speech Acts Searle')
  })
  it('keeps a venue prefix and does not quote the title', () => {
    expect(scholarQuery('EMNLP - Sarcasm as Contrast between a Positive Sentiment')).not.toContain('"')
  })
  it('drops BibTeX braces and extra whitespace', () => {
    expect(scholarQuery('A  {BERT}   model for sarcasm detection')).toBe('A BERT model for sarcasm detection')
  })
})

describe('scholarUrl', () => {
  it('encodes the query for the search URL', () => {
    expect(scholarUrl('Irony & humour: a study of "verbal" cues')).toBe(
      'https://scholar.google.com/scholar?q=' + encodeURIComponent('Irony & humour: a study of "verbal" cues'))
  })
})
