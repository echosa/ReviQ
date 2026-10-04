"""
Bulk decisions and duplicate review.

Two helpers for working through a large screening queue:

  * rule matching — "every undecided record whose language is X, or whose title
    contains Y" — so a reviewer can act on many records at once and still see
    what they are about to do first;
  * duplicate candidates — records that look like the same work but that import
    dedup (exact DOI, or exact title + venue) did not catch, typically because
    one copy has no DOI and a venue-prefixed title ("EMNLP - Sarcasm as …").

Both only ever offer *original* records. A record already flagged as a
duplicate is not part of the count and is never screened.

At the full-text phase the candidate set is the records screening included, the
same set the Eligibility page works on.
"""
import math
import re
from collections import Counter, defaultdict
from typing import Callable, Optional

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.models import FinalDecision, Paper, ReviewerDecision
from app.services.bibtex_service import normalize_title

PHASES = ("screening", "full-text")

# Records whose normalized titles are at least this similar (Jaccard over
# words) are offered as possible duplicates, provided the title is long
# enough that a high overlap means something.
TITLE_SIMILARITY = 0.85
SIMILAR_TITLE_MIN_WORDS = 6
EXACT_TITLE_MIN_WORDS = 3


# ── Candidate set ─────────────────────────────────────────────────────────────

def candidate_papers(
    session: Session, project_id: int, phase: str,
    sources: Optional[list[str]] = None,
) -> list[Paper]:
    """Original records in scope for ``phase``, whatever their decision state."""
    stmt = (
        select(Paper)
        .where(Paper.project_id == project_id)
        .where(Paper.dedup_status == "original")
    )
    if sources:
        stmt = stmt.where(Paper.source.in_(sources))
    papers = session.exec(stmt).all()
    if phase == "full-text":
        included = set(session.exec(
            select(FinalDecision.paper_id)
            .where(FinalDecision.project_id == project_id)
            .where(FinalDecision.phase == "screening")
            .where(FinalDecision.decision == "I")
        ).all())
        papers = [p for p in papers if p.id in included]
    return papers


def flagged_duplicates(session: Session, project_id: int) -> list[Paper]:
    """Records ReviQ has flagged as duplicates. Never actionable, but they are
    still copies of a work, so they count when asking whether a work is still in
    the review."""
    return list(session.exec(
        select(Paper)
        .where(Paper.project_id == project_id)
        .where(Paper.dedup_status != "original")
    ).all())


def own_decisions(
    session: Session, project_id: int, phase: str, reviewer_id: int,
) -> dict[int, tuple[str, Optional[str]]]:
    """paper_id -> (decision, criterion_label) for one reviewer in one phase."""
    rows = session.exec(
        select(ReviewerDecision)
        .where(ReviewerDecision.project_id == project_id)
        .where(ReviewerDecision.phase == phase)
        .where(ReviewerDecision.reviewer_id == reviewer_id)
    ).all()
    return {r.paper_id: (r.decision, r.criterion_label) for r in rows}


# What a reviewer's own decision on a record can be, for choosing which records
# to search: not decided yet, or Include / Exclude / Uncertain.
STATUSES = ("undecided", "I", "E", "U")


def papers_by_status(
    session: Session, project_id: int, phase: str, reviewer_id: int,
    statuses: list[str], sources: Optional[list[str]] = None,
) -> tuple[list[Paper], dict[int, tuple[str, Optional[str]]]]:
    """In-scope records whose decision by this reviewer is one of ``statuses``,
    with the reviewer's decisions (paper_id -> (decision, criterion))."""
    decided = own_decisions(session, project_id, phase, reviewer_id)
    wanted = set(statuses)

    def status(p: Paper) -> str:
        return decided[p.id][0] if p.id in decided else "undecided"

    papers = [p for p in candidate_papers(session, project_id, phase, sources)
              if status(p) in wanted]
    return papers, decided


# ── Rule matching ─────────────────────────────────────────────────────────────

class MatchRules(BaseModel):
    """Each field that is set is one rule. ``mode`` says whether a record must
    satisfy all of the enabled rules or any one of them. ``sources`` is a scope
    (which imports to look at), not a rule, so it always narrows."""
    mode: str = "all"                                   # all | any
    language_codes: list[str] = Field(default_factory=list)   # ISO codes, e.g. "fr"
    language_blank: bool = False
    # A language was detected and it is not English: every other code at once.
    language_non_english: bool = False
    title_terms: list[str] = Field(default_factory=list)
    # The partner of title_terms: the title contains none of these (same substring match).
    title_excludes: list[str] = Field(default_factory=list)
    year_min: Optional[int] = None
    year_max: Optional[int] = None    # "year outside [min, max]"; no year never matches
    venue_terms: list[str] = Field(default_factory=list)
    # Title *or* abstract. Matched at the start of a word, so "iron" finds
    # "irony" but not "environment", and a stem like "sarcas" finds "sarcasm".
    text_terms: list[str] = Field(default_factory=list)      # contains any of these
    text_excludes: list[str] = Field(default_factory=list)   # contains none of these
    abstract: Optional[str] = None                      # missing | present
    sources: list[str] = Field(default_factory=list)

    def predicates(self) -> list[Callable[[Paper], bool]]:
        rules: list[Callable[[Paper], bool]] = []
        codes = {c.strip().lower() for c in self.language_codes if c.strip()}
        if codes:
            rules.append(lambda p: (p.language or "").strip().lower() in codes)
        if self.language_blank:
            rules.append(lambda p: not (p.language or "").strip())
        if self.language_non_english:
            rules.append(lambda p: bool((p.language or "").strip())
                         and (p.language or "").strip().lower() != "en")
        terms = [t.strip().lower() for t in self.title_terms if t.strip()]
        if terms:
            rules.append(lambda p: any(t in (p.title or "").lower() for t in terms))
        if self.year_min is not None or self.year_max is not None:
            lo = self.year_min if self.year_min is not None else -10**9
            hi = self.year_max if self.year_max is not None else 10**9
            rules.append(lambda p: p.year is not None and (p.year < lo or p.year > hi))
        not_in_title = [t.strip().lower() for t in self.title_excludes if t.strip()]
        if not_in_title:
            rules.append(lambda p: not any(t in (p.title or "").lower() for t in not_in_title))
        venues = [t.strip().lower() for t in self.venue_terms if t.strip()]
        if venues:
            rules.append(lambda p: any(t in (p.venue or "").lower() for t in venues))
        wanted = _word_start(self.text_terms)
        if wanted:
            rules.append(lambda p: bool(wanted.search(_text(p))))
        unwanted = _word_start(self.text_excludes)
        if unwanted:
            rules.append(lambda p: not unwanted.search(_text(p)))
        if self.abstract == "missing":
            rules.append(lambda p: not (p.abstract or "").strip())
        elif self.abstract == "present":
            rules.append(lambda p: bool((p.abstract or "").strip()))
        return rules


