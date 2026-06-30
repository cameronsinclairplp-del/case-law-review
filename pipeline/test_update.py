#!/usr/bin/env python3
"""Unit tests for the pure helpers in update.py — the name-cleaning and scope
heuristics that decide what a Jade alert contributes to the watchlist.

These never touch the network, IMAP, or the Anthropic API. Run directly:

    python pipeline/test_update.py        # zero-dependency runner (needs bs4 only,
                                          # which update.py imports at module load)

or under pytest if available:

    pytest pipeline/test_update.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update as u  # noqa: E402


# ---------------------------------------------------------------------------
# clean_case_name — extract a clean "A v B" (or non-adversarial) party name from
# the messy text fragment that sits just before a citation in an alert.
# ---------------------------------------------------------------------------
def _name(raw, cite="[2026] WASC 1"):
    return u.clean_case_name(raw, cite)


def test_clean_name_plain_pair():
    assert _name("Stefanski v Western Australia") == "Stefanski v Western Australia"
    assert _name("Garlett v Western Australia & Anor") == "Garlett v Western Australia & Anor"


def test_clean_name_keeps_no_suffix():
    # the "[No 2]" / "(No 3)" hearing-number suffix is part of the name
    assert _name("The State of Western Australia v Raven [No 2]") == \
        "The State of Western Australia v Raven [No 2]"
    assert _name("Smith v Jones (No 3)") == "Smith v Jones (No 3)"


def test_clean_name_strips_leading_junk():
    # a leading court / preamble fragment must not be swallowed into the party name
    assert _name("Court of Appeal. The State of Western Australia v Mead [No 2]") == \
        "The State of Western Australia v Mead [No 2]"
    assert _name("Court of Appeal. The State of Western Australia v Mead") == \
        "The State of Western Australia v Mead"
    assert _name("New decision: Western Australia v Montani") == "Western Australia v Montani"
    assert _name("In re something. Smith v Jones") == "Smith v Jones"


def test_clean_name_strips_parallel_citations():
    assert _name("Smith v Jones (2020) 270 CLR 1", "[2020] HCA 5") == "Smith v Jones"
    assert _name("Smith v Jones (2020) 270 CLR 1;", "[2020] HCA 5") == "Smith v Jones"
    assert _name("Foo v Bar (2019) 55 WAR 12; [2019] WASCA 7", "[2019] WASCA 7") == "Foo v Bar"


def test_clean_name_keeps_abbreviated_state():
    # "W.A." must not be mistaken for a sentence boundary and truncate the plaintiff
    assert _name("The State of W.A. v Smith") == "The State of W.A. v Smith"


def test_clean_name_pseudonym():
    assert _name("RCB v The State of Western Australia (a pseudonym)") == \
        "RCB v The State of Western Australia (a pseudonym)"
    assert _name("Smith (a pseudonym) v The Queen") == "Smith (a pseudonym) v The Queen"


def test_clean_name_non_adversarial():
    # no "A v B" pair — fall back to the trailing fragment, intact
    assert _name("Re Jones; Ex parte Smith") == "Re Jones; Ex parte Smith"
    assert _name("Ex parte Coward") == "Ex parte Coward"


def test_clean_name_empty():
    assert _name("") == "(case name pending)"
    assert _name("   ", "[2026] WASC 1") == "(case name pending)"


# ---------------------------------------------------------------------------
# in_scope — the WASC/WASCA name-only civil filter. A criminal case must never be
# dropped; obvious civil matters (with no criminal-party signal) should be.
# ---------------------------------------------------------------------------
def _wa(name, tag="WASC"):
    return u.in_scope({"courtTag": tag, "caseName": name, "blurb": name})


CRIMINAL_KEEP = [
    "The State of Western Australia v Raven [No 2]",
    "Western Australia v Montani",
    "Stefanski v Western Australia",
    "RCB v The State of Western Australia (a pseudonym)",
    "The Queen v Smith",
    "Police v Jones",
    "DPP (WA) v Smith Holdings Pty Ltd",          # DPP signal beats the "Pty Ltd" civil word
    "Western Australia v BHP Billiton Iron Ore Pty Ltd",  # State prosecuting a company — keep
    "Inquest into the death of John Citizen",
    "The State of W.A. v Smith",
]

CIVIL_DROP = [
    "Westpac Banking Corporation Ltd v Smith",
    "ABC Nominees Pty Ltd v Minister for Lands (Western Australia)",
    "Bob Jane Corporation Pty Ltd v Sheriff of Western Australia",
    "Commissioner of State Revenue v Acme Holdings Pty Ltd",
    "Legal Profession Complaints Committee v Smith",
    "XYZ Pty Ltd v State Administrative Tribunal",
    "Insurance Commission of Western Australia v Jones",
]


def test_in_scope_keeps_criminal():
    for n in CRIMINAL_KEEP:
        ok, why = _wa(n)
        assert ok, f"criminal case wrongly dropped: {n!r} ({why})"


def test_in_scope_drops_civil():
    for n in CIVIL_DROP:
        ok, why = _wa(n)
        assert not ok, f"civil case wrongly kept: {n!r}"
        assert "civil party" in why, f"dropped for the wrong reason: {n!r} ({why})"


def test_in_scope_non_wa_courts_not_name_filtered():
    # HCA is not a name-only WA court: a civil-looking name is NOT dropped on that basis
    ok, _ = u.in_scope({"courtTag": "HCA", "caseName": "Westpac v Smith Pty Ltd",
                        "blurb": "Westpac v Smith Pty Ltd"})
    assert ok


def test_in_scope_drop_keywords_still_apply():
    ok, why = u.in_scope({"courtTag": "WASC", "caseName": "Smith v Minister for Immigration",
                         "blurb": "judicial review of a migration visa decision"})
    assert not ok and "out-of-scope topic" in why


def test_in_scope_gated_courts_need_topic():
    # a persuasive (gated) court with no investigation/evidence keyword is dropped
    ok, why = u.in_scope({"courtTag": "QCA", "caseName": "Re a costs dispute",
                         "blurb": "costs of a commercial appeal"})
    assert not ok and "no investigation/evidence topic" in why
    ok, _ = u.in_scope({"courtTag": "QCA", "caseName": "R v Smith",
                       "blurb": "admissibility of a confession at trial"})
    assert ok


# ---------------------------------------------------------------------------
# small regression locks for adjacent helpers
# ---------------------------------------------------------------------------
def test_jade_url_validation():
    assert u._is_jade_url("https://jade.io/article/123")
    assert u._is_jade_url("https://www.jade.io/x")
    assert not u._is_jade_url("https://evil.example/jade.io")
    assert not u._is_jade_url("javascript:alert(1)")
    assert not u._is_jade_url("")


def test_dmy_to_iso():
    assert u.dmy_to_iso("07/09/2022") == "2022-09-07"
    assert u.dmy_to_iso("7/9/2022") == "2022-09-07"
    assert u.dmy_to_iso("2022-09-07") == "2022-09-07"
    assert u.dmy_to_iso("September 2022") is None


# ---------------------------------------------------------------------------
# zero-dependency runner
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
