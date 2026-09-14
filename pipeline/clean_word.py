#!/usr/bin/env python3
"""
Clean a court Word export (.doc/.docx) — or an already-converted .txt — into
verbatim judgment text ready for pipeline/ingest.py, and REPORT whether it is
safe to ingest. Stdlib only (uses macOS `textutil` for the Word conversion).

Handles the formats seen so far: WASC/WASCA eResources Word exports (the
JURISDICTION / CITATION / CORAM / BETWEEN header template) and HCA eResources
Word exports. It:
  - converts .doc/.docx via `textutil -convert txt`
  - strips bidi / zero-width marks (U+200E, U+200F, U+200B, U+FEFF)
  - drops export pseudo-tags (the <CRJ> … </CRJ> lines around "Cases referred to")
  - drops HCA Word page-field lines ("PAGE 12.") and "(Footnote continues on next page)"
  - trims trailing whitespace and collapses 3+ blank lines to 2
It NEVER rewrites judgment prose and never invents paragraph numbers (Word
auto-numbering is lost by textutil; that is accepted rather than fabricated).

Integrity checks (from HANDOFF.md "How to add a case"), reported at the end:
  REFUSE (exit 2)  - the medium-neutral citation is not in the text (wrong file?)
  REFUSE (exit 2)  - digest markers present (LexisNexis CaseBase / "Catchwords &
                     Digest") -> that is an editorial summary, NOT the judgment
  WARN             - no coram or no counsel/solicitors block (may not be full text)
  WARN             - any year LATER than the citation year, with context, so a
                     human can decide: a parole-eligibility date or a listed future
                     hearing is legitimate; JADE citator bleed ("Cited by …") is not.

Usage:
  python pipeline/clean_word.py --citation "[2026] WASC 265" \\
      --in ~/Downloads/2026WASC0265.doc --out /path/to/hosie.clean.txt
Then:
  python pipeline/ingest.py --citation "[2026] WASC 265" \\
      --case "The State of Western Australia v Hosie" --file /path/to/hosie.clean.txt
"""
import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

CITATION_RE = re.compile(r"\[(\d{4})\]\s*([A-Za-z]{2,8})\s*(\d+)")
BIDI = "‎‏​﻿"
# eCourts pseudo-tags: <CRJ> / </CRJ> (Word exports), and in the RTF exports of older
# decisions the whole template — <Jurisdiction>, </TitleOfCourt>, <Catchword>,
# <Party Name1="…", Type1="Appellant", …,>, <p>12</p> round every paragraph number,
# <Judge>WHEELER JA</Judge>. A capitalised tag name, optional attributes; plus the
# lower-case <p>. "<not a tag>" and "x < y > z" in prose are untouched (tests).
TAG_SHAPE = r"</?(?:p|[A-Z][A-Za-z0-9]{1,24})(?:\s[^<>]*)?>"
TAG_LINE = re.compile(rf"^(?:{TAG_SHAPE}\s*)+$")                 # a line that is only tags
# ...and the same pseudo-tags welded to the end (or start) of a text line, which
# is how eCourts actually emits the closing one: [2019] WASC 84 arrived with
# "... v Staniforth-Smith [2014] WASCA 170</CRJ>" as the last "cases referred to"
# entry, and the line-only rule above sailed past it into the verbatim text (found
# by the case audit, 09/09/2026). No judgment prose contains <XX>-shaped markup,
# so stripping it anywhere on a line is safe.
TAG_INLINE = re.compile(TAG_SHAPE)
PAGE_FIELD = re.compile(r"^PAGE \d+\.?$", re.I)               # HCA Word page fields

