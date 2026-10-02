"""
Bulk decisions and duplicate review (routers.bulk / services.bulk_service).

What is pinned here:

  - rule matching offers only undecided *original* records, honours all/any,
    and a record with no year never counts as "outside the year range"
  - a bulk decision is all-or-nothing, never overwrites a decision, refuses a
    record flagged as a duplicate, and at full-text only takes records that
    screening included
  - it goes through the shared state machine (a second reviewer who disagrees
    opens a conflict, exactly as with single decisions)
  - duplicate candidates find the pairs import dedup misses (a venue-prefixed
    title, no DOI), and resolving a group can never exclude every copy of it
"""
import pytest
from sqlmodel import select

from app.models import Paper

SARCASM = "Sarcasm as Contrast between a Positive Sentiment and Negative Situation"

PAPERS = [
    {"citekey": "a1", "title": "Editorial: the year in review", "year": 2019, "doi": "10.1/a1"},
    {"citekey": "a2", "title": "A study of reviewer agreement in screening", "year": 2021, "doi": "10.1/a2"},
    {"citekey": "a3", "title": "An old paper about retrieval", "year": 1995, "doi": "10.1/a3"},
    {"citekey": "a4", "title": "Comment on the editorial", "year": 2022, "doi": "10.1/a4"},
    {"citekey": "a5", "title": SARCASM, "year": 2013, "doi": "10.18653/v1/d13-1066", "venue": "ACL"},
    {"citekey": "a6", "title": "EMNLP - " + SARCASM, "year": 2013, "venue": "EMNLP"},
]


class NS:
    def __init__(self, **kw): self.__dict__.update(kw)


@pytest.fixture
def ctx(instance):
    proj = instance.create_project(title="Bulk", lead="Alice")
    pid = proj["id"]
    r1 = instance.reviewers(pid)[0]
    for label in ("Off-topic", "Duplicate"):
        instance.client.post(f"/api/projects/{pid}/criteria/exclusion",
                             json={"label": label, "description": label, "phase": "screening"}
                             ).raise_for_status()
    instance.import_bib(pid, PAPERS, db_name="acm")
    by_key = {p["citekey"]: p for p in instance.papers(pid)}
    return NS(i=instance, pid=pid, r1=r1, p=by_key)


def _match(ctx, rules, *, phase="screening", reviewer=None):
    return ctx.i.client.post(f"/api/projects/{ctx.pid}/bulk/match", json={
        "reviewer_id": (reviewer or ctx.r1)["id"], "phase": phase, "rules": rules})


def _bulk(ctx, items, *, phase="screening", reviewer=None):
    return ctx.i.client.post(f"/api/projects/{ctx.pid}/decisions/bulk", json={
        "reviewer_id": (reviewer or ctx.r1)["id"], "phase": phase, "decisions": items})


def _ids(resp):
    return sorted(p["id"] for p in resp.json()["papers"])


def _flag_duplicate(ctx, citekey):
    paper = ctx.i.session.get(Paper, ctx.p[citekey]["id"])
    paper.dedup_status = "duplicate"
    ctx.i.session.add(paper)
    ctx.i.session.commit()


# ── Rule matching ─────────────────────────────────────────────────────────────

def test_match_needs_at_least_one_rule(ctx):
    assert _match(ctx, {}).status_code == 422


def test_title_rule_matches_only_undecided_originals(ctx):
    ids = {k: v["id"] for k, v in ctx.p.items()}
    ctx.i.decide(ctx.pid, ids["a4"], reviewer_id=ctx.r1["id"], decision="E")  # decided
    r = _match(ctx, {"title_terms": ["editorial", "comment"]})
    assert r.status_code == 200
    assert _ids(r) == [ids["a1"]]
    assert r.json()["matched"] == 1


def test_flagged_duplicates_are_never_offered(ctx):
    _flag_duplicate(ctx, "a1")
    r = _match(ctx, {"title_terms": ["editorial"]})
    assert ctx.p["a1"]["id"] not in _ids(r)


