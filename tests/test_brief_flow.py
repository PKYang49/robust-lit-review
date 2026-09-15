"""Evidence-brief flow control: the state machine, the doctor, the preview.

Nothing here touches the network or an agent. The search is monkeypatched to a
fake that writes the same ``pico_NN.json`` the real one would; the GRADE,
verify and writer agents are simulated by writing the JSON they are asked for.
What is under test is the order ``run_next`` does things in and where it stops.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from test_brief import GRADE, STUDIES, _writeup
from typer.testing import CliRunner

from litreview.cli import app
from litreview.config import Config
from litreview.models import PicoPrismaFlow
from litreview.pipeline import brief, brief_flow

PICOS = {
    "question": "X 對 Y 有幫助嗎？",
    "picos": [{
        "pico_id": "pico_01", "population": "adults", "intervention": "X",
        "comparator": "placebo", "outcome": "Y", "outcome_domain": "y",
        "claim_direction": "benefit",
        "question_text": "X 是否改善 Y？", "primary_terms": ["X"], "secondary_terms": ["Y"],
    }],
}


# ---------------------------------------------------------------------------
# Fixtures + stand-ins for the two things that leave the process
# ---------------------------------------------------------------------------


async def _fake_search(base: Path, pico, cfg: Config) -> PicoPrismaFlow:
    """Stand-in for ``search_pico``: writes the fixture study set, no network."""
    flow = PicoPrismaFlow(total_found=40, after_dedup=38, after_year_filter=30,
                          after_quality_filter=5, after_validation=5, after_crossref=2,
                          included=2)
    base.mkdir(parents=True, exist_ok=True)
    brief._save_pico(base, pico, flow, [brief._study_load(s) for s in STUDIES])
    return flow


def _agent_writes_grade(base: Path, **extra: object) -> None:
    """Simulate the GRADE (or verify) agent's output file."""
    data = dict(GRADE, body_of_evidence=["@Lin2021Trial"])
    data.update(extra)
    (base / "grade_pico_01.json").write_text(json.dumps(data), encoding="utf-8")


def _agent_writes_screen(base: Path, included: list[str] | None = None) -> None:
    """Simulate every pending screening batch result."""
    included = included or ["Lin2021Trial"]
    evidence = json.loads((base / "pico_01.evidence.json").read_text(encoding="utf-8"))
    studies = evidence["included_studies"]
    for index, start in enumerate(range(0, len(studies), 10), 1):
        decisions = []
        for study in studies[start:start + 10]:
            key = study["citation_key"]
            decisions.append({"citation_key": key, "include": key in included,
                              "reason": "match" if key in included else "wrong outcome",
                              "design": study.get("pub_type", "")})
        (base / f"screen_pico_01_{index:02d}.json").write_text(
            json.dumps({"pico_id": "pico_01", "batch": index, "decisions": decisions}),
            encoding="utf-8",
        )


def _fake_runner(tmp_path: Path) -> Path:
    """A JournalFetcher runner that succeeds for every DOI it is given."""
    path = tmp_path / "runner.py"
    path.write_text(
        "import sys, json\n"
        "for i, a in enumerate(sys.argv):\n"
        "    if sys.argv[i-1] == '--doi':\n"
        "        print(json.dumps({'doi': a, 'status': 'ok', 'pdf': 'p', 'markdown': '/m/x.md', 'chars': 9, 'error': ''}))\n"
    )
    return path


@pytest.fixture
def fresh(tmp_path: Path) -> Path:
    """A brief with nothing but an approved picos.json."""
    b = tmp_path / "demo"
    b.mkdir()
    (b / "picos.json").write_text(json.dumps(PICOS, ensure_ascii=False), encoding="utf-8")
    return b


@pytest.fixture
def cfg_no_fulltext(tmp_path: Path) -> Config:
    """Config whose JournalFetcher path does not exist -> abstract-level brief."""
    cfg = brief.brief_config()
    cfg.journalfetcher_dir = tmp_path / "no-journalfetcher"
    cfg.pubmed_email = "test@example.com"   # doctor checks it; do not depend on .env
    return cfg


@pytest.fixture
def searched(fresh: Path, cfg_no_fulltext: Config, monkeypatch) -> Path:
    """A brief past the search and the expert checkpoint."""
    monkeypatch.setattr(brief_flow, "search_pico", _fake_search)
    return fresh


