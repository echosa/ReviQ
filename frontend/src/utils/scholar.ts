/** A Google Scholar search for a record, from its title (and, for a title too
 *  short to find on its own, its first author's surname).
 *
 *  The title is not quoted: imported titles often carry a venue prefix
 *  ("EMNLP - Sarcasm as Contrast…") that an exact-phrase search would miss. */

const SHORT_TITLE_WORDS = 5

/** "Riloff, Ellen; Qadir, Ashequl" -> "Riloff";  "Ellen Riloff and X" -> "Riloff". */
export function firstAuthorSurname(authors?: string | null): string | null {
  const first = (authors ?? '').split(/;|\band\b/i)[0]?.trim()
  if (!first) return null
  const surname = first.includes(',') ? first.split(',')[0] : first.split(/\s+/).pop()
  const cleaned = (surname ?? '').replace(/[{}\\"]/g, '').trim()
  return cleaned || null
}

export function scholarQuery(title: string, authors?: string | null): string {
  const clean = title.replace(/[{}]/g, '').replace(/\s+/g, ' ').trim().slice(0, 250)
  const words = clean.split(' ').filter(Boolean).length
  const surname = words < SHORT_TITLE_WORDS ? firstAuthorSurname(authors) : null
  return surname ? `${clean} ${surname}` : clean
}

export function scholarUrl(title: string, authors?: string | null): string {
  return `https://scholar.google.com/scholar?q=${encodeURIComponent(scholarQuery(title, authors))}`
}