def test_all_versus_any(ctx):
    # "editorial" in the title; year outside 2000-2020 (here: after 2020 only).
    rules = {"title_terms": ["editorial"], "year_max": 2020}
    p = {k: v["id"] for k, v in ctx.p.items()}
    # ALL: only the 2022 comment on the editorial satisfies both rules.
    assert _ids(_match(ctx, {**rules, "mode": "all"})) == [p["a4"]]
    # ANY: the 2019 editorial (title) and the 2021/2022 papers (year) as well.
    any_ = set(_ids(_match(ctx, {**rules, "mode": "any"})))
    assert any_ == {p["a1"], p["a2"], p["a4"]}


def _set(ctx, citekey, **fields):
    paper = ctx.i.session.get(Paper, ctx.p[citekey]["id"])
    for k, v in fields.items():
        setattr(paper, k, v)
    ctx.i.session.add(paper)
    ctx.i.session.commit()


def test_text_terms_search_title_and_abstract_from_the_start_of_a_word(ctx):
    _set(ctx, "a2", abstract="We study verbal irony in tweets.")
    _set(ctx, "a3", abstract="The environment of the study.")
    got = _ids(_match(ctx, {"text_terms": ["iron"]}))
    assert got == [ctx.p["a2"]["id"]]            # "irony" yes, "environment" no
    assert _ids(_match(ctx, {"text_terms": ["EDITORIAL"]})) == sorted(
        [ctx.p["a1"]["id"], ctx.p["a4"]["id"]])  # title match, any case


def test_text_excludes_keeps_only_records_with_none_of_the_terms(ctx):
    _set(ctx, "a2", abstract="irony everywhere")
    got = set(_ids(_match(ctx, {"text_excludes": ["irony", "editorial"]})))
    assert got.isdisjoint({ctx.p["a1"]["id"], ctx.p["a2"]["id"], ctx.p["a4"]["id"]})
    assert ctx.p["a3"]["id"] in got


def test_text_rules_combine_like_the_others(ctx):
    _set(ctx, "a1", abstract="sentiment analysis of reviews")
    _set(ctx, "a2", abstract="sentiment analysis of irony")
    rules = {"text_terms": ["sentiment"], "text_excludes": ["irony"]}
    all_ = _ids(_match(ctx, {**rules, "mode": "all"}))
    assert ctx.p["a1"]["id"] in all_ and ctx.p["a2"]["id"] not in all_   # a2 mentions irony
    assert ctx.p["a2"]["id"] in _ids(_match(ctx, {**rules, "mode": "any"}))


def test_year_rule_ignores_records_without_a_year(ctx):
    paper = ctx.i.session.get(Paper, ctx.p["a3"]["id"])
    paper.year = None
    ctx.i.session.add(paper)
    ctx.i.session.commit()
    r = _match(ctx, {"year_min": 2000})
    assert ctx.p["a3"]["id"] not in _ids(r)


def test_language_rule_uses_codes_and_blank(ctx):
    fr = ctx.i.session.get(Paper, ctx.p["a2"]["id"])
    fr.language = "fr"
    ctx.i.session.add(fr)
    ctx.i.session.commit()
    assert _ids(_match(ctx, {"language_codes": ["FR"]})) == [ctx.p["a2"]["id"]]
    blank = _ids(_match(ctx, {"language_blank": True}))
    assert ctx.p["a2"]["id"] not in blank and ctx.p["a1"]["id"] in blank


def test_sources_narrow_the_candidates(ctx):
    r = _match(ctx, {"title_terms": ["a"], "sources": ["snowballing:1"]})
    assert r.json()["candidates"] == 0


def test_match_is_paged(ctx):
    r = ctx.i.client.post(f"/api/projects/{ctx.pid}/bulk/match", json={
        "reviewer_id": ctx.r1["id"], "rules": {"title_terms": ["a"]}, "limit": 2})
    body = r.json()
    assert len(body["papers"]) == 2 and body["matched"] > 2


# ── Bulk decisions ────────────────────────────────────────────────────────────

