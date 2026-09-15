"""Claim-appraisal orchestrator: claim -> N PICO -> per-PICO SR.

This composes the existing :class:`LitReviewPipeline` as a per-PICO worker so
the deterministic API work (search -> dedup -> year -> Q1 -> validate) is reused
verbatim. The LLM/MCP steps (claim decomposition, semantic screening, GRADE,
OpenEvidence cross-check) are dispatched by the ``/lit-review`` SKILL agent via
the SubagentTask contract and collected back here — Python never calls an LLM or
MCP tool directly.

Phase 2 implements the non-LLM thin slice: ``run_pico_search`` takes one PICO
and produces its included-study set plus a populated ``PicoPrismaFlow``.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from litreview.config import Config, get_config
from litreview.models import (
    ArticleMetadata,
    ClaimAppraisal,
    DatabaseSource,
    PicoPrismaFlow,
    PICOQuestion,
    PicoResult,
    SearchQuery,
)
from litreview.pipeline import claim_decomposer, crosscheck, grade_judge
from litreview.pipeline.filters import filter_by_year
from litreview.pipeline.journal_quality import assess_journal_quality
from litreview.pipeline.orchestrator import LitReviewPipeline
from litreview.pipeline.verdict_builder import assemble_verdict, derive_overall
from litreview.utils.crossref import (
    batch_verify_crossref,
    fetch_reference_dois,
    filter_crossref_verified,
)

logger = logging.getLogger(__name__)


@dataclass
class GateCounts:
    """Counts out of each gate for one batch of records."""

    after_year: int = 0
    excluded_by_year: int = 0
    after_quality: int = 0
    excluded_by_quality: int = 0
    after_validation: int = 0
    after_crossref: int = 0
    excluded_by_crossref: int = 0


class ClaimAppraisalPipeline:
    """Run a claim appraisal by fanning out one SR pipeline per PICO question."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or get_config()
        self._pipeline: LitReviewPipeline | None = None

    async def __aenter__(self) -> ClaimAppraisalPipeline:
        # Reuse the existing pipeline's client lifecycle verbatim.
        self._pipeline = await LitReviewPipeline(self.config).__aenter__()
        return self

    async def __aexit__(self, *args) -> None:  # noqa: ANN002
        if self._pipeline is not None:
            await self._pipeline.__aexit__(*args)

    # ------------------------------------------------------------------
    # Query construction
    # ------------------------------------------------------------------

    # PubMed publication-type filter for the design sweep. Relevance ranking
    # alone can bury a landmark trial below the result cap (DELIVER sat outside
    # the top 300 for an SGLT2i/HFpEF query while EMPEROR-Preserved was #2), so
    # a second pass restricted to trial and synthesis designs guarantees they
    # are identified. PubMed syntax only — never sent to Scopus/Embase.
    DESIGN_SWEEP_FILTER = (
        "(randomized controlled trial[pt] OR meta-analysis[pt] OR systematic review[pt])"
    )

    def build_pico_queries(self, pico: PICOQuestion) -> list[SearchQuery]:
        """Build the search queries for one PICO sub-question.

        Returns two queries over the same Boolean core (intervention terms AND
        outcome terms, ``date_from=min_year``): the relevance-ranked pass for
        all databases, then a PubMed-only design sweep (RCT / meta-analysis /
        systematic review) so capped relevance ranking cannot drop the trials
        the appraisal turns on. Duplicates are removed downstream.
        """
        intervention_terms = pico.primary_terms or [pico.intervention]
        intervention_terms = [t for t in intervention_terms if t]
        # `outcome` is a description for the GRADE/writer prompts and may be a
        # sentence; PubMed would AND its loose words. It is a search term only
        # when it is short enough to be one, or when nothing else describes the
        # outcome side.
        outcome_is_term = bool(pico.outcome) and (len(pico.outcome.split()) <= 5 or not pico.secondary_terms)
        outcome_terms = [t for t in (([pico.outcome] if outcome_is_term else []) + pico.secondary_terms) if t]

        groups: list[str] = []
        if intervention_terms:
            groups.append("(" + " OR ".join(f'"{t}"' for t in intervention_terms) + ")")
        if outcome_terms:
            groups.append("(" + " OR ".join(f'"{t}"' for t in outcome_terms) + ")")
        boolean_query = " AND ".join(groups) if groups else f'"{pico.intervention}"'

        topic = pico.question_text or pico.outcome_domain or pico.intervention
        relevance = SearchQuery(
            topic=topic,
            primary_terms=intervention_terms,
            secondary_terms=outcome_terms,
            mesh_terms=pico.mesh_terms,
            boolean_query=boolean_query,
            date_from=self.config.min_year,
            date_to=None,
        )
        design_sweep = relevance.model_copy(update={
            "topic": f"{topic} (design sweep: RCT/MA/SR)",
            "boolean_query": f"({boolean_query}) AND {self.DESIGN_SWEEP_FILTER}",
            "databases": ["pubmed"],
        })
        # Citation count is a ranking axis independent of relevance, and for
        # trials "most cited" and "landmark" are close to synonyms. Scopus is
        # the only engine here that exposes it. Field-restricted so the ranked
        # pool is the topic, not every record mentioning a drug name anywhere.
        most_cited = relevance.model_copy(update={
            "topic": f"{topic} (most cited)",
            "boolean_query": f"TITLE-ABS-KEY({boolean_query})",
            "databases": ["scopus"],
            "sort": "-citedby-count",
        })
        return [relevance, design_sweep, most_cited]

    # ------------------------------------------------------------------
    # Per-PICO deterministic pipeline (no LLM)
    # ------------------------------------------------------------------

    async def run_pico_search(
        self, pico: PICOQuestion, dropped_out: list[tuple[ArticleMetadata, str]] | None = None
    ) -> tuple[list[ArticleMetadata], PicoPrismaFlow]:
        """Search -> dedup -> year(>=min_year) -> Q1-only(strict) -> validate -> CrossRef,
        then backward snowballing from the included syntheses through the same gates.

        Returns the surviving studies and a fully-populated PRISMA flow. When
        ``dropped_out`` is supplied, every gate exclusion and its reason is
        appended to it. This is
        the authoritative count source for the PICO's PRISMA diagram; database
        records and citation-searching records are counted in separate boxes
        as PRISMA 2020 requires.
        """
        if self._pipeline is None:
            raise RuntimeError("ClaimAppraisalPipeline must be used as an async context manager")
        p = self._pipeline
        flow = PicoPrismaFlow()

        queries = self.build_pico_queries(pico)

        # Stage: search — every query runs (each against the databases it
        # targets); records identified are counted before dedup, per PRISMA.
        articles: list[ArticleMetadata] = []
        for query in queries:
            articles.extend(await p.search_all_databases([query]))
        flow.total_found = len(articles)

        # Stage: dedup by DOI
        deduped = p.deduplicate(articles)
        flow.after_dedup = len(deduped)

        # Scopus records carry no abstract or publication type; PubMed does.
        await self._backfill_from_pubmed(deduped)

        confirmed, gates, dropped = await self._apply_gates(deduped, pico.pico_id)
        if dropped_out is not None:
            dropped_out.extend(dropped)
        flow.after_year_filter = gates.after_year
        flow.excluded_by_year = gates.excluded_by_year
        flow.after_quality_filter = gates.after_quality
        flow.excluded_by_quality = gates.excluded_by_quality
        flow.after_validation = gates.after_validation
        flow.after_crossref = gates.after_crossref
        flow.excluded_by_crossref = gates.excluded_by_crossref

        # Stage: backward snowballing — trials the included syntheses pooled.
        candidates = await self._snowball_candidates(confirmed)
        flow.identified_by_citation = len(candidates)
        if candidates:
            extra, _, citation_dropped = await self._apply_gates(
                candidates, f"{pico.pico_id}/citation"
            )
            if dropped_out is not None:
                dropped_out.extend(citation_dropped)
            flow.included_by_citation = len(extra)
            confirmed.extend(extra)

        flow.included = len(confirmed)

        logger.info(
            "PICO %s flow: found=%d dedup=%d year>=%d=%d Q1=%d validated=%d crossref=%d "
            "citation=+%d/%d included=%d",
            pico.pico_id, flow.total_found, flow.after_dedup, self.config.min_year,
            flow.after_year_filter, flow.after_quality_filter, flow.after_validation,
            flow.after_crossref, flow.included_by_citation, flow.identified_by_citation,
            flow.included,
        )
        return confirmed, flow

    # ------------------------------------------------------------------
    # Gates (shared by the database pass, snowballing and expert additions)
    # ------------------------------------------------------------------

    async def _apply_gates(
        self, articles: list[ArticleMetadata], label: str
    ) -> tuple[list[ArticleMetadata], GateCounts, list[tuple[ArticleMetadata, str]]]:
        """year >= min_year -> Q1 strict -> DOI resolves -> exists in CrossRef."""
        p = self._pipeline
        assert p is not None
        counts = GateCounts()
        dropped: list[tuple[ArticleMetadata, str]] = []

        kept_year, dropped_year = filter_by_year(articles, self.config.min_year)
        dropped.extend(
            (a, "no year" if a.year is None else "year<min") for a in dropped_year
        )
        counts.after_year = len(kept_year)
        counts.excluded_by_year = len(dropped_year)

        q1 = await assess_journal_quality(
            kept_year,
            email=self.config.unpaywall_email,
            min_quartile=self.config.min_quartile,
            strict=True,
        )
        counts.after_quality = len(q1)
        counts.excluded_by_quality = len(kept_year) - len(q1)
        q1_ids = {id(a) for a in q1}
        dropped.extend(
            (a, f"quartile {a.journal_quartile or 'Unknown'}")
            for a in kept_year
            if id(a) not in q1_ids
        )

        validated = await p.validate_and_enrich(q1)
        if p._unpaywall is not None:
            # With Unpaywall configured, require a resolved DOI (or no DOI at all).
            valid = [a for a in validated if a.doi_validated or not a.doi]
        else:
            logger.warning("Unpaywall not configured; skipping DOI-validation gate for %s", label)
            valid = validated
        valid_ids = {id(a) for a in valid}
        dropped.extend(
            (a, "doi unresolved") for a in validated if id(a) not in valid_ids
        )
        counts.after_validation = len(valid)

        # CrossRef existence gate (anti-hallucination): records without a DOI
        # cannot be confirmed and are dropped here.
        await batch_verify_crossref(valid, mailto=self.config.unpaywall_email)
        confirmed, unconfirmed = filter_crossref_verified(valid)
        counts.after_crossref = len(confirmed)
        counts.excluded_by_crossref = len(unconfirmed)
        dropped.extend(
            (a, "no doi" if not a.doi else "not in crossref") for a in unconfirmed
        )
        if unconfirmed:
            logger.warning("CrossRef dropped %d unconfirmed record(s) for %s", len(unconfirmed), label)
        return confirmed, counts, dropped

    async def _backfill_from_pubmed(self, articles: list[ArticleMetadata]) -> None:
        """Fill pub_type / abstract / ISSN from PubMed for records that have a PMID but lack them."""
        p = self._pipeline
        if p is None or p._pubmed is None:
            return
        need = {a.pmid: a for a in articles if a.pmid and (not a.pub_type or not a.abstract or not a.issn)}
        if not need:
            return
        fetched = await p._pubmed.fetch_articles(list(need))
        filled = 0
        for raw in fetched:
            a = need.get(raw.get("pmid") or "")
            if a is None:
                continue
            if not a.pub_type and raw.get("pub_type"):
                a.pub_types = list(raw.get("pub_types") or [])
                a.pub_type = raw["pub_type"]
            if not a.abstract and raw.get("abstract"):
                a.abstract = raw["abstract"]
            if not a.issn and raw.get("issn"):
                a.issn = raw["issn"]
            if a.year is None and raw.get("year"):
                a.year = raw["year"]
            filled += 1
        logger.info("PubMed back-fill: %d/%d records completed", filled, len(need))

    # ------------------------------------------------------------------
    # Backward snowballing
    # ------------------------------------------------------------------

    SNOWBALL_SEED_TYPES = ("Meta-Analysis", "Systematic Review")

    async def _snowball_candidates(self, included: list[ArticleMetadata]) -> list[ArticleMetadata]:
        """Trials cited by the included syntheses that the search did not return.

        Relevance ranking under a result cap is a sample, not a search, and can
        drop a landmark trial (DELIVER sat outside PubMed's top 300 for an
        SGLT2i/HFpEF query). A trial that the included Q1 meta-analyses pooled
        is one the field counts, regardless of how any ranker scored it.
        Candidates must be cited by at least two seeds (or all, when fewer),
        which keeps a 250-reference narrative review from flooding the set.
        """
        p = self._pipeline
        if p is None or p._pubmed is None:
            return []
        seeds = [a for a in included if a.pub_type in self.SNOWBALL_SEED_TYPES and a.doi]
        if not seeds:
            return []

        ref_lists = await asyncio.gather(
            *(fetch_reference_dois(a.doi, mailto=self.config.unpaywall_email) for a in seeds)
        )
        cited = Counter(d for refs in ref_lists for d in set(refs))
        have = {a.doi.lower().strip() for a in included if a.doi}
        threshold = min(2, len(seeds))
        candidate_dois = [d for d, n in cited.items() if n >= threshold and d not in have]
        if not candidate_dois:
            logger.info("Snowball: %d seeds, no new DOIs cited by >=%d", len(seeds), threshold)
            return []

        pmids = await p._pubmed.search_by_dois(candidate_dois)
        raw = await p._pubmed.fetch_articles(pmids) if pmids else []
        trials = [
            ArticleMetadata(source_db=DatabaseSource.PUBMED, **r)
            for r in raw
            if r.get("pub_type") == "RCT" and r.get("doi") and r["doi"].lower() not in have
        ]
        logger.info(
            "Snowball: %d seeds -> %d DOIs cited by >=%d -> %d in PubMed -> %d RCTs",
            len(seeds), len(candidate_dois), threshold, len(raw), len(trials),
        )
        return trials

    # ------------------------------------------------------------------
    # Expert additions (human checkpoint)
    # ------------------------------------------------------------------

    async def add_expert_studies(
        self, pico_id: str, identifiers: list[str], included: list[ArticleMetadata]
    ) -> tuple[list[ArticleMetadata], dict[str, str]]:
        """Resolve PMIDs/DOIs a human named, run them through every gate, return (added, rejected).

        The gates are not relaxed for expert picks: a study the pipeline cannot
        confirm as Q1 / resolvable / present in CrossRef is reported with the
        reason and left out, so the page's promise about its references holds.
        """
        p = self._pipeline
        if p is None or p._pubmed is None:
            raise RuntimeError("PubMed client required to resolve expert additions")
        have = {a.doi.lower().strip() for a in included if a.doi}
        have_pmids = {a.pmid for a in included if a.pmid}

        pmids = [i for i in identifiers if i.strip().isdigit()]
        dois = [i for i in identifiers if not i.strip().isdigit()]
        if dois:
            pmids += await p._pubmed.search_by_dois(dois)
        raw = await p._pubmed.fetch_articles(pmids) if pmids else []
        found = [ArticleMetadata(source_db=DatabaseSource.PUBMED, **r) for r in raw]

        rejected: dict[str, str] = {}
        resolved = {a.pmid for a in found} | {(a.doi or "").lower() for a in found}
        for ident in identifiers:
            if ident.strip().isdigit() and ident.strip() not in resolved:
                rejected[ident] = "not found in PubMed"
            elif not ident.strip().isdigit() and ident.strip().lower() not in resolved:
                rejected[ident] = "DOI not found in PubMed"

        fresh = [a for a in found if (a.doi or "").lower() not in have and a.pmid not in have_pmids]
        for a in found:
            if a not in fresh:
                rejected[a.pmid or a.doi or a.title] = "already included"

        added, _counts, _dropped = await self._apply_gates(fresh, f"{pico_id}/expert")
        added_keys = {id(a) for a in added}
        for a in fresh:
            if id(a) not in added_keys:
                if a.year is None or a.year < self.config.min_year:
                    why = f"year {a.year} < {self.config.min_year}"
                elif (a.journal_quartile or "Unknown") != "Q1":
                    why = f"journal quartile {a.journal_quartile or 'Unknown'} (Q1 required)"
                elif a.doi and not a.doi_validated and p._unpaywall is not None:
                    why = "DOI does not resolve"
                else:
                    why = "not confirmed in CrossRef"
                rejected[a.pmid or a.doi or a.title] = why
        return added, rejected

    async def run_pico(self, pico: PICOQuestion) -> PicoResult:
        """Run the deterministic per-PICO pipeline and assemble a PicoResult.

        GRADE/verdict are left ``None`` here; they are attached in later phases
        once the SKILL agent has dispatched the GRADE and cross-check subagents.
        """
        included, flow = await self.run_pico_search(pico)
        queries = self.build_pico_queries(pico)
        audit = [
            {"stage": "search", "count": flow.total_found},
            {"stage": "dedup", "count": flow.after_dedup},
            {"stage": "year_filter", "count": flow.after_year_filter, "excluded": flow.excluded_by_year},
            {"stage": "quality_filter", "count": flow.after_quality_filter, "excluded": flow.excluded_by_quality},
            {"stage": "validation", "count": flow.after_validation},
            {"stage": "crossref", "count": flow.after_crossref, "excluded": flow.excluded_by_crossref},
        ]
        return PicoResult(
            question=pico,
            search_queries=queries,
            prisma=flow,
            included_studies=included,
            audit_trail=audit,
        )

    # ------------------------------------------------------------------
    # LLM-phase task delegators (dispatched by the /lit-review SKILL agent)
    # ------------------------------------------------------------------

    def decomposition_task(self, claim: str, output_dir: Path, max_picos: int = 6):
        """SubagentTask that decomposes *claim* into PICO questions."""
        return claim_decomposer.generate_decomposition_task(claim, output_dir, max_picos)

    def collect_picos(self, claim: str, output_dir: Path) -> list[PICOQuestion]:
        return claim_decomposer.collect_picos(claim, output_dir)

    def grade_task(self, pico, included_studies, extracted, output_dir: Path):
        return grade_judge.generate_grade_task(pico, included_studies, extracted, output_dir)

    def collect_grade(self, pico, output_dir: Path):
        return grade_judge.collect_grade(pico, output_dir)

    def crosscheck_task(self, pico, included_studies, draft_verdict: str, output_dir: Path):
        return crosscheck.generate_crosscheck_task(pico, included_studies, draft_verdict, output_dir)

    def collect_crosscheck(self, pico, output_dir: Path):
        return crosscheck.collect_crosscheck(pico, output_dir)

    # ------------------------------------------------------------------
    # Verdict + claim-level assembly (pure)
    # ------------------------------------------------------------------

    def build_pico_verdict(self, pico_result: PicoResult, output_dir: Path) -> PicoResult:
        """Attach GRADE + cross-check + Verdict to a PicoResult by collecting
        the subagent outputs the SKILL has produced for this PICO."""
        grade = grade_judge.collect_grade(pico_result.question, output_dir)
        check = crosscheck.collect_crosscheck(pico_result.question, output_dir)
        verdict = assemble_verdict(grade, check, claim_direction=pico_result.question.claim_direction)
        pico_result.grade = grade
        pico_result.verdict = verdict
        pico_result.audit_trail.append(
            {"stage": "grade", "certainty": grade.final_certainty, "effect": grade.effect_direction}
        )
        pico_result.audit_trail.append(
            {"stage": "verdict", "verdict": verdict.verdict, "oe_agreement": check.agreement}
        )
        return pico_result

    def assemble_appraisal(self, claim: str, pico_results: list[PicoResult]) -> ClaimAppraisal:
        """Roll per-PICO verdicts up into a ClaimAppraisal (the render contract)."""
        overall_verdict, overall_certainty = derive_overall(pico_results)
        return ClaimAppraisal(
            claim=claim,
            pico_results=pico_results,
            overall_verdict=overall_verdict,
            overall_certainty=overall_certainty,
            filters_applied={
                "min_year": self.config.min_year,
                "min_quartile": self.config.min_quartile,
            },
        )
