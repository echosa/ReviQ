/** Which parts of a record made it match the bulk-action rules.
 *
 * Mirrors the server's matching (backend/app/services/bulk_service.py): title,
 * venue and language terms are substrings, and the title-or-abstract terms
 * match at the start of a word. Only terms that *include* a record are
 * highlighted; a "contains none of" rule matches by an absence.
 */
import type { BulkRules } from '../api/types'

export interface Highlighters {
  title: RegExp | null
  abstract: RegExp | null
  venue: RegExp | null
  languages: Set<string>
  /** The "known and not English" rule is on: any other language code is a match. */
  nonEnglish: boolean
  /** The year range the "outside" rule was set with, or null if it is off. */
  years: { min: number; max: number } | null
}

export const escapeRegExp = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/** One case-insensitive pattern for a set of terms, longest first so that
 *  "sarcasm" is not cut short by "sarcas". */
function pattern(terms: string[], wordStart: boolean): RegExp | null {
  const cleaned = [...new Set(terms.map(t => t.trim()).filter(Boolean))]
    .sort((a, b) => b.length - a.length)
  if (cleaned.length === 0) return null
  const body = cleaned.map(t => (wordStart ? '(?<![\\p{L}\\p{N}_])' : '') + escapeRegExp(t)).join('|')
  return new RegExp(body, 'giu')
}

export function buildHighlighters(rules: BulkRules): Highlighters {
  const hasYears = rules.year_min !== null || rules.year_max !== null
  return {
    title: combine(pattern(rules.title_terms, false), pattern(rules.text_terms, true)),
    abstract: pattern(rules.text_terms, true),
    venue: pattern(rules.venue_terms, false),
    languages: new Set(rules.language_codes.map(c => c.trim().toLowerCase()).filter(Boolean)),
    nonEnglish: rules.language_non_english,
    years: hasYears
      ? { min: rules.year_min ?? Number.NEGATIVE_INFINITY, max: rules.year_max ?? Number.POSITIVE_INFINITY }
      : null,
  }
}

function combine(a: RegExp | null, b: RegExp | null): RegExp | null {
  if (a && b) return new RegExp(`${a.source}|${b.source}`, 'giu')
  return a ?? b
}

export interface Piece { text: string; hit: boolean }

/** `text` cut into runs, marking those the pattern matched. */
export function splitMatches(text: string, re: RegExp | null): Piece[] {
  if (!re || !text) return [{ text, hit: false }]
  const pieces: Piece[] = []
  let last = 0
  for (const m of text.matchAll(re)) {
    if (m[0] === '') continue
    if (m.index! > last) pieces.push({ text: text.slice(last, m.index), hit: false })
    pieces.push({ text: m[0], hit: true })
    last = m.index! + m[0].length
  }
  if (last < text.length) pieces.push({ text: text.slice(last), hit: false })
  return pieces.length > 0 ? pieces : [{ text, hit: false }]
}

export const hasMatch = (text: string | null | undefined, re: RegExp | null): boolean =>
  !!re && !!text && new RegExp(re.source, 'iu').test(text)