def test_bulk_exclude_records_decisions_and_finals(ctx):
    ids = [ctx.p["a1"]["id"], ctx.p["a4"]["id"]]
    r = _bulk(ctx, [{"paper_id": i, "decision": "E", "criterion_label": "Off-topic",
                     "rationale": "editorial"} for i in ids])
    assert r.status_code == 200 and r.json() == {"applied": 2}
    for i in ids:
        body = ctx.i.client.get(f"/api/projects/{ctx.pid}/papers/{i}/decisions").json()
        assert body["final_decision"]["decision"] == "E"
        assert body["decisions"][0]["criterion_label"] == "Off-topic"
        assert body["decisions"][0]["rationale"] == "editorial"
    # Decided now, so no longer offered.
    assert not set(ids) & set(_ids(_match(ctx, {"title_terms": ["editorial", "comment"]})))


def test_bulk_is_all_or_nothing(ctx):
    a1, a4 = ctx.p["a1"]["id"], ctx.p["a4"]["id"]
    ctx.i.decide(ctx.pid, a4, reviewer_id=ctx.r1["id"], decision="I")
    r = _bulk(ctx, [{"paper_id": a1, "decision": "E"}, {"paper_id": a4, "decision": "E"}])
    assert r.status_code == 409
    assert any("already decided" in e for e in r.json()["detail"]["errors"])
    # a1 untouched, a4 keeps its Include
    assert ctx.i.client.get(f"/api/projects/{ctx.pid}/papers/{a1}/decisions").json()["decisions"] == []
    body = ctx.i.client.get(f"/api/projects/{ctx.pid}/papers/{a4}/decisions").json()
    assert body["final_decision"]["decision"] == "I"


def test_bulk_refuses_flagged_duplicates(ctx):
    _flag_duplicate(ctx, "a1")
    r = _bulk(ctx, [{"paper_id": ctx.p["a1"]["id"], "decision": "I"}])
    assert r.status_code == 409


def test_bulk_rejects_bad_decision_and_repeats(ctx):
    a1 = ctx.p["a1"]["id"]
    assert _bulk(ctx, [{"paper_id": a1, "decision": "X"}]).status_code == 409
    assert _bulk(ctx, [{"paper_id": a1, "decision": "E"}, {"paper_id": a1, "decision": "E"}]
                 ).status_code == 409
    assert _bulk(ctx, []).status_code == 422


def test_bulk_at_full_text_only_takes_screening_included(ctx):
    a1, a2 = ctx.p["a1"]["id"], ctx.p["a2"]["id"]
    ctx.i.decide(ctx.pid, a2, reviewer_id=ctx.r1["id"], decision="I")        # screened in
    ctx.i.decide(ctx.pid, a1, reviewer_id=ctx.r1["id"], decision="E")        # screened out
    assert _bulk(ctx, [{"paper_id": a1, "decision": "I"}], phase="full-text").status_code == 409
    ok = _bulk(ctx, [{"paper_id": a2, "decision": "I"}], phase="full-text")
    assert ok.status_code == 200
    # and matching at full-text is limited to the same set
    r = _match(ctx, {"title_terms": ["study", "editorial"]}, phase="full-text")
    assert a1 not in _ids(r)


def test_unknown_phase_and_reviewer(ctx):
    assert _bulk(ctx, [{"paper_id": 1, "decision": "I"}], phase="nope").status_code == 422
    assert _bulk(ctx, [{"paper_id": 1, "decision": "I"}], reviewer={"id": 999}).status_code == 404


def test_second_reviewer_disagreeing_opens_a_conflict(ctx):
    r2 = ctx.i.add_reviewer(ctx.pid, name="Bob", role="R2")
    a1 = ctx.p["a1"]["id"]
    assert _bulk(ctx, [{"paper_id": a1, "decision": "E"}]).status_code == 200
    assert _bulk(ctx, [{"paper_id": a1, "decision": "I"}], reviewer=r2).status_code == 200
    assert [c["paper_id"] for c in ctx.i.conflicts(ctx.pid, phase="screening")] == [a1]


# ── Duplicate candidates ──────────────────────────────────────────────────────

def _groups(ctx, **params):
    r = ctx.i.client.get(f"/api/projects/{ctx.pid}/duplicate-candidates",
                         params={"reviewer_id": ctx.r1["id"], **params})
    assert r.status_code == 200
    return r.json()["groups"]


