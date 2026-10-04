"""Snowballing yield: what each iteration brought, and how much it re-found.

The numbers are what a decision to stop snowballing rests on, so each one is
pinned against a small project built by hand.
"""
import pytest

from tests.test_import_counts import bib


def _snowball(inst, pid, entries, kind="backward"):
    created = inst.client.post(f"/api/projects/{pid}/snowballing", json={"iteration_type": kind})
    created.raise_for_status()
    inst.client.post(f"/api/projects/{pid}/snowballing/{created.json()['id']}/import",
                     files={"file": ("f.bib", bib(entries), "application/x-bibtex")}).raise_for_status()


def _yield(inst, pid, **params):
    r = inst.client.get(f"/api/projects/{pid}/snowballing/yield", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _paper(inst, pid, citekey):
    return inst.paper_by_citekey(pid, citekey)["id"]


@pytest.fixture
def world(instance):
    inst = instance
    pid = inst.create_project(title="Y")["id"]
    r1 = inst.reviewers(pid)[0]["id"]
    inst.import_bib(pid, [
        {"citekey": "s1", "title": "Sarcasm detection seed paper", "doi": "10.1/s1"},
        {"citekey": "s2", "title": "Irony in context seed paper", "doi": "10.1/s2"},
        {"citekey": "s3", "title": "An unrelated seed paper", "doi": "10.1/s3"},
    ], db_name="acm")
    # iteration 1 (backward): two new records, plus the seed s1 again and a copy of its own a1
    _snowball(inst, pid, [
        {"citekey": "a1", "title": "Sarcasm detection with transformers", "doi": "10.1/a1", "venue": "V"},
        {"citekey": "a2", "title": "Cooking with spices at home", "doi": "10.1/a2", "venue": "V"},
        {"citekey": "a1b", "title": "Sarcasm detection with transformers", "doi": "10.1/a1", "venue": "W"},
        {"citekey": "s1again", "title": "Sarcasm detection seed paper", "doi": "10.1/s1", "venue": "X"},
    ])
    # iteration 2 (forward): one new record, and re-finds of s1 (included), s3 (excluded) and a1 (included)
    _snowball(inst, pid, [
        {"citekey": "b1", "title": "Another irony study of tweets", "doi": "10.1/b1", "venue": "V"},
        {"citekey": "s1x", "title": "Sarcasm detection seed paper", "doi": "10.1/s1", "venue": "Y"},
        {"citekey": "s3x", "title": "An unrelated seed paper", "doi": "10.1/s3", "venue": "Y"},
        {"citekey": "a1x", "title": "Sarcasm detection with transformers", "doi": "10.1/a1", "venue": "Y"},
    ], kind="forward")
    decide = lambda key, phase, d: inst.decide(pid, _paper(inst, pid, key), reviewer_id=r1, phase=phase, decision=d)
    for key, d in (("s1", "I"), ("s2", "I"), ("s3", "E"), ("a1", "I"), ("a2", "E"), ("b1", "I")):
        decide(key, "screening", d)
    for key, d in (("s1", "I"), ("s2", "I"), ("a1", "I")):
        decide(key, "full-text", d)
    return inst, pid


def _by_n(rows):
    return {r["iteration_number"]: r for r in rows}


def test_an_iterations_counts_add_up(world):
    inst, pid = world
    it1 = _by_n(_yield(inst, pid))[1]
    assert it1["iteration_type"] == "backward"
    assert (it1["retrieved"], it1["originals"], it1["duplicates"]) == (4, 2, 2)
    assert it1["duplicate_pct"] == 50.0
    assert it1["screened"] == 2 and it1["screening_included"] == 1
    assert it1["fulltext_decided"] == 1 and it1["fulltext_included"] == 1
    assert it1["yield_pct"] == 50.0                                   # 1 full-text include per 2 original records


def test_refound_counts_only_papers_the_project_already_had_from_elsewhere(world):
    inst, pid = world
    rows = _by_n(_yield(inst, pid))
    # Iteration 1 re-found seed s1 (included at screening). Its own second copy of a1
    # is a duplicate of itself, not something already known from before.
    assert rows[1]["refound_known"] == 1 and rows[1]["refound_included"] == 1
    assert rows[1]["refound_included_pct"] == 100.0
    # Iteration 2 re-found s1 (included), s3 (excluded) and a1 (included): 3 known, 2 included.
    assert rows[2]["refound_known"] == 3 and rows[2]["refound_included"] == 2
    assert rows[2]["refound_included_pct"] == 66.7


def test_yield_is_none_until_something_has_been_decided_at_full_text(world):
    inst, pid = world
    it2 = _by_n(_yield(inst, pid))[2]
    assert it2["screening_included"] == 1 and it2["fulltext_decided"] == 0
    assert it2["yield_pct"] is None                                   # b1 is not through full-text yet


def test_core_share_follows_the_terms_given_and_matches_word_starts(world):
    inst, pid = world
    assert _by_n(_yield(inst, pid))[1]["core_in_title"] is None       # nothing asked, nothing said
    row = _by_n(_yield(inst, pid, core_terms="sarcas, iron"))[1]
    assert row["core_in_title"] == 1 and row["core_in_title_pct"] == 100.0
    other = _by_n(_yield(inst, pid, core_terms="spices"))[1]
    assert other["core_in_title"] == 0 and other["core_in_title_pct"] == 0.0
    # "iron" must not match inside "environment"
    inst.import_bib(pid, [{"citekey": "env", "title": "The environment of tests", "doi": "10.1/env"}], db_name="ieee")
    assert _by_n(_yield(inst, pid, core_terms="iron"))[1]["core_in_title"] == 0


def test_a_project_with_no_iterations_has_nothing_to_report(instance):
    pid = instance.create_project(title="Empty")["id"]
    assert _yield(instance, pid) == []


def test_an_iteration_with_nothing_in_it_does_not_divide_by_zero(instance):
    pid = instance.create_project(title="E2")["id"]
    instance.client.post(f"/api/projects/{pid}/snowballing", json={"iteration_type": "forward"}).raise_for_status()
    row = _yield(instance, pid)[0]
    assert row["retrieved"] == 0 and row["duplicate_pct"] is None and row["yield_pct"] is None
    assert row["refound_included_pct"] is None


def test_unknown_project_is_a_404(instance):
    assert instance.client.get("/api/projects/9999/snowballing/yield").status_code == 404
