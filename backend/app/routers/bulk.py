"""
Bulk decisions and duplicate review (see services.bulk_service).

Every write here is all-or-nothing: the request is validated in full first, and
if any record fails nothing is recorded. Decisions go through the same
upsert + ``sync_decision_state`` path as a single decision, so conflicts and
final decisions come out the same either way.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.database import get_session
from app.models import ExclusionCriterion, Paper, Project, Reviewer
from app.services import bulk_service as bulk
from app.services.decision_service import record_reviewer_decision, sync_decision_state

router = APIRouter(tags=["bulk"])

MAX_BULK = 5000
DECISIONS = ("I", "E", "U")


def _paper_view(p: Paper) -> dict:
    return {
        "id": p.id, "citekey": p.citekey, "title": p.title, "authors": p.authors,
        "year": p.year, "venue": p.venue, "source": p.source, "doi": p.doi,
        "language": p.language, "abstract": p.abstract,
        "dedup_status": p.dedup_status,
    }


def _scope(project_id: int, reviewer_id: int, phase: str, session: Session) -> None:
    if not session.get(Project, project_id):
        raise HTTPException(404, "Project not found")
    reviewer = session.get(Reviewer, reviewer_id)
    if not reviewer or reviewer.project_id != project_id:
        raise HTTPException(404, "Reviewer not found")
    if phase not in bulk.PHASES:
        raise HTTPException(422, f"phase must be one of {', '.join(bulk.PHASES)}")


def _fail(errors: list[str], status: int = 422) -> None:
    raise HTTPException(status, {"errors": errors})


# ── Rule matching ─────────────────────────────────────────────────────────────

class MatchBody(BaseModel):
    reviewer_id: int
    phase: str = "screening"
    # Which records to search, by this reviewer's own decision on them.
    statuses: list[str] = Field(default_factory=lambda: ["undecided"])
    rules: bulk.MatchRules
    limit: int = Field(200, ge=1, le=1000)
    offset: int = Field(0, ge=0)


@router.post("/projects/{project_id}/bulk/match")
def match_undecided(project_id: int, body: MatchBody, session: Session = Depends(get_session)):
    """Records that satisfy the rules, among those whose decision by this
    reviewer is one of ``statuses`` (undecided unless asked otherwise).
    Read-only; ``POST /decisions/bulk`` is what applies a decision."""
    _scope(project_id, body.reviewer_id, body.phase, session)
    if not body.rules.predicates():
        _fail(["Enable at least one rule."])
    if body.rules.mode not in ("all", "any"):
        _fail(["mode must be 'all' or 'any'."])
    if not body.statuses or any(s not in bulk.STATUSES for s in body.statuses):
        _fail([f"statuses must be a non-empty subset of {', '.join(bulk.STATUSES)}."])

    candidates, decided = bulk.papers_by_status(
        session, project_id, body.phase, body.reviewer_id, body.statuses,
        body.rules.sources or None)
    matched = sorted(bulk.match_papers(candidates, body.rules), key=lambda p: p.id)
    page = matched[body.offset: body.offset + body.limit]

    def view(p: Paper) -> dict:
        decision, criterion = decided.get(p.id, (None, None))
        return {**_paper_view(p), "decision": decision, "criterion_label": criterion}

    by_status = {s: 0 for s in bulk.STATUSES}
    for p in matched:
        by_status[decided[p.id][0] if p.id in decided else "undecided"] += 1
    return {
        "candidates": len(candidates),
        "matched": len(matched),
        # Of all the matches (not just this page): how many have each decision now.
        "matched_by_status": by_status,
        "offset": body.offset,
        "limit": body.limit,
        "papers": [view(p) for p in page],
    }


# ── Bulk decisions ────────────────────────────────────────────────────────────

class BulkItem(BaseModel):
    paper_id: int
    decision: str
    criterion_label: Optional[str] = None
    rationale: Optional[str] = None


class BulkBody(BaseModel):
    reviewer_id: int
    phase: str = "screening"
    decisions: list[BulkItem]
    # Off unless asked: a record this reviewer has already decided is refused,
    # so that a stale preview cannot silently overwrite anything. On, it is
    # replaced, and ``changes`` says what each decision was before.
    replace: bool = False


@router.post("/projects/{project_id}/decisions/bulk")
def apply_bulk_decisions(project_id: int, body: BulkBody, session: Session = Depends(get_session)):
    """Record this reviewer's decision on many records at once.

    Only original records in scope for the phase are accepted: a record flagged
    as a duplicate is refused, and at full-text only records screening included
    can be assessed. A record this reviewer has already decided is refused too
    unless ``replace`` is set, in which case the decision is replaced and its
    previous value is returned.
    """
    _scope(project_id, body.reviewer_id, body.phase, session)
    if not body.decisions:
        _fail(["No decisions given."])
    if len(body.decisions) > MAX_BULK:
        _fail([f"At most {MAX_BULK} decisions per request."])

    in_scope = {p.id: p for p in bulk.candidate_papers(session, project_id, body.phase)}
    decided = bulk.own_decisions(session, project_id, body.phase, body.reviewer_id)

    errors: list[str] = []
    seen: set[int] = set()
    for item in body.decisions:
        pid = item.paper_id
        if item.decision not in DECISIONS:
            errors.append(f"Paper {pid}: decision must be one of I, E, U.")
        elif pid in seen:
            errors.append(f"Paper {pid}: listed more than once.")
        elif pid in decided and not body.replace:
            errors.append(f"Paper {pid}: you have already decided it in this phase "
                          "(ask to replace decisions to change it).")
        elif pid not in in_scope:
            errors.append(
                f"Paper {pid}: not an original record in scope for {body.phase} "
                "(missing, a flagged duplicate, or not included at screening).")
        seen.add(pid)
    if errors:
        _fail(errors, 409)

    changes = [{"paper_id": i.paper_id, "previous": decided[i.paper_id][0],
                "previous_criterion": decided[i.paper_id][1]}
               for i in body.decisions if i.paper_id in decided]
    for item in body.decisions:
        record_reviewer_decision(
            session, project_id=project_id, paper_id=item.paper_id,
            reviewer_id=body.reviewer_id, phase=body.phase, decision=item.decision,
            criterion_label=item.criterion_label, rationale=item.rationale,
        )
    session.commit()
    for item in body.decisions:
        sync_decision_state(session, project_id, item.paper_id, body.phase)
    session.commit()
    return {"applied": len(body.decisions), "replaced": len(changes), "changes": changes}


class ApplyMatchingBody(BaseModel):
    reviewer_id: int
    phase: str = "screening"
    statuses: list[str] = Field(default_factory=lambda: ["undecided"])
    rules: bulk.MatchRules
    decision: str
    criterion_label: Optional[str] = None
    rationale: Optional[str] = None
    replace: bool = False
    # How many records matched when the reviewer previewed. If the rules match a
    # different number now, nothing is applied: the preview is out of date.
    expected_matched: int
    # Records the reviewer unchecked in the preview, left alone.
    except_ids: list[int] = Field(default_factory=list)


MAX_MATCHING = 20000


@router.post("/projects/{project_id}/decisions/bulk-matching")
def apply_to_all_matches(project_id: int, body: ApplyMatchingBody, session: Session = Depends(get_session)):
    """Record this reviewer's decision on *every* record the rules match, not just
    the page a preview shows.

    The server finds the matches itself, with the same rules and search scope as
    ``/bulk/match``, so no list of ids travels from the browser. It refuses if the
    number of matches is not ``expected_matched`` (the preview is stale), if a
    match already has a decision and ``replace`` is not set, or if there are more
    than the cap. All or nothing, through the shared decision state machine.
    """
    _scope(project_id, body.reviewer_id, body.phase, session)
    if body.decision not in DECISIONS:
        _fail(["decision must be one of I, E, U."])
    if not body.rules.predicates():
        _fail(["Enable at least one rule."])
    if body.rules.mode not in ("all", "any"):
        _fail(["mode must be 'all' or 'any'."])
    if not body.statuses or any(s not in bulk.STATUSES for s in body.statuses):
        _fail([f"statuses must be a non-empty subset of {', '.join(bulk.STATUSES)}."])

    candidates, decided = bulk.papers_by_status(
        session, project_id, body.phase, body.reviewer_id, body.statuses,
        body.rules.sources or None)
    matched = bulk.match_papers(candidates, body.rules)
    if len(matched) != body.expected_matched:
        _fail([f"{len(matched)} records match now, not the {body.expected_matched} you previewed. "
               "Preview again before applying."], 409)

    skip = set(body.except_ids)
    targets = [p for p in matched if p.id not in skip]
    if not targets:
        _fail(["Nothing to apply: every match was left out."])
    if len(targets) > MAX_MATCHING:
        _fail([f"{len(targets)} records is more than the {MAX_MATCHING} allowed at once. Narrow the rules."])
    already = [p for p in targets if p.id in decided]
    if already and not body.replace:
        _fail([f"{len(already)} of the matches already have a decision from you "
               "(ask to replace decisions to change them)."], 409)

    changes = [{"paper_id": p.id, "previous": decided[p.id][0], "previous_criterion": decided[p.id][1]}
               for p in already]
    for p in targets:
        record_reviewer_decision(
            session, project_id=project_id, paper_id=p.id, reviewer_id=body.reviewer_id,
            phase=body.phase, decision=body.decision, criterion_label=body.criterion_label,
            rationale=body.rationale,
        )
    session.commit()
    for p in targets:
        sync_decision_state(session, project_id, p.id, body.phase)
    session.commit()
    return {"applied": len(targets), "replaced": len(changes), "skipped": len(matched) - len(targets),
            "changes": changes}


# ── Duplicate review ──────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/duplicate-candidates")
def duplicate_candidates(
    project_id: int,
    reviewer_id: int,
    phase: str = "screening",
    status: str = Query("open", pattern="^(open|all)$"),
    hide_lost: bool = False,
    hide_settled: bool = False,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
):
    """Groups of records that look like the same work.

    Groups are found among original records. ``status=open`` keeps those where
    this reviewer still has something to do: a copy they have not decided, or a
    group in which every copy was excluded only as "Duplicate"
    (``all_excluded_as_duplicate``), which loses the work entirely and wants a
    second look (``hide_lost`` leaves those out and reports how many in
    ``hidden``). ``hide_settled`` leaves out groups where every copy but one is
    already excluded as a duplicate: one copy stays, the rest are dealt with
    (``hidden_settled`` says how many). At screening that check also counts copies ReviQ flagged as
    duplicates, so a work whose original and flagged copy are both out is listed
    too (the flagged copy is shown but cannot be acted on).
    """
    _scope(project_id, reviewer_id, phase, session)
    papers = bulk.candidate_papers(session, project_id, phase)
    decided = bulk.own_decisions(session, project_id, phase, reviewer_id)

    def view(g: dict) -> dict:
        members = []
        for p in g["papers"]:
            decision, criterion = decided.get(p.id, (None, None))
            members.append({**_paper_view(p), "decision": decision, "criterion_label": criterion})
        dup_excluded = sum(m["decision"] == "E" and bulk.is_duplicate_criterion(m["criterion_label"])
                           for m in members)
        return {"papers": members, "reasons": g["reasons"], "score": g["score"],
                "all_excluded_as_duplicate": dup_excluded == len(members),
                # All but one copy is already excluded as a duplicate.
                "settled": len(members) >= 2 and dup_excluded == len(members) - 1}

    groups = []
    already_listed: set[int] = set()       # originals in a group listed below
    for g in bulk.find_duplicate_groups(papers):
        v = view(g)
        if v["all_excluded_as_duplicate"]:
            already_listed.update(m["id"] for m in v["papers"])
        if status == "open" and not v["all_excluded_as_duplicate"] \
                and all(m["decision"] for m in v["papers"]):
            continue
        groups.append(v)

    # A work whose every copy is out, counting the copies ReviQ flagged as
    # duplicates, has left the review. Those groups are listed too, with the
    # flagged copies shown for context (they cannot be kept or excluded).
    if phase == "screening":
        flagged = bulk.flagged_duplicates(session, project_id)
        for g in bulk.find_duplicate_groups([*papers, *flagged]):
            if all(p.dedup_status == "original" for p in g["papers"]):
                continue                      # already handled above
            v = view(g)
            originals = {m["id"] for m in v["papers"] if m["dedup_status"] == "original"}
            if v["all_excluded_as_duplicate"] and not originals <= already_listed:
                groups.append(v)
    # Optionally leave out the groups whose every copy is already excluded as a
    # duplicate, counting how many were left out.
    hidden = 0
    if hide_lost:
        hidden = sum(g["all_excluded_as_duplicate"] for g in groups)
        groups = [g for g in groups if not g["all_excluded_as_duplicate"]]
    hidden_settled = 0
    if hide_settled:
        hidden_settled = sum(g["settled"] for g in groups)
        groups = [g for g in groups if not g["settled"]]
    groups.sort(key=lambda g: min(m["id"] for m in g["papers"]))
    return {"total": len(groups), "offset": offset, "limit": limit,
            # Totals over every group, not just this page.
            "copies": sum(len(g["papers"]) for g in groups),
            "lost": sum(g["all_excluded_as_duplicate"] for g in groups),
            "hidden": hidden,
            "hidden_settled": hidden_settled,
            "groups": groups[offset: offset + limit]}


class DuplicateGroup(BaseModel):
    keep: list[int]
    exclude: list[int]


class ResolveBody(BaseModel):
    reviewer_id: int
    phase: str = "screening"
    criterion_label: str
    groups: list[DuplicateGroup]


@router.post("/projects/{project_id}/duplicates/resolve")
def resolve_duplicates(project_id: int, body: ResolveBody, session: Session = Depends(get_session)):
    """Exclude copies as duplicates, each group naming the copy that stays.

    Excluding every copy of a work would drop it from the review altogether, so
    a group without a keeper is refused, as is a keeper that this request also
    excludes. Replaces this reviewer's existing decision on an excluded copy;
    the previous decisions come back in ``changes`` so it can be undone.
    """
    _scope(project_id, body.reviewer_id, body.phase, session)
    if not body.groups:
        _fail(["No groups given."])
    labels = {c.label for c in session.exec(
        select(ExclusionCriterion).where(ExclusionCriterion.project_id == project_id)).all()}
    if body.criterion_label not in labels:
        _fail([f"'{body.criterion_label}' is not one of this project's exclusion criteria."])

    in_scope = {p.id for p in bulk.candidate_papers(session, project_id, body.phase)}
    decided = bulk.own_decisions(session, project_id, body.phase, body.reviewer_id)

    errors: list[str] = []
    to_exclude: dict[int, list[int]] = {}     # excluded paper -> the copies kept
    kept_anywhere: set[int] = set()
    for n, g in enumerate(body.groups, start=1):
        if not g.keep:
            errors.append(f"Group {n}: name at least one copy to keep.")
        if not g.exclude:
            errors.append(f"Group {n}: nothing to exclude.")
        if set(g.keep) & set(g.exclude):
            errors.append(f"Group {n}: a copy cannot be both kept and excluded.")
        for pid in [*g.keep, *g.exclude]:
            if pid not in in_scope:
                errors.append(f"Group {n}: paper {pid} is not an original record in scope for {body.phase}.")
        for pid in g.exclude:
            if pid in to_exclude:
                errors.append(f"Group {n}: paper {pid} is excluded in more than one group.")
            to_exclude[pid] = g.keep
        for pid in g.keep:
            decision, criterion = decided.get(pid, (None, None))
            if decision == "E" and criterion == body.criterion_label:
                errors.append(
                    f"Group {n}: paper {pid} is already excluded as '{body.criterion_label}', "
                    "so it cannot be the copy that stays.")
        kept_anywhere.update(g.keep)
    for pid in kept_anywhere & set(to_exclude):
        errors.append(f"Paper {pid} is kept in one group and excluded in another.")
    if errors:
        _fail(errors)

    changes = [{"paper_id": pid, "previous": (decided.get(pid) or (None, None))[0],
                "previous_criterion": (decided.get(pid) or (None, None))[1]}
               for pid in to_exclude]
    for pid, keepers in to_exclude.items():
        record_reviewer_decision(
            session, project_id=project_id, paper_id=pid, reviewer_id=body.reviewer_id,
            phase=body.phase, decision="E", criterion_label=body.criterion_label,
            rationale="Duplicate of paper " + ", ".join(str(k) for k in keepers),
        )
    session.commit()
    for pid in to_exclude:
        sync_decision_state(session, project_id, pid, body.phase)
    session.commit()
    return {"applied": len(to_exclude), "changes": changes}