# --- BarNet Jade PDF wrapper --------------------------------------------------
# A Jade PDF of a WA judgment is the COURT'S OWN document (its attribution box
# names the source file, e.g. "file:/2026WASCA0033.doc") inside a thin Jade
# wrapper. The wrapper is: a cover page, a per-page footer, and an attribution
# table. The footer carries the SUBSCRIBER'S EMAIL ADDRESS on every page — that
# must never reach the public repo, which is reason enough to strip it precisely.
JADE_FOOTER = re.compile(r"^BarNet publication information\b.*$", re.I)
JADE_CHROME = re.compile(r"^(?:BarNet Jade|jade\.io|View this document in a browser)$", re.I)
# Jade's cover-page title, "Pellew v The King - [2026] WASCA 33". It is Jade's
# heading, not the court's document, and the court's own CITATION line carries the
# same information — so it is wrapper, not judgment.
JADE_TITLE = re.compile(r"^.{3,120}\s[-–]\s\[\d{4}\]\s*[A-Z]{2,8}\s*\d+$")
# Attribution table, which pdftotext emits BETWEEN a header label and its value.
JADE_ATTR_START = re.compile(r"^Attribution$")
JADE_ATTR_LINE = re.compile(
    r"^(?:Original court site URL:|Content receive[d]? from|Content retrieved:?"
    r"|court:|Download/print date:|Source:|Citation:"
    r"|file:/\S+|https?://\S+|[A-Z][a-z]+ \d{1,2}, \d{4})$", re.I)

# WA header labels. In a Word export these are "LABEL\t:\tVALUE" on one line; a
# PDF splits the label from its value across lines, so they are rejoined (see
# rejoin_pdf_header). Scoped to these labels ONLY — never to body prose.
WA_HEADER_LABEL = re.compile(
    r"^(JURISDICTION|TITLE OF COURT|CITATION|CORAM|HEARD|DELIVERED|PUBLISHED"
    r"|FILE NO(?:/S)?|BETWEEN)$")

# Citator contamination — the §6 war story. A Jade PDF *can* be a clean copy of
# the court's document (verified on [2026] WASCA 33) or it can be the annotated
# citator view, which interleaves EXCERPTS FROM LATER CASES after most paragraphs
# and would inject another case's words into a "verbatim" judgment. Judge the file
# by its CONTENT, not by where it came from.
# "Litigation History" and "Annotations:" are Jade's OWN section headings, so they
# only count when they stand on a line by themselves in Jade's casing. As prose they
# are ordinary judgment language — "In light of the above litigation history, evidence
# relied on …" is a sentence in the Court's own PDF of Frigger [2026] WASCA 114, which
# the earlier anywhere-in-the-text rule refused as citator bleed (found 14/09/2026).
# The "cited by" markers stay anywhere: no judgment prose is shaped like them.
CITATOR = re.compile(
    r"Following paragraph cited by|Paragraphs? cited by|\bCited by:|\bCases Citing\b"
    r"|(?m:^[ \t]*Litigation History[ \t]*$)|(?m:^[ \t]*Annotations?[ \t]*:[ \t]*$)", re.I)
FOOTNOTE_CONT = re.compile(r"^\(Footnote continues on next page\)$", re.I)
DIGEST = re.compile(r"CaseBase|Catchwords\s*&\s*Digest", re.I)
CORAM = re.compile(r"\bCORAM\b|\b(?:CJ|JJ|JA|JJA|AJA|ACJ)\b", re.I)
COUNSEL = re.compile(r"Counsel|Solicitors|instructed by|appeared", re.I)


def pdf_to_text(path: Path) -> str:
    """PDF -> text via poppler's `pdftotext` in its DEFAULT mode.

    The mode matters. A WA judgment sets case names and legislation in italics,
    which is a separate styled run in the PDF's content stream; pypdf, PyMuPDF and
    `pdftotext -raw` all emit those runs out of reading order, so
    "... moral revulsion. A fortiori, a jury would be capable of following such
    directions ..." comes out as "... moral revulsion. , a jury would be capable of
    following such A fortiori directions ...". That is silent corruption of the
    verbatim text. Default-mode pdftotext keeps the reading order; `-layout` also
    does but pads every line with column whitespace. Measured on [2026] WASCA 33."""
    exe = shutil.which("pdftotext")
    if not exe:
        sys.exit("pdftotext not found — install poppler:  brew install poppler\n"
                 "(needed only for PDF sources; Word exports use macOS textutil)")
    r = subprocess.run([exe, "-q", str(path), "-"], capture_output=True, text=True)
    if not r.stdout.strip():
        sys.exit(f"pdftotext produced no text for {path}: {r.stderr.strip() or 'empty output'}\n"
                 "(a scanned/image-only PDF has no text layer and cannot be used)")
    return r.stdout


