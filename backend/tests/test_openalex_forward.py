"""The forward-snowballing tool (tools/openalex_forward.py).

The point of the tool is a file ReviQ's own importer reads correctly, so the
tests feed its output to that importer's parser. The OpenAlex and ReviQ calls
are faked: nothing here touches the network.
"""
import pytest

from app.services.bibtex_service import entry_to_paper_dict, parse_bib_content
from tools import openalex_forward as fw


def work(wid, title, year, *, doi=None, authors=("Ada Lovelace",), conference=False,
         venue="Journal of Tests", abstract=None, lang="en", wtype="article",
         refs=(), retracted=False):
    inverted = None
    if abstract:
        inverted = {}
        for i, word in enumerate(abstract.split()):
            inverted.setdefault(word, []).append(i)
    return {
        "id": f"https://openalex.org/{wid}", "doi": f"https://doi.org/{doi}" if doi else None,
        "title": title, "publication_year": year, "type": wtype, "language": lang,
        "is_retracted": retracted,
        "authorships": [{"author": {"display_name": a}} for a in authors],
        "primary_location": {"source": {"display_name": venue,
                                        "type": "conference" if conference else "journal"}},
        "abstract_inverted_index": inverted,
        "referenced_works": [f"https://openalex.org/{r}" for r in refs],
    }


# ── helpers ───────────────────────────────────────────────────────────────────

def test_doi_and_id_normalisation():
    assert fw.norm_doi("https://doi.org/10.1000/ABC") == "10.1000/abc"
    assert fw.norm_doi("doi:10.1000/abc") == "10.1000/abc"
    assert fw.norm_doi(None) == ""
    assert fw.short_id("https://openalex.org/W123") == "W123"


def test_abstract_is_rebuilt_in_word_order():
    inv = {"Sarcasm": [0], "needs": [1], "context": [2, 5], "and": [3], "more": [4]}
    assert fw.rebuild_abstract(inv) == "Sarcasm needs context and more context"
    assert fw.rebuild_abstract(None) == ""


def test_title_matching_tolerates_small_differences_only():
    assert fw.title_matches("Irony Detection Using Transformers", "Irony detection using transformers.")
    assert not fw.title_matches("Irony Detection Using Transformers", "A Survey of Sentiment Analysis")


def test_venue_prefix_is_stripped_once_and_only_when_it_looks_like_a_venue():
    assert fw.strip_venue_prefix("COLING - Tweet Sarcasm Detection Using Deep Learning") == \
        "Tweet Sarcasm Detection Using Deep Learning"
    assert fw.strip_venue_prefix("PACLIC - \u266fIrony or \u266fSarcasm - A Quantitative Analysis") == \
        "\u266fIrony or \u266fSarcasm - A Quantitative Analysis"        # the inner dash stays
    assert fw.strip_venue_prefix("Sarcasm - A Survey of Methods") == "Sarcasm - A Survey of Methods"   # not a venue
    assert fw.strip_venue_prefix("No prefix at all") == "No prefix at all"


def test_resolve_finds_a_title_that_carries_a_venue_prefix():
    def oa(url, params):
        return {"results": [{"id": "https://openalex.org/W300", "title": "Tweet Sarcasm Detection Using Deep Learning",
                             "publication_year": 2016}]}
    start = [{"id": 1, "title": "COLING - Tweet Sarcasm Detection Using Deep Learning", "doi": None, "year": 2016}]
    found, missing = fw.resolve(FakeClient({}, oa), start)
    assert found == {1: "W300"} and missing == []


# ── the file ReviQ imports ────────────────────────────────────────────────────

def parse(bib):
    return [entry_to_paper_dict(e, source="snowballing:4") for e in parse_bib_content(bib)]


def test_journal_article_round_trips_through_reviqs_importer():
    w = work("W11", "Sarcasm {and} Context: A Study", 2021, doi="10.1000/x1",
             authors=("Ada Lovelace", "Alan M. Turing"), venue="Cognitive Science",
             abstract="We study sarcasm in context.")
    [p] = parse(fw.bibtex_entry(w, cites=["W5", "W2"]))
    assert p["title"] == "Sarcasm and Context: A Study"
    assert p["authors"] == "Ada Lovelace and Alan M. Turing"
    assert p["year"] == 2021 and p["doi"] == "10.1000/x1"
    assert p["venue"] == "Cognitive Science"              # journal -> venue
    assert p["abstract"] == "We study sarcasm in context."
    assert p["citekey"] == "Lovelace2021_W11"
    assert p["source"] == "snowballing:4" and p["dedup_status"] == "original"