# ---------------------------------------------------------------------------
# next
# ---------------------------------------------------------------------------


async def test_run_next_searches_then_stops_at_the_expert_checkpoint(searched: Path, cfg_no_fulltext: Config):
    result = await brief_flow.run_next(searched, cfg_no_fulltext)

    assert result.status == "checkpoint"
    assert result.actions == ["search pico_01: found=40 Q1=5 citation=+0 -> 2 included"]
    assert result.dispatch == []
    assert (searched / "pico_01.json").exists()
    assert [s.stage for s in result.states] == ["screen"]


async def test_checkpoint_unblocks_grade_then_write_then_render(searched: Path, cfg_no_fulltext: Config):
    await brief_flow.run_next(searched, cfg_no_fulltext)
    assert not brief_flow.checkpoint_done(searched)
    brief_flow.record_checkpoint(searched, "沒有漏掉的")
    assert brief_flow.checkpoint_done(searched)

    result = await brief_flow.run_next(searched, cfg_no_fulltext)
    assert result.status == "dispatch"
    assert [(d.kind, d.pico_id, d.model) for d in result.dispatch] == [("Screen", "pico_01", "haiku")]
    _agent_writes_screen(searched)

    result = await brief_flow.run_next(searched, cfg_no_fulltext)
    assert result.status == "dispatch"
    assert [(d.kind, d.pico_id) for d in result.dispatch] == [("GRADE", "pico_01")]
    task = searched / "tasks" / "grade_pico_01.md"
    assert task.exists()
    assert result.dispatch[0].prompt == f"Read {task.resolve()} and do exactly what it says."

    _agent_writes_grade(searched)
    result = await brief_flow.run_next(searched, cfg_no_fulltext)
    assert result.status == "dispatch"
    assert [(d.kind, d.pico_id) for d in result.dispatch] == [("Write", "pico_01")]
    assert (searched / "tasks" / "write_pico_01.md").exists()
    # full text is off, so the fulltext/verify stages are skipped entirely
    assert not (searched / "pico_01.fulltext.json").exists()

    (searched / "write_pico_01.json").write_text(json.dumps(_writeup(), ensure_ascii=False), encoding="utf-8")
    result = await brief_flow.run_next(searched, cfg_no_fulltext)
    assert result.status == "rendered"
    assert result.rendered == searched / "brief.html"
    assert (searched / "brief.html").exists()

    result = await brief_flow.run_next(searched, cfg_no_fulltext)
    assert result.status == "done"
    assert result.actions == []
    assert [s.stage for s in result.states] == ["done"]


async def test_a_changed_study_set_makes_the_grade_stale_and_redispatches(searched: Path, cfg_no_fulltext: Config):
    await brief_flow.run_next(searched, cfg_no_fulltext)
    brief_flow.record_checkpoint(searched)
    await brief_flow.run_next(searched, cfg_no_fulltext)  # screen dispatch
    _agent_writes_screen(searched)
    await brief_flow.run_next(searched, cfg_no_fulltext)  # grade dispatch
    _agent_writes_grade(searched)

    _q, picos = brief.load_brief(searched)
    assert brief_flow.pico_state(searched, picos[0], False).stage == "write"

    # What `brief add` does: grow and rewrite pico_01.json after screening/GRADE.
    data = json.loads((searched / "pico_01.json").read_text(encoding="utf-8"))
    extra = dict(STUDIES[1], title="Registry C", doi="10.1000/cohort-c", pmid="3")
    data["included_studies"].append(extra)
    (searched / "pico_01.json").write_text(json.dumps(data), encoding="utf-8")
    newer = (searched / "grade_pico_01.json").stat().st_mtime + 2
    os.utime(searched / "pico_01.json", (newer, newer))

    state = brief_flow.pico_state(searched, picos[0], False)
    assert state.grade_stale and not state.graded and not state.screened and state.stage == "screen"

    result = await brief_flow.run_next(searched, cfg_no_fulltext)
    assert result.status == "dispatch"
    assert [(d.kind, d.pico_id, d.model) for d in result.dispatch] == [("Screen", "pico_01", "haiku")]
    assert result.actions == ["screen tasks pico_01: 1 batch(es)"]