def _text(p: Paper) -> str:
    return f"{p.title or ''} {p.abstract or ''}"


def _word_start(terms: list[str]) -> Optional["re.Pattern[str]"]:
    cleaned = [t.strip() for t in terms if t.strip()]
    if not cleaned:
        return None
    return re.compile("|".join(rf"(?<!\w){re.escape(t)}" for t in cleaned), re.IGNORECASE)


def match_papers(papers: list[Paper], rules: MatchRules) -> list[Paper]:
    predicates = rules.predicates()
    combine = any if rules.mode == "any" else all
    return [p for p in papers if combine(rule(p) for rule in predicates)]


# ── Duplicate candidates ──────────────────────────────────────────────────────

class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def _similar_title_pairs(words_by_id: dict[int, frozenset[str]]) -> list[tuple[int, int, float]]:
    """Pairs of titles whose word sets have Jaccard >= TITLE_SIMILARITY.

    Prefix filtering (rarest words first) so that 10k titles are not compared
    pairwise: two sets can only reach the threshold if they share a word in
    their prefixes.
    """
    freq = Counter(w for ws in words_by_id.values() for w in ws)
    index: dict[str, list[int]] = defaultdict(list)
    ordered: dict[int, list[str]] = {}
    for pid, ws in words_by_id.items():
        toks = sorted(ws, key=lambda w: (freq[w], w))
        ordered[pid] = toks
        n = len(toks)
        for w in toks[: n - math.ceil(TITLE_SIMILARITY * n) + 1]:
            index[w].append(pid)

    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int, float]] = []
    for ids in index.values():
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                key = (a, b) if a < b else (b, a)
                if key in seen:
                    continue
                seen.add(key)
                wa, wb = words_by_id[a], words_by_id[b]
                if min(len(wa), len(wb)) < TITLE_SIMILARITY * max(len(wa), len(wb)):
                    continue
                score = len(wa & wb) / len(wa | wb)
                if score >= TITLE_SIMILARITY:
                    out.append((key[0], key[1], score))
    return out


def find_duplicate_groups(papers: list[Paper]) -> list[dict]:
    """Group records that look like the same work.

    Links come from three signals, merged transitively: the same DOI, the same
    normalized title, and near-identical long titles. A group is *a candidate*:
    same-titled editions or chapters are grouped too, and it is the reviewer
    who decides which are copies.
    """
    uf = _UnionFind()
    links: dict[tuple[int, int], tuple[str, float]] = {}
    by_id = {p.id: p for p in papers}

    def link(a: int, b: int, reason: str, score: float) -> None:
        uf.union(a, b)
        links[(min(a, b), max(a, b))] = (reason, score)

    by_doi: dict[str, list[int]] = defaultdict(list)
    by_title: dict[str, list[int]] = defaultdict(list)
    words_by_id: dict[int, frozenset[str]] = {}
    for p in papers:
        if p.doi and p.doi.strip():
            by_doi[p.doi.strip().lower()].append(p.id)
        norm = normalize_title(p.title)
        words = norm.split()
        if len(words) >= EXACT_TITLE_MIN_WORDS:
            by_title[norm].append(p.id)
        if len(words) >= SIMILAR_TITLE_MIN_WORDS:
            words_by_id[p.id] = frozenset(words)

    for ids in by_doi.values():
        for other in ids[1:]:
            link(ids[0], other, "Same DOI", 1.0)
    for ids in by_title.values():
        for other in ids[1:]:
            link(ids[0], other, "Same title", 1.0)
    for a, b, score in _similar_title_pairs(words_by_id):
        if (a, b) not in links:
            link(a, b, "Similar title", score)

    members: dict[int, list[int]] = defaultdict(list)
    for pid in by_id:
        if pid in uf.parent:
            members[uf.find(pid)].append(pid)

    groups = []
    for ids in members.values():
        if len(ids) < 2:
            continue
        ids.sort()
        in_group = [v for k, v in links.items() if k[0] in ids and k[1] in ids]
        reasons = sorted({r for r, _ in in_group}, key=["Same DOI", "Same title", "Similar title"].index)
        groups.append({
            "papers": [by_id[i] for i in ids],
            "reasons": reasons,
            "score": min(s for _, s in in_group),
        })
    groups.sort(key=lambda g: g["papers"][0].id)
    return groups


def is_duplicate_criterion(label: Optional[str]) -> bool:
    return (label or "").strip().lower() == "duplicate"