def test_conference_paper_keeps_its_venue():
    w = work("W12", "A Conference Paper on Irony", 2019, conference=True, venue="EMNLP")
    [p] = parse(fw.bibtex_entry(w))
    assert p["venue"] == "EMNLP" and p["entry_type"] == "inproceedings"


def test_every_citekey_is_unique_even_for_the_same_author_and_year():
    a = work("W1", "First paper by Lovelace", 2020)
    b = work("W2", "Second paper by Lovelace", 2020)
    keys = [p["citekey"] for p in parse(fw.bibtex_entry(a) + "\n" + fw.bibtex_entry(b))]
    assert len(set(keys)) == 2


def test_awkward_text_cannot_break_the_file():
    w = work("W13", "Braces } and { backslash \\ in # a title & more", 2018,
             abstract="Unbalanced { brace and \\ slash")
    [p] = parse(fw.bibtex_entry(w))
    assert "Braces" in p["title"] and "title & more" in p["title"]
    assert "{" not in p["abstract"] and "\\" not in p["abstract"]


def test_a_work_with_no_authors_or_venue_still_imports():
    w = work("W14", "An anonymous note", None, authors=(), venue="")
    [p] = parse(fw.bibtex_entry(w))
    assert p["citekey"] == "Anon_W14" and p["title"] == "An anonymous note"


# ── filters ───────────────────────────────────────────────────────────────────

def test_filters_are_opt_in_and_each_does_one_thing():
    base = dict(min_year=None, english_only=False, exclude_types=set(), skip_retracted=False,
                known_dois=set(), known_titles=set(), skip_known=False)
    w = work("W20", "A paper", 2010, doi="10.1/a", lang="fr", wtype="paratext", retracted=True)
    assert fw.keep(w, **base)                                          # nothing asked: kept
    assert not fw.keep(w, **{**base, "min_year": 2015})
    assert not fw.keep(w, **{**base, "english_only": True})
    assert not fw.keep(w, **{**base, "exclude_types": {"paratext"}})
    assert not fw.keep(w, **{**base, "skip_retracted": True})
    assert not fw.keep(w, **{**base, "skip_known": True, "known_dois": {"10.1/a"}})
    assert not fw.keep(w, **{**base, "skip_known": True, "known_titles": {"a paper"}})
    assert fw.keep(work("W21", "x", 2010, lang=None), **{**base, "english_only": True})   # language unknown: kept


# ── the whole run, with faked services ────────────────────────────────────────

class FakeClient:
    """Answers the calls the tool makes, and records them."""
    def __init__(self, reviq, openalex):
        self.reviq, self.openalex, self.calls = reviq, openalex, []

    def get(self, url, params=None, retries=5):
        self.calls.append((url, params))
        if "/api/projects/" in url:
            for needle, data in self.reviq.items():
                if needle in url:
                    return data
            raise AssertionError(url)
        return self.openalex(url, params or {})


def reviq_data():
    def paper(i, src, title, doi=None, year=2020, fin="I"):
        return {"id": i, "source": src, "title": title, "doi": doi, "year": year,
                "dedup_status": "original", "final_decision": {"decision": fin}}
    ft = [paper(1, "snowballing:1", "Start paper one", "10.1/one"),
          paper(2, "snowballing:2", "Start paper two without a doi"),
          paper(3, "snowballing:3", "Not in the start set", "10.1/three"),
          paper(4, "snowballing:1", "Excluded at full-text", "10.1/four", fin="E"),
          paper(5, "snowballing:1", "Excluded at screening", "10.1/five")]
    sc = [dict(p, final_decision={"decision": "E" if p["id"] == 5 else "I"}) for p in ft]
    return {"phase=full-text": ft, "phase=screening": sc}


def test_start_set_is_included_at_both_phases_from_the_chosen_sources():
    start, dois, titles = fw.start_set(FakeClient(reviq_data(), None), "http://x", 1,
                                       ["snowballing:1", "snowballing:2"])
    assert sorted(p["id"] for p in start) == [1, 2]                    # not 3 (source), 4 (E), 5 (screened out)
    assert "10.1/one" in dois and "start paper one" in titles


