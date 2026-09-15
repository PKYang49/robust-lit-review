"""Async PubMed API client using NCBI E-utilities.

XML parsing follows the rules learned in the JournalFetcher weekly pipeline,
each of which guards against a record being silently damaged or dropped:

* inline markup must be flattened with ``itertext()`` — ``.text`` stops at the
  first child element and truncates titles and structured abstracts;
* the DOI frequently lives only in ``PubmedData/ArticleIdList``, not in
  ``Article/ELocationID``, and a record with no DOI cannot clear the CrossRef
  gate downstream;
* ``ArticleDate`` (ePub) is a truer publication year than the issue-assigned
  ``PubDate``, and ``MedlineDate`` is the last resort before a record loses its
  year entirely and is dropped by ``filter_by_year``.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Self

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from litreview.models import ArticleMetadata, DatabaseSource

logger = logging.getLogger(__name__)

_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# Priority order for reducing PubMed's PublicationType list to one label.
# Highest evidence level first, so a record tagged both "Randomized Controlled
# Trial" and "Journal Article" reports as an RCT. This gives GRADE its starting
# level (RCT- vs observational-dominant) from PubMed's own indexing instead of
# leaving the judge to infer study design from prose.
PUB_TYPE_PRIORITY: tuple[tuple[str, str], ...] = (
    ("Meta-Analysis", "Meta-Analysis"),
    ("Systematic Review", "Systematic Review"),
    ("Practice Guideline", "Guideline"),
    ("Guideline", "Guideline"),
    ("Randomized Controlled Trial", "RCT"),
    ("Clinical Trial, Phase IV", "Phase IV Trial"),
    ("Clinical Trial, Phase III", "Phase III Trial"),
    ("Clinical Trial, Phase II", "Phase II Trial"),
    ("Clinical Trial, Phase I", "Phase I Trial"),
    ("Clinical Trial", "Trial"),
    # Ranked above the commentary types (and above "Review", unlike the
    # JournalFetcher table) because a design label an appraisal can act on
    # beats a publication-format label.
    ("Observational Study", "Observational"),
    ("Review", "Review"),
    ("Editorial", "Editorial"),
    ("Comment", "Comment"),
    ("Letter", "Letter"),
    ("Case Reports", "Case Report"),
    ("News", "News"),
)


class PubMedSearchError(RuntimeError):
    """ESearch failed, including 200 OK responses carrying an error payload."""


def _flatten(node: ET.Element | None) -> str:
    """Return an element's full text, including inline markup.

    Titles and abstracts carry ``<i>``/``<sup>``/``<sub>``/``<b>``. Reading
    ``.text`` stops at the first child element, silently truncating e.g.
    "Chronic PM<sub>2.5</sub> Exposure and ..." to "Chronic PM".
    """
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


def classify_pub_type(pub_types: list[str]) -> str:
    """Pick the most informative publication type from PubMed's list."""
    pub_set = set(pub_types)
    for key, label in PUB_TYPE_PRIORITY:
        if key in pub_set:
            return label
    if "Journal Article" in pub_set:
        return "Original"
    return pub_types[0] if pub_types else ""