def _resolve(ctx, groups, *, criterion="Duplicate", phase="screening"):
    return ctx.i.client.post(f"/api/projects/{ctx.pid}/duplicates/resolve", json={
        "reviewer_id": ctx.r1["id"], "phase": phase, "criterion_label": criterion,
        "groups": groups})


def test_finds_a_venue_prefixed_title_that_import_dedup_missed(ctx):
    groups = _groups(ctx)
    assert len(groups) == 1
    g = groups[0]
    assert sorted(m["id"] for m in g["papers"]) == sorted([ctx.p["a5"]["id"], ctx.p["a6"]["id"]])
    assert "Similar title" in g["reasons"]
    assert g["all_excluded_as_duplicate"] is False


def test_flagged_duplicates_are_not_grouped(ctx):
    _flag_duplicate(ctx, "a6")
    assert _groups(ctx) == []


def test_same_doi_and_same_title_are_grouped(ctx):
    ctx.i.import_bib(ctx.pid, [
        {"citekey": "b1", "title": "Indirect speech acts", "year": 2001, "doi": "10.1/b", "venue": "X"},
        {"citekey": "b2", "title": "Indirect Speech Acts", "year": 1979, "venue": "Y"},
    ], db_name="springerlink")
    reasons = [g["reasons"] for g in _groups(ctx)]
    assert ["Same title"] in reasons


def test_resolve_excludes_copies_and_keeps_the_keeper(ctx):
    keep, drop = ctx.p["a5"]["id"], ctx.p["a6"]["id"]
    r = _resolve(ctx, [{"keep": [keep], "exclude": [drop]}])
    assert r.status_code == 200
    assert r.json()["applied"] == 1
    assert r.json()["changes"] == [{"paper_id": drop, "previous": None, "previous_criterion": None}]
    body = ctx.i.client.get(f"/api/projects/{ctx.pid}/papers/{drop}/decisions").json()
    assert body["final_decision"]["decision"] == "E"
    assert body["decisions"][0]["criterion_label"] == "Duplicate"
    assert body["decisions"][0]["rationale"] == f"Duplicate of paper {keep}"
    # the keeper is untouched
    assert ctx.i.client.get(f"/api/projects/{ctx.pid}/papers/{keep}/decisions").json()["decisions"] == []


def test_resolve_reports_what_it_replaced(ctx):
    keep, drop = ctx.p["a5"]["id"], ctx.p["a6"]["id"]
    ctx.i.decide(ctx.pid, drop, reviewer_id=ctx.r1["id"], decision="I", criterion_label="Terminology")
    r = _resolve(ctx, [{"keep": [keep], "exclude": [drop]}])
    assert r.json()["changes"] == [
        {"paper_id": drop, "previous": "I", "previous_criterion": "Terminology"}]


def test_resolve_refuses_to_exclude_every_copy(ctx):
    a5, a6 = ctx.p["a5"]["id"], ctx.p["a6"]["id"]
    for body in (
        [{"keep": [], "exclude": [a5, a6]}],                     # nobody stays
        [{"keep": [a5], "exclude": [a5, a6]}],                   # keeper also excluded
        [{"keep": [a5], "exclude": [a6]}, {"keep": [a6], "exclude": [a5]}],   # swap: both go
    ):
        r = _resolve(ctx, body)
        assert r.status_code == 422, body
    # nothing was written by any of the refusals
    assert ctx.i.client.get(f"/api/projects/{ctx.pid}/papers/{a5}/decisions").json()["decisions"] == []
    assert ctx.i.client.get(f"/api/projects/{ctx.pid}/papers/{a6}/decisions").json()["decisions"] == []


def test_resolve_refuses_a_keeper_already_excluded_as_duplicate(ctx):
    a5, a6 = ctx.p["a5"]["id"], ctx.p["a6"]["id"]
    ctx.i.decide(ctx.pid, a5, reviewer_id=ctx.r1["id"], decision="E", criterion_label="Duplicate")
    r = _resolve(ctx, [{"keep": [a5], "exclude": [a6]}])
    assert r.status_code == 422
    assert any("already excluded" in e for e in r.json()["detail"]["errors"])


