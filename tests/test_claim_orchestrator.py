"""Phase 2 tests: single-PICO thin slice (claim_orchestrator).

The DB search is stubbed so the test runs offline; it exercises the
dedup -> year -> Q1(strict) -> validate counting that populates PicoPrismaFlow.
"""

from __future__ import annotations

import litreview.pipeline.claim_orchestrator as co
from litreview.config import Config
from litreview.models import ArticleMetadata, DatabaseSource, PICOQuestion
from litreview.pipeline.claim_orchestrator import ClaimAppraisalPipeline
from litreview.pipeline.orchestrator import LitReviewPipeline


def _art(title, doi, year, citescore, abstract=""):
    # No ISSN -> OpenAlex lookup skipped; CiteScore drives the quartile fallback.
    return ArticleMetadata(title=title, doi=doi, year=year, citescore=citescore, abstract=abstract)


async def _stub_crossref_all_verified(articles, mailto="", concurrency=8):
    # Offline stub: mark every DOI-bearing article as confirmed in CrossRef.
    for a in articles:
        if a.doi:
            a.crossref_verified = True
            a.crossref_title_match = 1.0
    return articles


def _canned_corpus():
    return [
        _art("A q1 recent", "10.1/a", 2020, 15.0, abstract="long abstract here"),
        _art("A duplicate", "10.1/a", 2020, 15.0, abstract="short"),   # dup DOI of A
        _art("C too old", "10.1/c", 2010, 15.0),                        # dropped by year
        _art("D unranked", "10.1/d", 2020, None),                       # dropped by strict Q1
        _art("E q1 recent", "10.1/e", 2020, 15.0),                      # survives
    ]


async def test_run_pico_search_prisma_counts(monkeypatch):
    monkeypatch.setattr(co, "batch_verify_crossref", _stub_crossref_all_verified)
    config = Config(unpaywall_email="")  # no Unpaywall -> validation gate skipped, all kept
    pipe = ClaimAppraisalPipeline(config)
    # Compose a non-entered LitReviewPipeline (clients are None); stub the search.
    inner = LitReviewPipeline(config)
    pipe._pipeline = inner

    async def fake_search(queries):
        return _canned_corpus()

    inner.search_all_databases = fake_search  # type: ignore[assignment]

    pico = PICOQuestion(
        pico_id="pico_01",
        intervention="4+2R diet",
        outcome="weight loss",
        primary_terms=["4+2R diet", "metabolic diet"],
    )
    included, flow = await pipe.run_pico_search(pico)

    assert flow.total_found == 15           # three passes (relevance, design sweep, most-cited); stub returns 5 each
    assert flow.after_dedup == 4            # A, C, D, E
    assert flow.after_year_filter == 3      # A, D, E (C dropped: 2010)
    assert flow.excluded_by_year == 1
    assert flow.after_quality_filter == 2   # A, E (D dropped: Unknown quartile)
    assert flow.excluded_by_quality == 1
    assert flow.after_validation == 2       # no Unpaywall gate
    assert flow.after_crossref == 2         # both confirmed by stubbed CrossRef
    assert flow.excluded_by_crossref == 0
    assert flow.included == 2
    assert {a.title for a in included} == {"A q1 recent", "E q1 recent"}


async def test_run_pico_search_drops_crossref_unconfirmed(monkeypatch):
    # CrossRef confirms only "A q1 recent" -> the other Q1 study is dropped as
    # a possible hallucination.
    async def stub(articles, mailto="", concurrency=8):
        for a in articles:
            a.crossref_verified = a.title == "A q1 recent"
            a.crossref_title_match = 1.0 if a.crossref_verified else 0.0
        return articles

    monkeypatch.setattr(co, "batch_verify_crossref", stub)
    config = Config(unpaywall_email="")
    pipe = ClaimAppraisalPipeline(config)
    inner = LitReviewPipeline(config)
    pipe._pipeline = inner
    inner.search_all_databases = lambda queries: _async_return(_canned_corpus())  # type: ignore[assignment]

    pico = PICOQuestion(pico_id="pico_01", intervention="4+2R diet", outcome="weight loss",
                        primary_terms=["4+2R diet"])
    included, flow = await pipe.run_pico_search(pico)
    assert flow.after_crossref == 1
    assert flow.excluded_by_crossref == 1
    assert {a.title for a in included} == {"A q1 recent"}


