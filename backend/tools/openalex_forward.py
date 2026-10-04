#!/usr/bin/env python3
"""Forward snowballing from ReviQ's included papers, using OpenAlex.

ReviQ has no citation database, so a forward iteration (the papers that *cite*
the ones you included) is found elsewhere and imported. This does that search
against OpenAlex and writes a BibTeX file for the iteration's "Import BibTeX".

  1. In ReviQ, create the forward iteration (Snowballing -> New iteration).
  2. Run this (standard library only; ReviQ must be running):

       python3 backend/tools/openalex_forward.py --project 1 \\
           --sources snowballing:1,snowballing:2 --out iteration4.bib

  To snowball only from the core papers (and so not keep widening the review
  through background reading), add --criterion Core (papers you included under
  that criterion) and/or --require-terms sarcas,iron,satir (papers whose title or
  abstract mention a core term).

  3. In ReviQ, open that iteration and "Import BibTeX" with the file.

The start set is the papers included at full-text (and at screening) from the
given sources. Each is looked up in OpenAlex by DOI, or by title when it has no
DOI; then every work that cites any of them is fetched, with its abstract. The
file keeps everything OpenAlex returns unless you filter, because ReviQ counts
what was identified and what was a duplicate for PRISMA: its import flags the
duplicates itself. Filters below are opt-in for that reason.

OpenAlex works without a key. For a key or a polite-pool e-mail, set
OPENALEX_API_KEY / pass --mailto; this script never sends one unless you do.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable, Optional

OPENALEX = "https://api.openalex.org"
REVIQ_DEFAULT = "http://localhost:8000"
SELECT = ("id,doi,title,publication_year,type,language,is_retracted,authorships,"
          "primary_location,abstract_inverted_index,referenced_works")
BATCH = 50          # ids per OR-filter; OpenAlex accepts up to 100


# ── Small pure helpers (tested) ───────────────────────────────────────────────

def norm_doi(doi: Optional[str]) -> str:
    d = (doi or "").strip().lower()
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", d)


def norm_title(title: Optional[str]) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", "", (title or "").lower())).strip()


def short_id(openalex_id: str) -> str:
    """'https://openalex.org/W123' -> 'W123'."""
    return (openalex_id or "").rsplit("/", 1)[-1]


def title_matches(a: str, b: str, threshold: float = 0.9) -> bool:
    wa, wb = set(norm_title(a).split()), set(norm_title(b).split())
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= threshold


def strip_venue_prefix(title: str) -> str:
    """'COLING - Tweet Sarcasm Detection…' -> 'Tweet Sarcasm Detection…'. Imported
    titles often carry the venue like this; only a short, acronym-like prefix is
    removed, and only once, so a dash inside a real title survives."""
    m = re.match(r"^\s*([^\s-][^-]{0,28}?)\s+[-\u2013\u2014]\s+(.+)$", title or "")
    if m and re.search(r"[A-Z0-9]{2,}", m.group(1)) and len(m.group(2).split()) >= 2:
        return m.group(2).strip()
    return (title or "").strip()


def rebuild_abstract(inverted: Optional[dict]) -> str:
    """OpenAlex ships abstracts as {word: [positions]}; put them back in order."""
    if not inverted:
        return ""
    slots: dict[int, str] = {}
    for word, positions in inverted.items():
        for pos in positions:
            slots[pos] = word
    return " ".join(slots[i] for i in sorted(slots))


def _clean(value: str) -> str:
    """Safe inside a BibTeX {…} value: no braces or backslashes to unbalance it."""
    return re.sub(r"\s+", " ", re.sub(r"[{}\\]", " ", value or "")).strip()


def surname(display_name: str) -> str:
    parts = (display_name or "").replace(",", " ").split()
    return re.sub(r"[^A-Za-z]", "", parts[-1]) if parts else ""


def bibtex_entry(work: dict, cites: Iterable[str] = ()) -> str:
    """One BibTeX entry in the shape ReviQ's importer reads: a unique citekey, a
    title, ' and '-separated authors, and the venue in the field its entry type
    uses (booktitle for a conference, journal for everything else)."""
    wid = short_id(work["id"])
    authors = [a["author"]["display_name"] for a in work.get("authorships", [])
               if a.get("author", {}).get("display_name")]
    year = work.get("publication_year")
    source = ((work.get("primary_location") or {}).get("source") or {})
    venue = _clean(source.get("display_name") or "")
    conference = source.get("type") == "conference"
    key = f"{surname(authors[0]) if authors else 'Anon'}{year or ''}_{wid}"
    fields = [("title", _clean(work.get("title") or ""))]
    if authors:
        fields.append(("author", " and ".join(_clean(a) for a in authors)))
    if year:
        fields.append(("year", str(year)))
    if work.get("doi"):
        fields.append(("doi", norm_doi(work["doi"])))
    if venue:
        fields.append(("booktitle" if conference else "journal", venue))
    abstract = _clean(rebuild_abstract(work.get("abstract_inverted_index")))
    if abstract:
        fields.append(("abstract", abstract))
    cited = sorted(set(cites))
    note = f"OpenAlex {wid}" + (f"; cites {', '.join(cited)}" if cited else "")
    fields.append(("note", note))
    body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields)
    return f"@{'inproceedings' if conference else 'article'}{{{key},\n{body}\n}}\n"


# ── OpenAlex and ReviQ access ─────────────────────────────────────────────────

class Client:
    def __init__(self, mailto: Optional[str] = None, api_key: Optional[str] = None,
                 pause: float = 0.12, log=print):
        self.mailto, self.api_key, self.pause, self.log = mailto, api_key, pause, log

    def get(self, url: str, params: Optional[dict] = None, retries: int = 5) -> dict:
        params = dict(params or {})
        if self.mailto:
            params["mailto"] = self.mailto
        if self.api_key:
            params["api_key"] = self.api_key
        full = url + ("?" + urllib.parse.urlencode(params, safe="|,:/") if params else "")
        req = urllib.request.Request(full, headers={"User-Agent": "ReviQ-forward-snowballing/1.0"})
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    time.sleep(self.pause)
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    wait = 2 ** attempt * 2
                    self.log(f"  ... {e.code} from {urllib.parse.urlparse(url).netloc}, retrying in {wait}s")
                    time.sleep(wait)
                    continue
                raise SystemExit(f"{e.code} from {url}: {e.read()[:300].decode('utf-8', 'replace')}")
        raise SystemExit(f"Giving up on {url}")


def reviq(client: Client, base: str, path: str) -> list | dict:
    return client.get(base.rstrip("/") + "/api" + path)


def term_pattern(terms: Iterable[str]) -> Optional["re.Pattern[str]"]:
    """Case-insensitive, matched at the start of a word: "iron" finds "irony" but
    not "environment"."""
    cleaned = [t.strip() for t in terms if t.strip()]
    if not cleaned:
        return None
    return re.compile("|".join(rf"(?<!\w){re.escape(t)}" for t in cleaned), re.IGNORECASE)


def is_core(paper: dict, criteria: set[str], pattern: Optional["re.Pattern[str]"]) -> bool:
    """Whether a paper counts as core under the filters given. With no filter,
    everything does. With both, it must satisfy both.

    * criteria: a reviewer included it at full-text under one of these labels.
    * pattern: its title or abstract mentions one of the core terms."""
    if criteria:
        labels = {c.lower() for c in criteria}
        if not any(d.get("decision") == "I" and (d.get("criterion_label") or "").lower() in labels
                   for d in paper.get("decisions", [])):
            return False
    if pattern is not None and not pattern.search(f"{paper.get('title') or ''} {paper.get('abstract') or ''}"):
        return False
    return True


def start_set(client: Client, base: str, project: int, sources: list[str],
              criteria: Optional[set[str]] = None, terms: Optional[list[str]] = None,
              ) -> tuple[list[dict], set[str], set[str]]:
    """(start papers, DOIs the project has, normalized titles the project has).
    Start = included at full-text and at screening, from the given sources, and
    (if asked) core: see ``is_core``."""
    ft = reviq(client, base, f"/projects/{project}/papers?phase=full-text&dedup_status=original")
    sc = reviq(client, base, f"/projects/{project}/papers?phase=screening")
    decision = lambda p: (p.get("final_decision") or {}).get("decision")
    screened_in = {p["id"] for p in sc if decision(p) == "I" and p["dedup_status"] == "original"}
    start = [p for p in ft if decision(p) == "I" and p["id"] in screened_in and p["source"] in sources]
    pattern = term_pattern(terms or [])
    if criteria or pattern is not None:
        start = [p for p in start if is_core(p, criteria or set(), pattern)]
    known_dois = {norm_doi(p["doi"]) for p in sc if p.get("doi")}
    known_titles = {norm_title(p["title"]) for p in sc}
    return start, known_dois, known_titles


def resolve(client: Client, start: list[dict]) -> tuple[dict[int, str], list[dict]]:
    """Map each start paper to its OpenAlex id: by DOI in batches, then by title."""
    found: dict[int, str] = {}
    by_doi: dict[str, dict] = {}
    for p in start:
        d = norm_doi(p.get("doi"))
        if d and "," not in d:
            by_doi[d] = p
    dois = list(by_doi)
    for i in range(0, len(dois), BATCH):
        chunk = dois[i:i + BATCH]
        data = client.get(f"{OPENALEX}/works", {
            "filter": "doi:" + "|".join(chunk), "per_page": 100, "select": "id,doi"})
        for w in data.get("results", []):
            p = by_doi.get(norm_doi(w.get("doi")))
            if p:
                found[p["id"]] = short_id(w["id"])
    unresolved = [p for p in start if p["id"] not in found]
    still = []
    for p in unresolved:
        # The title as imported, then without a leading venue ("COLING - …").
        queries = list(dict.fromkeys([p["title"], strip_venue_prefix(p["title"])]))
        hit = None
        for q in queries:
            data = client.get(f"{OPENALEX}/works", {
                "search": q, "per_page": 5, "select": "id,title,publication_year"})
            for w in data.get("results", []):
                near = abs((w.get("publication_year") or 0) - (p.get("year") or 0)) <= 1
                if near and any(title_matches(w.get("title") or "", t) for t in queries):
                    hit = short_id(w["id"])
                    break
            if hit:
                break
        if hit:
            found[p["id"]] = hit
        else:
            still.append(p)
    return found, still


def citing_works(client: Client, openalex_ids: list[str], log=print) -> dict[str, dict]:
    """Every work that cites any of the ids, keyed by OpenAlex id."""
    works: dict[str, dict] = {}
    for i in range(0, len(openalex_ids), BATCH):
        chunk = openalex_ids[i:i + BATCH]
        cursor = "*"
        while cursor:
            data = client.get(f"{OPENALEX}/works", {
                "filter": "cites:" + "|".join(chunk), "per_page": 200,
                "cursor": cursor, "select": SELECT})
            for w in data.get("results", []):
                works.setdefault(short_id(w["id"]), w)
            cursor = (data.get("meta") or {}).get("next_cursor")
            log(f"  ids {i + 1}-{i + len(chunk)}: {len(works)} citing works so far")
    return works


# ── Filters (opt-in) ──────────────────────────────────────────────────────────

def keep(work: dict, *, min_year: Optional[int], english_only: bool, exclude_types: set[str],
         skip_retracted: bool, known_dois: set[str], known_titles: set[str], skip_known: bool) -> bool:
    year = work.get("publication_year")
    if min_year and (year is None or year < min_year):
        return False
    if english_only and work.get("language") not in (None, "en"):
        return False
    if work.get("type") in exclude_types:
        return False
    if skip_retracted and work.get("is_retracted"):
        return False
    if skip_known and (norm_doi(work.get("doi")) in known_dois
                       or norm_title(work.get("title")) in known_titles):
        return False
    return bool(work.get("title"))


# ── Main ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--project", type=int, required=True, help="ReviQ project id")
    ap.add_argument("--sources", required=True,
                    help="comma-separated sources that make up the start set, e.g. snowballing:1,snowballing:2")
    ap.add_argument("--criterion",
                    help="only snowball from papers included at full-text under this criterion label "
                         "(comma-separated for several), e.g. Core")
    ap.add_argument("--require-terms",
                    help="only snowball from papers whose title or abstract mention one of these terms "
                         "(comma-separated, matched at the start of a word), e.g. sarcas,iron,satir")
    ap.add_argument("--out", required=True, help="BibTeX file to write")
    ap.add_argument("--reviq", default=REVIQ_DEFAULT, help=f"ReviQ URL (default {REVIQ_DEFAULT})")
    ap.add_argument("--mailto", help="optional e-mail for OpenAlex's polite pool (never sent unless given)")
    ap.add_argument("--limit-start", type=int, help="only use the first N start papers (for a trial run)")
    ap.add_argument("--min-year", type=int, help="drop works published before this year")
    ap.add_argument("--english-only", action="store_true", help="drop works OpenAlex marks as another language")
    ap.add_argument("--exclude-types", default="", help="comma-separated OpenAlex types to drop, e.g. paratext,erratum")
    ap.add_argument("--skip-retracted", action="store_true")
    ap.add_argument("--skip-known", action="store_true",
                    help="drop works already in the project (by DOI or title); ReviQ would flag them as "
                         "duplicates, and PRISMA counts those, so this is off by default")
    args = ap.parse_args(argv)

    client = Client(mailto=args.mailto, api_key=os.environ.get("OPENALEX_API_KEY"))
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    criteria = {c.strip() for c in (args.criterion or "").split(",") if c.strip()}
    terms = [t.strip() for t in (args.require_terms or "").split(",") if t.strip()]
    everyone, _, _ = start_set(client, args.reviq, args.project, sources)
    start, known_dois, known_titles = start_set(client, args.reviq, args.project, sources, criteria, terms)
    if args.limit_start:
        start = start[: args.limit_start]
    filters = "".join([f"; criterion {', '.join(sorted(criteria))}" if criteria else "",
                       f"; mentioning {', '.join(terms)}" if terms else ""])
    print(f"Start set: {len(start)} papers included from {', '.join(sources)}{filters}"
          + (f" (of {len(everyone)} included from those sources)" if filters else ""))
    if not start:
        if criteria and everyone:
            labels = {}
            for p in everyone:
                for d in p.get("decisions", []):
                    if d.get("decision") == "I":
                        labels[d.get("criterion_label") or "(none)"] = labels.get(d.get("criterion_label") or "(none)", 0) + 1
            print("  No included paper has that criterion. The labels on included papers are: "
                  + ", ".join(f"{k} ({v})" for k, v in sorted(labels.items(), key=lambda kv: -kv[1])))
        return 1

    found, unresolved = resolve(client, start)
    print(f"Found in OpenAlex: {len(found)} | not found: {len(unresolved)}")
    for p in unresolved:
        print(f"   not found: id {p['id']} {p['year']} {p['title'][:70]}")

    ids = sorted(set(found.values()))
    works = citing_works(client, ids)
    start_ids = set(ids)
    kept = [w for w in works.values() if keep(
        w, min_year=args.min_year, english_only=args.english_only,
        exclude_types={t for t in args.exclude_types.split(",") if t},
        skip_retracted=args.skip_retracted, known_dois=known_dois,
        known_titles=known_titles, skip_known=args.skip_known)]
    kept.sort(key=lambda w: (w.get("publication_year") or 0, w["id"]))

    with open(args.out, "w", encoding="utf-8") as f:
        for w in kept:
            cites = [short_id(r) for r in w.get("referenced_works", []) if short_id(r) in start_ids]
            f.write(bibtex_entry(w, cites) + "\n")

    with_abs = sum(1 for w in kept if w.get("abstract_inverted_index"))
    print(f"\nCiting works found: {len(works)} | written: {len(kept)} to {args.out}")
    print(f"  with an abstract: {with_abs} ({100 * with_abs // max(len(kept), 1)}%)")
    print(f"  already in the project (by DOI or title): "
          f"{sum(1 for w in works.values() if norm_doi(w.get('doi')) in known_dois or norm_title(w.get('title')) in known_titles)}")
    print(f"\nFor your methods: forward search of {len(found)} papers, OpenAlex, run "
          f"{datetime.date.today().isoformat()}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
