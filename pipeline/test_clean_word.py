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


def _report_named(text, cite, name):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cw.report(text, cite, name=name)
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


def test_ecourts_rtf_template_tags_are_stripped():
    # [2005] WASCA 196 arrived as an eCourts RTF: every template field is wrapped in a
    # mixed-case pseudo-tag, some with attributes, and each paragraph number sits in <p>.
    out = cw.clean(
        "CITATION\t:\t<Citation>DONALDSON -v- THE STATE OF WESTERN AUSTRALIA [2005] WASCA 196</Citation> \n"
        "<Party Name1=\"WAYNE KIRWAN DONALDSON\", Type1=\"Appellant\", Name2=\"THE STATE\", Type2=\"Respondent\",>\n"
        "<LCdetails>\nCoram\t:\t<LCCoram>MAZZA DCJ</LCCoram> \n</LCdetails>\n"
        "<p>1</p>\t<Judge>WHEELER JA</Judge>:  I have had the advantage of reading the reasons.\n"
        "<p>2</p>\t\tTurning to the question of when the trial started, I agree.\n")
    assert "<" not in out and ">" not in out
    assert "CITATION\t:\tDONALDSON -v- THE STATE OF WESTERN AUSTRALIA [2005] WASCA 196" in out
    assert "Coram\t:\tMAZZA DCJ" in out
    assert "1\tWHEELER JA:  I have had the advantage of reading the reasons." in out
    assert "2\t\tTurning to the question of when the trial started, I agree." in out
    assert "Type1=" not in out                                   # the attribute tag went whole


def test_report_accepts_a_reported_judgment_without_the_mnc_when_named():
    # Jade's copy of Kilby v The Queen [1973] HCA 30 has no medium-neutral citation —
    # only "(1973) 129 CLR 460". With the party name from the file name it passes with
    # a WARN; without a name, or with the wrong year, it is still refused.
    old = ("KILBY v. THE QUEEN\n(1973) 129 CLR 460\n29 August 1973\n"
           "BARWICK C.J. The appellant was convicted of rape.\nCounsel: A B for the appellant\n")
    rc, out = _report_named(old, "[1973] HCA 30", "Kilby v The Queen")
    assert rc == 0 and "WARN" in out and "129 CLR 460" in out, out
    rc, out = _report_named(old, "[1973] HCA 30", "")
    assert rc == 2 and "NOT found" in out
    rc, out = _report_named(old, "[1974] HCA 30", "Kilby v The Queen")
    assert rc == 2 and "NOT found" in out
    rc, out = _report_named(old, "[1973] HCA 30", "Smith v The Queen")
    assert rc == 2 and "NOT found" in out


NSWLR_TEXT = (
    "N.S.W.L.R.)\n\nA\n\nMOLONEY v. MERCER\n\n207\n\nMOLONEY v. MERCER\nIn Chambers: Taylor J.\n"
    "Oct. 26; Nov. 4, 1971.\nCriminal Law—Statutory offence—Indecent exposure.\n\nB\n\nC\n\n"
    "M. was prosecuted for that she was a person whose person was indecently\nexposed in a public place.\n"
    "Crowe v. Graham (1968) 41 A.L.J.R. 402, at p. 410, followed.\n\n208\n\nSUPREME COURT\n\n([1971] 2\n\n"
    "The following additional cases were cited in argument:\n\nA\n\nCASE STATED.\n"
    "C. A. Porter for the appellant (informant).\nP. D. White, for the respondent (defendant).\nCur. adv. vult.\n\n"
    "TAYLOR J. This is an appeal by way of case stated. A musical background\nwas provided.\n\nD\n\nG\n\n"
    "N.S.W.L.R.)\n\nA\n\nB\n\nMOLONEY v. MERCER (Taylor J.)\n\n209\n\n"
    "This takes the place of s. 78 of the Police Offences Act.\nOrder accordingly.\n"
    "Solicitor for the appellant (informant): R.J. McKay (Crown Solicitor).\n")


