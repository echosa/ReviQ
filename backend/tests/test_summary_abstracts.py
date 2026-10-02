"""The Quality and Extraction lists carry each paper's abstract, so a reviewer
can read it from the list the way they can on Screening and Eligibility."""
from app.models import Paper

ABSTRACT = "We study verbal irony in tweets."


def test_qa_and_extraction_summaries_include_the_abstract(instance):
    proj = instance.create_project(title="Abstracts", lead="Alice")
    pid = proj["id"]
    r1 = instance.reviewers(pid)[0]
    instance.add_qa_criterion(pid, label="Q1")
    instance.import_bib(pid, [{"citekey": "p1", "title": "Irony in tweets", "year": 2021,
                               "doi": "10.1/p1"}])
    paper = instance.session.get(Paper, instance.paper_by_citekey(pid, "p1")["id"])
    paper.abstract = ABSTRACT
    instance.session.add(paper)
    instance.session.commit()
    for phase in ("screening", "full-text"):
        instance.decide(pid, paper.id, reviewer_id=r1["id"], phase=phase, decision="I")

    qa = instance.client.get(f"/api/projects/{pid}/qa-summary").json()
    assert [p["paper_abstract"] for p in qa["papers"]] == [ABSTRACT]

    ext = instance.client.get(f"/api/projects/{pid}/extraction/summary").json()
    assert [p["abstract"] for p in ext["papers"]] == [ABSTRACT]
