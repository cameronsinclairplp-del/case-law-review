#!/usr/bin/env python3
"""Unit tests for clean_word.py (stdlib only, no network). Run directly:

    python pipeline/test_clean_word.py

or under pytest. Covers: artifact stripping (bidi marks, <CRJ> export tags, HCA
page fields, footnote-continuation lines, blank-line collapse), prose left
untouched, idempotence on already-clean text, and the integrity report's
REFUSE / WARN / OK decisions (wrong citation, digest markers, later years).
"""
import io
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clean_word as cw  # noqa: E402

SYNTH = (
    "JURISDICTION\t:\tSUPREME COURT OF WESTERN AUSTRALIA\n"
    "CITATION\t:\tSTATE OF WESTERN AUSTRALIA -v- TEST [2026] WASC 1\n"
    "CORAM\t:\tWHITBY J\n\n\n\n"
    "\u200fCriminal law - test catchword\n"
    "<CRJ>\nSmith v Jones [2020] HCA 5\n</CRJ>\n"
    "PAGE 12.\n(Footnote continues on next page)\n"
    "Counsel: A B for the applicant   \n"
    "WHITBY J: The respondent was sentenced in 2027 hypothetically.\n"
)


def _report(text, cite):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cw.report(text, cite)
    return rc, buf.getvalue()


def test_strips_artifacts_keeps_prose():
    out = cw.clean(SYNTH)
    assert "\u200f" not in out                       # bidi mark gone
    assert "<CRJ>" not in out and "</CRJ>" not in out  # export tags gone …
    assert "Smith v Jones [2020] HCA 5" in out        # … but the cases-referred list kept
    assert "PAGE 12." not in out
    assert "Footnote continues" not in out
    assert "\n\n\n" not in out                        # 3+ blanks collapsed
    assert "Counsel: A B for the applicant\n" in out  # trailing whitespace trimmed
    assert "WHITBY J: The respondent was sentenced in 2027 hypothetically." in out


def test_strips_a_pseudo_tag_welded_to_a_text_line():
    # Regression, [2019] WASC 84: eCourts closed the "cases referred to" block by
    # appending </CRJ> to the LAST ENTRY rather than putting it on its own line, so
    # the whole-line rule missed it and the tag reached the verbatim judgment.
    out = cw.clean(
        "<CRJ>\n"
        "Drago v The Queen (1992) 8 WAR 488\n"
        "The State of Western Australia v Staniforth-Smith [2014] WASCA 170</CRJ>\n"
        "SMITH J: The appellant seeks leave to appeal.\n")
    assert "CRJ" not in out                                     # no tag anywhere, welded or not
    assert "The State of Western Australia v Staniforth-Smith [2014] WASCA 170\n" in out
    assert "Drago v The Queen (1992) 8 WAR 488" in out          # the list itself survives
    assert "SMITH J: The appellant seeks leave to appeal." in out


def test_inline_tag_strip_leaves_prose_punctuation_alone():
    # The inline rule must not eat ordinary angle brackets or lower-case markup-ish
    # text that can legitimately appear in judgment prose.
    out = cw.clean("The ratio was expressed as x < y > z in the expert report.\n"
                   "An email header read <not a tag> and stays.\n")
    assert "x < y > z" in out
    assert "<not a tag>" in out


# ---------------------------------------------------------------------------
# BarNet Jade PDF wrapper (added session 8, [2026] WASCA 33)
# ---------------------------------------------------------------------------
JADE_PDF = (
    "BarNet Jade\n"
    "Pellew v The King - [2026] WASCA 33\n\n"
    "BarNet publication information - Date: Thursday, 10.09.2026 - - Publication "
    "number: 00081 - - User: someone@example.com\n\n"
    "jade.io\n\n"
    "View this document in a browser\n\n"
    "Attribution\n\n"
    "JURISDICTION\n\n"
    "Original court site URL:\n\n"
    "file:/2026WASCA0033.doc\n\n"
    "Content received from\ncourt:\n\n"
    "July 03, 2026\n\n"
    "Download/print date:\n\n"
    "September 10, 2026\n\n"
    ": SUPREME COURT OF WESTERN AUSTRALIA\n\n"
    "CITATION : PELLEW -v- THE KING [2026] WASCA 33\n\n"
    "CORAM\n\n"
    ": QUINLAN CJ\nSWEENEY JA\n\n"
    "1. The appellant was convicted of an offence.\n"
)


