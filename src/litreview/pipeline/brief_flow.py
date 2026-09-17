"""Evidence-brief flow control: what is done, what is next, what is wrong.

``next`` is the only command the SKILL has to loop on. It performs every
deterministic stage that is ready and stops exactly when subagents must be
dispatched (printing their one-line prompts), when automatic expert review is
pending, or when the page is rendered. State is whatever is on disk under
``output/<slug>/`` — no database, so any stage can be redone by deleting its
file, and a change upstream (an expert addition, a verify pass) makes the
downstream artefacts stale by mtime or content, never by a flag.

``doctor`` and ``preview`` are pre-flight: they catch a broken contract or a
dead search term before an agent is paid to discover it.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from litreview.clients.pubmed import PubMedClient
from litreview.config import Config
from litreview.models import PICOQuestion
from litreview.pipeline import brief
from litreview.pipeline.brief import (
    AGENT_VIEW_TYPES,
    _norm_doi,
    check_citations,
    collect_screen,
    expert_added_dois,
    fetch_fulltext,
    load_brief,
    load_pico_result,
    render_brief,
    search_pico,
    write_grade_task,
    write_screen_tasks,
    write_verify_task,
    write_writer_task,
)
from litreview.pipeline.claim_orchestrator import ClaimAppraisalPipeline
from litreview.utils.llm import parse_json_result

logger = logging.getLogger(__name__)

CHECKPOINT_LOG = "checkpoint_log.json"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _mtime(p: Path) -> float:
    return p.stat().st_mtime if p.exists() else -1.0


@dataclass
class PicoState:
    pico_id: str
    searched: bool = False
    included: int = 0
    screen_required: bool = False
    screened: bool = False
    screen_included: int = 0
    screen_total: int = 0
    graded: bool = False            # GRADE result exists and post-dates the search/adds
    grade_stale: bool = False       # result exists but pico_NN.json changed since
    body: int = 0                   # body-of-evidence size
    fulltext_applicable: bool = False
    fulltext_fresh: bool = False    # index covers the current body of evidence
    fulltext_ok: int = 0
    fulltext_total: int = 0
    verified: bool = False          # grade JSON carries fulltext_verified
    written: bool = False           # write JSON exists and post-dates the GRADE
    certainty: str = ""
    verdict: str = ""

    @property
    def stage(self) -> str:
        if not self.searched:
            return "search"
        if self.screen_required and not self.screened:
            return "screen"
        if not self.graded:
            return "grade (stale)" if self.grade_stale else "grade"
        if self.fulltext_applicable and not self.fulltext_fresh:
            return "fulltext"
        if self.fulltext_applicable and self.fulltext_ok and not self.verified:
            return "verify"
        if not self.written:
            return "write"
        return "done"


def fulltext_available(cfg: Config) -> bool:
    """JournalFetcher present and its Python can import the converter."""
    if not (cfg.journalfetcher_dir / "dlbydoi.py").exists():
        return False
    if shutil.which(cfg.journalfetcher_python) is None and not Path(cfg.journalfetcher_python).exists():
        return False
    try:
        r = subprocess.run([cfg.journalfetcher_python, "-c", "import pymupdf4llm, curl_cffi"],
                           capture_output=True, timeout=60, check=False)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pico_state(base: Path, pico: PICOQuestion, fulltext_enabled: bool) -> PicoState:
    pid = pico.pico_id
    st = PicoState(pico_id=pid)
    pico_json = base / f"{pid}.json"
    grade_json = base / f"grade_{pid}.json"
    screen_json = base / f"{pid}.screen.json"
    write_json = base / f"write_{pid}.json"

    st.searched = pico_json.exists()
    if not st.searched:
        return st
    _q, _flow, studies = load_pico_result(base, pid)
    st.included = len(studies)

    expert = expert_added_dois(base, pid)
    evidence_studies = [
        a for a in studies
        if a.pub_type in AGENT_VIEW_TYPES or _norm_doi(a.doi or "") in expert
    ]
    evidence_keys = {a.citation_key for a in evidence_studies}
    st.screen_total = len(evidence_keys)
    # Legacy briefs that already have a GRADE result never had screening and
    # stay on the old path. Once a screen file exists, it remains part of the
    # dependency graph and a changed study set makes it stale.
    st.screen_required = screen_json.exists() or not grade_json.exists()
    if screen_json.exists():
        screen = parse_json_result(screen_json)
        if screen is not None:
            covered = set(screen.get("included", [])) | {
                e.get("citation_key") for e in screen.get("excluded", []) if isinstance(e, dict)
            }
            st.screen_included = len(screen.get("included", []))
            st.screened = _mtime(screen_json) >= _mtime(pico_json) and evidence_keys <= covered

    raw = parse_json_result(grade_json) if grade_json.exists() else None
    if raw is not None and isinstance(raw.get("domains"), list):
        # An expert addition or re-search rewrites pico_NN.json; a GRADE made
        # before that no longer describes the set it is supposed to rate.
        upstream_mtime = max(_mtime(pico_json), _mtime(screen_json) if screen_json.exists() else -1)
        st.grade_stale = upstream_mtime > _mtime(grade_json)
        st.graded = not st.grade_stale
    if not st.graded:
        return st

    grade, verdict = brief.collect_verdict(base, pid)
    st.certainty, st.verdict = grade.final_certainty, verdict.verdict
    body_keys = {k.lstrip("@") for k in grade.body_of_evidence}
    body = [a for a in studies if a.citation_key in body_keys or _norm_doi(a.doi or "") in expert]
    st.body = len(body)
    st.fulltext_applicable = fulltext_enabled and st.body > 0

    # Read whatever the full-text stage already left behind even when it is no
    # longer applicable: a GRADE result that names no body of evidence (or a
    # machine without JournalFetcher) must still report the coverage on disk
    # rather than claiming the brief was never verified.
    index = brief.load_fulltext_index(base, pid)
    if index:
        covered = {e["citation_key"] for e in index["body_of_evidence"]} | \
                  {e["citation_key"] for e in index.get("not_fetched", [])}
        st.fulltext_fresh = {a.citation_key for a in body} <= covered
        for entry in index["body_of_evidence"]:
            if entry.get("status") == "failed" and brief.find_manual_pdf(
                base / "fulltext" / "pdf", entry.get("doi") or ""
            ):
                st.fulltext_fresh = False
                break
        st.fulltext_ok = sum(1 for e in index["body_of_evidence"] if e["status"] == "ok")
        st.fulltext_total = len(index["body_of_evidence"]) + len(index.get("not_fetched", []))
    st.verified = isinstance(raw.get("fulltext_verified"), list)

    st.written = write_json.exists() and _mtime(write_json) > _mtime(grade_json) \
        and parse_json_result(write_json) is not None
    return st


def flow_state(base: Path, cfg: Config) -> tuple[str, list[PICOQuestion], list[PicoState], bool]:
    question, picos = load_brief(base)
    ft = fulltext_available(cfg)
    return question, picos, [pico_state(base, p, ft) for p in picos], ft


# ---------------------------------------------------------------------------
# Expert checkpoint marker
# ---------------------------------------------------------------------------


def checkpoint_done(base: Path) -> bool:
    path = base / CHECKPOINT_LOG
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    return bool(data.get("expert", {}).get("done"))


def record_checkpoint(base: Path, note: str = "") -> None:
    path = base / CHECKPOINT_LOG
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data["expert"] = {"done": True, "at": datetime.now(UTC).isoformat(timespec="seconds"), "note": note}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# next
# ---------------------------------------------------------------------------


@dataclass
class Dispatch:
    kind: str       # GRADE | Verify | Write
    pico_id: str
    task: Path
    model: str = "opus"

    @property
    def prompt(self) -> str:
        return f"Read {self.task.resolve()} and do exactly what it says."


@dataclass
class NextResult:
    status: str                     # "checkpoint" | "dispatch" | "rendered" | "done"
    actions: list[str] = field(default_factory=list)   # deterministic things done this call
    dispatch: list[Dispatch] = field(default_factory=list)
    rendered: Path | None = None
    states: list[PicoState] = field(default_factory=list)
    fulltext_enabled: bool = False  # JournalFetcher usable, so fulltext/verify ran
    gaps: dict[str, list[dict]] = field(default_factory=dict)


async def run_next(base: Path, cfg: Config) -> NextResult:
    """Do everything deterministic that is ready; stop where an agent is needed."""
    result = NextResult(status="done")
    _question, picos, states, ft_enabled = flow_state(base, cfg)
    if not picos:
        raise ValueError(f"{base / 'picos.json'} has no PICO sub-questions — nothing to run")
    result.fulltext_enabled = ft_enabled

    # 1. search whatever is not searched
    unsearched = [p for p, s in zip(picos, states) if not s.searched]
    for p in unsearched:
        flow = await search_pico(base, p, cfg)
        result.actions.append(
            f"search {p.pico_id}: found={flow.total_found} Q1={flow.after_quality_filter} "
            f"citation=+{flow.included_by_citation} -> {flow.included} included"
        )
    if unsearched:
        states = [pico_state(base, p, ft_enabled) for p in picos]

    # 2. automatic expert review gates everything downstream of the search
    if not checkpoint_done(base):
        result.status = "checkpoint"
        result.states = states
        for p in picos:
            try:
                result.gaps[p.pico_id] = brief.load_gaps(base, p.pico_id, limit=5)
            except FileNotFoundError:
                result.gaps[p.pico_id] = []
        return result

    # 3. per-PICO: deterministic stages inline, agent stages collected
    changed = True
    while changed:
        changed = False
        result.dispatch = []
        for p, st in zip(picos, states):
            pid = p.pico_id
            if st.screen_required and not st.screened:
                tasks = sorted((base / "tasks").glob(f"screen_{pid}_[0-9][0-9].md")) \
                    if (base / "tasks").exists() else []
                expected_batches = (st.screen_total + 9) // 10
                current_tasks = [t for t in tasks if _mtime(t) >= _mtime(base / f"{pid}.json")]
                # Same membership rule as write_evidence_view; the expert set is
                # read once rather than per study.
                expert = expert_added_dois(base, pid)
                expected_keys = [a.citation_key for a in load_pico_result(base, pid)[2]
                                 if a.pub_type in AGENT_VIEW_TYPES or _norm_doi(a.doi or "") in expert]
                task_keys: list[str] = []
                for task_path in current_tasks:
                    task_keys.extend(re.findall(r"^citation_key:\s*(\S+)\s*$",
                                                task_path.read_text(encoding="utf-8"), flags=re.MULTILINE))
                if len(current_tasks) != expected_batches or task_keys != expected_keys:
                    current_tasks = write_screen_tasks(base, pid)
                    result.actions.append(f"screen tasks {pid}: {len(current_tasks)} batch(es)")
                pending = [
                    task for task in current_tasks
                    if not (base / f"{task.stem}.json").exists()
                    or _mtime(base / f"{task.stem}.json") < _mtime(task)
                ]
                if pending:
                    result.dispatch.extend(Dispatch("Screen", pid, task, model="haiku") for task in pending)
                    continue
                collect_screen(base, pid)
                result.actions.append(f"screen {pid}: collected")
                changed = True
                continue
            if not st.graded:
                task = base / "tasks" / f"grade_{pid}.md"
                grade_upstream = max(
                    _mtime(base / f"{pid}.json"), _mtime(base / f"{pid}.screen.json")
                )
                if not task.exists() or _mtime(task) < grade_upstream:
                    write_grade_task(base, pid)
                    result.actions.append(f"grade task {pid}" + (" (regenerated: study set changed)" if st.grade_stale else ""))
                result.dispatch.append(Dispatch("GRADE", pid, task))
                continue
            if st.fulltext_applicable and not st.fulltext_fresh:
                index = fetch_fulltext(base, pid, cfg)
                ok = sum(1 for e in index["body_of_evidence"] if e["status"] == "ok")
                result.actions.append(f"fulltext {pid}: {ok}/{len(index['body_of_evidence'])} retrieved")
                changed = True
                continue
            if st.fulltext_applicable and st.fulltext_ok and not st.verified:
                task = base / "tasks" / f"verify_{pid}.md"
                if not task.exists() or _mtime(task) < _mtime(base / f"{pid}.fulltext.json"):
                    write_verify_task(base, pid)
                    result.actions.append(f"verify task {pid}")
                result.dispatch.append(Dispatch("Verify", pid, task))
                continue
            if not st.written:
                task = base / "tasks" / f"write_{pid}.md"
                if not task.exists() or _mtime(task) < _mtime(base / f"grade_{pid}.json"):
                    write_writer_task(base, pid, min_year=cfg.min_year)
                    result.actions.append(f"write task {pid}")
                result.dispatch.append(Dispatch("Write", pid, task))
                continue
        if changed:
            states = [pico_state(base, p, ft_enabled) for p in picos]
    result.states = states

    if result.dispatch:
        result.status = "dispatch"
        return result

    # 4. everything written: check + render (only if stale)
    html = base / "brief.html"
    newest_write = max(_mtime(base / f"write_{p.pico_id}.json") for p in picos)
    if _mtime(html) < newest_write:
        problems = {k: v for k, v in check_citations(base, [p.pico_id for p in picos]).items() if v}
        if problems:
            raise ValueError("writer cited DOIs outside the included set — " +
                             "; ".join(f"{k}: {', '.join(v)}" for k, v in problems.items()) +
                             ". Re-dispatch that writer with the offending list.")
        result.rendered = render_brief(base)
        result.actions.append(f"rendered {result.rendered}")
        result.status = "rendered"
    else:
        result.status = "done"
        result.rendered = html
    return result


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    warn: bool = False   # informational; does not fail the run


def run_doctor(base: Path, cfg: Config) -> list[Check]:
    checks: list[Check] = []

    # contract: picos.json
    try:
        _q, picos = load_brief(base)
        checks.append(Check("picos.json", True, f"{len(picos)} PICO(s)"))
    except (FileNotFoundError, ValueError) as e:
        return [Check("picos.json", False, str(e))]
    for p in picos:
        problems = []
        if not p.primary_terms and not p.intervention:
            problems.append("no primary_terms/intervention")
        if p.claim_direction not in ("benefit", "harm"):
            problems.append(f"claim_direction={p.claim_direction!r} (benefit|harm)")
        if not p.question_text:
            problems.append("no question_text (shown on the page)")
        long_terms = [t for t in (p.secondary_terms + p.primary_terms) if t and len(t.split()) > 5]
        if long_terms:
            problems.append(f"phrase-length terms unlikely to match: {long_terms}")
        checks.append(Check(f"{p.pico_id} contract", not problems, "; ".join(problems) or "ok"))

    # credentials
    identity = cfg.pubmed_email or cfg.unpaywall_email
    checks.append(Check("PubMed identity", bool(identity),
                        "contact address set" if identity else "PUBMED_EMAIL or UNPAYWALL_EMAIL required"))
    checks.append(Check("Scopus key", bool(cfg.scopus_api_key),
                        "set: most-cited pass on" if cfg.scopus_api_key else "unset: most-cited pass skipped", warn=True))
    scimago = list(Path("data").glob("scimago*.csv")) + list(Path("data").glob("scimagojr*.csv"))
    checks.append(Check("Scimago CSV", bool(scimago), scimago[0].name if scimago else "missing: quartiles fall back to OpenAlex", warn=True))

    # full text
    ft = fulltext_available(cfg)
    checks.append(Check("JournalFetcher", ft,
                        f"{cfg.journalfetcher_dir} via {cfg.journalfetcher_python}" if ft
                        else f"not usable at {cfg.journalfetcher_dir}: fulltext/verify stages skipped (abstract-level brief)", warn=True))

    # stage artefacts, if present
    for p in picos:
        pid = p.pico_id
        if (base / f"{pid}.json").exists():
            _pico, _flow, studies = load_pico_result(base, pid)
            expert = expert_added_dois(base, pid)
            view = [a for a in studies if a.pub_type in AGENT_VIEW_TYPES or _norm_doi(a.doi or "") in expert]
            checks.append(Check(f"{pid} evidence view", bool(view), f"{len(view)}/{len(studies)} studies shown to agents"))
            missing = expert - {_norm_doi(a.doi or "") for a in view}
            if expert:
                checks.append(Check(f"{pid} expert additions visible", not missing, ", ".join(sorted(missing)) or f"{len(expert)} ok"))
            no_abs = [a.citation_key for a in view if not a.abstract]
            if no_abs:
                # Advisory: the GRADE agent rates these blind unless the full
                # text is fetched. ``ok=False`` so it prints as a warn, ``warn``
                # so it does not fail the run.
                checks.append(Check(f"{pid} abstracts", False, f"{len(no_abs)} without abstract (full text needed): {', '.join(no_abs[:4])}", warn=True))
            index = brief.load_fulltext_index(base, pid)
            if index:
                for entry in index.get("body_of_evidence", []):
                    if entry.get("status") != "failed" or not entry.get("doi"):
                        continue
                    filename = f"{brief.doi_filename_token(entry['doi'])}.pdf"
                    checks.append(Check(
                        f"{pid} manual PDF {entry.get('citation_key', '')}",
                        False,
                        f"drop PDF at {base / 'fulltext' / 'pdf' / filename}",
                        warn=True,
                    ))
        for task in sorted((base / "tasks").glob(f"*_{pid}.md")) if (base / "tasks").exists() else []:
            text = task.read_text(encoding="utf-8")
            paths = [Path(t.rstrip(".,;:)")) for t in text.split() if t.startswith("/")]
            dead = [str(x) for x in paths if x.suffix in (".json", ".md") and "write_" not in x.name and "grade_" not in x.name and not x.exists()]
            if dead:
                checks.append(Check(f"{task.name} paths", False, f"referenced but missing: {dead[:3]}"))
        gj = base / f"grade_{pid}.json"
        if gj.exists():
            raw = parse_json_result(gj)
            names = [d.get("name") for d in (raw or {}).get("domains", []) if isinstance(d, dict)]
            ok = raw is not None and set(names) >= {"risk_of_bias", "inconsistency", "indirectness", "imprecision", "publication_bias"}
            checks.append(Check(f"{pid} GRADE JSON", ok, "5 domains" if ok else f"unparseable or domains={names}"))
    return checks


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------


async def run_preview(base: Path, cfg: Config) -> list[dict]:
    """PubMed hit counts per PICO: the whole query, the design sweep, and every term alone."""
    _q, picos = load_brief(base)
    builder = ClaimAppraisalPipeline(cfg)
    rows: list[dict] = []
    async with PubMedClient(cfg.pubmed_api_key, email=cfg.pubmed_email or cfg.unpaywall_email) as c:
        for p in picos:
            relevance, sweep, _cited = builder.build_pico_queries(p)
            row = {"pico_id": p.pico_id,
                   "query": await c.count(relevance.boolean_query, date_from=cfg.min_year),
                   "design_sweep": await c.count(sweep.boolean_query, date_from=cfg.min_year),
                   "terms": []}
            # ``outcome`` is prepended to secondary_terms by the query builder,
            # so it repeats whenever the SKILL also listed it as a term.
            seen: set[str] = set()
            for group, terms in (("intervention", relevance.primary_terms), ("outcome", relevance.secondary_terms)):
                for t in terms:
                    if t in seen:
                        continue
                    seen.add(t)
                    n = await c.count(f'"{t}"', date_from=cfg.min_year)
                    row["terms"].append({"group": group, "term": t, "hits": n})
            rows.append(row)
    return rows
