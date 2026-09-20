#!/usr/bin/env python3
"""Unit tests for pipeline/hca.py — the High Court fetcher. Offline: every request is
served from fixtures shaped like the Court's pages as they were on 19/09/2026.

    python pipeline/test_hca.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hca  # noqa: E402

ROW = """
<div class="views-row"><div class="views-field views-field-nothing-2"><span class="field-content">
<a href="/cases-and-judgments/judgments/judgments-1998-current/{slug}" class="views-row-item views-row-item-judgement">
<div class="field field--title text-bold">{name}</div><br>
<div class="field field--citation"><strong>Citation:</strong> {cite}</div>
<div class="field field--legacy-before"><div class="field field--name-field-hca-justices field__item"><strong>Before:</strong> {coram}</div></div>
<div class="field field--hca-date-issued"><strong>Date:</strong> {date}</div>
<div class="field field--hca-matter-number"><strong>Case Number:</strong> {num}</div>
<span class="more-link">Read more</span></a></span></div></div>
"""


def listing(rows):
    return ('<div class="view-content">' + "".join(ROW.format(**r) for r in rows) +
            '</div><nav class="pager"><a href="?page=1">Next page</a></nav>')


PAGE0 = listing([
    dict(slug="dale-haines-v-attorney-general-nsw", name="Dale Haines by his litigation guardian Barbara Ramjan v Attorney General (NSW)",
         cite="[2026] HCA 34", coram="Edelman, Jagot, Beech-Jones JJ", date="09 Sep 2026", num="S57/2026"),
    dict(slug="king-v-ko", name="The King v Ko", cite="[2026] HCA 29",
         coram="Gageler CJ, Gordon, Edelman, Steward, Gleeson, Jagot, Beech-Jones JJ", date="12 Aug 2026", num="S172/2025"),
])
PAGE1 = listing([
    dict(slug="farrugia-v-king", name="Farrugia v The King", cite="[2026] HCA 28", coram="Gageler CJ", date="06 Aug 2026", num="M12/2026"),
])
PAGE_OLD = listing([
    dict(slug="old-v-older", name="Old v Older", cite="[2024] HCA 3", coram="Gageler CJ", date="01 Feb 2024", num="S1/2024"),
])
KO_PAGE = """
<h1 class="title page-title">The King v Ko</h1>
<div class="citation-wrapper"><span class="citation">[2026] HCA 29</span></div>
<div class="field field--name-field-hca-date-issued field--type-datetime field--label-visually_hidden">
  <div class="field__label visually-hidden">Judgment date</div><div class="field__item">12 August 2026</div></div>
<div class="field field--name-field-hca-matter-number field--type-string field--label-above">
  <div class="field__label">Case number</div><div class="field__item">S172/2025</div></div>
<div class="field field--name-field-hca-justices field--type-string field--label-above">
  <div class="field__label">Before</div><div class="field__item">Gageler CJ, Gordon, Edelman, Steward, Gleeson, Jagot, Beech-Jones JJ</div></div>
<div class="text-content clearfix field field--name-field-hca-catchwords field--type-text-long field--label-above">
  <div class="field__label">Catchwords</div><div class="field__item"><p>Criminal practice – Trial – Adequacy of jury directions – Attempted importation of commercial quantity of border controlled drug.</p></div></div>
<div class="field field--name-field-resource-files field--type-file field--label-above"><div class="field__label">Files</div><div class="field__items">
  <div class="field__item"><span class="file file--mime-application-vnd-openxmlformats-officedocument-wordprocessingml-document file--x-office-document">
    <a href="/sites/default/files/eresources/2026-08-12/HCA/The%20King%20v%20Ko%20%28S172-2025%29%20%5B2026%5D%20HCA%2029.docx">The King v Ko (S172-2025) [2026] HCA 29.docx</a> (163.69 KB)</span></div>
  <div class="field__item"><span class="file file--mime-application-pdf file--application-pdf">
    <a href="/sites/default/files/eresources/2026-08-12/HCA/The%20King%20v%20Ko%20%28S172-2025%29%20%5B2026%5D%20HCA%2029.pdf">The King v Ko (S172-2025) [2026] HCA 29.pdf</a> (401.5 KB)</span></div>
