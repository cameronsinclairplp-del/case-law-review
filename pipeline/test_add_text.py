#!/usr/bin/env python3
"""Unit tests for pipeline/add_text.py — the full-text-only ingest route.

These matter more than most: add_text.py replaces a model call with deterministic
parsing, so the case NAME and the DELIVERED date now come from regexes rather than
from Opus. If one of these silently mis-reads, a case goes into the library under
the wrong name or sorts to the wrong place, with nothing to catch it.

No network, no IMAP, no Anthropic API. Run directly:

    python pipeline/test_add_text.py        # needs bs4 importable (update.py imports it)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import add_text as A  # noqa: E402


# A faithful slice of the real [2019] WASC 84 eCourts Word header.
WASC_HEADER = (
    "JURISDICTION\t:\tSUPREME COURT OF WESTERN AUSTRALIA\n"
    "\t\tIN CRIMINAL\n"
    "CITATION\t:\tJOHNSON -v- RAMSDEN [2019] WASC 84\n"
    "CORAM\t:\tSMITH J\n"
    "HEARD\t:\t5 MARCH 2019\n"
    "DELIVERED\t:\t5 MARCH 2019\n"
    "PUBLISHED\t:\t15 MARCH 2019\n"
    "FILE NO/S\t:\tSJA 1036 of 2018\n"
)


# ---------------------------------------------------------------------------
# citation_from_filename — batch mode derives the citation from the file name
# ---------------------------------------------------------------------------
def test_citation_from_filename():
    f = A.citation_from_filename
    assert f("2019WASC0084 indecent.docx") == "[2019] WASC 84"
    assert f("2026WASCA0104.doc") == "[2026] WASCA 104"
    assert f("2026HCA0028.txt") == "[2026] HCA 28"
    assert f("/some/dir/2026WASC0380.doc") == "[2026] WASC 380"


def test_citation_from_filename_longest_court_token_wins():
    # "WASCA" must not be read as "WASC" + a stray "A"; likewise HCASJ over HCA.
    assert A.citation_from_filename("2026WASCA0091.doc") == "[2026] WASCA 91"
    assert A.citation_from_filename("2026HCASJ0031.doc") == "[2026] HCASJ 31"


def test_citation_from_filename_rejects_junk():
    for n in ("random notes.docx", "scan.pdf", "notes 2026.docx", ""):
        assert A.citation_from_filename(n) is None, n


# ---------------------------------------------------------------------------
# titlecase_party — proper-case a SHOUTED header name without wrecking initials
# ---------------------------------------------------------------------------
def test_titlecase_converts_ecourts_v_separator():
    assert A.titlecase_party("JOHNSON -v- RAMSDEN") == "Johnson v Ramsden"


def test_titlecase_keeps_initials_only_parties_upper():
    # WA identity-protection convention: short all-caps tokens are initials or a
    # pseudonym code and must survive verbatim. "Mrv V Snw" would be a real defect.
    assert A.titlecase_party("MRV -v- SNW") == "MRV v SNW"
    assert A.titlecase_party("THE STATE OF WESTERN AUSTRALIA -v- TJD") == \
        "The State of Western Australia v TJD"
    assert A.titlecase_party("DJF -v- DIRECTOR OF PUBLIC PROSECUTIONS") == \
        "DJF v Director of Public Prosecutions"


def test_titlecase_lowercases_connectives_but_not_a_leading_one():
    assert A.titlecase_party("THE STATE OF WESTERN AUSTRALIA -v- BLURTON") == \
        "The State of Western Australia v Blurton"
    assert A.titlecase_party("RE FRIGGER") == "Re Frigger"     # leading word keeps its cap


def test_titlecase_leaves_already_proper_text_alone():
    assert A.titlecase_party("Johnson v Ramsden") == "Johnson v Ramsden"


def test_titlecase_starts_a_fresh_party_after_the_v():
    # the defendant's leading article is capitalised — the library writes
    # "Frigger v The State of Western Australia", never "v the State of ...".
    assert A.titlecase_party("GRM -v- THE STATE OF WESTERN AUSTRALIA") == \
        "GRM v The State of Western Australia"
    assert A.titlecase_party("PELLEW -v- THE KING") == "Pellew v The King"


def test_titlecase_keeps_the_pseudonym_formula_lower_case():
    # "(a pseudonym)" is a fixed lower-case formula in WA judgments.
    assert A.titlecase_party("WARREN (A PSEUDONYM) -v- THE STATE OF WESTERN AUSTRALIA") == \
        "Warren (a pseudonym) v The State of Western Australia"
    assert A.titlecase_party("THE STATE OF WESTERN AUSTRALIA -v- AIDEN SANTO (A PSEUDONYM)") == \
        "The State of Western Australia v Aiden Santo (a pseudonym)"


def test_titlecase_on_every_real_name_shape_in_the_backlog():
    """The shapes actually waiting in data/state.json and the aged-out list."""
    for shouted, want in [
        ("THE STATE OF WESTERN AUSTRALIA -v- TJD [No 2]",
         "The State of Western Australia v TJD [No 2]"),
        ("THE STATE OF WESTERN AUSTRALIA -v- MWX [No 2]",
         "The State of Western Australia v MWX [No 2]"),
        ("ATTORNEY GENERAL FOR WESTERN AUSTRALIA -v- JFE",
         "Attorney General for Western Australia v JFE"),
        ("PHOEBE BUCKLEY (A PSEUDONYM) -v- DIRECTOR OF PUBLIC PROSECUTIONS [No 2]",
         "Phoebe Buckley (a pseudonym) v Director of Public Prosecutions [No 2]"),
        ("SUPPRESSED", "Suppressed"),
        ("MRV -v- SNW [No 2]", "MRV v SNW [No 2]"),
    ]:
        got = A.titlecase_party(shouted)
        assert got == want, f"{shouted!r} -> {got!r}, wanted {want!r}"


# ---------------------------------------------------------------------------
# name_from_text / decided_from_text — read from the judgment's own header
# ---------------------------------------------------------------------------
def test_name_from_the_citation_line():
    assert A.name_from_text(WASC_HEADER, "[2019] WASC 84") == "Johnson v Ramsden"


def test_name_is_empty_rather_than_guessed():
    # no CITATION line -> "" (add_one then writes "(case name pending)" and logs it)
    assert A.name_from_text("no header here at all\n", "[2019] WASC 84") == ""


def test_decided_prefers_delivered_over_heard_and_published():
    # HEARD 5 MARCH, DELIVERED 5 MARCH, PUBLISHED 15 MARCH -> the DELIVERED date.
    assert A.decided_from_text(WASC_HEADER) == "05/03/2019"


def test_decided_reads_a_two_digit_day_and_every_month():
    for mon, num in (("JANUARY", "01"), ("JUNE", "06"), ("SEPTEMBER", "09"), ("DECEMBER", "12")):
        got = A.decided_from_text(f"DELIVERED\t:\t17 {mon} 2026\n")
        assert got == f"17/{num}/2026", (mon, got)


def test_decided_reads_the_high_courts_own_template():
    """The HCA does not use the WA "DELIVERED :" line — it writes
    "Date of Judgment: 12 August 2026", with "Date of Hearing:" ABOVE it. Taking
    the hearing date would misdate the case and sort it wrongly in the library."""
    hca = ("HIGH COURT OF AUSTRALIA\n"
           "Farrugia v The King\n[2026] HCA 28\n"
           "Date of Hearing: 11 February 2026\n"
           "Date of Judgment: 12 August 2026\n"
           "S139/2025\n")
    assert A.decided_from_text(hca) == "12/08/2026"


def test_wa_delivered_still_wins_over_the_hca_pattern():
    both = ("CITATION\t:\tX -v- Y [2019] WASC 84\n"
            "DELIVERED\t:\t5 MARCH 2019\n"
            "Date of Judgment: 1 January 2020\n")
    assert A.decided_from_text(both) == "05/03/2019"


def test_decided_is_empty_rather_than_guessed():
    assert A.decided_from_text("DELIVERED\t:\tsome time in 2019\n") == ""
    assert A.decided_from_text("no delivered line\n") == ""


def test_decided_feeds_the_sort_key():
    # build_text_only_case turns DD/MM/YYYY into the ISO date cases.json sorts on,
    # and falls back to the citation year when the header has no delivered date.
    import update as P
    item = {"id": "wasc-2019-84", "citation": "[2019] WASC 84", "courtTag": "WASC",
            "year": "2019", "num": "84", "caseName": "Johnson v Ramsden"}
    c = A.build_text_only_case(item, WASC_HEADER, "src")
    assert c["date"] == "2019-03-05" and c["decided"] == "05/03/2019"
    c2 = A.build_text_only_case(item, "no header\n", "src")
    assert c2["date"] == "2019" and c2["decided"] == "2019"


# ---------------------------------------------------------------------------
# build_text_only_case — asserts nothing it hasn't read
# ---------------------------------------------------------------------------
def test_text_only_case_has_no_analysis_and_no_relevance():
    item = {"id": "wasc-2019-84", "citation": "[2019] WASC 84", "courtTag": "WASC",
            "year": "2019", "num": "84", "caseName": "Johnson v Ramsden"}
    c = A.build_text_only_case(item, WASC_HEADER, "src")
    # the whole point: an unclassified case must not claim a classification
    assert c["relevance"] == ""
    for f in ("oneLine", "whatHappened", "whatHeld", "whatItMeans", "verdict",
              "outcome", "weight", "appealFrom"):
        assert c[f] == "", f
    assert c["tags"] == []
    assert c["textOnly"] is True
    assert c["files"]["llm"] == "data/files/wasc-2019-84/wasc-2019-84.md"
    assert c["austliiUrl"].endswith("/wa/WASC/2019/84.html")


# ---------------------------------------------------------------------------
# zero-dependency runner (same shape as test_update.py)
# ---------------------------------------------------------------------------
def _main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
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
    total = len(tests)
    print(f"\n{total - failed}/{total} passed" + ("" if not failed else f", {failed} FAILED"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