def test_resolve_by_doi_then_by_title_and_reports_what_it_could_not_find():
    def oa(url, params):
        if "filter" in params:
            assert params["filter"] == "doi:10.1/one"
            return {"results": [{"id": "https://openalex.org/W100", "doi": "https://doi.org/10.1/one"}]}
        return {"results": [{"id": "https://openalex.org/W200", "title": "Start paper two without a DOI",
                             "publication_year": 2020}]}
    start = [{"id": 1, "title": "Start paper one", "doi": "10.1/one", "year": 2020},
             {"id": 2, "title": "Start paper two without a doi", "doi": None, "year": 2020},
             {"id": 9, "title": "Nothing like it", "doi": None, "year": 2001}]
    found, missing = fw.resolve(FakeClient({}, oa), start)
    assert found == {1: "W100", 2: "W200"}
    assert [p["id"] for p in missing] == [9]


def test_citing_works_follow_the_cursor_and_dedupe():
    pages = iter([
        {"results": [work("W1", "a", 2020), work("W2", "b", 2021)], "meta": {"next_cursor": "c2"}},
        {"results": [work("W2", "b", 2021), work("W3", "c", 2022)], "meta": {"next_cursor": None}},
    ])
    out = fw.citing_works(FakeClient({}, lambda u, p: next(pages)), ["W100"], log=lambda *_: None)
    assert sorted(out) == ["W1", "W2", "W3"]


def test_main_writes_a_file_reviq_can_read(tmp_path, monkeypatch, capsys):
    def oa(url, params):
        if "cites:" in params.get("filter", ""):
            return {"results": [work("W7", "Citing the first", 2022, abstract="About irony.", refs=["W100"]),
                                work("W8", "Citing both", 2023, refs=["W100", "W200"])],
                    "meta": {"next_cursor": None}}
        if "filter" in params:
            return {"results": [{"id": "https://openalex.org/W100", "doi": "https://doi.org/10.1/one"}]}
        return {"results": [{"id": "https://openalex.org/W200", "title": "Start paper two without a doi",
                             "publication_year": 2020}]}
    fake = FakeClient(reviq_data(), oa)
    monkeypatch.setattr(fw, "Client", lambda **kw: fake)
    out = tmp_path / "it4.bib"
    code = fw.main(["--project", "1", "--sources", "snowballing:1,snowballing:2", "--out", str(out)])
    assert code == 0
    papers = parse(out.read_text())
    assert [p["title"] for p in papers] == ["Citing the first", "Citing both"]
    assert papers[0]["abstract"] == "About irony."
    text = out.read_text()
    assert "cites W100" in text and "cites W100, W200" in text         # which start papers each cites
    report = capsys.readouterr().out
    assert "Start set: 2 papers" in report and "Found in OpenAlex: 2" in report


def test_main_stops_when_there_is_no_start_set(monkeypatch, capsys, tmp_path):
    fake = FakeClient({"phase=full-text": [], "phase=screening": []}, None)
    monkeypatch.setattr(fw, "Client", lambda **kw: fake)
    assert fw.main(["--project", "1", "--sources", "snowballing:1", "--out", str(tmp_path / "x.bib")]) == 1


# ── snowballing only from core papers ─────────────────────────────────────────

def _paper_with(i, src, title, label, abstract="", *, fin="I", extra=()):
    ds = [{"decision": "I", "criterion_label": label, "reviewer_id": 1}, *extra]
    return {"id": i, "source": src, "title": title, "doi": f"10.1/{i}", "year": 2020,
            "abstract": abstract, "dedup_status": "original", "decisions": ds,
            "final_decision": {"decision": fin}}


def _core_world():
    ft = [_paper_with(1, "snowballing:1", "Sarcasm detection with BERT", "Core"),
          _paper_with(2, "snowballing:1", "A survey of sentiment analysis", "Terminology",
                      abstract="We review methods, including irony and sarcasm."),
          _paper_with(3, "snowballing:2", "Cooking in the environment", "Terminology"),
          _paper_with(4, "snowballing:2", "Satire in the news", "Core")]
    sc = [dict(p, final_decision={"decision": "I"}) for p in ft]
    return {"phase=full-text": ft, "phase=screening": sc}


