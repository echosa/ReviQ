import { describe, expect, it } from 'vitest'
import { buildRules, criteriaFor, EMPTY_FORM, ruleCount, splitTerms } from './bulk'

describe('splitTerms', () => {
  it('trims and drops empties', () => {
    expect(splitTerms(' fr, DE ,, zh ')).toEqual(['fr', 'DE', 'zh'])
    expect(splitTerms('')).toEqual([])
  })
})

describe('buildRules', () => {
  it('maps an empty form to no rules at all', () => {
    expect(ruleCount(EMPTY_FORM)).toBe(0)
    const r = buildRules(EMPTY_FORM)
    expect(r).toMatchObject({
      mode: 'all', language_codes: [], language_blank: false, language_non_english: false, title_terms: [],
      year_min: null, year_max: null, venue_terms: [], text_terms: [], text_excludes: [],
      abstract: null, sources: [],
    })
  })

  it('turns text into terms and years into numbers', () => {
    const r = buildRules({ ...EMPTY_FORM, languageCodes: 'fr, es', yearMin: '2000', yearMax: 'abc', abstract: 'missing' })
    expect(r.language_codes).toEqual(['fr', 'es'])
    expect(r.year_min).toBe(2000)
    expect(r.year_max).toBeNull()
    expect(r.abstract).toBe('missing')
  })
})

describe('non-English language rule', () => {
  it('is sent as its own flag, not as a list of codes', () => {
    const r = buildRules({ ...EMPTY_FORM, languageNonEnglish: true })
    expect(r.language_non_english).toBe(true)
    expect(r.language_codes).toEqual([])
  })
})

describe('ruleCount', () => {
  it('counts each switched-on rule once, and not the source scope', () => {
    expect(ruleCount({ ...EMPTY_FORM, sources: ['snowballing:3'] })).toBe(0)
    expect(ruleCount({ ...EMPTY_FORM, yearMin: '2000', yearMax: '2020' })).toBe(1)
    expect(ruleCount({ ...EMPTY_FORM, languageBlank: true, titleTerms: 'editorial', abstract: 'present' })).toBe(3)
    expect(ruleCount({ ...EMPTY_FORM, textTerms: 'irony', textExcludes: 'sentiment' })).toBe(2)
    expect(ruleCount({ ...EMPTY_FORM, languageNonEnglish: true })).toBe(1)
  })
})

describe('criteriaFor', () => {
  const crit = [{ label: 'A', phase: 'screening' }, { label: 'B', phase: 'full-text' }]
  it('prefers the phase\'s own list', () => {
    expect(criteriaFor(crit, 'full-text').map(c => c.label)).toEqual(['B'])
  })
  it('falls back to every criterion when the phase has none of its own', () => {
    expect(criteriaFor([crit[0]], 'full-text').map(c => c.label)).toEqual(['A'])
  })
})
