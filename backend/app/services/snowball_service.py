"""How much a snowballing iteration is still finding.

Snowballing has no natural end: every included paper has references and
citations, and more "similar" work keeps turning up. Two numbers say when it is
running out, and both are here so the decision to stop can rest on them rather
than on a feeling:

  * **yield**: how many records an iteration brought that got through
    full-text, per record it retrieved;
  * **re-found**: of the papers an iteration found that the project already
    had, how many had been included. A rising share means the citation
    neighbourhood is closing, since new searches keep landing on papers already
    in the review.

Also reported, because they explain the numbers: how many records were
duplicates, how many got through screening, and (optionally) what share of the
full-text includes still mention the core terms, a check that included work is
not drifting away from the topic.
"""
from __future__ import annotations

import re
from typing import Optional

from sqlmodel import Session, select

from app.models import FinalDecision, Paper, SnowballingIteration
from app.services.bibtex_service import normalize_title


def _core_pattern(terms: list[str]) -> Optional["re.Pattern[str]"]:
    cleaned = [t.strip() for t in terms if t.strip()]
    if not cleaned:
        return None
    return re.compile("|".join(rf"(?<!\w){re.escape(t)}" for t in cleaned), re.IGNORECASE)


def _pct(part: int, whole: int) -> Optional[float]:
    return round(100 * part / whole, 1) if whole else None


def snowball_yield(session: Session, project_id: int, core_terms: Optional[list[str]] = None) -> list[dict]:
    iterations = session.exec(
        select(SnowballingIteration)
        .where(SnowballingIteration.project_id == project_id)
        .order_by(SnowballingIteration.iteration_number)
    ).all()
    papers = session.exec(select(Paper).where(Paper.project_id == project_id)).all()

    final: dict[tuple[int, str], str] = {}
    for f in session.exec(select(FinalDecision).where(FinalDecision.project_id == project_id)).all():
        final.setdefault((f.paper_id, f.phase), f.decision)
    screening = lambda p: final.get((p.id, "screening"))
    fulltext = lambda p: final.get((p.id, "full-text"))

    # A record the project already had is found again by DOI, else by title.
    originals = [p for p in papers if p.dedup_status == "original"]
    by_doi: dict[str, Paper] = {}
    by_title: dict[str, Paper] = {}
    for p in originals:
        if p.doi and p.doi.strip():
            by_doi.setdefault(p.doi.strip().lower(), p)
        by_title.setdefault(normalize_title(p.title), p)

    def twin(p: Paper) -> Optional[Paper]:
        hit = by_doi.get(p.doi.strip().lower()) if p.doi and p.doi.strip() else None
        return hit or by_title.get(normalize_title(p.title))

    core = _core_pattern(core_terms or [])
    out = []
    for it in iterations:
        source = f"snowballing:{it.iteration_number}"
        records = [p for p in papers if p.source == source]
        orig = [p for p in records if p.dedup_status == "original"]
        dups = [p for p in records if p.dedup_status != "original"]
        screened_in = [p for p in orig if screening(p) == "I"]
        ft_decided = [p for p in screened_in if fulltext(p) is not None]
        ft_in = [p for p in ft_decided if fulltext(p) == "I"]

        known = included = 0
        for p in dups:
            t = twin(p)
            if t is not None and t.source != source:
                known += 1
                included += screening(t) == "I"

        entry = {
            "iteration_number": it.iteration_number,
            "iteration_type": it.iteration_type,
            "retrieved": len(records),
            "originals": len(orig),
            "duplicates": len(dups),
            "duplicate_pct": _pct(len(dups), len(records)),
            "screened": sum(1 for p in orig if screening(p) is not None),
            "screening_included": len(screened_in),
            "fulltext_decided": len(ft_decided),
            "fulltext_included": len(ft_in),
            # Full-text includes per original record. Only meaningful once
            # full-text is done, so it is None until something has been decided there.
            "yield_pct": _pct(len(ft_in), len(orig)) if ft_decided else None,
            "refound_known": known,
            "refound_included": included,
            "refound_included_pct": _pct(included, known),
            "core_in_title": None, "core_in_title_pct": None,
        }
        if core is not None:
            in_title = sum(1 for p in ft_in if core.search(p.title or ""))
            entry["core_in_title"] = in_title
            entry["core_in_title_pct"] = _pct(in_title, len(ft_in))
        out.append(entry)
    return out
