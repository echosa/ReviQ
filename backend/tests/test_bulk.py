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


def _match(ctx, rules, *, phase="screening", reviewer=None, statuses=None):
    body = {"reviewer_id": (reviewer or ctx.r1)["id"], "phase": phase, "rules": rules}
    if statuses is not None:
        body["statuses"] = statuses
    return ctx.i.client.post(f"/api/projects/{ctx.pid}/bulk/match", json=body)


def _bulk(ctx, items, *, phase="screening", reviewer=None, replace=None):
    body = {"reviewer_id": (reviewer or ctx.r1)["id"], "phase": phase, "decisions": items}
    if replace is not None:
        body["replace"] = replace
    return ctx.i.client.post(f"/api/projects/{ctx.pid}/decisions/bulk", json=body)


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
    assert r.status_code == 200 and r.json() == {"applied": 2, "replaced": 0, "changes": []}
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


def test_duplicate_candidates_report_totals_over_every_page(ctx):
    ctx.i.import_bib(ctx.pid, [
        {"citekey": "b1", "title": "Indirect speech acts", "year": 2001, "doi": "10.1/b", "venue": "X"},
        {"citekey": "b2", "title": "Indirect Speech Acts", "year": 1979, "venue": "Y"},
        {"citekey": "b3", "title": "Indirect speech acts!", "year": 1980, "venue": "Z"},
    ], db_name="springerlink")
    by_key = {p["citekey"]: p for p in ctx.i.papers(ctx.pid)}
    for k in ("a5", "a6"):
        ctx.i.decide(ctx.pid, ctx.p[k]["id"], reviewer_id=ctx.r1["id"], decision="E",
                     criterion_label="Duplicate")
    r = ctx.i.client.get(f"/api/projects/{ctx.pid}/duplicate-candidates",
                         params={"reviewer_id": ctx.r1["id"], "limit": 1})
    body = r.json()
    assert body["total"] == 2 and len(body["groups"]) == 1      # one page of two groups
    assert body["copies"] == 5                                   # 3 speech-act copies + the pair
    assert body["lost"] == 1                                     # the pair, both excluded as Duplicate


def test_hide_lost_leaves_out_groups_with_every_copy_excluded_as_duplicate(ctx):
    ctx.i.import_bib(ctx.pid, [
        {"citekey": "b1", "title": "Indirect speech acts", "year": 2001, "doi": "10.1/b", "venue": "X"},
        {"citekey": "b2", "title": "Indirect Speech Acts", "year": 1979, "venue": "Y"},
    ], db_name="springerlink")
    for k in ("a5", "a6"):
        ctx.i.decide(ctx.pid, ctx.p[k]["id"], reviewer_id=ctx.r1["id"], decision="E",
                     criterion_label="Duplicate")
    get = lambda **kw: ctx.i.client.get(f"/api/projects/{ctx.pid}/duplicate-candidates",
                                        params={"reviewer_id": ctx.r1["id"], **kw}).json()
    shown = get()
    assert shown["total"] == 2 and shown["lost"] == 1 and shown["hidden"] == 0
    hidden = get(hide_lost=True)
    assert hidden["total"] == 1 and hidden["lost"] == 0 and hidden["hidden"] == 1
    assert hidden["copies"] == 2
    assert not any(g["all_excluded_as_duplicate"] for g in hidden["groups"])
    # also under status=all
    assert get(status="all", hide_lost=True)["hidden"] == 1


# ── Searching by decision status, and replacing decisions ─────────────────────

def _decide_all(ctx):
    """a1 included, a2 excluded, a3 uncertain; a4.. left undecided."""
    r1 = ctx.r1["id"]
    ctx.i.decide(ctx.pid, ctx.p["a1"]["id"], reviewer_id=r1, decision="I", criterion_label="Terminology")
    ctx.i.decide(ctx.pid, ctx.p["a2"]["id"], reviewer_id=r1, decision="E", criterion_label="Off-topic")
    ctx.i.decide(ctx.pid, ctx.p["a3"]["id"], reviewer_id=r1, decision="U")


def test_match_searches_only_undecided_unless_asked(ctx):
    _decide_all(ctx)
    r = _match(ctx, {"title_terms": ["a"]})
    ids = set(_ids(r))
    assert not ids & {ctx.p[k]["id"] for k in ("a1", "a2", "a3")}


def test_match_can_search_included_excluded_or_uncertain(ctx):
    _decide_all(ctx)
    rules = {"title_terms": ["e", "a", "o", "i"]}      # matches every title
    for status, key in (("I", "a1"), ("E", "a2"), ("U", "a3")):
        r = _match(ctx, rules, statuses=[status])
        assert _ids(r) == [ctx.p[key]["id"]], status
    both = _match(ctx, rules, statuses=["I", "E"])
    assert _ids(both) == sorted([ctx.p["a1"]["id"], ctx.p["a2"]["id"]])
    everything = _match(ctx, rules, statuses=["undecided", "I", "E", "U"])
    assert everything.json()["candidates"] == len(ctx.p)