class PubMedClient:
    """Async client for the PubMed E-utilities API."""

    def __init__(
        self,
        api_key: str,
        email: str = "",
        tool: str = "robust-lit-review",
    ) -> None:
        self.api_key = api_key
        self.email = email
        self.tool = tool
        self._client = httpx.AsyncClient(base_url=_BASE_URL, timeout=30.0)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:  # noqa: ANN001
        await self.close()

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    def _params(self, **extra: object) -> dict:
        """Build E-utilities params, identifying this client on every call.

        NCBI asks every client to send ``tool`` + ``email``; doing so puts us in
        a higher-trust bucket and surfaces a real contact in their logs instead
        of us being throttled silently. The API key (10 req/s rather than 3) is
        sent only when one is configured.
        """
        params: dict = {"db": "pubmed", "tool": self.tool}
        if self.email:
            params["email"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key
        params.update(extra)
        return params

    # ------------------------------------------------------------------
    # ESearch
    # ------------------------------------------------------------------

    @retry(wait=wait_exponential(min=1, max=10), stop=stop_after_attempt(3), reraise=True)
    async def search(
        self,
        query: str,
        max_results: int = 100,
        date_from: int | None = None,
        date_to: int | None = None,
        sort: str = "relevance",
    ) -> list[str]:
        """Search PubMed and return a list of PMIDs.

        When *date_from* is set, the publication-date range is appended to the
        term using PubMed's ``[pdat]`` syntax (e.g. ``2016:2026[pdat]``).

        *sort* defaults to ``relevance``: E-utilities otherwise returns the most
        recently indexed records first, so a capped search would return only
        this year's papers and miss the landmark trials a review is built on.
        Pass ``"pub_date"`` for a feed-style newest-first listing.

        Raises:
            PubMedSearchError: after the retries are exhausted. NCBI answers
                200 OK with an ``ERROR`` payload, or with a body missing
                ``idlist``, when it is under load; both are transient, so they
                are raised rather than returned as a silent zero-hit search.
        """
        term = query
        if date_from is not None:
            hi = date_to if date_to is not None else 3000
            term = f"({query}) AND {date_from}:{hi}[pdat]"

        resp = await self._client.get(
            "/esearch.fcgi",
            params=self._params(retmode="json", retmax=max_results, term=term, sort=sort),
        )
        resp.raise_for_status()
        try:
            result = resp.json().get("esearchresult", {})
        except ValueError as e:
            raise PubMedSearchError(f"esearch returned non-JSON: {e}") from e

        if "ERROR" in result:
            raise PubMedSearchError(f"NCBI ERROR: {result['ERROR']!r}")
        if "idlist" not in result:
            raise PubMedSearchError(f"esearch body missing 'idlist'; keys={sorted(result)}")

        pmids: list[str] = result["idlist"]
        logger.info("PubMed search returned %d PMIDs for query: %s", len(pmids), query)
        return pmids

    @retry(wait=wait_exponential(min=1, max=10), stop=stop_after_attempt(3), reraise=True)
    async def count(self, query: str, date_from: int | None = None, date_to: int | None = None) -> int:
        """Total hits for *query* without fetching any IDs (``retmax=0``)."""
        term = query
        if date_from is not None:
            hi = date_to if date_to is not None else 3000
            term = f"({query}) AND {date_from}:{hi}[pdat]"
        resp = await self._client.get("/esearch.fcgi", params=self._params(retmode="json", retmax=0, term=term))
        resp.raise_for_status()
        result = resp.json().get("esearchresult", {})
        if "ERROR" in result:
            raise PubMedSearchError(f"NCBI ERROR: {result['ERROR']!r}")
        return int(result.get("count", 0) or 0)

    async def search_by_dois(self, dois: list[str], chunk: int = 50) -> list[str]:
        """Resolve DOIs to PMIDs with ``[AID]`` lookups, many per request.

        Unknown DOIs (e.g. ahead-of-print not yet indexed) simply produce no
        PMID; the caller matches records back by the DOI on the fetched record.
        """
        pmids: list[str] = []
        for start in range(0, len(dois), chunk):
            batch = [d.strip() for d in dois[start : start + chunk] if d and d.strip()]
            if not batch:
                continue
            term = " OR ".join(f'"{d}"[AID]' for d in batch)
            try:
                pmids.extend(await self.search(term, max_results=len(batch) * 2))
            except PubMedSearchError as e:
                logger.warning("DOI->PMID lookup failed for a batch of %d: %s", len(batch), e)
        return pmids

    # ------------------------------------------------------------------
    # EFetch
    # ------------------------------------------------------------------

    @retry(wait=wait_exponential(min=1, max=10), stop=stop_after_attempt(3), reraise=True)
    async def _fetch_batch(self, pmids: list[str]) -> str:
        """Fetch a single batch of articles as XML text."""
        resp = await self._client.get(
            "/efetch.fcgi",
            params=self._params(retmode="xml", rettype="abstract", id=",".join(pmids)),
        )
        resp.raise_for_status()
        return resp.text

    async def fetch_articles(self, pmids: list[str]) -> list[dict]:
        """Fetch article details for *pmids* in batches of 200.

        Returns a list of dicts with keys: title, authors, abstract, doi,
        pmid, year, journal, issn, volume, issue, pages, pub_types, pub_type.
        """
        articles: list[dict] = []
        batch_size = 200
        for start in range(0, len(pmids), batch_size):
            batch = pmids[start : start + batch_size]
            try:
                xml_text = await self._fetch_batch(batch)
                articles.extend(self._parse_articles_xml(xml_text))
            except Exception:
                logger.exception("Failed to fetch batch starting at index %d", start)
        return articles

    # ------------------------------------------------------------------
    # Combined search + fetch
    # ------------------------------------------------------------------

    async def search_and_fetch(
        self,
        query: str,
        max_results: int = 100,
        date_from: int | None = None,
        date_to: int | None = None,
        sort: str = "relevance",
    ) -> list[ArticleMetadata]:
        """Search PubMed and return fully-populated ArticleMetadata objects."""
        pmids = await self.search(
            query, max_results=max_results, date_from=date_from, date_to=date_to, sort=sort
        )
        if not pmids:
            return []
        raw_articles = await self.fetch_articles(pmids)
        return [
            ArticleMetadata(
                source_db=DatabaseSource.PUBMED,
                **article,
            )
            for article in raw_articles
        ]

    # ------------------------------------------------------------------
    # XML parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_articles_xml(xml_text: str) -> list[dict]:
        """Parse EFetch XML into a list of article dicts."""
        articles: list[dict] = []
        try:
            root = ET.fromstring(xml_text)  # noqa: S314
        except ET.ParseError:
            logger.exception("Failed to parse PubMed XML response")
            return articles

        for article_el in root.findall(".//PubmedArticle"):
            try:
                articles.append(PubMedClient._parse_single_article(article_el))
            except Exception:
                logger.exception("Failed to parse a PubmedArticle element")
        return articles

    @staticmethod
    def _parse_single_article(article_el: ET.Element) -> dict:
        """Extract metadata from a single <PubmedArticle> element."""
        citation = article_el.find("MedlineCitation")
        article = citation.find("Article") if citation is not None else None

        def _text(el: ET.Element | None, path: str) -> str:
            node = el.find(path) if el is not None else None
            return (node.text or "").strip() if node is not None else ""

        # Title — flattened so inline markup does not truncate it.
        title = _flatten(article.find("ArticleTitle") if article is not None else None)

        # Authors — "Last, First"; ArticleMetadata.citation_key splits on the comma.
        authors: list[str] = []
        author_list = article.find("AuthorList") if article is not None else None
        if author_list is not None:
            for author in author_list.findall("Author"):
                last = _text(author, "LastName")
                fore = _text(author, "ForeName")
                if last:
                    authors.append(f"{last}, {fore}" if fore else last)

        # Abstract — structured abstracts repeat AbstractText with a Label.
        abstract_parts: list[str] = []
        abstract_el = article.find("Abstract") if article is not None else None
        if abstract_el is not None:
            for abs_text in abstract_el.findall("AbstractText"):
                text = _flatten(abs_text)
                label = abs_text.get("Label")
                abstract_parts.append(f"{label}: {text}" if label else text)
        abstract = " ".join(p for p in abstract_parts if p)

        # Journal metadata
        journal_el = article.find("Journal") if article is not None else None
        journal = _text(journal_el, "Title")
        # ISSN — prefer linking ISSN; needed for quartile resolution downstream.
        issn = _text(journal_el, "ISSN")
        if not issn and citation is not None:
            issn = _text(citation, "MedlineJournalInfo/ISSNLinking")
        journal_issue = journal_el.find("JournalIssue") if journal_el is not None else None
        volume = _text(journal_issue, "Volume")
        issue = _text(journal_issue, "Issue")

        year = PubMedClient._extract_year(article, journal_issue)
        pages = _text(article, "Pagination/MedlinePgn")
        doi = PubMedClient._extract_doi(article_el, article)

        pmid = _text(citation, "PMID")

        pub_types = [
            (t.text or "").strip()
            for t in article_el.findall(".//PublicationTypeList/PublicationType")
            if (t.text or "").strip()
        ]

        return {
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "doi": doi or None,
            "pmid": pmid or None,
            "year": year,
            "journal": journal,
            "issn": issn or None,
            "volume": volume or None,
            "issue": issue or None,
            "pages": pages or None,
            "pub_types": pub_types,
            "pub_type": classify_pub_type(pub_types),
        }

    @staticmethod
    def _extract_doi(article_el: ET.Element, article: ET.Element | None) -> str:
        """Find the DOI, checking both places PubMed publishes it.

        ``Article/ELocationID`` is the documented home, but a large share of
        records carry the DOI only in ``PubmedData/ArticleIdList``. Missing it
        costs the record its CrossRef verification and drops it from the
        appraisal, so both are consulted.
        """
        if article is not None:
            for eloc in article.findall("ELocationID"):
                if eloc.get("EIdType") == "doi" and (eloc.text or "").strip():
                    return (eloc.text or "").strip()
        for id_node in article_el.findall(".//ArticleId"):
            if id_node.get("IdType") == "doi" and (id_node.text or "").strip():
                return (id_node.text or "").strip()
        return ""

    @staticmethod
    def _extract_year(
        article: ET.Element | None, journal_issue: ET.Element | None
    ) -> int | None:
        """Publication year, preferring the ePub date.

        ``ArticleDate`` is when the paper actually appeared; ``PubDate`` is the
        issue it was later assigned to, which can be a year later and would push
        a record across a year cutoff it should not cross. ``MedlineDate``
        ("2016 Nov-Dec") is the final fallback — without it such records have no
        year at all and ``filter_by_year`` drops them.
        """
        candidates: list[str] = []
        if article is not None:
            candidates += [d.findtext("Year", "") for d in article.findall("ArticleDate")]
        if journal_issue is not None:
            pub_date = journal_issue.find("PubDate")
            if pub_date is not None:
                candidates.append(pub_date.findtext("Year", ""))
                candidates.append((pub_date.findtext("MedlineDate", "") or "")[:4])
        for value in candidates:
            value = (value or "").strip()
            if value.isdigit():
                return int(value)
        return None