def test_jade_wrapper_is_stripped_including_the_subscriber_email():
    out = cw.strip_jade_wrapper(JADE_PDF)
    # the footer carries the subscriber's email on EVERY page — it must never be
    # published to the repo, so this assertion is a privacy guard, not a tidy-up
    assert "someone@example.com" not in out
    assert "BarNet publication information" not in out
    assert "BarNet Jade" not in out and "jade.io" not in out
    assert "View this document in a browser" not in out
    assert "Attribution" not in out and "Download/print date" not in out
    assert "file:/2026WASCA0033.doc" not in out
    assert "July 03, 2026" not in out and "September 10, 2026" not in out
    # Jade's cover-page title is wrapper too; the court's CITATION line carries it
    assert "Pellew v The King - [2026] WASCA 33" not in out
    # ...and the judgment itself survives intact
    assert "SUPREME COURT OF WESTERN AUSTRALIA" in out
    assert "QUINLAN CJ" in out and "SWEENEY JA" in out
    assert "1. The appellant was convicted of an offence." in out


def test_jade_header_label_trapped_inside_the_attribution_box_survives():
    # pdftotext lays the attribution TABLE between "JURISDICTION" and its value.
    # Losing the label would break the reader's masthead.
    out = cw.strip_jade_wrapper(JADE_PDF)
    assert "JURISDICTION" in out


def test_pdf_header_is_rejoined_into_the_word_shape():
    # the app's masthead keys on the literal tab, so "LABEL\n: VALUE" must become
    # "LABEL\t:\tVALUE" — the same shape a Word export produces
    out = cw.rejoin_pdf_header(cw.strip_jade_wrapper(JADE_PDF))
    assert "JURISDICTION\t:\tSUPREME COURT OF WESTERN AUSTRALIA" in out
    assert "CORAM\t:\tQUINLAN CJ" in out
    assert "SWEENEY JA" in out                      # continuation line left alone


def test_rejoin_never_touches_body_prose():
    # only the fixed WA_HEADER_LABEL set is rejoined; a colon line in the reasons
    # must be left exactly as it is
    body = "17. His Honour said\n: this is not a header\n"
    assert cw.rejoin_pdf_header(body) == body


def test_citator_contamination_is_refused():
    # content-based, not source-based: a Jade PDF may be a clean copy of the
    # court's document OR the annotated citator view. Judge the file, not its origin.
    bad = (SYNTH + "Following paragraph cited by:\nJones v The King [2027] WASCA 12 at [44]\n")
    rc, out = _report(cw.clean(bad), "[2026] WASC 1")
    assert rc == 2, out
    assert "citator contamination" in out


def test_clean_jade_pdf_text_is_not_refused():
    # the whole point of making the check content-based: a clean copy passes
    rc, out = _report(cw.clean(cw.rejoin_pdf_header(cw.strip_jade_wrapper(
        JADE_PDF + "Counsel: A B for the appellant\n"))), "[2026] WASCA 33")
    assert rc == 0, out
    assert "citator contamination" not in out


def test_idempotent_on_clean_text():
    once = cw.clean(SYNTH)
    assert cw.clean(once) == once


def test_report_ok_with_later_year_warning():
    rc, out = _report(cw.clean(SYNTH), "[2026] WASC 1")
    assert rc == 0
    assert "WARN" in out and "2027" in out           # later year surfaced for a human, not refused
    assert "REFUSE" not in out


def test_report_refuses_wrong_citation():
    rc, out = _report(cw.clean(SYNTH), "[2026] WASC 999")
    assert rc == 2 and "REFUSE" in out and "NOT found" in out


def test_report_refuses_digest():
    digest = "CaseBase\nCatchwords & Digest\n[2026] WASC 1 Held (i) ... End of Document"
    rc, out = _report(digest, "[2026] WASC 1")
    assert rc == 2 and "digest" in out.lower()


def test_report_warns_when_not_full_text():
    rc, out = _report("Some text mentioning [2026] WASC 1 only.", "[2026] WASC 1")
    assert rc == 0 and "no coram" in out and "no counsel" in out


def test_missing_input_exits():
    try:
        cw.to_text(cw.Path("/nonexistent/file.doc"))
    except SystemExit as e:
        assert "not found" in str(e)
    else:
        raise AssertionError("to_text must exit on a missing input")


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