</div></div>
"""


def fake_get(pages):
    calls = []

    def get(url):
        calls.append(url)
        for key, body in pages.items():
            hit = ("&page=" not in url and "keywords" not in url) if key == "page0" else (key in url)
            if hit:
                return body.encode("utf-8") if isinstance(body, str) else body
        raise AssertionError(f"unexpected request {url}")
    get.calls = calls
    return get


def test_citation_normalises_and_rejects_other_courts():
    assert hca.norm_citation("Citation: [2026]  HCA 029") == "[2026] HCA 29"
    assert hca.norm_citation("[2025] HCASJ 7") == "[2025] HCASJ 7"
    assert hca.norm_citation("[2026] WASCA 111") is None
    assert hca.dmy("12 August 2026") == "12/08/2026" and hca.dmy("09 Sep 2026") == "09/09/2026"
    assert hca.dmy("not a date") == ""


def test_parse_listing_reads_every_field():
    rows = hca.parse_listing(PAGE0)
    assert [r["citation"] for r in rows] == ["[2026] HCA 34", "[2026] HCA 29"]
    ko = rows[1]
    assert ko["name"] == "The King v Ko" and ko["url"].endswith("/judgments-1998-current/king-v-ko")
    assert ko["date"] == "12/08/2026" and ko["caseNumber"] == "S172/2025"
    assert ko["coram"].startswith("Gageler CJ, Gordon")


def test_parse_judgment_page_reads_metadata_and_file_links():
    m = hca.parse_judgment_page(KO_PAGE, hca.BASE + "/x/king-v-ko")
    assert m["name"] == "The King v Ko" and m["citation"] == "[2026] HCA 29"
    assert m["decided"] == "12/08/2026" and m["caseNumber"] == "S172/2025"
    assert m["coram"].startswith("Gageler CJ")
    assert m["catchwords"].startswith("Criminal practice – Trial – Adequacy of jury directions")
    assert m["pdf"].endswith("%5B2026%5D%20HCA%2029.pdf") and m["pdf"].startswith("https://www.hcourt.gov.au/")
    assert m["docx"].endswith(".docx")
    assert "Catchwords" not in m["catchwords"]


def test_lookup_walks_pages_newest_first_and_stops_past_the_year():
    get = fake_get({"&page=1": PAGE1, "farrugia-v-king": KO_PAGE.replace("[2026] HCA 29", "[2026] HCA 28"),
                    "page0": PAGE0})
    m = hca.lookup("[2026] HCA 28", get=get)
    assert m and m["citation"] == "[2026] HCA 28"
    assert any("&page=1" in u for u in get.calls)
    # a 2024 citation: page 0 and 1 are all 2026, page 2 is 2024 and does not list it -> stop, None
    get = fake_get({"&page=1": PAGE1, "&page=2": PAGE_OLD, "&page=3": listing([]), "page0": PAGE0})
    assert hca.lookup("[2024] HCA 99", get=get) is None
    assert any("&page=2" in u for u in get.calls) and not any("&page=3" in u for u in get.calls)   # stopped past the year


def test_lookup_falls_back_to_the_keyword_search_with_a_distinctive_party():
    get = fake_get({"keywords=Haines": PAGE0, "dale-haines": KO_PAGE.replace("[2026] HCA 29", "[2026] HCA 34"),
                    "&page=1": listing([]), "page0": PAGE1})
    m = hca.lookup("[2026] HCA 34", name="Dale Haines by his litigation guardian Barbara Ramjan v Attorney General (NSW)", get=get)
    assert m and m["citation"] == "[2026] HCA 34"
    assert any("keywords=Haines" in u for u in get.calls)
    assert hca._first_party("The King v Ko") == ""      # nothing distinctive enough to search on
    assert hca._first_party("EGH19 v Minister for Immigration & Citizenship") == "EGH19"
    assert hca._first_party("R Lawyers v Mr Daily [No 2]") == "Lawyers"
    assert hca._first_party("Dale Haines by his litigation guardian Barbara Ramjan v Attorney General (NSW)") == "Haines"


def test_lookup_refuses_a_page_whose_citation_differs_from_the_listing():
    get = fake_get({"king-v-ko": KO_PAGE.replace("[2026] HCA 29", "[2026] HCA 30"), "page0": PAGE0})
    assert hca.lookup("[2026] HCA 29", get=get) is None


def test_fetch_text_downloads_the_pdf_through_the_shared_gate_and_labels_the_source():
    meta = hca.parse_judgment_page(KO_PAGE, hca.BASE + "/x/king-v-ko")
    seen = {}

    def clean(path, cite):
        seen["path"] = path
        seen["cite"] = cite
        return "HIGH COURT OF AUSTRALIA ... [2026] HCA 29 ...", ["no counsel / solicitors block found"]
    get = fake_get({"HCA%2029.pdf": b"%PDF-1.5 fake"})
    text, warns, source = hca.fetch_text(meta, get=get, clean_and_report=clean)
    assert text.startswith("HIGH COURT") and warns == ["no counsel / solicitors block found"]
    assert seen["path"].endswith("The King v Ko - [2026] HCA 29.pdf") and seen["cite"] == "[2026] HCA 29"
    assert source == ("High Court of Australia (copy of the version at https://www.hcourt.gov.au/sites/default/files/"
                      "eresources/2026-08-12/HCA/The%20King%20v%20Ko%20%28S172-2025%29%20%5B2026%5D%20HCA%2029.pdf)")
    # a refusal from the gate propagates — nothing is attached
    def refuse(path, cite):
        raise ValueError("citation [2026] HCA 29 NOT found in the text — wrong file or wrong citation")
    try:
        hca.fetch_text(meta, get=get, clean_and_report=refuse)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "NOT found" in str(e)
    try:
        hca.fetch_text(dict(meta, pdf=""), get=get, clean_and_report=clean)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "no PDF link" in str(e)


def _main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERR  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed" + ("" if not failed else f", {failed} FAILED"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