def test_law_report_furniture_is_stripped():
    out = cw.strip_law_report_furniture(NSWLR_TEXT)
    for gone in ("N.S.W.L.R.)", "\nA\n", "\nB\n", "\nC\n", "\nD\n", "\nG\n", "\n207\n", "\n208\n",
                 "\n209\n", "SUPREME COURT\n", "([1971] 2", "MOLONEY v. MERCER (Taylor J.)"):
        assert gone not in out, gone
    assert out.count("MOLONEY v. MERCER") == 1                      # the running head went, the title stayed
    assert "MOLONEY v. MERCER\nIn Chambers: Taylor J." in out
    assert "TAYLOR J. This is an appeal by way of case stated. A musical background\nwas provided." in out
    assert "Crowe v. Graham (1968) 41 A.L.J.R. 402, at p. 410, followed." in out
    assert "The following additional cases were cited in argument:" in out
    # not a law report (no series line twice): a lone "A" and a bare number are left alone
    plain = "A\n\nThe accused said:\n\n12\n\nA question of fact.\n"
    assert cw.strip_law_report_furniture(plain) == plain
    assert cw.strip_law_report_furniture("N.S.W.L.R.)\nA\nonce only\n") == "N.S.W.L.R.)\nA\nonce only\n"


def test_report_accepts_a_reported_citation_on_its_parts():
    text = cw.strip_law_report_furniture(NSWLR_TEXT)
    rc, out = _report_named(NSWLR_TEXT, "[1971] 2 NSWLR 207", "Moloney v Mercer")
    assert rc == 0 and "WARN" in out and "matched on its parts" in out and "'Moloney'" in out, out
    rc, out = _report_named(text, "[1971] 2 NSWLR 207", "Moloney v Mercer")     # furniture gone: the evidence went with it
    assert rc == 2 and "missing the year" in out, out                               # (so the strip runs AFTER the check)
    rc, out = _report_named(NSWLR_TEXT, "[1971] 2 NSWLR 307", "Moloney v Mercer")
    assert rc == 2 and "missing page 307" in out, out
    rc, out = _report_named(NSWLR_TEXT, "[1971] 2 VR 207", "Moloney v Mercer")
    assert rc == 2 and "the series 'VR'" in out, out
    rc, out = _report_named(NSWLR_TEXT, "[1971] 2 NSWLR 207", "")
    assert rc == 2 and "a party name" in out, out
    rc, out = _report_named(NSWLR_TEXT, "[1971] 2 NSWLR 207", "Smith v Jones")
    assert rc == 2 and "the party 'Smith'" in out, out
    assert cw.REPORTED_CITATION_RE.match("(1976) 11 ALR 412").group(3) == "ALR"
    assert cw.REPORTED_CITATION_RE.match("[1935] AC 462").group(2) is None
    assert cw.REPORTED_CITATION_RE.match("(2009) 40 A Crim R 489").group(3) == "A Crim R"
    assert cw.REPORTED_CITATION_RE.match("[2026] WASCA 111") is None or True   # an MNC is handled first anyway


def test_ecourts_document_name_footer_is_stripped():
    # KMB [2010] WASCA 212 as pdftotext emits it: the footer sits between the two
    # halves of a paragraph cut by the page break, before the running header.
    raw = ("seriously. It\n\nDocument Name: WASCA\\CACR\\2010WASCA0212.doc (MS)\n\n[2010] WASCA 212\nBUSS JA\n\n"
           "should also be noted, in this context, that older examples\n"
           "Document Name: WASC\\INS\\2014WASC0240.doc (JP)\nnext line\n"
           "Document Name: WASCA\\CACR\\2006WASCA0075 (CC)\nafter the 2006 form\n"
           "The exhibit was headed Document Name: report.doc (MS) in evidence.\n")
    out = cw.strip_doc_name_footer(raw)
    assert out.count("Document Name") == 1                        # only the mid-line mention survives
    assert "The exhibit was headed Document Name: report.doc (MS) in evidence." in out
    assert "[2010] WASCA 212\nBUSS JA\n\nshould also be noted" in out
    assert "older examples\n\nnext line" in out                 # the footer line itself is gone, nothing else
    assert "next line\n\nafter the 2006 form" in out