# --- the older eCourts PDF's page footer ----------------------------------------
# To about 2014 the Court's PDFs carry a document-management footer on every page,
# "Document Name: WASCA\\CACR\\2010WASCA0212.doc (MS)", on a line of its own. It is
# not the judgment. Only a whole line of exactly that shape goes.
# (2006 files name the document without its extension: "…\\2006WASCA0075 (CC)".)
DOC_NAME_FOOTER = re.compile(r"^[ \t]*Document Name:[ \t]*\S+[ \t]*(?:\([A-Z]{1,4}\))?[ \t]*$", re.M)


def strip_doc_name_footer(text: str) -> str:
    return DOC_NAME_FOOTER.sub("", text)


# --- pdftotext's de-hyphenation of the eCourts "-v-" ----------------------------
# pdftotext joins a word hyphenated across a line break. The eCourts header puts the
# party separator "-v-" at the end of a line when the parties are long, so
# "THE STATE OF WESTERN AUSTRALIA -v-" / "JACKSON" arrives as "-vJACKSON". Only "-v"
# straight after whitespace and straight before a capital: no prose word has that
# shape ("x-value", "-vitamin" and a real "-v-" are untouched — tests).
VERSUS_SPLIT = re.compile(r"(?<=\s)-v(?=[A-Z])")


def repair_versus_split(text: str) -> str:
    return VERSUS_SPLIT.sub("-v- ", text)


# --- the High Court's own PDF (hcourt.gov.au) --------------------------------
HCA_FIRST_LINE = re.compile(r"^\s*HIGH COURT OF AUSTRALIA\s*$")
HCA_SUFFIX = re.compile(r"^(?:CJ|ACJ|J|JJ)$")
HCA_PAGE_NO = re.compile(r"^\d{1,3}\.$")


def hca_coram_names(first_page: str) -> set:
    """Surnames from the coram block under "HIGH COURT OF AUSTRALIA":
    "GLEESON CJ" / "GUMMOW, KIRBY, HAYNE, CALLINAN, HEYDON AND CRENNAN JJ"."""
    names = set()
    for ln in first_page.split("\n")[:8]:
        s = ln.strip()
        if not s or HCA_FIRST_LINE.match(s):
            continue
        toks = [t for t in re.split(r"[\s,]+", s) if t]
        if not toks or not HCA_SUFFIX.match(toks[-1]):
            continue
        for t in toks[:-1]:
            if t.isupper() and t not in ("AND", "&"):
                names.add(t.lower())
    return names


def strip_hca_page_headers(text: str) -> str:
    """Drop the per-page running header of a High Court PDF — the judge name(s), the
    bare judicial suffix and the page number that pdftotext emits at the top of every
    page — and nothing else. Only applies to a document whose first page opens with
    "HIGH COURT OF AUSTRALIA" and only to lines BEFORE the first body line of each
    page, and a name is only a header line if it is in the first page's coram."""
    pages = text.split("\x0c")
    if len(pages) < 2 or not HCA_FIRST_LINE.match(pages[0].lstrip("\n").split("\n", 1)[0]):
        return text
    names = hca_coram_names(pages[0])
    if not names:
        return text

    def is_header(s):
        if not s or HCA_SUFFIX.match(s) or HCA_PAGE_NO.match(s):
            return True
        toks = s.split()
        if len(toks) == 1:
            return toks[0].lower() in names
        if len(toks) == 2:
            return toks[0].lower() in names and HCA_SUFFIX.match(toks[1]) is not None
        return False

    out = [pages[0]]
    for pg in pages[1:]:
        lines = pg.split("\n")
        i = 0
        while i < len(lines) and i < 12 and is_header(lines[i].strip()):
            i += 1
        lines = lines[i:]
        # the page number sometimes trails the page's footnotes instead — drop it there too
        j = len(lines)
        while j > 0 and not lines[j - 1].strip():
            j -= 1
        if j > 0 and HCA_PAGE_NO.match(lines[j - 1].strip()):
            lines = lines[:j - 1] + lines[j:]
        out.append("\n".join(lines))
    return "\x0c".join(out)


