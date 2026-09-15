#!/usr/bin/env python3
"""Fetch full text for DOIs through JournalFetcher and emit markdown.

JournalFetcher (the user's weekly-journal tool) holds the institutional-access
download logic — Elsevier API, publisher direct URLs, PRIMO/Ovid, NEJM and OUP
Playwright sessions, PMC and Unpaywall fallbacks — some 3,600 lines of
publisher-specific handling that is not worth porting. So this runner executes
*inside* that project: its Python, its cwd (for ``.env``), its modules.

    python3 scripts/fetch_fulltext.py --journalfetcher ~/JournalFetcher \\
        --out output/<slug>/fulltext --doi 10.1056/NEJMoa2206286 [--doi ...]

Prints one JSON line per DOI on stdout:
    {"doi", "status": "ok"|"failed", "pdf", "markdown", "chars_raw",
     "chars", "source": "manual"|"download", "error"}
All of JournalFetcher's own progress chatter goes to stderr.
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import json
import os
import re
import sys
from pathlib import Path

_REFERENCES_HEADING_RE = re.compile(
    r"^#{1,4}\s*(?:references|bibliography|literature cited|參考文獻)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
_SUPPLEMENTARY_HEADING_RE = re.compile(
    r"^#{1,4}\s*supplementary(?:\s+materials?)?\b.*$",
    re.IGNORECASE | re.MULTILINE,
)


def doi_filename_token(doi: str) -> str:
    """Return the case-insensitive filename token used for manual PDFs."""
    value = doi.strip().lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    return value.replace("/", "_")


def find_manual_pdf(pdf_dir: Path, doi: str) -> Path | None:
    """Find a dropped-in PDF whose filename contains the DOI token."""
    token = doi_filename_token(doi)
    if not token or not pdf_dir.exists():
        return None
    return next(
        (path for path in sorted(pdf_dir.glob("*.pdf")) if token in path.name.lower()),
        None,
    )


def strip_trailing_sections(markdown: str) -> str:
    """Heuristically remove late reference and supplementary sections."""
    threshold = int(len(markdown) * 0.4)
    candidates = []
    for pattern in (_REFERENCES_HEADING_RE, _SUPPLEMENTARY_HEADING_RE):
        matches = [match for match in pattern.finditer(markdown) if match.start() >= threshold]
        if matches:
            candidates.append(matches[-1].start())
    if not candidates:
        return markdown
    return markdown[:min(candidates)].rstrip()


def cap_markdown(markdown: str, max_chars: int) -> str:
    """Cap markdown length while retaining an explicit truncation marker."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if len(markdown) <= max_chars:
        return markdown
    marker = f"\n\n[truncated at {max_chars} chars]\n"
    if len(marker) >= max_chars:
        return marker[:max_chars]
    return markdown[:max_chars - len(marker)].rstrip() + marker


def _journalfetcher_stripper():
    """Return JournalFetcher's one-argument reference stripper when importable."""
    try:
        from weekly.appraise_selected import _strip_references  # type: ignore
    except Exception:
        return None
    try:
        signature = inspect.signature(_strip_references)
    except (TypeError, ValueError):
        return None
    required = [
        p for p in signature.parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return _strip_references if len(required) == 1 else None


def strip_markdown(markdown: str) -> str:
    """Use JournalFetcher's stripper when available, then drop supplements."""
    stripper = _journalfetcher_stripper()
    if stripper is None:
        return strip_trailing_sections(markdown)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            stripped = stripper(markdown)
    except Exception:
        return strip_trailing_sections(markdown)
    return strip_trailing_sections(stripped)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--journalfetcher", required=True, help="path to the JournalFetcher checkout")
    ap.add_argument("--out", required=True, help="directory for markdown; PDFs go to <out>/pdf")
    ap.add_argument("--doi", action="append", required=True, help="DOI to fetch (repeatable)")
    ap.add_argument("--max-chars", type=int, default=60000, help="maximum markdown characters per study")
    args = ap.parse_args()

    jf = Path(args.journalfetcher).expanduser().resolve()
    if not (jf / "dlbydoi.py").exists():
        raise SystemExit(f"{jf} does not look like JournalFetcher (no dlbydoi.py)")
    os.chdir(jf)  # dlbydoi/modules read .env relative to cwd
    sys.path.insert(0, str(jf))

    import pymupdf4llm
    from dlbydoi import download_one

    out = Path(args.out).resolve()
    pdf_dir = out / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    for doi in args.doi:
        rec: dict = {"doi": doi, "status": "failed", "pdf": None, "markdown": None,
                     "chars_raw": 0, "chars": 0, "source": None, "error": ""}
        try:
            with contextlib.redirect_stdout(sys.stderr):
                pdf = find_manual_pdf(pdf_dir, doi)
                source = "manual" if pdf is not None else "download"
                if pdf is None:
                    pdf = download_one(doi, pdf_dir)
                if pdf is None:
                    rec["error"] = "download failed (all methods)"
                else:
                    md = pymupdf4llm.to_markdown(str(pdf), ignore_images=True, show_progress=False)
                    chars_raw = len(md)
                    md = cap_markdown(strip_markdown(md), args.max_chars)
                    md_path = out / f"{pdf.stem}.md"
                    md_path.write_text(md, encoding="utf-8")
                    rec.update(status="ok", pdf=str(pdf), markdown=str(md_path),
                               chars_raw=chars_raw, chars=len(md), source=source)
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
        print(json.dumps(rec, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