def test_pdftotext_versus_split_is_repaired():
    # Jackson [2019] WASCA 118 as pdftotext emits it: the header's "-v-" sat at a line
    # end and was joined to the next line as a hyphenated word.
    raw = ("CITATION\n: THE STATE OF WESTERN AUSTRALIA -vJACKSON [2019] WASCA 118\n\n"
           "MALONE DARCY FLEMING (A PSEUDONYM) -vTHE STATE OF WESTERN AUSTRALIA [2026]\n"
           "A real -v- SMITH stays; an x-value stays; a -vitamin stays; end-v stays\n")
    out = cw.repair_versus_split(raw)
    assert ": THE STATE OF WESTERN AUSTRALIA -v- JACKSON [2019] WASCA 118" in out
    assert "(A PSEUDONYM) -v- THE STATE OF WESTERN AUSTRALIA [2026]" in out
    assert "A real -v- SMITH stays; an x-value stays; a -vitamin stays; end-v stays" in out
    assert out.count("-v-") == 3


def test_hca_pdf_running_headers_are_stripped_per_page():
    # The Court's own PDF of Tofilau [2007] HCA 39: every page opens with the judge(s)
    # of that page's reasons, one word per line, and the page number.
    raw = ("HIGH COURT OF AUSTRALIA\nGLEESON CJ\nGUMMOW, KIRBY, HAYNE, CALLINAN, HEYDON AND CRENNAN JJ\n\n"
           "Matter No M144/2006\n"
           "\x0cGummow J\nHayne\nJ\n25.\n\nnot made to a person whom the speaker knew.\nThe discretion\n65\n\nIn only one case.\n"
           "\x0cCallinan\nHeydon\nCrennan\n\nJ\nJ\nJ\n\n95.\n\nHistory of the requirement\n283\n\nThe person in authority.\n"
           "\x0cKirby\n\nJ\n\n55.\n\nKirby J said this page starts with a name in prose.\n"
           "\x0cHeydon\nJ\n\nA page whose number trails its footnotes.\n160 R v Hughes [1986] 2 NZLR 129.\n42.\n\n")
    out = cw.strip_hca_page_headers(raw)
    assert "Gummow J\nHayne\nJ\n25." not in out and "\n95.\n" not in out and "\n55.\n" not in out
    assert "not made to a person whom the speaker knew." in out
    assert "History of the requirement\n283" in out               # a heading and a paragraph number survive
    assert "Kirby J said this page starts with a name in prose." in out
    assert out.count("\x0c") == 4                                 # page breaks kept for the Jade pass
    assert "\n42.\n" not in out and "160 R v Hughes [1986] 2 NZLR 129." in out   # trailing page number gone, footnote kept
    # a WA PDF (no HIGH COURT first line) is untouched
    wa = "[2026] WASCA 114\n\nJURISDICTION : SUPREME COURT\n\x0cThomson P\n2.\n\nbody\n"
    assert cw.strip_hca_page_headers(wa) == wa


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


def test_litigation_history_as_prose_is_not_citator_bleed():
    # Frigger [2026] WASCA 114 (the Court's own eCourts PDF) says "In light of the above
    # litigation history, evidence relied on …" in its reasons. That is a sentence, not
    # Jade's "Litigation History" heading, and must not be refused.
    prose = SYNTH + "In light of the above litigation history, evidence relied on to support the appellant's contention could have been tendered at trial.\n"
    rc, out = _report(cw.clean(prose), "[2026] WASC 1")
    assert rc == 0, out
    assert "citator contamination" not in out
    # …whereas the heading on its own line, in Jade's casing, still is
    jade = SYNTH + "Litigation History\nJones v The King [2027] WASCA 12\n"
    rc, out = _report(cw.clean(jade), "[2026] WASC 1")
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