async def test_fulltext_is_fetched_then_verify_then_write(fresh: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(brief_flow, "search_pico", _fake_search)
    monkeypatch.setattr(brief_flow, "fulltext_available", lambda _cfg: True)
    monkeypatch.setattr(brief, "_RUNNER", _fake_runner(tmp_path))
    cfg = brief.brief_config()
    cfg.journalfetcher_python = "python3"
    cfg.journalfetcher_dir = tmp_path

    await brief_flow.run_next(fresh, cfg)
    brief_flow.record_checkpoint(fresh)
    await brief_flow.run_next(fresh, cfg)          # -> Screen
    _agent_writes_screen(fresh)
    await brief_flow.run_next(fresh, cfg)          # -> GRADE
    _agent_writes_grade(fresh)

    result = await brief_flow.run_next(fresh, cfg)
    assert result.status == "dispatch"
    assert "fulltext pico_01: 1/1 retrieved" in result.actions
    assert [(d.kind, d.pico_id) for d in result.dispatch] == [("Verify", "pico_01")]
    assert (fresh / "tasks" / "verify_pico_01.md").exists()
    assert (fresh / "grade_pico_01.abstract.json").exists()

    # The verify agent overwrites the GRADE with the full-text evidence.
    _agent_writes_grade(fresh, fulltext_verified=["@Lin2021Trial"],
                        rob_notes={"@Lin2021Trial": "central randomisation; ITT"})
    result = await brief_flow.run_next(fresh, cfg)
    assert result.status == "dispatch"
    assert [(d.kind, d.pico_id) for d in result.dispatch] == [("Write", "pico_01")]
    assert result.actions == ["write task pico_01"]      # no second fetch

    state = result.states[0]
    assert (state.fulltext_ok, state.fulltext_total, state.verified) == (1, 1, True)


async def test_manual_pdf_makes_failed_fulltext_index_stale(searched: Path, cfg_no_fulltext: Config):
    await brief_flow.run_next(searched, cfg_no_fulltext)
    _agent_writes_grade(searched)  # legacy GRADE: no screen file required
    (searched / "pico_01.fulltext.json").write_text(json.dumps({
        "body_of_evidence": [{"citation_key": "Lin2021Trial", "doi": "10.1000/trial-a",
                              "status": "failed", "chars": 0, "error": "paywall"}],
        "not_fetched": [],
    }), encoding="utf-8")
    _q, picos = brief.load_brief(searched)
    assert brief_flow.pico_state(searched, picos[0], True).fulltext_fresh
    pdf_dir = searched / "fulltext" / "pdf"
    pdf_dir.mkdir(parents=True)
    (pdf_dir / "my_10.1000_trial-a.pdf").write_bytes(b"pdf")
    assert not brief_flow.pico_state(searched, picos[0], True).fulltext_fresh


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _checks_by_name(checks: list[brief_flow.Check]) -> dict[str, brief_flow.Check]:
    return {c.name: c for c in checks}


def test_doctor_passes_a_well_formed_brief(fresh: Path, cfg_no_fulltext: Config):
    checks = _checks_by_name(brief_flow.run_doctor(fresh, cfg_no_fulltext))
    assert checks["picos.json"].ok
    assert checks["pico_01 contract"].ok and checks["pico_01 contract"].detail == "ok"
    assert not [c for c in brief_flow.run_doctor(fresh, cfg_no_fulltext) if not c.ok and not c.warn]


def test_doctor_fails_a_broken_picos_contract(tmp_path: Path, cfg_no_fulltext: Config):
    b = tmp_path / "bad"
    b.mkdir()
    pico = dict(PICOS["picos"][0],
                claim_direction="maybe",
                outcome="major adverse cardiovascular events at twelve months",  # prose: allowed, not a term
                secondary_terms=["major adverse cardiovascular events at twelve months"])  # 7-word term: flagged
    (b / "picos.json").write_text(json.dumps({"question": "q", "picos": [pico]}, ensure_ascii=False), encoding="utf-8")

    contract = _checks_by_name(brief_flow.run_doctor(b, cfg_no_fulltext))["pico_01 contract"]
    assert not contract.ok and not contract.warn
    assert "claim_direction='maybe'" in contract.detail
    assert "phrase-length terms unlikely to match" in contract.detail


def test_doctor_fails_when_an_expert_addition_never_reaches_the_agents(searched: Path, cfg_no_fulltext: Config):
    import asyncio
    asyncio.run(brief_flow.run_next(searched, cfg_no_fulltext))
    data = json.loads((searched / "pico_01.json").read_text(encoding="utf-8"))
    data["expert_added"] = ["10.9999/never-added"]
    (searched / "pico_01.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    check = _checks_by_name(brief_flow.run_doctor(searched, cfg_no_fulltext))["pico_01 expert additions visible"]
    assert not check.ok and check.detail == "10.9999/never-added"

    data["expert_added"] = ["10.1000/trial-a"]  # a study that is in the view
    (searched / "pico_01.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    check = _checks_by_name(brief_flow.run_doctor(searched, cfg_no_fulltext))["pico_01 expert additions visible"]
    assert check.ok


def test_doctor_warns_with_manual_pdf_drop_path(searched: Path, cfg_no_fulltext: Config):
    import asyncio
    asyncio.run(brief_flow.run_next(searched, cfg_no_fulltext))
    (searched / "pico_01.fulltext.json").write_text(json.dumps({
        "body_of_evidence": [{"citation_key": "Lin2021Trial", "doi": "10.1000/trial-a",
                              "status": "failed", "chars": 0, "error": "paywall"}]
    }), encoding="utf-8")
    checks = brief_flow.run_doctor(searched, cfg_no_fulltext)
    warning = next(c for c in checks if c.name == "pico_01 manual PDF Lin2021Trial")
    assert warning.warn and warning.detail.endswith("fulltext/pdf/10.1000_trial-a.pdf")


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------


async def test_preview_counts_the_query_the_sweep_and_every_term(fresh: Path, cfg_no_fulltext: Config, monkeypatch):
    asked: list[str] = []

    async def fake_count(self, query: str, date_from: int | None = None, date_to: int | None = None) -> int:
        asked.append(query)
        assert date_from == cfg_no_fulltext.min_year
        if "[pt]" in query:
            return 40
        if query.startswith("("):
            return 500
        return {'"X"': 1200, '"Y"': 0}[query]

    monkeypatch.setattr(brief_flow.PubMedClient, "count", fake_count)
    rows = await brief_flow.run_preview(fresh, cfg_no_fulltext)

    assert len(rows) == 1
    row = rows[0]
    assert row["pico_id"] == "pico_01"
    assert (row["query"], row["design_sweep"]) == (500, 40)
    # the outcome repeats in secondary_terms; it is counted once
    assert row["terms"] == [
        {"group": "intervention", "term": "X", "hits": 1200},
        {"group": "outcome", "term": "Y", "hits": 0},
    ]
    assert asked.count('"Y"') == 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_reports_a_missing_brief_as_one_line_not_a_traceback(tmp_path: Path):
    result = CliRunner().invoke(app, ["brief", "status", "nope", "-o", str(tmp_path)],
                                env={"COLUMNS": "200"})
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "picos.json not found" in result.output


def test_cli_next_prints_the_dispatch_block(searched: Path, monkeypatch):
    monkeypatch.setattr(brief_flow, "fulltext_available", lambda _cfg: False)
    brief_flow.record_checkpoint(searched)
    result = CliRunner().invoke(app, ["brief", "next", searched.name, "-o", str(searched.parent)],
                                env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "dispatch 1 agent(s) in ONE message:" in result.output
    assert "Screen pico_01   model=haiku" in result.output
    assert f"Read {(searched / 'tasks' / 'screen_pico_01_01.md').resolve()}" in result.output
    assert "Screen" in result.output  # status table includes the screen column


def test_cli_gaps_ranks_by_citations(searched: Path):
    (searched / "pico_01.excluded.json").write_text(json.dumps([
        {"year": 2022, "citation_count": 2, "reason": "quartile Q2", "journal": "B", "title": "low"},
        {"year": 2020, "citation_count": 200, "reason": "year<min", "journal": "A", "title": "high"},
    ]), encoding="utf-8")
    result = CliRunner().invoke(app, ["brief", "gaps", searched.name, "-o", str(searched.parent)],
                                env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert result.output.index("high") < result.output.index("low")
    assert "did not pass the deterministic gates" in result.output