async def _async_return(value):
    return value


async def test_run_pico_builds_result_and_audit_trail(monkeypatch):
    monkeypatch.setattr(co, "batch_verify_crossref", _stub_crossref_all_verified)
    config = Config(unpaywall_email="")
    pipe = ClaimAppraisalPipeline(config)
    inner = LitReviewPipeline(config)
    pipe._pipeline = inner

    async def fake_search(queries):
        return _canned_corpus()

    inner.search_all_databases = fake_search  # type: ignore[assignment]

    pico = PICOQuestion(pico_id="pico_01", intervention="4+2R diet", outcome="weight loss",
                        primary_terms=["4+2R diet"])
    result = await pipe.run_pico(pico)

    assert result.question.pico_id == "pico_01"
    assert result.prisma.included == 2
    assert len(result.search_queries) == 3
    assert result.search_queries[0].date_from == 2016
    assert result.search_queries[1].databases == ["pubmed"]
    assert result.search_queries[2].databases == ["scopus"]
    assert [step["stage"] for step in result.audit_trail] == [
        "search", "dedup", "year_filter", "quality_filter", "validation", "crossref",
    ]


def test_build_pico_queries_combines_intervention_and_outcome():
    pipe = ClaimAppraisalPipeline(Config())
    pico = PICOQuestion(intervention="4+2R diet", outcome="HbA1c",
                        primary_terms=["4+2R diet", "meal replacement"])
    queries = pipe.build_pico_queries(pico)
    assert len(queries) == 3
    bq = queries[0].boolean_query
    assert '"4+2R diet"' in bq and '"meal replacement"' in bq
    assert '"HbA1c"' in bq
    assert " AND " in bq
    assert queries[0].date_from == 2016
    assert queries[0].databases == ["scopus", "pubmed", "embase"]


def test_build_pico_queries_adds_pubmed_only_design_sweep():
    pipe = ClaimAppraisalPipeline(Config())
    pico = PICOQuestion(intervention="empagliflozin", outcome="HFpEF", primary_terms=["empagliflozin"])
    relevance, sweep, most_cited = pipe.build_pico_queries(pico)
    assert sweep.boolean_query.startswith(f"({relevance.boolean_query}) AND ")
    assert "randomized controlled trial[pt]" in sweep.boolean_query
    assert "meta-analysis[pt]" in sweep.boolean_query
    assert sweep.databases == ["pubmed"]  # [pt] is PubMed syntax
    assert sweep.date_from == 2016
    assert sweep.sort is None


def test_build_pico_queries_adds_scopus_most_cited_pass():
    pipe = ClaimAppraisalPipeline(Config())
    pico = PICOQuestion(intervention="empagliflozin", outcome="HFpEF", primary_terms=["empagliflozin"])
    relevance, _sweep, most_cited = pipe.build_pico_queries(pico)
    assert most_cited.databases == ["scopus"]
    assert most_cited.sort == "-citedby-count"
    assert most_cited.boolean_query == f"TITLE-ABS-KEY({relevance.boolean_query})"


class _FakePubMed:
    """Stands in for PubMedClient: DOI->PMID and PMID->record from a canned table."""

    def __init__(self, records: list[dict]):
        self.records = records

    async def search_by_dois(self, dois, chunk=50):
        want = {d.lower() for d in dois}
        return [r["pmid"] for r in self.records if r["doi"].lower() in want]

    async def fetch_articles(self, pmids):
        return [dict(r) for r in self.records if r["pmid"] in set(pmids)]


def _kw_art(**kw) -> ArticleMetadata:
    base = dict(title="t", journal="J", year=2020, journal_quartile="Q1", source_db=DatabaseSource.PUBMED)
    base.update(kw)
    return ArticleMetadata(**base)


