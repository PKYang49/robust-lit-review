"""Evidence-brief pipeline: the deterministic stages, end to end on fixtures.

Search is not exercised (network); everything after it is — task generation,
verdict derivation, the citation whitelist gate, and rendering.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

from litreview.models import PicoPrismaFlow
from litreview.pipeline import brief

STUDIES = [
    {
        "title": "Trial A", "authors": ["Lin, Hsieh-Ting", "Chen, Wei", "Wu, Mei", "Kao, Yu"],
        "journal": "J Test Med", "year": 2021, "doi": "10.1000/trial-a", "pmid": "1",
        "issn": "1234-5678", "journal_quartile": "Q1", "pub_type": "RCT",
        "citation_count": 10, "abstract": "n=240 randomized. HR 0.65 (95% CI 0.5-0.8), p<0.001.",
    },
    {
        "title": "Cohort B", "authors": ["Huang, Li"],
        "journal": "J Test Med", "year": 2019, "doi": "10.1000/cohort-b", "pmid": "2",
        "issn": "1234-5678", "journal_quartile": "Q1", "pub_type": "Observational",
        "citation_count": 3, "abstract": "Prospective cohort of 1,204 patients.",
    },
]

GRADE = {
    "starting_level": "high",
    "domains": [
        {"name": "risk_of_bias", "rating": "not_serious", "downgrade": 0, "justification": "ok"},
        {"name": "inconsistency", "rating": "serious", "downgrade": -1, "justification": "two studies disagree"},
        {"name": "indirectness", "rating": "not_serious", "downgrade": 0, "justification": "ok"},
        {"name": "imprecision", "rating": "not_serious", "downgrade": 0, "justification": "ok"},
        {"name": "publication_bias", "rating": "not_serious", "downgrade": 0, "justification": "ok"},
    ],
    "effect_direction": "beneficial",
    "final_certainty": "high",  # advisory; Python recomputes to moderate
    "n_studies": 2, "n_rct": 1, "summary": "One RCT, one cohort.",
}


def _writeup(extra_doi: str | None = None) -> dict:
    pro = "RCT 顯示 HR 0.65 [doi:10.1000/trial-a]。\n\n世代研究支持 [doi:10.1000/cohort-b]。"
    if extra_doi:
        pro += f" 另見 [doi:{extra_doi}]。"
    return {
        "pico_id": "pico_01",
        "headline": "看起來有效，但研究之間不一致。",
        "lay": "一個 RCT 說有效 [doi:10.1000/trial-a]。\n\n另一個觀察性研究也支持。",
        "pro": pro,
        "key_studies": [{"doi": "10.1000/trial-a", "design": "RCT", "n": "n=240", "finding": "HR 0.65"}],
        "caveats": "只有兩篇。",
        "dissent": "",
    }


@pytest.fixture
def base(tmp_path: Path) -> Path:
    b = tmp_path / "demo"
    b.mkdir()
    (b / "picos.json").write_text(json.dumps({
        "question": "X 對 Y 有幫助嗎？",
        "picos": [{
            "pico_id": "pico_01", "population": "adults", "intervention": "X",
            "comparator": "placebo", "outcome": "Y", "outcome_domain": "y",
            "question_text": "X 是否改善 Y？", "primary_terms": ["X"], "secondary_terms": ["Y"],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    (b / "pico_01.json").write_text(json.dumps({
        "pico": {"pico_id": "pico_01", "population": "adults", "intervention": "X",
                 "comparator": "placebo", "outcome": "Y", "question_text": "X 是否改善 Y？"},
        "prisma": {"total_found": 40, "after_dedup": 38, "after_year_filter": 30,
                   "after_quality_filter": 5, "after_validation": 5, "after_crossref": 2, "included": 2},
        "included_studies": STUDIES,
    }, ensure_ascii=False), encoding="utf-8")
    (b / "grade_pico_01.json").write_text(json.dumps(GRADE), encoding="utf-8")
    return b


def test_load_brief_assigns_pico_ids_when_missing(tmp_path: Path):
    b = tmp_path / "q"
    b.mkdir()
    (b / "picos.json").write_text(json.dumps({"question": "q", "picos": [{"intervention": "a"}, {"intervention": "b"}]}))
    _q, picos = brief.load_brief(b)
    assert [p.pico_id for p in picos] == ["pico_01", "pico_02"]


def test_load_brief_rejects_bare_list(tmp_path: Path):
    b = tmp_path / "q"
    b.mkdir()
    (b / "picos.json").write_text("[]")
    with pytest.raises(ValueError):
        brief.load_brief(b)


def test_grade_task_points_agent_at_evidence_view(base: Path):
    path = brief.write_grade_task(base, "pico_01")
    text = path.read_text(encoding="utf-8")
    assert path == base / "tasks" / "grade_pico_01.md"
    assert str((base / "pico_01.evidence.json").resolve()) in text
    assert str((base / "pico_01.json").resolve()) not in text
    assert "@Lin2021Trial" in text  # study line from generate_grade_task
    assert "RCT" in text  # pub_type surfaced, not left to the LLM


def test_screen_tasks_batch_and_force_include_expert(base: Path):
    data = json.loads((base / "pico_01.json").read_text(encoding="utf-8"))
    data["expert_added"] = ["10.1000/cohort-b"]
    (base / "pico_01.json").write_text(json.dumps(data), encoding="utf-8")
    tasks = brief.write_screen_tasks(base, "pico_01", batch_size=1)
    assert [p.name for p in tasks] == ["screen_pico_01_01.md", "screen_pico_01_02.md"]
    assert "Population: adults" in tasks[0].read_text(encoding="utf-8")

    for index, key in enumerate(("Lin2021Trial", "Huang2019Cohort"), 1):
        (base / f"screen_pico_01_{index:02d}.json").write_text(json.dumps({
            "pico_id": "pico_01", "batch": index,
            "decisions": [{"citation_key": key, "include": index == 1,
                           "reason": "agent excluded", "design": "RCT" if index == 1 else "cohort"}],
        }), encoding="utf-8")
    screen = brief.collect_screen(base, "pico_01")
    assert screen["included"] == ["Lin2021Trial", "Huang2019Cohort"]
    assert screen["excluded"] == []


def test_collect_screen_merges_and_reports_missing_batch(base: Path):
    brief.write_screen_tasks(base, "pico_01", batch_size=1)
    (base / "screen_pico_01_01.json").write_text(json.dumps({
        "decisions": [{"citation_key": "Lin2021Trial", "include": True,
                       "reason": "match", "design": "RCT"}]
    }), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="screen_pico_01_02.json"):
        brief.collect_screen(base, "pico_01")

    (base / "screen_pico_01_02.json").write_text(json.dumps({
        "decisions": [{"citation_key": "Huang2019Cohort", "include": False,
                       "reason": "wrong comparator", "design": "cohort"}]
    }), encoding="utf-8")
    screen = brief.collect_screen(base, "pico_01")
    assert screen["included"] == ["Lin2021Trial"]
    assert screen["excluded"] == [{"citation_key": "Huang2019Cohort", "reason": "wrong comparator"}]

    stale_at = (base / "screen_pico_01_02.json").stat().st_mtime + 2
    os.utime(base / "tasks" / "screen_pico_01_02.md", (stale_at, stale_at))
    with pytest.raises(FileNotFoundError, match="stale screening batch result"):
        brief.collect_screen(base, "pico_01")


def test_grade_task_and_verdict_are_limited_to_screened_set(base: Path):
    (base / "pico_01.screen.json").write_text(json.dumps({
        "pico_id": "pico_01", "included": ["Lin2021Trial"],
        "excluded": [{"citation_key": "Huang2019Cohort", "reason": "wrong outcome"}],
        "designs": {"Lin2021Trial": "RCT", "Huang2019Cohort": "cohort"},
    }), encoding="utf-8")
    path = brief.write_grade_task(base, "pico_01")
    text = path.read_text(encoding="utf-8")
    assert str((base / "pico_01.body.json").resolve()) in text
    assert "@Lin2021Trial" in text and "@Huang2019Cohort" not in text

    grade_data = json.loads((base / "grade_pico_01.json").read_text(encoding="utf-8"))
    grade_data["body_of_evidence"] = ["@Lin2021Trial", "@Huang2019Cohort"]
    (base / "grade_pico_01.json").write_text(json.dumps(grade_data), encoding="utf-8")
    grade, _verdict = brief.collect_verdict(base, "pico_01")
    assert grade.body_of_evidence == ["@Lin2021Trial"]

    grade_data["body_of_evidence"] = []
    grade_data["domains"][0]["evidence_refs"] = ["@Lin2021Trial"]
    (base / "grade_pico_01.json").write_text(json.dumps(grade_data), encoding="utf-8")
    grade, _verdict = brief.collect_verdict(base, "pico_01")
    assert grade.body_of_evidence == []  # explicit narrowing to empty is not legacy fallback


async def test_repeated_expert_add_preserves_prior_expert_dois(base: Path, monkeypatch):
    additions = [
        brief._study_load(dict(STUDIES[0], title="Named One", doi="10.1000/named-one", pmid="11")),
        brief._study_load(dict(STUDIES[0], title="Named Two", doi="10.1000/named-two", pmid="12")),
    ]

    class FakePipeline:
        def __init__(self, _cfg):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def add_expert_studies(self, _pico_id, _identifiers, _studies):
            return [additions.pop(0)], {}

    monkeypatch.setattr(brief, "ClaimAppraisalPipeline", FakePipeline)
    cfg = brief.brief_config()
    await brief.add_studies(base, "pico_01", ["11"], cfg)
    await brief.add_studies(base, "pico_01", ["12"], cfg)
    data = json.loads((base / "pico_01.json").read_text(encoding="utf-8"))
    assert data["expert_added"] == ["10.1000/named-one", "10.1000/named-two"]


def test_evidence_view_drops_reviews_but_whitelist_keeps_them(base: Path):
    data = json.loads((base / "pico_01.json").read_text(encoding="utf-8"))
    data["included_studies"].append({
        "title": "Narrative review", "authors": ["Solo, A"], "journal": "J", "year": 2023,
        "doi": "10.1000/review-c", "pmid": "3", "issn": None, "journal_quartile": "Q1",
        "pub_type": "Review", "citation_count": 0, "abstract": "prose",
    })
    (base / "pico_01.json").write_text(json.dumps(data), encoding="utf-8")

    _path, shown, total = brief.write_evidence_view(base, "pico_01")
    assert (shown, total) == (2, 3)
    view = json.loads((base / "pico_01.evidence.json").read_text(encoding="utf-8"))
    assert {s["doi"] for s in view["included_studies"]} == {"10.1000/trial-a", "10.1000/cohort-b"}

    # The review is still a legitimate citation: it passed every gate.
    w = _writeup("10.1000/review-c")
    (base / "write_pico_01.json").write_text(json.dumps(w, ensure_ascii=False), encoding="utf-8")
    assert brief.check_citations(base, ["pico_01"]) == {"pico_01": []}


async def test_search_pico_writes_rankable_gate_exclusions(tmp_path: Path, monkeypatch):
    study = brief._study_load(STUDIES[0])
    dropped = brief._study_load(dict(STUDIES[1], citation_count=88))

    class FakePipeline:
        def __init__(self, _cfg):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def run_pico_search(self, _pico, dropped_out=None):
            dropped_out.append((dropped, "quartile Q2"))
            return [study], PicoPrismaFlow(included=1)

    monkeypatch.setattr(brief, "ClaimAppraisalPipeline", FakePipeline)
    pico = brief.PICOQuestion(pico_id="pico_01", intervention="X", outcome="Y")
    await brief.search_pico(tmp_path, pico, brief.brief_config())
    excluded = json.loads((tmp_path / "pico_01.excluded.json").read_text(encoding="utf-8"))
    assert excluded[0]["citation_count"] == 88 and excluded[0]["reason"] == "quartile Q2"
    assert brief.load_gaps(tmp_path, "pico_01")[0]["title"] == "Cohort B"


def test_verdict_is_recomputed_not_taken_from_agent(base: Path):
    grade, verdict = brief.collect_verdict(base, "pico_01")
    assert grade.final_certainty == "moderate"  # high + (-1), agent said "high"
    assert verdict.verdict == "supported"
    assert verdict.confidence == "moderate"


def test_writer_task_carries_frozen_verdict(base: Path):
    path = brief.write_writer_task(base, "pico_01")
    text = path.read_text(encoding="utf-8")
    assert "證據支持（supported）" in text
    assert "證據確定性：中（moderate）" in text
    assert str((base / "write_pico_01.json").resolve()) in text


def test_cited_dois_dedup_and_normalise():
    w = _writeup()
    w["lay"] += " 再一次 [doi:https://doi.org/10.1000/TRIAL-A]."
    assert brief.cited_dois(w) == ["10.1000/trial-a", "10.1000/cohort-b"]


def test_check_passes_when_all_dois_included(base: Path):
    (base / "write_pico_01.json").write_text(json.dumps(_writeup(), ensure_ascii=False), encoding="utf-8")
    assert brief.check_citations(base, ["pico_01"]) == {"pico_01": []}


def test_check_flags_hallucinated_doi(base: Path):
    (base / "write_pico_01.json").write_text(
        json.dumps(_writeup("10.9999/made-up"), ensure_ascii=False), encoding="utf-8")
    assert brief.check_citations(base, ["pico_01"]) == {"pico_01": ["10.9999/made-up"]}


def test_render_refuses_on_unknown_doi(base: Path):
    (base / "write_pico_01.json").write_text(
        json.dumps(_writeup("10.9999/made-up"), ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="10.9999/made-up"):
        brief.render_brief(base)
    assert not (base / "brief.html").exists()


def test_render_produces_numbered_citations_and_references(base: Path):
    (base / "write_pico_01.json").write_text(json.dumps(_writeup(), ensure_ascii=False), encoding="utf-8")
    out = brief.render_brief(base)
    html = out.read_text(encoding="utf-8")

    assert out == base / "brief.html"
    assert "X 對 Y 有幫助嗎？" in html
    assert "證據支持" in html and "看起來有效" in html
    # [doi:...] tokens became superscript links, in order of first appearance
    assert "[doi:" not in html
    assert '<sup><a href="#ref-1">1</a></sup>' in html
    assert '<sup><a href="#ref-2">2</a></sup>' in html
    # references: AMA-ish authors, et al after 3, doi + pubmed links
    assert "Lin HT, Chen W, Wu M, et al. Trial A. J Test Med. 2021." in html
    assert 'href="https://doi.org/10.1000/trial-a"' in html
    assert "pubmed.ncbi.nlm.nih.gov/1/" in html
    # prose is escaped
    assert "<script" not in html
    # flow counts made it through
    assert "CrossRef 確認 <b>2</b>" in html


def test_render_escapes_writer_html(base: Path):
    w = _writeup()
    w["lay"] = "<script>alert(1)</script> [doi:10.1000/trial-a]"
    (base / "write_pico_01.json").write_text(json.dumps(w, ensure_ascii=False), encoding="utf-8")
    html = brief.render_brief(base).read_text(encoding="utf-8")
    assert "&lt;script&gt;" in html and "<script>" not in html


# ---------------------------------------------------------------------------
# Full-text stage
# ---------------------------------------------------------------------------

def test_collect_grade_falls_back_to_domain_refs_for_body_of_evidence(base: Path):
    grade, _ = brief.collect_verdict(base, "pico_01")
    # fixture GRADE has no body_of_evidence field -> union of evidence_refs (none here)
    assert grade.body_of_evidence == []

    data = json.loads((base / "grade_pico_01.json").read_text())
    data["body_of_evidence"] = ["@Lin2021Trial"]
    (base / "grade_pico_01.json").write_text(json.dumps(data))
    studies = brief.body_of_evidence_studies(base, "pico_01")
    assert [a.title for a in studies] == ["Trial A"]


def test_fetch_fulltext_uses_runner_and_writes_index(base: Path, tmp_path: Path, monkeypatch):
    data = json.loads((base / "grade_pico_01.json").read_text())
    data["body_of_evidence"] = ["@Lin2021Trial", "@Huang2019Cohort"]
    (base / "grade_pico_01.json").write_text(json.dumps(data))

    # A stand-in runner: succeeds for trial-a, fails for cohort-b.
    fake = tmp_path / "runner.py"
    fake.write_text(
        "import sys, json\n"
        "dois = [a for i, a in enumerate(sys.argv) if sys.argv[i-1] == '--doi']\n"
        "out = [a for i, a in enumerate(sys.argv) if sys.argv[i-1] == '--out'][0]\n"
        "for d in dois:\n"
        "    if 'trial-a' in d:\n"
        "        print(json.dumps({'doi': d, 'status': 'ok', 'pdf': out + '/pdf/x.pdf', 'markdown': out + '/x.md', 'chars': 1234, 'error': ''}))\n"
        "    else:\n"
        "        print(json.dumps({'doi': d, 'status': 'failed', 'pdf': None, 'markdown': None, 'chars': 0, 'error': 'paywall'}))\n"
    )
    monkeypatch.setattr(brief, "_RUNNER", fake)
    cfg = brief.brief_config()
    cfg.journalfetcher_python = "python3"
    cfg.journalfetcher_dir = tmp_path

    index = brief.fetch_fulltext(base, "pico_01", cfg)
    by_key = {e["citation_key"]: e for e in index["body_of_evidence"]}
    assert by_key["Lin2021Trial"]["status"] == "ok" and by_key["Lin2021Trial"]["chars"] == 1234
    assert by_key["Lin2021Trial"]["chars_raw"] == 1234
    assert by_key["Huang2019Cohort"]["status"] == "failed" and by_key["Huang2019Cohort"]["error"] == "paywall"
    assert (base / "pico_01.fulltext.json").exists()

    # verify task: lists the retrieved text, names the missing one, backs up the abstract GRADE
    path = brief.write_verify_task(base, "pico_01")
    text = path.read_text(encoding="utf-8")
    assert "@Lin2021Trial" in text and "/x.md" in text
    assert "@Huang2019Cohort — paywall" in text
    assert (base / "grade_pico_01.abstract.json").exists()

    # writer task now points at the full text too
    wtext = brief.write_writer_task(base, "pico_01").read_text(encoding="utf-8")
    assert "全文" in wtext and "/x.md" in wtext


def test_render_shows_fulltext_coverage_and_rob_notes(base: Path):
    data = json.loads((base / "grade_pico_01.json").read_text())
    data.update({
        "body_of_evidence": ["@Lin2021Trial"],
        "fulltext_verified": ["@Lin2021Trial"],
        "rob_notes": {"@Lin2021Trial": "central randomisation; double-blind; ITT; 2% lost"},
        "discrepancies": ["abstract omits that CV death alone was not significant"],
    })
    (base / "grade_pico_01.json").write_text(json.dumps(data))
    (base / "pico_01.fulltext.json").write_text(json.dumps({"pico_id": "pico_01", "body_of_evidence": [
        {"citation_key": "Lin2021Trial", "doi": "10.1000/trial-a", "status": "ok", "markdown": "x.md", "chars": 10, "error": ""},
    ]}))
    (base / "write_pico_01.json").write_text(json.dumps(_writeup(), ensure_ascii=False), encoding="utf-8")
    html = brief.render_brief(base).read_text(encoding="utf-8")
    assert "全文複核 1／1" in html
    assert "central randomisation; double-blind" in html
    assert "abstract omits that CV death" in html
    assert "<td class=\"num\">✓</td>" in html  # trial-a is a key study with full text


def test_render_without_fulltext_says_abstract_level(base: Path):
    (base / "write_pico_01.json").write_text(json.dumps(_writeup(), ensure_ascii=False), encoding="utf-8")
    html = brief.render_brief(base).read_text(encoding="utf-8")
    assert "摘要層級" in html


def test_harm_claim_flips_verdict_mapping(base: Path):
    from litreview.pipeline.verdict_builder import map_verdict
    assert map_verdict("high", "harmful", "harm") == "supported"
    assert map_verdict("high", "no_effect", "harm") == "refuted"
    assert map_verdict("high", "beneficial", "harm") == "refuted"
    assert map_verdict("low", "harmful", "harm") == "uncertain"
    assert map_verdict("high", "harmful") == "refuted"  # default: benefit claim

    picos = json.loads((base / "picos.json").read_text(encoding="utf-8"))
    picos["picos"][0]["claim_direction"] = "harm"
    (base / "picos.json").write_text(json.dumps(picos, ensure_ascii=False), encoding="utf-8")
    data = json.loads((base / "pico_01.json").read_text(encoding="utf-8"))
    data["pico"]["claim_direction"] = "harm"
    (base / "pico_01.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _grade, verdict = brief.collect_verdict(base, "pico_01")
    assert verdict.verdict == "refuted"  # fixture GRADE says beneficial, moderate -> harm claim refuted


def test_expert_added_study_is_shown_to_agents_even_if_letter(base: Path):
    data = json.loads((base / "pico_01.json").read_text(encoding="utf-8"))
    data["included_studies"].append({
        "title": "Research Letter", "authors": ["Solo, A"], "journal": "Circulation", "year": 2022,
        "doi": "10.1000/letter-d", "pmid": "4", "issn": None, "journal_quartile": "Q1",
        "pub_type": "Letter", "citation_count": 0, "abstract": "cohort data",
    })
    (base / "pico_01.json").write_text(json.dumps(data), encoding="utf-8")
    _p, shown, total = brief.write_evidence_view(base, "pico_01")
    assert (shown, total) == (2, 3)  # letter hidden by default

    data["expert_added"] = ["10.1000/letter-d"]
    (base / "pico_01.json").write_text(json.dumps(data), encoding="utf-8")
    _p, shown, total = brief.write_evidence_view(base, "pico_01")
    assert (shown, total) == (3, 3)  # expert override


def test_expert_added_study_is_fetched_and_flagged_for_verify(base: Path, tmp_path: Path, monkeypatch):
    data = json.loads((base / "pico_01.json").read_text(encoding="utf-8"))
    data["expert_added"] = ["10.1000/cohort-b"]
    (base / "pico_01.json").write_text(json.dumps(data), encoding="utf-8")
    g = json.loads((base / "grade_pico_01.json").read_text())
    g["body_of_evidence"] = ["@Lin2021Trial"]  # agent left the cohort out
    (base / "grade_pico_01.json").write_text(json.dumps(g))

    assert {a.citation_key for a in brief.body_of_evidence_studies(base, "pico_01")} == {"Lin2021Trial", "Huang2019Cohort"}

    fake = tmp_path / "runner.py"
    fake.write_text(
        "import sys, json\n"
        "dois = [a for i, a in enumerate(sys.argv) if sys.argv[i-1] == '--doi']\n"
        "for d in dois: print(json.dumps({'doi': d, 'status': 'ok', 'pdf': 'p', 'markdown': '/m/' + d.split('/')[-1] + '.md', 'chars': 5, 'error': ''}))\n"
    )
    monkeypatch.setattr(brief, "_RUNNER", fake)
    cfg = brief.brief_config(); cfg.journalfetcher_python = "python3"; cfg.journalfetcher_dir = tmp_path
    brief.fetch_fulltext(base, "pico_01", cfg)
    text = brief.write_verify_task(base, "pico_01").read_text(encoding="utf-8")
    assert "@Huang2019Cohort — /m/cohort-b.md" in text and "NAMED BY THE DOMAIN EXPERT" in text
    assert "@Lin2021Trial — /m/trial-a.md (5 chars, of 5 raw)\n" in text  # not flagged


def test_fulltext_cap_prefers_expert_then_citations(base: Path, tmp_path: Path, monkeypatch):
    data = json.loads((base / "pico_01.json").read_text(encoding="utf-8"))
    data["included_studies"][0]["citation_count"] = 1      # trial-a: low citations
    data["included_studies"][1]["citation_count"] = 500    # cohort-b: high citations
    data["included_studies"].append({
        "title": "Named", "authors": ["Solo, A"], "journal": "J", "year": 2020, "doi": "10.1000/named-e",
        "pmid": "5", "issn": None, "journal_quartile": "Q1", "pub_type": "RCT", "citation_count": 0, "abstract": "x"})
    data["expert_added"] = ["10.1000/named-e"]
    (base / "pico_01.json").write_text(json.dumps(data), encoding="utf-8")
    g = json.loads((base / "grade_pico_01.json").read_text())
    g["body_of_evidence"] = ["@Lin2021Trial", "@Huang2019Cohort", "@Solo2020Named"]
    (base / "grade_pico_01.json").write_text(json.dumps(g))

    fake = tmp_path / "runner.py"
    fake.write_text("import sys, json\n"
                    "for i, a in enumerate(sys.argv):\n"
                    "    if sys.argv[i-1] == '--doi': print(json.dumps({'doi': a, 'status': 'ok', 'pdf': 'p', 'markdown': 'm', 'chars': 1, 'error': ''}))\n")
    monkeypatch.setattr(brief, "_RUNNER", fake)
    cfg = brief.brief_config(); cfg.journalfetcher_python = "python3"; cfg.journalfetcher_dir = tmp_path; cfg.max_fulltext = 2
    index = brief.fetch_fulltext(base, "pico_01", cfg)
    assert [e["citation_key"] for e in index["body_of_evidence"]] == ["Solo2020Named", "Huang2019Cohort"]
    assert [e["citation_key"] for e in index["not_fetched"]] == ["Lin2021Trial"]
    text = brief.write_verify_task(base, "pico_01").read_text(encoding="utf-8")
    assert "Abstract only by design" in text and "@Lin2021Trial" in text


def _load_fetch_runner():
    path = Path(__file__).parents[1] / "scripts" / "fetch_fulltext.py"
    spec = importlib.util.spec_from_file_location("fetch_fulltext_runner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fulltext_runner_strips_references_and_caps():
    runner = _load_fetch_runner()
    text = "A" * 3000 + "\n# References\n" + "citation\n" * 300
    stripped = runner.strip_trailing_sections(text)
    assert "References" not in stripped and len(stripped) == 3000
    capped = runner.cap_markdown("x" * 1000, 100)
    assert len(capped) <= 100 and capped.endswith("[truncated at 100 chars]\n")


def test_fulltext_runner_uses_manual_pdf(tmp_path: Path, monkeypatch, capsys):
    runner = _load_fetch_runner()
    jf = tmp_path / "jf"
    jf.mkdir()
    (jf / "dlbydoi.py").write_text("# marker", encoding="utf-8")
    out = tmp_path / "out"
    pdf_dir = out / "pdf"
    pdf_dir.mkdir(parents=True)
    manual = pdf_dir / "prefix_10.1148_radiol.2019190562.pdf"
    manual.write_bytes(b"pdf")

    dlbydoi = types.ModuleType("dlbydoi")
    dlbydoi.download_one = lambda *_args: (_ for _ in ()).throw(AssertionError("download called"))
    converter = types.ModuleType("pymupdf4llm")
    converter.to_markdown = lambda *_args, **_kw: "body"
    monkeypatch.setitem(sys.modules, "dlbydoi", dlbydoi)
    monkeypatch.setitem(sys.modules, "pymupdf4llm", converter)
    monkeypatch.setattr(runner, "strip_markdown", lambda text: text)
    monkeypatch.setattr(sys, "argv", ["fetch_fulltext.py", "--journalfetcher", str(jf),
                                      "--out", str(out), "--doi", "10.1148/radiol.2019190562"])
    runner.main()
    record = json.loads(capsys.readouterr().out)
    assert record["status"] == "ok" and record["source"] == "manual"
    assert record["chars_raw"] == record["chars"] == 4
