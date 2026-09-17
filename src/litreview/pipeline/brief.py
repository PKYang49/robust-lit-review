"""Evidence brief: one question -> PubMed-only PICO search -> GRADE -> one page.

The token-lean sibling of the claim appraisal. Everything deterministic runs
here; the two LLM steps (GRADE, writing) are dispatched by the
``/evidence-brief`` SKILL through the same generate-task -> collect contract
the rest of the pipeline uses. Deliberately absent: Scopus/Embase, the PRISMA
27-item audit loop, adversarial verify, OpenEvidence, deploy.

Layout under ``output/<slug>/``::

  picos.json           {"question": ..., "picos": [PICOQuestion, ...]}  (SKILL writes)
  pico_NN.json         included studies + PRISMA flow                   (search)
  tasks/grade_NN.md    prompt for the GRADE agent                       (grade-tasks)
  grade_pico_NN.json   the GRADE agent's output
  tasks/write_NN.md    prompt for the writer agent                      (write-tasks)
  write_pico_NN.json   the writer's output
  brief.html           the page                                         (render)
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from litreview.config import Config, get_config
from litreview.models import (
    ArticleMetadata,
    GradeAssessment,
    PicoPrismaFlow,
    PICOQuestion,
    PicoResult,
    Verdict,
)
from litreview.pipeline import grade_judge
from litreview.pipeline.claim_orchestrator import ClaimAppraisalPipeline
from litreview.pipeline.enrichment import enrich_articles
from litreview.pipeline.verdict_builder import assemble_verdict, derive_overall
from litreview.utils.llm import parse_json_result

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "templates" / "brief"
_DOI_TOKEN = re.compile(r"\[doi:\s*([^\]\s]+)\s*\]", re.IGNORECASE)

# Fields persisted per study. Model field names, so the JSON reloads with
# ArticleMetadata(**study) and stays small enough for an agent to read whole.
_STUDY_FIELDS = (
    "title", "authors", "journal", "year", "doi", "pmid", "issn",
    "journal_quartile", "pub_type", "citation_count", "abstract",
)

VERDICT_ZH = {"supported": "證據支持", "uncertain": "證據不足", "refuted": "證據不支持"}
CERTAINTY_ZH = {"high": "高", "moderate": "中", "low": "低", "very_low": "極低"}
EFFECT_ZH = {"beneficial": "有益", "no_effect": "無效果", "harmful": "有害", "mixed": "結果不一致"}
_CERTAINTY_PIPS = {"very_low": 1, "low": 2, "moderate": 3, "high": 4}


# ---------------------------------------------------------------------------
# Config + IO
# ---------------------------------------------------------------------------


def brief_config(max_results: int = 50, min_year: int = 2000) -> Config:
    """PubMed-only configuration with the appraisal's hard gates."""
    cfg = get_config()
    # Scopus contributes one most-cited pass (only if a key is configured);
    # PubMed does everything else and back-fills Scopus records.
    cfg.databases = ["pubmed", "scopus"]
    cfg.max_results_per_db = max_results
    cfg.min_year = min_year
    # A question is not a claim appraisal: an unranked journal is usually a
    # specialty title missing from the ranking table, not a bad one. Abstract
    # screening is the real filter, so unranked journals are admitted and
    # reported separately rather than dropped unseen.
    cfg.strict_quartile = False
    cfg.min_quartile = "Q1"
    return cfg


def load_brief(base: Path) -> tuple[str, list[PICOQuestion]]:
    """Read ``picos.json``: the question and its approved PICO sub-questions."""
    path = base / "picos.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — the SKILL writes it after PICO approval")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "picos" not in data:
        raise ValueError(f'{path} must be {{"question": ..., "picos": [...]}}')
    picos = [PICOQuestion(**p) for p in data["picos"]]
    for i, p in enumerate(picos, 1):
        if not p.pico_id:
            p.pico_id = f"pico_{i:02d}"
    return str(data.get("question", "")), picos


def _pico_path(base: Path, pico_id: str) -> Path:
    return base / f"{pico_id}.json"


def _study_dump(a: ArticleMetadata) -> dict:
    d = {k: getattr(a, k) for k in _STUDY_FIELDS}
    d["citation_key"] = a.citation_key
    return d


def _study_load(d: dict) -> ArticleMetadata:
    values = {k: v for k, v in d.items() if k in ArticleMetadata.model_fields}
    # ``citation_key`` is a derived field in ArticleMetadata, but evidence
    # batches need a stable key even when two papers share author/year/title.
    if isinstance(d.get("citation_key"), str) and d["citation_key"].strip():
        values["citation_key_override"] = d["citation_key"].strip()
    return ArticleMetadata(**values)


def _ensure_unique_citation_keys(studies: list[ArticleMetadata]) -> None:
    """Disambiguate derived keys deterministically within one PICO."""
    used: set[str] = set()
    for index, article in enumerate(studies, 1):
        key = article.citation_key
        if key not in used:
            used.add(key)
            continue
        identity = article.pmid or _norm_doi(article.doi or "")
        suffix = re.sub(r"[^A-Za-z0-9]", "", str(identity))[-10:] or str(index)
        candidate = f"{key}{suffix}"
        serial = 2
        while candidate in used:
            candidate = f"{key}{suffix}{serial}"
            serial += 1
        article.citation_key_override = candidate
        used.add(candidate)


