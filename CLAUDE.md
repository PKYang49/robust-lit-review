# Robust Literature Review Pipeline

## Overview
Automated systematic literature review pipeline that searches Scopus, PubMed, and Embase,
filters by journal quality (CiteScore/SJR), validates DOIs, and generates publication-ready
Quarto documents with BibTeX references.

## Skills
- `/lit-review` — Run the complete pipeline for a topic
- `/brainstorm-topic` — Brainstorm and refine search terms before running
- `/evidence-brief` — One question → PubMed-only PICO search → GRADE → single easy-to-read page (token-lean)

## Project Structure
```
src/litreview/
  clients/        — API clients (Scopus, PubMed, Embase, Unpaywall, Zotero)
  pipeline/       — Orchestrator + Quarto renderer
  utils/          — BibTeX generator, DOI validator, statistics
  cli.py          — Typer CLI interface
  config.py       — Environment-based configuration
  models.py       — Pydantic data models
output/           — Generated review files (.qmd, .bib, .pdf, .docx)
templates/        — Quarto templates
.github/workflows/ — GitHub Actions for render + release
```

## Commands
```bash
# Install
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"

# Run review
lit-review review "topic" --term "term1" --target 50 --min-citescore 3.0

# Validate DOIs in existing BibTeX
lit-review validate output/references.bib

# Check API config
lit-review check-config

# Evidence brief (PubMed + optional Scopus most-cited pass; needs PUBMED_EMAIL, SCOPUS_API_KEY optional)
lit-review brief doctor <slug>        # pre-flight: picos.json contract, credentials, artefacts on disk
lit-review brief preview <slug>       # PubMed hit counts per query / design sweep / term (0 hits = dead term)
lit-review brief next <slug>          # THE NORMAL ENTRY POINT: runs every ready stage, then says
                                      #   checkpoint | dispatch <agents> | rendered | done — loop on it
lit-review brief status <slug>        # one row per PICO: stage, GRADE, full-text coverage, written
lit-review brief checkpoint <slug> --note "..."   # record the expert review; unblocks `next`

# The individual stages `next` drives, for redoing one on its own:
lit-review brief search <slug>        # reads output/<slug>/picos.json; 3 passes + CrossRef snowballing
lit-review brief studies <slug>       # table of included RCT/MA/SR for the expert checkpoint
lit-review brief gaps <slug>          # top gate exclusions by citation count (checkpoint near-misses)
lit-review brief add <slug> <pico> --id <PMID|DOI>   # expert additions, same gates
lit-review brief screen-tasks <slug>  # Haiku batches (<=10 abstracts); agent writes screen batch JSON
lit-review brief collect-screen <slug> # merge batches; expert-added studies are force-included
lit-review brief grade-tasks <slug>   # then dispatch one Sonnet agent per tasks/grade_*.md
lit-review brief fulltext <slug>      # full text for the body via ~/JournalFetcher; failed DOI PDFs can be dropped into fulltext/pdf/
lit-review brief verify-tasks <slug>  # then dispatch one agent per tasks/verify_*.md (re-rates from full text)
lit-review brief write-tasks <slug>   # then dispatch one agent per tasks/write_*.md
lit-review brief check <slug>         # every cited DOI must be in the included set
lit-review brief render <slug>        # -> output/<slug>/brief.html
```

## Environment Variables (in .env)
- SCOPUS_API_KEY — Elsevier/Scopus API key
- PUBMED_API_KEY — NCBI E-utilities API key
- PUBMED_EMAIL — contact for NCBI `tool`+`email` identification (PubMed works with this alone)
- EMBASE_API_KEY — Elsevier/Embase API key
- UNPAYWALL_EMAIL — Email for Unpaywall API
- ZOTERO_API_KEY — Zotero API key
- ZOTERO_LIBRARY_TYPE — "user" or "group"
- ZOTERO_LIBRARY_ID — Zotero library ID
- ZOTERO_COLLECTION_KEY — Target collection key

## Quality Standards
- Only include articles from Q1/Q2 journals (CiteScore >= 3.0)
- Every DOI must be validated via doi.org handle API
- All URLs checked for accessibility
- PRISMA-compliant methodology reporting
- APA citation format via CSL