def strip_jade_wrapper(text: str) -> str:
    """Remove the BarNet Jade PDF wrapper, leaving the court's own document.

    Also removes the per-page footer, which carries the subscriber's email address
    — it is not part of the judgment and must not be published."""
    out, in_attr, attr_budget, seen_body = [], False, 0, False
    for ln in text.replace("\r", "").replace("\x0c", "").split("\n"):
        s = ln.strip()
        if JADE_FOOTER.match(s) or JADE_CHROME.match(s):
            continue
        if not seen_body and JADE_TITLE.match(s):
            continue          # cover-page title, before any judgment text
        if WA_HEADER_LABEL.match(s) or s.startswith("JURISDICTION"):
            seen_body = True
        if JADE_ATTR_START.match(s):
            in_attr, attr_budget = True, 20      # bounded: never eat the judgment
            continue
        if in_attr:
            attr_budget -= 1
            if s.startswith(":") or attr_budget <= 0:
                in_attr = False                   # the header's value line — we're out
            elif JADE_ATTR_LINE.match(s) or not s:
                continue
            elif WA_HEADER_LABEL.match(s):
                out.append(ln)                    # a real header label caught inside the box
                continue
            else:
                in_attr = False
        out.append(ln)
    return "\n".join(out)


def rejoin_pdf_header(text: str) -> str:
    """Rejoin "LABEL" / ":  VALUE" split across lines into the Word export's
    "LABEL\t:\tVALUE" shape.

    A PDF lays the front-matter table out in columns, so pdftotext emits the label
    and its value as separate lines. The app's judgment reader keys its masthead on
    the literal tab separator, so without this a PDF-sourced case renders its court
    and coram as plain paragraphs instead. This ONLY touches the fixed set of WA
    header labels in WA_HEADER_LABEL and only where the very next non-blank line
    starts with ':' — it can never reflow body prose."""
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        s = lines[i].strip()
        if WA_HEADER_LABEL.match(s):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and lines[j].strip().startswith(":"):
                val = lines[j].strip()[1:].strip()
                out.append(f"{s}\t:\t{val}")
                i = j + 1
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def to_text(path: Path) -> str:
    if not path.is_file():
        sys.exit(f"input not found: {path}")
    if path.suffix.lower() == ".pdf":
        return rejoin_pdf_header(strip_jade_wrapper(strip_hca_page_headers(
            repair_versus_split(strip_doc_name_footer(pdf_to_text(path))))))
    if path.suffix.lower() in (".doc", ".docx", ".rtf"):
        r = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)],
                           capture_output=True, text=True)
        # macOS textutil exits 0 even when it cannot read the file — judge by the output
        if r.returncode != 0 or not r.stdout.strip():
            sys.exit(f"textutil produced no text for {path}: {r.stderr.strip() or 'empty output'}")
        return r.stdout
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        sys.exit(f"input is empty: {path}")
    return text


def clean(text: str) -> str:
    for ch in BIDI:
        text = text.replace(ch, "")
    out = []
    for ln in text.replace("\r", "").split("\n"):
        ln = ln.rstrip()
        s = ln.strip()
        if TAG_LINE.match(s) or PAGE_FIELD.match(s) or FOOTNOTE_CONT.match(s):
            continue
        ln = TAG_INLINE.sub("", ln).rstrip()
        out.append(ln)
    s = "\n".join(out)
    s = re.sub(r"\n{3,}", "\n\n", s).strip() + "\n"
    return s


