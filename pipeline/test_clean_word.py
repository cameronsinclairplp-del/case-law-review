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