def test_match_reports_each_records_current_decision(ctx):
    _decide_all(ctx)
    r = _match(ctx, {"title_terms": ["a"]}, statuses=["undecided", "I", "E"])
    by_id = {p["id"]: p for p in r.json()["papers"]}
    assert (by_id[ctx.p["a1"]["id"]]["decision"], by_id[ctx.p["a1"]["id"]]["criterion_label"]) == ("I", "Terminology")
    assert by_id[ctx.p["a2"]["id"]]["decision"] == "E"
    assert by_id[ctx.p["a4"]["id"]]["decision"] is None


def test_match_rejects_an_unknown_or_empty_status(ctx):
    assert _match(ctx, {"title_terms": ["a"]}, statuses=["maybe"]).status_code == 422
    assert _match(ctx, {"title_terms": ["a"]}, statuses=[]).status_code == 422


def test_status_follows_this_reviewers_own_decision(ctx):
    r2 = ctx.i.add_reviewer(ctx.pid, name="Bob", role="R2")
    ctx.i.decide(ctx.pid, ctx.p["a1"]["id"], reviewer_id=ctx.r1["id"], decision="I")
    # Bob has not decided a1, so for him it is undecided.
    assert ctx.p["a1"]["id"] in _ids(_match(ctx, {"title_terms": ["editorial"]}, reviewer=r2))
    assert ctx.p["a1"]["id"] not in _ids(_match(ctx, {"title_terms": ["editorial"]}, reviewer=r2, statuses=["I"]))


def test_bulk_refuses_a_decided_record_unless_replace_is_set(ctx):
    _decide_all(ctx)
    a1 = ctx.p["a1"]["id"]
    r = _bulk(ctx, [{"paper_id": a1, "decision": "E", "criterion_label": "Off-topic"}])
    assert r.status_code == 409
    r = _bulk(ctx, [{"paper_id": a1, "decision": "E", "criterion_label": "Off-topic"}], replace=False)
    assert r.status_code == 409


def test_bulk_replace_changes_the_decision_and_says_what_it_was(ctx):
    _decide_all(ctx)
    a1, a2, a4 = (ctx.p[k]["id"] for k in ("a1", "a2", "a4"))
    r = _bulk(ctx, [
        {"paper_id": a1, "decision": "E", "criterion_label": "Off-topic", "rationale": "second look"},
        {"paper_id": a2, "decision": "I", "criterion_label": "Terminology"},
        {"paper_id": a4, "decision": "E", "criterion_label": "Off-topic"},          # not decided before
    ], replace=True)
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] == 3 and body["replaced"] == 2
    assert sorted(body["changes"], key=lambda c: c["paper_id"]) == sorted([
        {"paper_id": a1, "previous": "I", "previous_criterion": "Terminology"},
        {"paper_id": a2, "previous": "E", "previous_criterion": "Off-topic"},
    ], key=lambda c: c["paper_id"])
    for pid, want in ((a1, "E"), (a2, "I"), (a4, "E")):
        got = ctx.i.client.get(f"/api/projects/{ctx.pid}/papers/{pid}/decisions").json()
        assert got["final_decision"]["decision"] == want
        assert len(got["decisions"]) == 1                 # replaced, not added to


def test_bulk_replace_still_refuses_what_is_out_of_scope(ctx):
    _decide_all(ctx)
    _flag_duplicate(ctx, "a1")
    r = _bulk(ctx, [{"paper_id": ctx.p["a1"]["id"], "decision": "E"}], replace=True)
    assert r.status_code == 409                           # flagged duplicate
    # at full-text, only records screening included
    r = _bulk(ctx, [{"paper_id": ctx.p["a2"]["id"], "decision": "I"}], phase="full-text", replace=True)
    assert r.status_code == 409


def test_bulk_replace_is_all_or_nothing(ctx):
    _decide_all(ctx)
    a1 = ctx.p["a1"]["id"]
    r = _bulk(ctx, [{"paper_id": a1, "decision": "E"}, {"paper_id": 99999, "decision": "E"}], replace=True)
    assert r.status_code == 409
    got = ctx.i.client.get(f"/api/projects/{ctx.pid}/papers/{a1}/decisions").json()
    assert got["final_decision"]["decision"] == "I"       # untouched


def test_replacing_a_decision_goes_through_the_conflict_state_machine(ctx):
    r2 = ctx.i.add_reviewer(ctx.pid, name="Bob", role="R2")
    a1 = ctx.p["a1"]["id"]
    ctx.i.decide(ctx.pid, a1, reviewer_id=ctx.r1["id"], decision="I")
    ctx.i.decide(ctx.pid, a1, reviewer_id=r2["id"], decision="I")        # agreement
    assert ctx.i.conflicts(ctx.pid, phase="screening") == []
    assert _bulk(ctx, [{"paper_id": a1, "decision": "E"}], replace=True).status_code == 200
    assert [c["paper_id"] for c in ctx.i.conflicts(ctx.pid, phase="screening")] == [a1]