async def test_snowball_adds_trials_cited_by_two_syntheses(monkeypatch):
    monkeypatch.setattr(co, "batch_verify_crossref", _stub_crossref_all_verified)
    refs = {
        "10.1/ma1": ["10.1/deliver", "10.1/only-one", "10.1/already"],
        "10.1/ma2": ["10.1/deliver", "10.1/already"],
    }

    async def fake_refs(doi, mailto=""):
        return refs.get(doi, [])

    monkeypatch.setattr(co, "fetch_reference_dois", fake_refs)

    config = Config(unpaywall_email="")
    pipe = ClaimAppraisalPipeline(config)
    inner = LitReviewPipeline(config)
    inner._pubmed = _FakePubMed([
        {"pmid": "1", "doi": "10.1/deliver", "title": "DELIVER", "journal": "NEJM", "year": 2022,
         "pub_type": "RCT", "pub_types": ["Randomized Controlled Trial"], "abstract": "x", "authors": [],
         "issn": None, "volume": None, "issue": None, "pages": None},
        {"pmid": "2", "doi": "10.1/only-one", "title": "one seed", "journal": "NEJM", "year": 2022,
         "pub_type": "RCT", "pub_types": [], "abstract": "x", "authors": [],
         "issn": None, "volume": None, "issue": None, "pages": None},
    ])
    pipe._pipeline = inner

    async def fake_quality(articles, **kw):
        for a in articles:
            a.journal_quartile = "Q1"
        return articles

    monkeypatch.setattr(co, "assess_journal_quality", fake_quality)

    included = [
        _kw_art(doi="10.1/ma1", pub_type="Meta-Analysis"),
        _kw_art(doi="10.1/ma2", pub_type="Systematic Review"),
        _kw_art(doi="10.1/already", pub_type="RCT"),
    ]
    candidates = await pipe._snowball_candidates(included)
    assert [a.doi for a in candidates] == ["10.1/deliver"]  # cited by 2; "only-one" cited by 1; "already" present

    added, counts, dropped = await pipe._apply_gates(candidates, "test")
    assert [a.title for a in added] == ["DELIVER"]
    assert counts.after_crossref == 1
    assert dropped == []


async def test_apply_gates_returns_dropped_records_with_reasons(monkeypatch):
    async def fake_quality(articles, **kw):
        for article in articles:
            article.journal_quartile = "Q1" if article.title == "kept" else "Q2"
        return [article for article in articles if article.journal_quartile == "Q1"]

    monkeypatch.setattr(co, "assess_journal_quality", fake_quality)
    monkeypatch.setattr(co, "batch_verify_crossref", _stub_crossref_all_verified)
    config = Config(unpaywall_email="")
    pipe = ClaimAppraisalPipeline(config)
    pipe._pipeline = LitReviewPipeline(config)
    articles = [_art("missing year", "10.1/no-year", None, 15),
                _art("old", "10.1/old", 2010, 15),
                _art("q2", "10.1/q2", 2020, 15),
                _art("kept", "10.1/kept", 2020, 15)]

    kept, _counts, dropped = await pipe._apply_gates(articles, "test")

    assert [article.title for article in kept] == ["kept"]
    assert {(article.title, reason) for article, reason in dropped} == {
        ("missing year", "no year"), ("old", "year<min"), ("q2", "quartile Q2")
    }


async def test_snowball_skips_without_seeds_or_pubmed():
    pipe = ClaimAppraisalPipeline(Config(unpaywall_email=""))
    pipe._pipeline = LitReviewPipeline(Config())  # _pubmed is None
    assert await pipe._snowball_candidates([_kw_art(doi="10.1/x", pub_type="Meta-Analysis")]) == []