def test_resolve_validates_criterion_scope_and_flagged_records(ctx):
    a5, a6 = ctx.p["a5"]["id"], ctx.p["a6"]["id"]
    assert _resolve(ctx, [{"keep": [a5], "exclude": [a6]}], criterion="Nope").status_code == 422
    # full-text: neither record was screening-included
    assert _resolve(ctx, [{"keep": [a5], "exclude": [a6]}], phase="full-text").status_code == 422
    _flag_duplicate(ctx, "a6")
    assert _resolve(ctx, [{"keep": [a5], "exclude": [a6]}]).status_code == 422


def test_a_group_with_every_copy_excluded_as_duplicate_is_surfaced(ctx):
    a5, a6 = ctx.p["a5"]["id"], ctx.p["a6"]["id"]
    for i in (a5, a6):
        ctx.i.decide(ctx.pid, i, reviewer_id=ctx.r1["id"], decision="E", criterion_label="Duplicate")
    groups = _groups(ctx)                       # status=open is the default
    assert len(groups) == 1 and groups[0]["all_excluded_as_duplicate"] is True


def test_settled_groups_drop_out_of_the_open_list_but_not_all(ctx):
    a5, a6 = ctx.p["a5"]["id"], ctx.p["a6"]["id"]
    ctx.i.decide(ctx.pid, a5, reviewer_id=ctx.r1["id"], decision="I", criterion_label="T")
    ctx.i.decide(ctx.pid, a6, reviewer_id=ctx.r1["id"], decision="E", criterion_label="Duplicate")
    assert _groups(ctx) == []
    assert len(_groups(ctx, status="all")) == 1


def test_existing_single_decision_endpoint_is_unchanged(ctx):
    a1 = ctx.p["a1"]["id"]
    ctx.i.decide(ctx.pid, a1, reviewer_id=ctx.r1["id"], decision="I", criterion_label="T")
    ctx.i.decide(ctx.pid, a1, reviewer_id=ctx.r1["id"], decision="E", criterion_label="Off-topic")
    body = ctx.i.client.get(f"/api/projects/{ctx.pid}/papers/{a1}/decisions").json()
    assert len(body["decisions"]) == 1
    assert body["decisions"][0]["decision"] == "E"
    assert body["final_decision"]["decision"] == "E"


def test_a_work_lost_to_a_flagged_copy_is_surfaced(ctx):
    """An original excluded as 'Duplicate' whose twin was flagged by import is
    out of the review altogether; nothing else would show it."""
    ctx.i.import_bib(ctx.pid, [
        {"citekey": "c1", "title": "Beyond exchange appraisal systems in english", "year": 2000,
         "doi": "10.1/c1", "venue": "V"},
    ], db_name="acm")
    ctx.i.import_bib(ctx.pid, [
        {"citekey": "c2", "title": "Beyond exchange appraisal systems in english", "year": 2000,
         "doi": "10.1/c2", "venue": "V"},          # same title + venue: flagged by import
    ], db_name="ieee")
    by_key = {p["citekey"]: p for p in ctx.i.papers(ctx.pid)}
    c1, c2 = by_key["c1"], by_key["c2"]
    assert c1["dedup_status"] == "original" and c2["dedup_status"] != "original"
    # Not a candidate while c1 is undecided: one original, one flagged copy.
    assert all(c1["id"] not in [m["id"] for m in g["papers"]] for g in _groups(ctx))

    for p in (c1, c2):
        ctx.i.decide(ctx.pid, p["id"], reviewer_id=ctx.r1["id"], decision="E",
                     criterion_label="Duplicate")
    groups = [g for g in _groups(ctx) if c1["id"] in [m["id"] for m in g["papers"]]]
    assert len(groups) == 1 and groups[0]["all_excluded_as_duplicate"] is True
    flagged = [m for m in groups[0]["papers"] if m["dedup_status"] != "original"]
    assert [m["id"] for m in flagged] == [c2["id"]]

    # The flagged copy cannot be named in a resolution.
    r = _resolve(ctx, [{"keep": [c2["id"]], "exclude": [c1["id"]]}])
    assert r.status_code == 422