def report(text: str, citation: str, name: str = "") -> int:
    m = CITATION_RE.search(citation)
    if not m:
        sys.exit(f"--citation {citation!r} is not a medium-neutral citation like '[2026] WASC 265'")
    year, court, num = int(m.group(1)), m.group(2).upper(), m.group(3)
    norm = re.sub(r"\s+", " ", text)
    cite_pat = re.compile(rf"\[{year}\]\s*{re.escape(court)}\s*{num}(?!\d)", re.I)
    problems, warnings = [], []

    if not cite_pat.search(norm):
        # A pre-1998 High Court report carries no medium-neutral citation — Jade's copy
        # of Kilby v The Queen [1973] HCA 30 says "(1973) 129 CLR 460" and nothing else.
        # Accept it on the year's reported citation plus a party name from the file name;
        # without both it is still "wrong file".
        reported = re.search(rf"\(\s*{year}\s*\)\s*\d{{1,3}}\s*(?:CLR|ALR|ALJR|A Crim R)\s*\d+", norm)
        party = re.search(r"^\s*(?:the\s+)?([A-Za-z][A-Za-z'’-]{2,})", name or "")
        head = norm[:6000].lower()
        if reported and party and party.group(1).lower() in head:
            warnings.append(f"medium-neutral citation {citation} not in the text (a reported "
                            f"judgment); matched on '{reported.group(0)}' and the party "
                            f"'{party.group(1)}' — check it is the right case")
        else:
            problems.append(f"citation {citation} NOT found in the text — wrong file or wrong citation")
    if DIGEST.search(text):
        problems.append("digest markers (CaseBase / Catchwords & Digest) — this is an editorial "
                        "summary, not the judgment; export the full-text 'Unreported Judgments' version")
    hits = sorted({h.strip() for h in CITATOR.findall(text)})
    if hits:
        problems.append(
            f"citator contamination ({', '.join(hits[:4])}) — this copy interleaves EXCERPTS "
            "FROM LATER CASES with the reasons; ingesting it would inject another case's words "
            "into the verbatim judgment. Get the court's own document instead.")
    if not CORAM.search(text):
        warnings.append("no coram / judicial suffix found — is this the full judgment?")
    if not COUNSEL.search(text):
        warnings.append("no counsel / solicitors block found — is this the full judgment?")

    later = {}
    for ln in text.split("\n"):
        for y in re.findall(r"\b(19\d\d|20\d\d)\b", ln):
            if int(y) > year:
                later.setdefault(int(y), ln.strip()[:140])
    if later:
        warnings.append("years LATER than the citation year (decide: legitimate future date, or "
                        "citator bleed?):")
        for y, ctx in sorted(later.items()):
            warnings.append(f"    {y}: {ctx}")

    paras = len(re.findall(r"(?m)^\s*\d{1,4}[\.\t ]", text))
    print(f"chars: {len(text):,}   lines: {text.count(chr(10)):,}   "
          f"numbered-line markers: {paras}   citation: {citation}")
    for w in warnings:
        print(f"WARN  {w}")
    for p in problems:
        print(f"REFUSE {p}")
    if problems:
        print("=> NOT safe to ingest.")
        return 2
    print("=> OK to ingest (review any WARN lines first)." if warnings else "=> OK to ingest.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--citation", required=True, help='e.g. "[2026] WASC 265"')
    ap.add_argument("--in", dest="src", required=True, help=".doc/.docx/.txt to clean")
    ap.add_argument("--out", required=True, help="where to write the cleaned .txt")
    a = ap.parse_args()
    text = clean(to_text(Path(a.src).expanduser()))
    Path(a.out).expanduser().write_text(text, encoding="utf-8")
    print(f"wrote {a.out}")
    sys.exit(report(text, a.citation))


if __name__ == "__main__":
    main()
