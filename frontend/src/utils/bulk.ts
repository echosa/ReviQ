/** Turning the bulk-actions form into the rules the API takes, and choosing
 *  which criteria a phase offers. Pure, so the form's rules can be tested
 *  without rendering it. */
import type { BulkRules } from '../api/types'

export interface RuleForm {
  mode: 'all' | 'any'
  languageCodes: string
  languageBlank: boolean
  titleTerms: string
  yearMin: string
  yearMax: string
  venueTerms: string
  textTerms: string
  textExcludes: string
  abstract: '' | 'missing' | 'present'
  sources: string[]
}

export const EMPTY_FORM: RuleForm = {
  mode: 'all', languageCodes: '', languageBlank: false, titleTerms: '',
  yearMin: '', yearMax: '', venueTerms: '', textTerms: '', textExcludes: '', abstract: '', sources: [],
}

/** "fr, DE ,, zh" -> ["fr", "DE", "zh"] */
export function splitTerms(text: string): string[] {
  return text.split(',').map(t => t.trim()).filter(Boolean)
}

function year(text: string): number | null {
  const n = parseInt(text.trim(), 10)
  return Number.isNaN(n) ? null : n
}

export function buildRules(form: RuleForm): BulkRules {
  return {
    mode: form.mode,
    language_codes: splitTerms(form.languageCodes),
    language_blank: form.languageBlank,
    title_terms: splitTerms(form.titleTerms),
    year_min: year(form.yearMin),
    year_max: year(form.yearMax),
    venue_terms: splitTerms(form.venueTerms),
    text_terms: splitTerms(form.textTerms),
    text_excludes: splitTerms(form.textExcludes),
    abstract: form.abstract || null,
    sources: form.sources,
  }
}

/** How many rules the form switches on. `sources` is a scope, not a rule. */
export function ruleCount(form: RuleForm): number {
  const r = buildRules(form)
  return [
    r.language_codes.length > 0,
    r.language_blank,
    r.title_terms.length > 0,
    r.year_min !== null || r.year_max !== null,
    r.venue_terms.length > 0,
    r.text_terms.length > 0,
    r.text_excludes.length > 0,
    r.abstract !== null,
  ].filter(Boolean).length
}

/** The criteria a phase offers: those defined for it, or all of them when none
 *  were defined for it (a project often defines one list and uses it at both
 *  phases, which is what the Eligibility page already assumes). */
export function criteriaFor<T extends { phase?: string | null }>(
  criteria: T[], phase: 'screening' | 'full-text',
): T[] {
  const own = criteria.filter(c => c.phase === phase)
  return own.length > 0 ? own : criteria
}
