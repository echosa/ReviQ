import { describe, expect, it } from 'vitest'
import type { BulkRules } from '../api/types'
import { buildHighlighters, hasMatch, splitMatches } from './highlight'

const rules = (over: Partial<BulkRules> = {}): BulkRules => ({
  mode: 'all', language_codes: [], language_blank: false, title_terms: [], year_min: null,
  year_max: null, venue_terms: [], text_terms: [], text_excludes: [], abstract: null, sources: [],
  ...over,
})
const hits = (text: string, re: RegExp | null) => splitMatches(text, re).filter(p => p.hit).map(p => p.text)

describe('splitMatches', () => {
  it('cuts the text into runs and marks the matches, whatever the case', () => {
    const re = buildHighlighters(rules({ title_terms: ['editorial'] })).title
    expect(splitMatches('An Editorial: the year', re)).toEqual([
      { text: 'An ', hit: false }, { text: 'Editorial', hit: true }, { text: ': the year', hit: false },
    ])
  })

  it('returns the text unmarked when there is no pattern', () => {
    expect(splitMatches('Plain', null)).toEqual([{ text: 'Plain', hit: false }])
  })

  it('treats terms as text, not as patterns', () => {
    const re = buildHighlighters(rules({ title_terms: ['c++', 'a.b'] })).title
    expect(hits('Learning C++ and axb and a.b', re)).toEqual(['C++', 'a.b'])
  })
})

describe('buildHighlighters', () => {
  it('title terms match anywhere in a word', () => {
    expect(hits('Environment', buildHighlighters(rules({ title_terms: ['iron'] })).title)).toEqual(['iron'])
  })

  it('title-or-abstract terms match only from the start of a word, like the server', () => {
    const hl = buildHighlighters(rules({ text_terms: ['iron', 'sarcas'] }))
    expect(hits('Irony in an environment of sarcasm', hl.abstract)).toEqual(['Iron', 'sarcas'])
  })

  it('prefers the longest term where terms overlap', () => {
    const hl = buildHighlighters(rules({ text_terms: ['sarcas', 'sarcasm'] }))
    expect(hits('sarcasm', hl.abstract)).toEqual(['sarcasm'])
  })

  it('marks the title with both kinds of term, and the abstract with only the text terms', () => {
    const hl = buildHighlighters(rules({ title_terms: ['editorial'], text_terms: ['irony'] }))
    expect(hits('Editorial on irony', hl.title)).toEqual(['Editorial', 'irony'])
    expect(hits('Editorial on irony', hl.abstract)).toEqual(['irony'])
  })

  it('does not highlight what an exclusion rule left out', () => {
    const hl = buildHighlighters(rules({ text_excludes: ['irony'] }))
    expect(hl.title).toBeNull()
    expect(hl.abstract).toBeNull()
  })

  it('keeps venue terms, language codes and the year range for the row', () => {
    const hl = buildHighlighters(rules({ venue_terms: ['workshop'], language_codes: ['FR ', 'de'], year_min: 2000 }))
    expect(hits('ACL Workshop on X', hl.venue)).toEqual(['Workshop'])
    expect([...hl.languages]).toEqual(['fr', 'de'])
    expect(hl.years).toEqual({ min: 2000, max: Number.POSITIVE_INFINITY })
    expect(buildHighlighters(rules()).years).toBeNull()
  })
})

describe('hasMatch', () => {
  it('is repeatable, whatever flags the pattern was built with', () => {
    const re = buildHighlighters(rules({ text_terms: ['irony'] })).abstract
    expect(hasMatch('verbal irony', re)).toBe(true)
    expect(hasMatch('verbal irony', re)).toBe(true)
    expect(hasMatch('none here', re)).toBe(false)
    expect(hasMatch(null, re)).toBe(false)
  })
})