def test_hide_settled_leaves_out_groups_where_all_but_one_copy_is_excluded_as_duplicate(ctx):
    # Three copies of one work (same title, different venues so import keeps them all).
    ctx.i.import_bib(ctx.pid, [
        {"citekey": "s1", "title": "Understanding indirect speech acts", "year": 2001, "venue": "V1"},
        {"citekey": "s2", "title": "Understanding indirect speech acts", "year": 2001, "venue": "V2"},
        {"citekey": "s3", "title": "Understanding indirect speech acts", "year": 2001, "venue": "V3"},
    ], db_name="springerlink")
    by_key = {p["citekey"]: p for p in ctx.i.papers(ctx.pid)}
    s1, s2, s3 = (by_key[k]["id"] for k in ("s1", "s2", "s3"))
    get = lambda **kw: ctx.i.client.get(f"/api/projects/{ctx.pid}/duplicate-candidates",
                                        params={"reviewer_id": ctx.r1["id"], **kw}).json()

    def group_of(body):
        return next(g for g in body["groups"] if s1 in [m["id"] for m in g["papers"]])

    # one copy excluded as a duplicate: not yet settled
    ctx.i.decide(ctx.pid, s2, reviewer_id=ctx.r1["id"], decision="E", criterion_label="Duplicate")
    assert group_of(get())["settled"] is False
    # two of three excluded as duplicates, the survivor undecided: settled
    ctx.i.decide(ctx.pid, s3, reviewer_id=ctx.r1["id"], decision="E", criterion_label="Duplicate")
    shown = get()
    assert group_of(shown)["settled"] is True
    assert group_of(shown)["all_excluded_as_duplicate"] is False
    base = shown["total"]

    hidden = get(hide_settled=True)
    assert hidden["total"] == base - 1 and hidden["hidden_settled"] == 1
    assert all(s1 not in [m["id"] for m in g["papers"]] for g in hidden["groups"])
    assert hidden["copies"] == shown["copies"] - 3                    # the whole group is gone from the totals

    # the survivor can be in any state: included, or excluded for another reason, it is still settled
    ctx.i.decide(ctx.pid, s1, reviewer_id=ctx.r1["id"], decision="I", criterion_label="Terminology")
    assert get(status="all", hide_settled=True)["hidden_settled"] == 1
    ctx.i.decide(ctx.pid, s1, reviewer_id=ctx.r1["id"], decision="E", criterion_label="Off-topic")
    assert get(status="all", hide_settled=True)["hidden_settled"] == 1

    # all three excluded as duplicates is the other case ("lost"), not settled
    ctx.i.decide(ctx.pid, s1, reviewer_id=ctx.r1["id"], decision="E", criterion_label="Duplicate")
    lost = get(status="all")
    assert group_of(lost)["all_excluded_as_duplicate"] is True and group_of(lost)["settled"] is False
    assert get(status="all", hide_settled=True)["hidden_settled"] == 0
    assert get(status="all", hide_lost=True, hide_settled=True)["hidden"] >= 1      # the two filters are independent


def test_a_pair_with_one_copy_excluded_as_duplicate_is_settled(ctx):
    a5, a6 = ctx.p["a5"]["id"], ctx.p["a6"]["id"]
    ctx.i.decide(ctx.pid, a6, reviewer_id=ctx.r1["id"], decision="E", criterion_label="Duplicate")
    body = ctx.i.client.get(f"/api/projects/{ctx.pid}/duplicate-candidates",
                            params={"reviewer_id": ctx.r1["id"], "hide_settled": True}).json()
    assert body["hidden_settled"] == 1
    assert a5 not in [m["id"] for g in body["groups"] for m in g["papers"]]


# ── Non-English language option ───────────────────────────────────────────────

def test_non_english_matches_any_known_language_that_is_not_english(ctx):
    langs = {"a1": "fr", "a2": "en", "a3": "zh", "a4": "EN", "a5": None, "a6": ""}
    for key, lang in langs.items():
        _set(ctx, key, language=lang)
    got = set(_ids(_match(ctx, {"language_non_english": True})))
    assert got == {ctx.p["a1"]["id"], ctx.p["a3"]["id"]}            # fr and zh; not en/EN, not unknown


def test_non_english_does_not_need_the_codes_listed(ctx):
    for key, lang in {"a1": "de", "a2": "ar", "a3": "no", "a4": "la"}.items():
        _set(ctx, key, language=lang)
    got = set(_ids(_match(ctx, {"language_non_english": True})))
    assert {ctx.p[k]["id"] for k in ("a1", "a2", "a3", "a4")} <= got


def test_non_english_counts_as_a_rule_on_its_own_and_combines_with_the_others(ctx):
    assert _match(ctx, {"language_non_english": True}).status_code == 200      # not "enable a rule"
    _set(ctx, "a1", language="fr", year=1990)
    _set(ctx, "a2", language="fr", year=2020)
    # "year is outside ... 2000" means after 2000, so only a2 (2020) satisfies both rules.
    both = _ids(_match(ctx, {"language_non_english": True, "year_max": 2000, "mode": "all"}))
    assert both == [ctx.p["a2"]["id"]]
    either = set(_ids(_match(ctx, {"language_non_english": True, "year_max": 2000, "mode": "any"})))
    assert {ctx.p["a1"]["id"], ctx.p["a2"]["id"]} <= either                  # a1 by language alone
