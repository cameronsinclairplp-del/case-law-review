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
import subprocess
import sys
from pathlib import Path

CITATION_RE = re.compile(r"\[(\d{4})\]\s*([A-Za-z]{2,8})\s*(\d+)")
BIDI = "‎‏​﻿"
TAG_LINE = re.compile(r"^</?[A-Z]{2,5}>$")                    # <CRJ> / </CRJ>
PAGE_FIELD = re.compile(r"^PAGE \d+\.?$", re.I)               # HCA Word page fields
FOOTNOTE_CONT = re.compile(r"^\(Footnote continues on next page\)$", re.I)
DIGEST = re.compile(r"CaseBase|Catchwords\s*&\s*Digest", re.I)
CORAM = re.compile(r"\bCORAM\b|\b(?:CJ|JJ|JA|JJA|AJA|ACJ)\b", re.I)
COUNSEL = re.compile(r"Counsel|Solicitors|instructed by|appeared", re.I)


def to_text(path: Path) -> str:
    if not path.is_file():
        sys.exit(f"input not found: {path}")
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
        out.append(ln)
    s = "\n".join(out)
    s = re.sub(r"\n{3,}", "\n\n", s).strip() + "\n"
    return s


def report(text: str, citation: str) -> int:
    m = CITATION_RE.search(citation)
    if not m:
        sys.exit(f"--citation {citation!r} is not a medium-neutral citation like '[2026] WASC 265'")
    year, court, num = int(m.group(1)), m.group(2).upper(), m.group(3)
    norm = re.sub(r"\s+", " ", text)
    cite_pat = re.compile(rf"\[{year}\]\s*{re.escape(court)}\s*{num}(?!\d)", re.I)
    problems, warnings = [], []

    if not cite_pat.search(norm):
        problems.append(f"citation {citation} NOT found in the text — wrong file or wrong citation")
    if DIGEST.search(text):
        problems.append("digest markers (CaseBase / Catchwords & Digest) — this is an editorial "
                        "summary, not the judgment; export the full-text 'Unreported Judgments' version")
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