def test_core_by_criterion_label_is_case_insensitive_and_needs_an_include():
    assert fw.is_core({"decisions": [{"decision": "I", "criterion_label": "Core"}]}, {"core"}, None)
    assert not fw.is_core({"decisions": [{"decision": "I", "criterion_label": "Terminology"}]}, {"Core"}, None)
    # an exclusion under that label is not an inclusion
    assert not fw.is_core({"decisions": [{"decision": "E", "criterion_label": "Core"}]}, {"Core"}, None)
    assert not fw.is_core({"decisions": []}, {"Core"}, None)


def test_core_by_terms_reads_title_and_abstract_from_the_start_of_a_word():
    pat = fw.term_pattern(["iron", "sarcas"])
    assert fw.is_core({"title": "Irony in tweets"}, set(), pat)
    assert fw.is_core({"title": "A survey", "abstract": "It covers sarcasm."}, set(), pat)
    assert not fw.is_core({"title": "The environment", "abstract": "Nothing here."}, set(), pat)
    assert fw.term_pattern(["", "  "]) is None


def test_no_filter_means_every_paper_is_core():
    assert fw.is_core({"title": "Anything", "decisions": []}, set(), None)


def test_both_filters_must_hold():
    p = {"title": "Sarcasm detection", "decisions": [{"decision": "I", "criterion_label": "Terminology"}]}
    pat = fw.term_pattern(["sarcas"])
    assert fw.is_core(p, {"Terminology"}, pat)
    assert not fw.is_core(p, {"Core"}, pat)                       # right terms, wrong label
    assert not fw.is_core({**p, "title": "Cooking"}, {"Terminology"}, pat)   # right label, wrong terms


def test_start_set_can_be_narrowed_to_core_papers():
    fake = FakeClient(_core_world(), None)
    srcs = ["snowballing:1", "snowballing:2"]
    assert sorted(p["id"] for p in fw.start_set(fake, "http://x", 1, srcs)[0]) == [1, 2, 3, 4]
    by_label = fw.start_set(fake, "http://x", 1, srcs, criteria={"Core"})[0]
    assert sorted(p["id"] for p in by_label) == [1, 4]
    by_terms = fw.start_set(fake, "http://x", 1, srcs, terms=["sarcas", "iron", "satir"])[0]
    assert sorted(p["id"] for p in by_terms) == [1, 2, 4]            # 2 by its abstract; 3 ("environment") is out
    both = fw.start_set(fake, "http://x", 1, srcs, criteria={"Core"}, terms=["satir"])[0]
    assert [p["id"] for p in both] == [4]


def _run(monkeypatch, tmp_path, argv, world=None):
    def oa(url, params):
        if "cites:" in params.get("filter", ""):
            return {"results": [work("W7", "Citing the core", 2022)], "meta": {"next_cursor": None}}
        return {"results": [{"id": "https://openalex.org/W100", "doi": "https://doi.org/10.1/1"},
                            {"id": "https://openalex.org/W104", "doi": "https://doi.org/10.1/4"}]}
    fake = FakeClient(world or _core_world(), oa)
    monkeypatch.setattr(fw, "Client", lambda **kw: fake)
    return fw.main(["--project", "1", "--sources", "snowballing:1,snowballing:2",
                    "--out", str(tmp_path / "x.bib"), *argv])


def test_main_reports_the_filter_and_snowballs_only_from_core(monkeypatch, capsys, tmp_path):
    assert _run(monkeypatch, tmp_path, ["--criterion", "Core"]) == 0
    out = capsys.readouterr().out
    assert "Start set: 2 papers" in out and "criterion Core" in out and "of 4 included from those sources" in out


def test_main_says_which_labels_exist_when_the_criterion_matches_nothing(monkeypatch, capsys, tmp_path):
    assert _run(monkeypatch, tmp_path, ["--criterion", "Cor"]) == 1       # a typo
    out = capsys.readouterr().out
    assert "Start set: 0 papers" in out
    assert "The labels on included papers are: Terminology (2), Core (2)" in out or \
           "The labels on included papers are: Core (2), Terminology (2)" in out


def test_main_with_require_terms(monkeypatch, capsys, tmp_path):
    assert _run(monkeypatch, tmp_path, ["--require-terms", "satir"]) == 0
    assert "Start set: 1 papers" in capsys.readouterr().out