async def test_add_expert_studies_reports_gate_failures(monkeypatch):
    monkeypatch.setattr(co, "batch_verify_crossref", _stub_crossref_all_verified)
    config = Config(unpaywall_email="")
    pipe = ClaimAppraisalPipeline(config)
    inner = LitReviewPipeline(config)
    inner._pubmed = _FakePubMed([
        {"pmid": "10", "doi": "10.1/good", "title": "Good trial", "journal": "NEJM", "year": 2021,
         "pub_type": "RCT", "pub_types": [], "abstract": "x", "authors": [], "issn": "0028-4793",
         "volume": None, "issue": None, "pages": None},
        {"pmid": "11", "doi": "10.1/old", "title": "Old trial", "journal": "NEJM", "year": 2009,
         "pub_type": "RCT", "pub_types": [], "abstract": "x", "authors": [], "issn": "0028-4793",
         "volume": None, "issue": None, "pages": None},
    ])
    pipe._pipeline = inner

    async def fake_quality(articles, **kw):
        for a in articles:
            a.journal_quartile = "Q1"
        return articles

    monkeypatch.setattr(co, "assess_journal_quality", fake_quality)

    added, rejected = await pipe.add_expert_studies("pico_01", ["10", "10.1/old", "99"], included=[])
    assert [a.title for a in added] == ["Good trial"]
    assert rejected["11"].startswith("year 2009")
    assert rejected["99"] == "not found in PubMed"


def test_deduplicate_merges_pubmed_fields_into_scopus_survivor():
    inner = LitReviewPipeline(Config())
    scopus = _kw_art(doi="10.1/x", abstract="long abstract from scopus", citation_count=500,
                  source_db=DatabaseSource.SCOPUS, citescore=20.0)
    pubmed = _kw_art(doi="10.1/x", abstract="", pmid="123", pub_type="RCT",
                  pub_types=["Randomized Controlled Trial"], issn="0028-4793")
    (kept,) = inner.deduplicate([scopus, pubmed])
    assert kept is scopus                      # longer abstract wins
    assert kept.pmid == "123" and kept.pub_type == "RCT" and kept.issn == "0028-4793"
    assert kept.citation_count == 500 and kept.citescore == 20.0


def test_build_pico_queries_keeps_prose_outcome_out_of_the_query():
    pipe = ClaimAppraisalPipeline(Config())
    prose = "cardiovascular events: myocardial infarction, stroke, coronary heart disease incidence"
    pico = PICOQuestion(intervention="vaping", outcome=prose, primary_terms=["vaping"],
                        secondary_terms=["myocardial infarction", "stroke"])
    bq = pipe.build_pico_queries(pico)[0].boolean_query
    assert prose not in bq and '"myocardial infarction"' in bq

    short = PICOQuestion(intervention="vaping", outcome="HbA1c", primary_terms=["vaping"])
    assert '"HbA1c"' in pipe.build_pico_queries(short)[0].boolean_query  # short, and the only outcome term

    only_prose = PICOQuestion(intervention="vaping", outcome=prose, primary_terms=["vaping"])
    assert prose in pipe.build_pico_queries(only_prose)[0].boolean_query  # nothing else describes the outcome


async def test_expert_addition_named_twice_is_added_once(monkeypatch):
    """One paper named by both PMID and DOI must not enter the set twice."""
    monkeypatch.setattr(co, "batch_verify_crossref", _stub_crossref_all_verified)
    config = Config(unpaywall_email="")
    pipe = ClaimAppraisalPipeline(config)
    inner = LitReviewPipeline(config)
    record = {"pmid": "27748956", "doi": "10.1113/JP273196", "title": "Physiological adaptations",
              "journal": "J Physiol", "year": 2016, "pub_type": "Review", "pub_types": [],
              "abstract": "x", "authors": [], "issn": None, "volume": None, "issue": None, "pages": None}

    class _Dup(_FakePubMed):
        async def fetch_articles(self, pmids):
            # EFetch echoes a repeated id as a repeated record.
            return [dict(record) for _ in pmids if _ == "27748956"]

    inner._pubmed = _Dup([record])
    pipe._pipeline = inner

    async def fake_quality(articles, **kw):
        for a in articles:
            a.journal_quartile = "Q1"
        return articles

    monkeypatch.setattr(co, "assess_journal_quality", fake_quality)

    added, rejected = await pipe.add_expert_studies(
        "pico_01", ["27748956", "10.1113/JP273196"], included=[])
    assert len(added) == 1
    assert added[0].pmid == "27748956"
    assert rejected == {}
