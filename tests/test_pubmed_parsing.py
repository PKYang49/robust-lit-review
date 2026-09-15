"""Parsing rules for the PubMed EFetch XML.

Each case here corresponds to a way a record used to be silently damaged or
dropped: truncated at inline markup, stripped of a DOI that lived only in
ArticleIdList, or left with no year (and so removed by ``filter_by_year``).
"""

from __future__ import annotations

import pytest

from litreview.clients.pubmed import PubMedClient, classify_pub_type

# One record per failure mode.
#  - 31111111: inline <sub> in the title, structured abstract with inline <i>,
#              DOI only in ArticleIdList, ArticleDate a year before PubDate.
#  - 31222222: no ArticleDate and no PubDate/Year — only a MedlineDate.
SAMPLE_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>31111111</PMID>
      <Article>
        <Journal>
          <ISSN>1234-5678</ISSN>
          <JournalIssue>
            <Volume>42</Volume>
            <Issue>7</Issue>
            <PubDate><Year>2021</Year></PubDate>
          </JournalIssue>
          <Title>Journal of Testing</Title>
        </Journal>
        <ArticleTitle>Chronic PM<sub>2.5</sub> Exposure and Risk</ArticleTitle>
        <Pagination><MedlinePgn>101-110</MedlinePgn></Pagination>
        <Abstract>
          <AbstractText Label="METHODS">We enrolled <i>n</i> = 240 adults.</AbstractText>
          <AbstractText Label="RESULTS">Risk rose (p &lt; 0.001).</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Lin</LastName><ForeName>Hsieh-Ting</ForeName></Author>
          <Author><LastName>Chen</LastName></Author>
        </AuthorList>
        <ArticleDate DateType="Electronic"><Year>2020</Year><Month>11</Month></ArticleDate>
        <PublicationTypeList>
          <PublicationType>Journal Article</PublicationType>
          <PublicationType>Randomized Controlled Trial</PublicationType>
        </PublicationTypeList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">31111111</ArticleId>
        <ArticleId IdType="doi">10.1000/only-in-articleidlist</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>31222222</PMID>
      <Article>
        <Journal>
          <JournalIssue>
            <PubDate><MedlineDate>2016 Nov-Dec</MedlineDate></PubDate>
          </JournalIssue>
          <Title>Journal of Fallbacks</Title>
        </Journal>
        <ArticleTitle>A record with only a MedlineDate</ArticleTitle>
        <ELocationID EIdType="doi">10.1000/in-elocationid</ELocationID>
        <PublicationTypeList>
          <PublicationType>Journal Article</PublicationType>
          <PublicationType>Observational Study</PublicationType>
          <PublicationType>Review</PublicationType>
        </PublicationTypeList>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>
"""


@pytest.fixture(scope="module")
def parsed() -> list[dict]:
    return PubMedClient._parse_articles_xml(SAMPLE_XML)


def test_both_records_parse(parsed):
    assert len(parsed) == 2


def test_title_keeps_text_after_inline_markup(parsed):
    # `.text` would stop at <sub> and yield "Chronic PM".
    assert parsed[0]["title"] == "Chronic PM2.5 Exposure and Risk"


def test_structured_abstract_keeps_labels_and_inline_markup(parsed):
    abstract = parsed[0]["abstract"]
    assert abstract.startswith("METHODS: We enrolled n = 240 adults.")
    assert "RESULTS: Risk rose (p < 0.001)." in abstract


def test_doi_found_in_articleidlist(parsed):
    # Not present in ELocationID at all — the old lookup returned None here,
    # which costs the record its CrossRef verification.
    assert parsed[0]["doi"] == "10.1000/only-in-articleidlist"


def test_doi_still_found_in_elocationid(parsed):
    assert parsed[1]["doi"] == "10.1000/in-elocationid"


def test_epub_articledate_beats_issue_pubdate(parsed):
    # ArticleDate 2020 vs PubDate 2021: the paper appeared in 2020.
    assert parsed[0]["year"] == 2020


def test_medlinedate_is_used_when_no_year_element(parsed):
    # Without this fallback the record has year=None and filter_by_year drops it.
    assert parsed[1]["year"] == 2016


def test_authors_keep_last_comma_first_for_citation_keys(parsed):
    assert parsed[0]["authors"] == ["Lin, Hsieh-Ting", "Chen"]


def test_pub_type_prefers_highest_evidence_label(parsed):
    assert parsed[0]["pub_type"] == "RCT"
    assert "Journal Article" in parsed[0]["pub_types"]


def test_observational_outranks_review(parsed):
    assert parsed[1]["pub_type"] == "Observational"


def test_other_metadata_still_extracted(parsed):
    first = parsed[0]
    assert first["pmid"] == "31111111"
    assert first["issn"] == "1234-5678"
    assert first["journal"] == "Journal of Testing"
    assert first["volume"] == "42"
    assert first["issue"] == "7"
    assert first["pages"] == "101-110"


@pytest.mark.parametrize(
    ("pub_types", "expected"),
    [
        (["Journal Article", "Meta-Analysis", "Review"], "Meta-Analysis"),
        (["Journal Article"], "Original"),
        ([], ""),
        (["Some Unmapped Type"], "Some Unmapped Type"),
    ],
)
def test_classify_pub_type(pub_types, expected):
    assert classify_pub_type(pub_types) == expected