def load_pico_result(base: Path, pico_id: str) -> tuple[PICOQuestion, PicoPrismaFlow, list[ArticleMetadata]]:
    path = _pico_path(base, pico_id)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — run `lit-review brief search` first")
    data = json.loads(path.read_text(encoding="utf-8"))
    studies = [_study_load(s) for s in data["included_studies"]]
    _ensure_unique_citation_keys(studies)
    return (PICOQuestion(**data["pico"]), PicoPrismaFlow(**data["prisma"]), studies)


# ---------------------------------------------------------------------------
# Stage 1: search (deterministic, no LLM)
# ---------------------------------------------------------------------------


async def search_pico(base: Path, pico: PICOQuestion, cfg: Config) -> PicoPrismaFlow:
    """PubMed -> dedup -> year -> Q1 strict -> DOI -> CrossRef, saved to pico_NN.json."""
    dropped: list[tuple[ArticleMetadata, str]] = []
    async with ClaimAppraisalPipeline(cfg) as pipe:
        included, flow = await pipe.run_pico_search(pico, dropped_out=dropped)
    base.mkdir(parents=True, exist_ok=True)
    _save_pico(base, pico, flow, included)
    excluded = [
        {
            "title": a.title,
            "journal": a.journal,
            "year": a.year,
            "doi": a.doi,
            "pmid": a.pmid,
            "citation_count": a.citation_count,
            "pub_type": a.pub_type,
            "reason": reason,
        }
        for a, reason in dropped
    ]
    (base / f"{pico.pico_id}.excluded.json").write_text(
        json.dumps(excluded, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return flow


def _save_pico(base: Path, pico: PICOQuestion, flow: PicoPrismaFlow, studies: list[ArticleMetadata]) -> None:
    payload = {
        "pico": pico.model_dump(),
        "prisma": flow.model_dump(),
        "included_studies": [_study_dump(a) for a in studies],
    }
    _pico_path(base, pico.pico_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


EVIDENCE_TYPES = ("RCT", "Meta-Analysis", "Systematic Review")

# What the GRADE and writer agents are shown. Narrative reviews, guidelines
# and commentary are kept in the included set (they passed every gate and may
# be cited) but are not primary evidence and would only inflate the agents'
# reading; the Scopus most-cited pass returns many of them.
AGENT_VIEW_TYPES = (
    "RCT", "Phase IV Trial", "Phase III Trial", "Phase II Trial", "Phase I Trial", "Trial",
    "Meta-Analysis", "Systematic Review", "Observational", "Original", "",
)


def write_evidence_view(base: Path, pico_id: str) -> tuple[Path, int, int]:
    """Write ``pico_NN.evidence.json`` — the agent-facing subset — and return (path, shown, total)."""
    pico, _flow, studies = load_pico_result(base, pico_id)
    expert = set(json.loads(_pico_path(base, pico_id).read_text(encoding="utf-8")).get("expert_added", []))
    shown = [a for a in studies if a.pub_type in AGENT_VIEW_TYPES or _norm_doi(a.doi or "") in expert]
    payload = {
        "pico": pico.model_dump(),
        "note": (f"{len(shown)} of {len(studies)} included studies; narrative reviews, guidelines "
                 f"and commentary are omitted here but remain in {_pico_path(base, pico_id).name}."),
        "included_studies": [_study_dump(a) for a in shown],
    }
    path = base / f"{pico_id}.evidence.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, len(shown), len(studies)


def list_evidence(base: Path, pico_id: str) -> list[ArticleMetadata]:
    """Included trials and syntheses, newest first — what a domain expert scans for gaps."""
    _pico, _flow, studies = load_pico_result(base, pico_id)
    picked = [a for a in studies if a.pub_type in EVIDENCE_TYPES]
    return sorted(picked, key=lambda a: (-(a.year or 0), a.pub_type))


def load_gaps(base: Path, pico_id: str, limit: int = 10) -> list[dict]:
    """Load gate exclusions ranked by citation count, highest first."""
    path = base / f"{pico_id}.excluded.json"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — this brief predates persisted gate exclusions; re-run search to create it"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise TypeError(f"{path} must contain a JSON list")
    return sorted(
        (row for row in data if isinstance(row, dict)),
        key=lambda row: (-(int(row.get("citation_count") or 0)), -(int(row.get("year") or 0))),
    )[:limit]


async def add_studies(
    base: Path, pico_id: str, identifiers: list[str], cfg: Config
) -> tuple[list[ArticleMetadata], dict[str, str]]:
    """Automatic expert review: add PMIDs/DOIs through the same gates; PRISMA counts them separately."""
    existing_payload = json.loads(_pico_path(base, pico_id).read_text(encoding="utf-8"))
    existing_expert = set(existing_payload.get("expert_added", []))
    pico, flow, studies = load_pico_result(base, pico_id)
    async with ClaimAppraisalPipeline(cfg) as pipe:
        added, rejected = await pipe.add_expert_studies(pico_id, identifiers, studies)
    flow.identified_by_expert += len(identifiers)
    flow.included_by_expert += len(added)
    studies.extend(added)
    flow.included = len(studies)
    _save_pico(base, pico, flow, studies)
    # Remember what the expert named: it always reaches the agents, whatever
    # PubMed's publication type says (a Circulation Research Letter is "Letter").
    path = _pico_path(base, pico_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["expert_added"] = sorted(existing_expert | {_norm_doi(a.doi) for a in added if a.doi})
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return added, rejected


# ---------------------------------------------------------------------------
# Stage 2: abstract screening (LLM, Haiku batches), then GRADE
# ---------------------------------------------------------------------------


def _screen_path(base: Path, pico_id: str) -> Path:
    return base / f"{pico_id}.screen.json"


def write_screen_tasks(base: Path, pico_id: str, batch_size: int = 10) -> list[Path]:
    """Write Haiku screening tasks in batches of at most ``batch_size`` studies."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    evidence_path, _shown, _total = write_evidence_view(base, pico_id)
    data = json.loads(evidence_path.read_text(encoding="utf-8"))
    pico = data["pico"]
    studies = data["included_studies"]
    tasks_dir = base / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, start in enumerate(range(0, len(studies), batch_size), 1):
        batch = studies[start:start + batch_size]
        output_path = (base / f"screen_{pico_id}_{index:02d}.json").resolve()
        lines = []
        for study in batch:
            lines.append(
                "\n".join(
                    [
                        f"citation_key: {study['citation_key']}",
                        f"title: {study.get('title', '')}",
                        f"year: {study.get('year') or ''}",
                        f"pub_type: {study.get('pub_type', '')}",
                        f"journal: {study.get('journal', '')}",
                        f"abstract: {study.get('abstract', '')}",
                    ]
                )
            )
        study_text = "\n\n".join(lines) or "(none)"
        prompt = f"""Screen this batch for the body of evidence.

PICO ID: {pico_id}
Population: {pico.get('population', '')}
Intervention: {pico.get('intervention', '')}
Comparator: {pico.get('comparator', '')}
Outcome: {pico.get('outcome', '')}
Claim direction: {pico.get('claim_direction', '')}

Include only human studies for which population, intervention, comparator, and outcome all match the PICO. Include primary data or a synthesis. Exclude narrative reviews, editorials, and guidelines.

Studies:
{study_text}

Return ONLY JSON with this schema and write it to {output_path}:
{{"pico_id":"{pico_id}","batch":{index},"decisions":[{{"citation_key":"...","include":true,"reason":"one line","design":"RCT|cohort|cross-sectional|meta-analysis|..."}}]}}
"""
        path = tasks_dir / f"screen_{pico_id}_{index:02d}.md"
        path.write_text(prompt, encoding="utf-8")
        paths.append(path)
    return paths


def collect_screen(base: Path, pico_id: str) -> dict:
    """Merge screening batches and make expert-named studies authoritative includes."""
    evidence_path = base / f"{pico_id}.evidence.json"
    if not evidence_path.exists():
        write_evidence_view(base, pico_id)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    studies = evidence.get("included_studies", [])
    expected = [s["citation_key"] for s in studies]
    # Every task in the active generation is newer than the evidence view,
    # which was rewritten immediately before the tasks. This excludes obsolete
    # batches left by an older run with a different study count/batch size.
    task_paths = [
        path for path in sorted((base / "tasks").glob(f"screen_{pico_id}_[0-9][0-9].md"))
        if path.stat().st_mtime >= evidence_path.stat().st_mtime
    ]
    if studies and not task_paths:
        raise FileNotFoundError(f"no screening tasks found for {pico_id} — run `lit-review brief screen-tasks` first")

    decisions: dict[str, dict] = {}
    for task_path in task_paths:
        result_path = base / f"{task_path.stem}.json"
        if not result_path.exists():
            raise FileNotFoundError(f"missing screening batch result: {result_path}")
        if result_path.stat().st_mtime < task_path.stat().st_mtime:
            raise FileNotFoundError(f"stale screening batch result (re-dispatch required): {result_path}")
        raw = parse_json_result(result_path)
        if raw is None or not isinstance(raw.get("decisions"), list):
            raise ValueError(f"invalid screening batch result: {result_path}")
        for decision in raw["decisions"]:
            if isinstance(decision, dict) and decision.get("citation_key") in expected:
                decisions[str(decision["citation_key"])] = decision

    expert = expert_added_dois(base, pico_id)
    expert_keys = {
        s["citation_key"] for s in studies if _norm_doi(str(s.get("doi") or "")) in expert
    }
    missing = [key for key in expected if key not in decisions and key not in expert_keys]
    if missing:
        raise ValueError(f"screening decisions missing {len(missing)} study/studies: {', '.join(missing)}")

    included: list[str] = []
    excluded: list[dict] = []
    designs: dict[str, str] = {}
    for key in expected:
        decision = decisions.get(key, {})
        if key in expert_keys:
            decision = dict(decision, include=True, reason="named by domain expert")
        designs[key] = str(decision.get("design", ""))
        if decision.get("include") is True:
            included.append(key)
        else:
            excluded.append({"citation_key": key, "reason": str(decision.get("reason", ""))})
    result = {"pico_id": pico_id, "included": included, "excluded": excluded, "designs": designs}
    _screen_path(base, pico_id).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def write_grade_task(base: Path, pico_id: str) -> Path:
    """Write the GRADE prompt to ``tasks/grade_NN.md``; the agent reads it by path.

    Reuses ``grade_judge.generate_grade_task`` (so ``collect_grade`` parses the
    result and recomputes certainty). Regex extraction supplies the numbers on
    each study line at zero token cost; the full abstracts are referenced by
    file rather than inlined, which keeps the dispatching prompt to one line.
    """
    pico, _flow, _all = load_pico_result(base, pico_id)
    evidence_path, _shown, _total = write_evidence_view(base, pico_id)
    view = json.loads(evidence_path.read_text(encoding="utf-8"))
    source_path = evidence_path
    screen_path = _screen_path(base, pico_id)
    if screen_path.exists():
        screen = json.loads(screen_path.read_text(encoding="utf-8"))
        wanted = set(screen.get("included", []))
        view = {
            "pico": view["pico"],
            "note": f"{len(wanted)} studies included by abstract screening.",
            "included_studies": [s for s in view["included_studies"] if s["citation_key"] in wanted],
        }
        source_path = base / f"{pico_id}.body.json"
        source_path.write_text(json.dumps(view, ensure_ascii=False, indent=2), encoding="utf-8")
    studies = [_study_load(d) for d in view["included_studies"]]
    extracted = {a.citation_key: data for a, data in enrich_articles(studies)}
    task = grade_judge.generate_grade_task(pico, studies, extracted, base)
    prompt = (
        f"{task.prompt}\n\n"
        f"Full abstracts, designs (pub_type) and journals for every study listed above "
        f"are in {source_path.resolve()} — read that file before rating. Count n_studies and n_rct "
        f"from it. Do not cite anything not in that file."
    )
    tasks_dir = base / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    path = tasks_dir / f"grade_{pico_id}.md"
    path.write_text(prompt, encoding="utf-8")
    return path


def collect_verdict(base: Path, pico_id: str) -> tuple[GradeAssessment, Verdict]:
    """Parse the GRADE output and derive the verdict deterministically."""
    pico, _flow, _studies = load_pico_result(base, pico_id)
    grade_path = base / f"grade_{pico_id}.json"
    if not grade_path.exists():
        raise FileNotFoundError(f"{grade_path} not found — the GRADE agent has not written it")
    authoritative_body = None
    screen_path = _screen_path(base, pico_id)
    if screen_path.exists():
        screen = json.loads(screen_path.read_text(encoding="utf-8"))
        authoritative_body = [str(k) for k in screen.get("included", [])]
    grade = grade_judge.collect_grade(pico, base, authoritative_body=authoritative_body)
    return grade, assemble_verdict(grade, crosscheck=None, claim_direction=pico.claim_direction)


# ---------------------------------------------------------------------------
# Stage 2b: full text for the body of evidence (JournalFetcher), then a
#           verification pass by the GRADE agent
# ---------------------------------------------------------------------------

_RUNNER = Path(__file__).resolve().parents[3] / "scripts" / "fetch_fulltext.py"


def _fulltext_index_path(base: Path, pico_id: str) -> Path:
    return base / f"{pico_id}.fulltext.json"


def load_fulltext_index(base: Path, pico_id: str) -> dict | None:
    path = _fulltext_index_path(base, pico_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def doi_filename_token(doi: str) -> str:
    """Filename-safe DOI token understood by the full-text runner."""
    return _norm_doi(doi).replace("/", "_")


def find_manual_pdf(pdf_dir: Path, doi: str) -> Path | None:
    """Find a manually dropped PDF for ``doi`` without importing the runner."""
    token = doi_filename_token(doi)
    if not token or not pdf_dir.exists():
        return None
    return next(
        (path for path in sorted(pdf_dir.glob("*.pdf")) if token in path.name.lower()),
        None,
    )


def expert_added_dois(base: Path, pico_id: str) -> set[str]:
    return set(json.loads(_pico_path(base, pico_id).read_text(encoding="utf-8")).get("expert_added", []))


def body_of_evidence_studies(base: Path, pico_id: str) -> list[ArticleMetadata]:
    """The studies the GRADE agent circled, plus anything the expert named.

    An expert-named study is fetched even when the abstract-level GRADE set it
    aside: research letters and some cohort reports have no PubMed abstract,
    so the agent had nothing to judge it on. The full text settles it.
    """
    _pico, _flow, studies = load_pico_result(base, pico_id)
    grade, _verdict = collect_verdict(base, pico_id)
    wanted = {k.lstrip("@") for k in grade.body_of_evidence}
    expert = expert_added_dois(base, pico_id)
    return [a for a in studies if a.citation_key in wanted or _norm_doi(a.doi or "") in expert]


def fetch_fulltext(base: Path, pico_id: str, cfg: Config) -> dict:
    """Download + convert full text for the body of evidence; write ``pico_NN.fulltext.json``.

    Only the body of evidence is fetched — a full RCT is ~10k tokens and the
    included set can hold fifty studies; the verification pass reads what the
    estimate rests on, not everything the search returned.
    """
    studies = body_of_evidence_studies(base, pico_id)
    if not studies:
        raise ValueError(f"{pico_id}: GRADE result names no body of evidence — run the GRADE agent first")
    # A full RCT is ~10k tokens; a 16-study body of evidence would cost the
    # verify agent more than the rest of the brief combined. Take what the
    # expert named, then the most-cited (landmark-ness), up to the cap; the
    # rest stay abstract-rated and the task file says so.
    expert = expert_added_dois(base, pico_id)
    studies.sort(key=lambda a: (0 if _norm_doi(a.doi or "") in expert else 1, -a.citation_count, -(a.year or 0)))
    skipped = studies[cfg.max_fulltext:]
    studies = studies[: cfg.max_fulltext]
    dois = [a.doi for a in studies if a.doi]
    out_dir = base / "fulltext"
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [cfg.journalfetcher_python, str(_RUNNER), "--journalfetcher", str(cfg.journalfetcher_dir),
           "--out", str(out_dir.resolve()), "--max-chars", str(cfg.fulltext_max_chars)]
    for d in dois:
        cmd += ["--doi", d]
    logger.info("Fetching full text for %d studies via %s", len(dois), cfg.journalfetcher_dir)
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=cfg.fulltext_timeout * max(1, len(dois)), check=False,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(f"fetch_fulltext runner failed:\n{proc.stderr[-2000:]}")

    results: dict[str, dict] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            rec = json.loads(line)
            results[_norm_doi(rec["doi"])] = rec

    entries = []
    for a in studies:
        rec = results.get(_norm_doi(a.doi or ""), {})
        entries.append({
            "citation_key": a.citation_key,
            "doi": a.doi,
            "title": a.title,
            "status": rec.get("status", "failed"),
            "markdown": rec.get("markdown"),
            "chars_raw": rec.get("chars_raw", rec.get("chars", 0)),
            "chars": rec.get("chars", 0),
            "source": rec.get("source"),
            "error": rec.get("error", "" if rec else "no DOI" if not a.doi else "no result"),
        })
    index = {
        "pico_id": pico_id,
        "body_of_evidence": entries,
        "not_fetched": [{"citation_key": a.citation_key, "doi": a.doi, "reason": f"cap {cfg.max_fulltext}"} for a in skipped],
    }
    _fulltext_index_path(base, pico_id).write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for e in entries if e["status"] == "ok")
    logger.info("%s: full text %d/%d", pico_id, ok, len(entries))
    return index


VERIFY_PROMPT_TEMPLATE = """You are the GRADE methodologist who rated this PICO from abstracts. Full texts are now available for the body of evidence. Re-assess with them.

PICO: {question_text}
Your abstract-level assessment (read it first): {grade_json}
A copy is kept at {backup}; you will OVERWRITE {grade_json}.

Full texts (markdown converted from the publisher PDF):
{fulltext_lines}
{missing_note}
Do three things, reading each full text in turn:
1. RISK OF BIAS from the Methods, per study: randomisation method, allocation concealment, blinding of participants/assessors, attrition and intention-to-treat, selective reporting vs the registered outcomes. Write one line per study under "rob_notes".
2. VERIFY the effect estimates and any subgroup numbers you relied on. Where the abstract and the full text disagree, or the abstract omitted a qualifier the full text states (e.g. a component of a composite not significant on its own), record it under "discrepancies".
3. UPDATE the five domain ratings and justifications accordingly. Other domains may change too if the full text adds information (e.g. imprecision from a wide CI on a component outcome).

Return ONLY JSON with the SAME schema as your previous output (body_of_evidence, starting_level, domains, upgrades, effect_direction, final_certainty, n_studies, n_rct, summary) plus:
  "fulltext_verified": ["@key", ...]   the studies whose full text you actually read
  "rob_notes": {{"@key": "one line", ...}}
  "discrepancies": ["...", ...]         empty list if none
Write it to: {grade_json}
"""


def write_verify_task(base: Path, pico_id: str) -> Path:
    """Write ``tasks/verify_NN.md`` once full texts exist; back up the abstract-level GRADE."""
    index = load_fulltext_index(base, pico_id)
    if index is None:
        raise FileNotFoundError(f"{_fulltext_index_path(base, pico_id)} not found — run `brief fulltext` first")
    pico, _flow, _studies = load_pico_result(base, pico_id)
    grade_json = (base / f"grade_{pico_id}.json").resolve()
    backup = (base / f"grade_{pico_id}.abstract.json").resolve()
    if not backup.exists():
        shutil.copyfile(grade_json, backup)

    have = [e for e in index["body_of_evidence"] if e["status"] == "ok"]
    missing = [e for e in index["body_of_evidence"] if e["status"] != "ok"]
    grade, _ = collect_verdict(base, pico_id)
    in_body = {k.lstrip("@") for k in grade.body_of_evidence}
    expert = expert_added_dois(base, pico_id)

    def _line(e: dict) -> str:
        tag = ""
        if e["citation_key"] not in in_body and _norm_doi(e["doi"] or "") in expert:
            tag = ("   <- NAMED BY THE DOMAIN EXPERT, not in your body of evidence (its abstract was "
                   "missing or thin). Read it; if it matches the PICO, add it to body_of_evidence "
                   "and rate it like the others.")
        return (
            f"  @{e['citation_key']} — {e['markdown']} "
            f"({e['chars']:,} chars, of {e.get('chars_raw', e['chars']):,} raw){tag}"
        )

    fulltext_lines = "\n".join(_line(e) for e in have) or "  (none)"
    missing_note = ""
    if missing:
        missing_note = ("\nNot retrievable (keep rating these from the abstract, and say so in rob_notes):\n"
                        + "\n".join(f"  @{e['citation_key']} — {e['error']}" for e in missing) + "\n")
    not_fetched = index.get("not_fetched", [])
    if not_fetched:
        missing_note += ("\nAbstract only by design (full-text cap; these are the less-cited members of the body "
                         "of evidence — keep their abstract-level rating, and put \"abstract only\" in their rob_notes):\n"
                         + "\n".join(f"  @{e['citation_key']}" for e in not_fetched) + "\n")

    prompt = VERIFY_PROMPT_TEMPLATE.format(
        question_text=pico.question_text or pico.outcome,
        grade_json=grade_json, backup=backup,
        fulltext_lines=fulltext_lines, missing_note=missing_note,
    )
    tasks_dir = base / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    path = tasks_dir / f"verify_{pico_id}.md"
    path.write_text(prompt, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Stage 3: writer task (LLM, one agent per PICO)
# ---------------------------------------------------------------------------


WRITER_PROMPT_TEMPLATE = """你是醫學實證寫作者，要把一個子問題的系統性檢索結果寫成一般人也看得懂的評讀。

子問題：{question_text}
Population: {population}
Intervention: {intervention}
Comparator: {comparator}
Outcome: {outcome}

已由 GRADE 判定（不可更改）：
  結論：{verdict_zh}（{verdict}）
  證據確定性：{certainty_zh}（{certainty}）
  效果方向：{effect_zh}
  GRADE 摘要：{grade_summary}

納入研究（{min_year} 年後、經期刊品質與 CrossRef 閘門；分級無法判定者可能保留）在這個檔案：
  {pico_json}
請先完整讀它，再開始寫。
{fulltext_note}
寫作規則：
1. 用繁體中文（台灣用語）。專有名詞可附英文。
2. 每一個有事實內容的句子都要引用，格式固定為 [doi:10.xxxx/yyyy]，DOI 必須逐字來自上面那個檔案的 "doi" 欄位。絕對不能引用檔案裡沒有的研究——渲染前會逐一比對，對不上就整段退回。
3. 優先引用人體研究；動物或體外研究只能當機轉假說，且要明說。
4. 引用具體數字：樣本數、效果量、信賴區間、p 值、追蹤時間。不要寫「許多研究」「研究顯示」這種空話。
5. 不要改上面的結論標籤。如果你認為證據其實不支持那個標籤，寫在 "dissent" 欄位，不要改寫 headline 去繞過它。
6. 若納入研究少於 3 篇，headline 要直接說明證據稀少。

只回傳下面這個 JSON（不要 markdown 圍欄、不要前言），寫到：
  {output_path}

{{
  "pico_id": "{pico_id}",
  "headline": "一句話結論，30 字內，一般人看得懂",
  "lay": "白話解釋，1-2 段，用日常語言說明證據說了什麼、有多可信、對一般人的意義。段落用空行分隔。可引用 [doi:...]。",
  "pro": "專業摘要，2-4 段，給臨床人員看：研究設計、族群、效果量、異質性、限制。每個論點都要 [doi:...]。段落用空行分隔。",
  "key_studies": [
    {{"doi": "10.xxxx/yyyy", "design": "RCT / cohort / meta-analysis ...", "n": "n=240", "finding": "一句話說這篇發現了什麼"}}
  ],
  "caveats": "限制與注意事項，1 段，可空字串",
  "dissent": "若不同意 GRADE 標籤，說明理由；否則空字串"
}}
key_studies 最多 5 篇，挑最能代表證據方向與品質的。"""


def _fulltext_note(base: Path, pico_id: str, grade: GradeAssessment) -> str:
    index = load_fulltext_index(base, pico_id)
    if not index:
        return ""
    have = [e for e in index["body_of_evidence"] if e["status"] == "ok"]
    if not have:
        return ""
    lines = "\n".join(f"  @{e['citation_key']} — {e['markdown']}" for e in have)
    extra = ""
    if grade.discrepancies:
        extra = "\n全文複核發現摘要與全文的落差（務必反映在文字裡）：\n" + "\n".join(f"  - {d}" for d in grade.discrepancies)
    return (f"\nbody of evidence 的全文（markdown）也可以讀，數字以全文為準（亞組、次要終點、安全性都在裡面）：\n{lines}\n"
            f"其餘研究只有摘要。{extra}\n")


def write_writer_task(base: Path, pico_id: str, min_year: int = 2000) -> Path:
    """Write the writer prompt to ``tasks/write_NN.md`` once the verdict is known."""
    pico, _flow, _studies = load_pico_result(base, pico_id)
    grade, verdict = collect_verdict(base, pico_id)
    evidence_path, _shown, _total = write_evidence_view(base, pico_id)
    output_path = (base / f"write_{pico_id}.json").resolve()
    prompt = WRITER_PROMPT_TEMPLATE.format(
        question_text=pico.question_text or pico.outcome,
        population=pico.population,
        intervention=pico.intervention,
        comparator=pico.comparator,
        outcome=pico.outcome,
        verdict=verdict.verdict,
        verdict_zh=VERDICT_ZH.get(verdict.verdict, verdict.verdict),
        certainty=verdict.confidence,
        certainty_zh=CERTAINTY_ZH.get(verdict.confidence, verdict.confidence),
        effect_zh=EFFECT_ZH.get(grade.effect_direction, grade.effect_direction),
        grade_summary=grade.summary or "（無）",
        min_year=min_year,
        pico_json=evidence_path.resolve(),
        output_path=output_path,
        pico_id=pico_id,
        fulltext_note=_fulltext_note(base, pico_id, grade),
    )
    tasks_dir = base / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    path = tasks_dir / f"write_{pico_id}.md"
    path.write_text(prompt, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Stage 4: citation whitelist check (deterministic — replaces the audit loop)
# ---------------------------------------------------------------------------


def _norm_doi(doi: str) -> str:
    doi = doi.strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    return doi.rstrip(".,;)")


def cited_dois(writeup: dict) -> list[str]:
    """Every DOI the writer referenced, in order of first appearance."""
    seen: list[str] = []
    text = "\n".join(str(writeup.get(k, "")) for k in ("headline", "lay", "pro", "caveats", "dissent"))
    for m in _DOI_TOKEN.finditer(text):
        d = _norm_doi(m.group(1))
        if d not in seen:
            seen.append(d)
    for ks in writeup.get("key_studies", []) or []:
        d = _norm_doi(str(ks.get("doi", "")))
        if d and d not in seen:
            seen.append(d)
    return seen


def load_writeup(base: Path, pico_id: str) -> dict:
    path = base / f"write_{pico_id}.json"
    data = parse_json_result(path)
    if data is None:
        raise FileNotFoundError(f"{path} missing or not valid JSON — the writer agent has not finished")
    return data


def check_citations(base: Path, pico_ids: list[str]) -> dict[str, list[str]]:
    """Return ``{pico_id: [unknown DOIs]}``; empty lists mean the PICO passes.

    A DOI is *known* only if it is in that PICO's own included-study set. This
    is the anti-hallucination gate for the prose: nothing the search did not
    return can appear on the page.
    """
    problems: dict[str, list[str]] = {}
    for pico_id in pico_ids:
        _pico, _flow, studies = load_pico_result(base, pico_id)
        allowed = {_norm_doi(a.doi) for a in studies if a.doi}
        writeup = load_writeup(base, pico_id)
        unknown = [d for d in cited_dois(writeup) if d not in allowed]
        problems[pico_id] = unknown
        if unknown:
            logger.warning("%s cites %d DOI(s) not in its included set: %s", pico_id, len(unknown), unknown)
    return problems


# ---------------------------------------------------------------------------
# Stage 5: render
# ---------------------------------------------------------------------------


def _ama_author(name: str) -> str:
    """'Lin, Hsieh-Ting' -> 'Lin HT'; 'Chen' -> 'Chen'."""
    if "," in name:
        last, fore = (s.strip() for s in name.split(",", 1))
        initials = "".join(part[0] for part in re.split(r"[\s-]+", fore) if part)
        return f"{last} {initials}".strip()
    return name.strip()


def _format_reference(a: ArticleMetadata) -> str:
    authors = [_ama_author(n) for n in a.authors[:3]]
    if len(a.authors) > 3:
        authors.append("et al")
    bits = [", ".join(authors) + "." if authors else ""]
    bits.append(a.title.rstrip(".") + ".")
    tail = a.journal
    if a.year:
        tail += f". {a.year}"
    bits.append(tail + ".")
    return " ".join(b for b in bits if b)


def _prose_to_html(text: str, ref_numbers: dict[str, int]) -> Markup:
    """Escape, split paragraphs on blank lines, turn [doi:X] into superscript links."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    out: list[str] = []
    for para in paragraphs:
        html = escape(para)

        def _sup(m: re.Match) -> str:
            n = ref_numbers.get(_norm_doi(m.group(1)))
            return f'<sup><a href="#ref-{n}">{n}</a></sup>' if n else ""

        html = _DOI_TOKEN.sub(_sup, html)
        out.append(f"<p>{html}</p>")
    return Markup("\n".join(out))


def render_brief(
    base: Path, output_path: Path | None = None, *, min_year: int | None = None
) -> Path:
    """Assemble ``brief.html`` from the JSON on disk. Refuses on unknown DOIs.

    ``min_year`` is passed by the per-job worker. For older CLI-created briefs,
    fall back to the value persisted in ``picos.json`` and then the brief
    default so the report remains traceable to the search window.
    """
    question, picos = load_brief(base)
    pico_ids = [p.pico_id for p in picos]

    problems = check_citations(base, pico_ids)
    bad = {k: v for k, v in problems.items() if v}
    if bad:
        raise ValueError(
            "Refusing to render: writer cited DOIs outside the included set — "
            + "; ".join(f"{k}: {', '.join(v)}" for k, v in bad.items())
        )

    ref_numbers: dict[str, int] = {}
    ref_articles: list[ArticleMetadata] = []
    pico_cards: list[dict] = []
    pico_results: list[PicoResult] = []

    for pico in picos:
        pq, flow, studies = load_pico_result(base, pico.pico_id)
        grade, verdict = collect_verdict(base, pico.pico_id)
        writeup = load_writeup(base, pico.pico_id)
        by_doi = {_norm_doi(a.doi): a for a in studies if a.doi}

        for d in cited_dois(writeup):
            if d not in ref_numbers:
                ref_numbers[d] = len(ref_articles) + 1
                ref_articles.append(by_doi[d])

        ft_index = load_fulltext_index(base, pico.pico_id) or {"body_of_evidence": []}
        ft_ok = {_norm_doi(e["doi"] or "") for e in ft_index["body_of_evidence"] if e["status"] == "ok"}
        ft_total = len(ft_index["body_of_evidence"]) + len(ft_index.get("not_fetched", []))
        screen_path = _screen_path(base, pico.pico_id)
        screen = json.loads(screen_path.read_text(encoding="utf-8")) if screen_path.exists() else {}

        key_studies = []
        for ks in writeup.get("key_studies", []) or []:
            d = _norm_doi(str(ks.get("doi", "")))
            a = by_doi.get(d)
            if a is None:
                continue
            key_studies.append({
                "fulltext": d in ft_ok,
                "n_ref": ref_numbers[d],
                "year": a.year,
                "design": ks.get("design") or a.pub_type or "",
                "n": ks.get("n", ""),
                "finding": ks.get("finding", ""),
                "doi": a.doi,
                "journal": a.journal,
            })

        pico_cards.append({
            "pico_id": pico.pico_id,
            "question": pq.question_text or pq.outcome,
            "verdict": verdict.verdict,
            "verdict_zh": VERDICT_ZH.get(verdict.verdict, verdict.verdict),
            "certainty": verdict.confidence,
            "certainty_zh": CERTAINTY_ZH.get(verdict.confidence, verdict.confidence),
            "pips": _CERTAINTY_PIPS.get(verdict.confidence, 1),
            "effect_zh": EFFECT_ZH.get(grade.effect_direction, grade.effect_direction),
            "headline": writeup.get("headline", ""),
            "lay_html": _prose_to_html(writeup.get("lay", ""), ref_numbers),
            "pro_html": _prose_to_html(writeup.get("pro", ""), ref_numbers),
            "caveats_html": _prose_to_html(writeup.get("caveats", ""), ref_numbers),
            "dissent": writeup.get("dissent", ""),
            "key_studies": key_studies,
            "grade_domains": [
                {"name": d.name, "rating": d.rating, "downgrade": d.downgrade, "why": d.justification}
                for d in grade.domains
            ],
            "starting_level": grade.starting_level,
            "n_unranked": sum(1 for a in studies if (a.journal_quartile or "Unknown") == "Unknown"),
            "body_of_evidence": [k.lstrip("@") for k in grade.body_of_evidence],
            "fulltext_ok": len(ft_ok),
            "fulltext_total": ft_total,
            "rob_notes": [{"key": k.lstrip("@"), "note": v} for k, v in grade.rob_notes.items()],
            "discrepancies": grade.discrepancies,
            "screen_excluded": screen.get("excluded", []),
            "n_studies": len(studies),
            "n_rct": sum(1 for a in studies if a.pub_type == "RCT"),
            "flow": flow.model_dump(),
        })
        pico_results.append(PicoResult(question=pq, prisma=flow, included_studies=studies,
                                       grade=grade, verdict=verdict))

    overall_verdict, overall_certainty = derive_overall(pico_results)
    if min_year is None:
        raw_picos = json.loads((base / "picos.json").read_text(encoding="utf-8"))
        persisted_year = raw_picos.get("min_year") if isinstance(raw_picos, dict) else None
        min_year = int(persisted_year) if persisted_year is not None else brief_config().min_year
    env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)),
                      autoescape=select_autoescape(["html", "j2"]))
    unranked_total = sum(card["n_unranked"] for card in pico_cards)
    html = env.get_template("brief.html.j2").render(
        question=question,
        unranked_total=unranked_total,
        generated=datetime.now(UTC).date().isoformat(),
        overall_verdict=overall_verdict,
        overall_verdict_zh=VERDICT_ZH.get(overall_verdict, overall_verdict),
        overall_certainty_zh=CERTAINTY_ZH.get(overall_certainty, overall_certainty),
        overall_pips=_CERTAINTY_PIPS.get(overall_certainty, 1),
        min_year=min_year,
        picos=pico_cards,
        references=[
            {"n": i + 1, "text": _format_reference(a), "doi": a.doi, "pmid": a.pmid}
            for i, a in enumerate(ref_articles)
        ],
    )
    out = output_path or (base / "brief.html")
    out.write_text(html, encoding="utf-8")
    logger.info("Rendered %s (%d PICO, %d references)", out, len(pico_cards), len(ref_articles))
    return out
