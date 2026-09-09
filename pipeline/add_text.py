#!/usr/bin/env python3
"""
Add a judgment to the library as READABLE FULL TEXT ONLY — no model analysis.

This is the bulk route. `ingest.py` runs an Opus analysis per case (~20-40s, and a
two-lens adversarial audit before it can go live); that is right for a case you
want written up, but it is a grind for a 60-case backlog you mainly want to READ.
This script does the cheap half: it writes data/files/<id>/<id>.md with the
verbatim judgment and adds a minimal entry to cases.json, so the case is
searchable and fully readable in the app's judgment reader immediately.

Nothing is inferred by a model. The citation, case name and delivered date are
either given on the command line or read VERBATIM out of the judgment's own
header, and anything that cannot be read is left empty rather than guessed.

Such a case has NO relevance classification. That is deliberate and the app
renders it as a neutral "Full text" badge — an ACTION/AWARENESS badge would
assert a call nobody made. To upgrade one later, once you have read it:

    python pipeline/ingest.py --from-stored wasca-2026-104

which re-analyses from the stored verbatim text (no source file needed).

Usage — one case:
    python pipeline/add_text.py --citation "[2026] WASCA 104" \
        --in "Cases/2026WASCA0104.doc"

Usage — a whole folder (this is the point; cases.json is written ONCE at the end,
so there is no race and no half-written library):
    python pipeline/add_text.py --batch Cases/

In batch mode the citation is taken from each FILENAME (e.g. "2026WASCA0104.doc",
"2019WASC0084 indecent.docx") and then verified against the file's own text — a
file whose citation is not in its text is refused, not guessed at, which is the
same rule clean_word.py enforces. Files that fail are reported and skipped; the
rest still land.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update as P          # noqa: E402  (sibling module; reuse its helpers wholesale)
import clean_word as CW     # noqa: E402  (Word -> clean verbatim text)

# "2026WASCA0104.doc", "2019WASC0084 indecent.docx", "2026HCA0028.txt".
# WASCA before WASC, HCASJ before HCA — longest court token must win.
FILENAME_CITE_RE = re.compile(r"(\d{4})\s*(WASCA|WASC|HCASJ|HCA|NTSC|NTCCA|QCA|TASCCA|WADC)\s*0*(\d+)", re.I)

# WASC/WASCA eCourts header: "CITATION\t:\tJOHNSON -v- RAMSDEN [2019] WASC 84"
CITATION_LINE_RE = re.compile(r"^\s*CITATION\s*:\s*(.+?)\s*$", re.M)
# "DELIVERED : 5 MARCH 2019"  (prefer DELIVERED over HEARD/PUBLISHED)
DELIVERED_RE = re.compile(r"^\s*DELIVERED\s*:\s*(\d{1,2})\s+([A-Z]+)\s+(\d{4})\s*$", re.M | re.I)
MONTHS = {m: i for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
     "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"], 1)}

_LOWER = {"v", "of", "the", "and", "for", "in", "a", "by", "ex", "parte", "re", "on", "to",
          "pseudonym"}   # "(a pseudonym)" is a fixed lower-case formula in WA judgments
# Short all-caps tokens are normally initials or a pseudonym code (MRV, TJD, JFE)
# and must stay upper-case. These are the short words that are NOT — without them
# "THE STATE OF WA" comes out as "THE State OF Western Australia".
_NOT_INITIALS = {"THE", "OF", "AND", "FOR", "IN", "ON", "TO", "BY", "RE", "EX", "A", "AN",
                 "ANOR", "ORS", "KING", "REX", "V", "VS"}


def citation_from_filename(path):
    """Citation from a file name, in either shape people actually save:
      * the eCourts export name, "2026WASCA0033.doc"
      * a readable name carrying the citation, "Pellew v The King - [2026] WASCA 33.pdf"
    """
    stem = Path(path).stem
    m = P.CITATION_RE.search(stem)          # bracketed form wins — it is unambiguous
    if m:
        return f"[{m.group(1)}] {m.group(2).upper()} {int(m.group(3))}"
    m = FILENAME_CITE_RE.search(stem)
    if not m:
        return None
    return f"[{m.group(1)}] {m.group(2).upper()} {int(m.group(3))}"


def titlecase_party(raw):
    """Proper-case a SHOUTED party name from a Word header without destroying the
    things that are meant to stay upper-case.

    Order matters: connectives are decided FIRST, because "THE" and "OF" are also
    short all-caps tokens and would otherwise be mistaken for initials ("THE State
    OF Western Australia"). Initials-only parties and pseudonym codes ("MRV v SNW",
    "TJD", "JFE") are the WA identity-protection convention and must survive intact,
    so a short all-caps token that is not a known word is left exactly as it is.

    Known limit: a genuine surname of four letters or fewer stays upper-case
    ("LE v The King"), because an eCourts header is entirely upper-case and nothing
    in it distinguishes a short surname from initials. That is the safe direction to
    fail — it is visible and cosmetic, where lower-casing initials ("Mrv v Snw")
    would destroy meaning. Pass --case to override any name."""
    first_word_seen = False
    out = []
    for tok in re.split(r"(\s+)", raw.strip()):
        if not tok.strip():
            out.append(tok)
            continue
        core = tok.strip("[](),.;:'\"")
        low = core.lower()
        is_first = not first_word_seen
        first_word_seen = True
        if low in ("v", "v.", "-v-"):
            # the defendant's first word starts a fresh party: "... v The King",
            # not "... v the King" (the library writes "v The State of WA").
            out.append("v")
            first_word_seen = False
            continue
        if low in _LOWER and not is_first:
            out.append(tok.lower())                                   # of, the, and, v
        elif core.isupper() and core.isalpha() and len(core) <= 4 and core not in _NOT_INITIALS:
            out.append(tok)                                           # MRV, SNW, TJD, JFE
        elif tok.isupper():
            out.append(tok[:1].upper() + tok[1:].lower())             # BLURTON -> Blurton
        else:
            out.append(tok)                                           # already mixed case
    s = "".join(out)
    s = re.sub(r"\s*-v-\s*", " v ", s, flags=re.I)      # eCourts writes "-v-"
    return re.sub(r"\s+", " ", s).strip()


def name_from_text(text, citation):
    """Case name from the judgment's own CITATION line, with the citation removed.
    Returns "" when it cannot be read — never a guess."""
    for line in CITATION_LINE_RE.findall(text[:4000]):
        cleaned = P.clean_case_name(line, citation)
        if cleaned and cleaned != "(case name pending)":
            return titlecase_party(cleaned)
    return ""


def decided_from_text(text):
    """DD/MM/YYYY from the header's DELIVERED line, or "" if it isn't there.
    The delivered date is what the library sorts on; a missing one falls back to
    the citation year rather than being invented."""
    m = DELIVERED_RE.search(text[:4000])
    if not m:
        return ""
    mon = MONTHS.get(m.group(2).upper())
    return f"{int(m.group(1)):02d}/{mon:02d}/{m.group(3)}" if mon else ""


def build_text_only_case(item, text, source):
    """A case object with the metadata we can read and NOTHING we cannot. Every
    analysis field is empty; the app omits an empty section rather than rendering
    a blank heading, and shows a neutral 'Full text' badge for the empty
    relevance."""
    decided = decided_from_text(text)
    iso = P.dmy_to_iso(decided)
    return {
        "id": item["id"],
        "date": iso or item["year"],
        "court": P.COURTS[item["courtTag"]]["name"],
        "courtTag": item["courtTag"],
        "caseName": item["caseName"],
        "citation": item["citation"],
        "decided": decided or item["year"],
        "appealFrom": "", "outcome": "", "weight": "", "tags": [],
        "relevance": "",                     # unclassified — see the module docstring
        "oneLine": "", "whatHappened": "", "whatHeld": "", "whatItMeans": "", "verdict": "",
        "austliiUrl": P.austlii_url(item),
        "jadeUrl": P.jade_summary_url(item["courtTag"], item["year"], item["num"]),
        "files": {"llm": f"data/files/{item['id']}/{item['id']}.md"},
        "textOnly": True,                    # so a later pass can find these
    }


def write_text_only_md(case, text, source):
    out_dir = P.FILES_DIR / case["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    parts = [
        "---",
        f"id: {case['id']}",
        f"caseName: {P.yaml_str(case['caseName'])}",
        f"citation: {P.yaml_str(case['citation'])}",
        f"court: {P.yaml_str(case['court'])}",
        f"decided: {P.yaml_str(case['decided'])}",
        "relevance:",
        f"austliiUrl: {P.yaml_str(case['austliiUrl'])}",
        f"source: {P.yaml_str(source)}",
        "textOnly: true",
        "---", "",
        f"# {case['caseName']} {case['citation']}".strip(), "",
        "_Full text only — no analysis has been written for this case yet._", "",
        "---", "",
        f"## Full judgment (source text - {source})",
        "", text.strip(), "",
    ]
    (out_dir / f"{case['id']}.md").write_text("\n".join(parts), encoding="utf-8")


def load_clean_text(path, citation):
    """Word/txt -> clean verbatim text, refusing a file that does not contain its
    own citation (the wrong-file guard from HANDOFF §5)."""
    text = CW.clean(CW.to_text(Path(path).expanduser()))
    m = P.CITATION_RE.search(citation)
    pat = re.compile(rf"\[{m.group(1)}\]\s*{re.escape(m.group(2).upper())}\s*{m.group(3)}(?!\d)", re.I)
    if not pat.search(re.sub(r"\s+", " ", text)):
        raise ValueError(f"{citation} does not appear in the file — wrong file?")
    if len(text) < 800:
        raise ValueError(f"only {len(text)} chars of text — not a full judgment")
    return text


def add_one(path, citation, case_name, source):
    text = load_clean_text(path, citation)
    m = P.CITATION_RE.search(citation)
    if not m:
        raise ValueError(f"no medium-neutral citation in {citation!r}")
    name = case_name or name_from_text(text, citation)
    item = P._item_from_match(m, name or "", "", name or "", via="submission")
    if item["courtTag"] not in P.COURTS:
        raise ValueError(f"court {item['courtTag']} is not in COURTS")
    if not name:
        item["caseName"] = "(case name pending)"
        P.log(f"  NOTE {item['id']}: could not read a case name from the header — "
              f"set it by hand in cases.json and the .md")
    truncated = len(text) > P.MAX_JUDGMENT_CHARS
    if truncated:
        P.log(f"  NOTE {item['id']}: judgment truncated at {P.MAX_JUDGMENT_CHARS:,} chars")
        text = text[:P.MAX_JUDGMENT_CHARS]
    case = build_text_only_case(item, text, source or str(path))
    write_text_only_md(case, text, source or str(path))
    return case, len(text)


def main():
    ap = argparse.ArgumentParser(
        description="Add judgments to the library as full text only (no model analysis).")
    ap.add_argument("--in", dest="src", help="a .doc/.docx/.rtf/.txt/.pdf judgment file")
    ap.add_argument("--citation", help='e.g. "[2026] WASCA 104" (defaults to the filename)')
    ap.add_argument("--case", default="", help="case name; defaults to the judgment's own header")
    ap.add_argument("--batch", help="a directory — add every judgment file in it")
    ap.add_argument("--source", default="", help="provenance label (default: the file path)")
    args = ap.parse_args()

    jobs = []
    if args.batch:
        d = Path(args.batch).expanduser()
        if not d.is_dir():
            P.die(f"--batch {d} is not a directory")
        for f in sorted(d.iterdir()):
            if f.suffix.lower() not in (".doc", ".docx", ".rtf", ".txt", ".pdf"):
                continue
            cite = citation_from_filename(f)
            if not cite:
                P.log(f"  skip {f.name}: no citation in the filename "
                      f'(expected like "2026WASCA0104.doc")')
                continue
            jobs.append((f, cite, ""))
    elif args.src:
        cite = args.citation or citation_from_filename(args.src)
        if not cite:
            P.die("no --citation given and none found in the filename")
        jobs.append((Path(args.src).expanduser(), cite, args.case))
    else:
        P.die("give --in <file> or --batch <dir>")

    if not jobs:
        P.die("nothing to do — no judgment files found")

    existing = P.load_cases()
    by_id = {c["id"]: c for c in existing}
    added, failed = [], []
    for path, cite, name in jobs:
        try:
            case, n = add_one(path, cite, name, args.source)
        except Exception as e:                       # one bad file must not stop the batch
            failed.append((path.name, str(e)))
            P.log(f"  FAILED {path.name}: {e}")
            continue
        was = by_id.get(case["id"])
        if was and not was.get("textOnly"):
            # never silently replace a written-up case with a bare text-only one
            failed.append((path.name, f"{case['id']} already has a full analysis — "
                                      f"use attach_text.py to refresh its text"))
            P.log(f"  SKIPPED {path.name}: {case['id']} is already analysed")
            continue
        by_id[case["id"]] = case
        added.append(case)
        P.log(f"  added {case['id']} — {case['caseName']} ({case['decided']}) — {n:,} chars")

    if added:
        merged = sorted(by_id.values(), key=lambda c: str(c.get("date", "")), reverse=True)
        P.save_cases(merged)                          # written ONCE, after every file
        P.log(f"cases.json: {len(existing)} -> {len(merged)}")

    print(f"\nAdded {len(added)} case(s) as full text." +
          (f" {len(failed)} failed:" if failed else ""))
    for n, why in failed:
        print(f"  - {n}: {why}")
    if added:
        print("\nThey are readable in the app now. To write up any of them later:")
        for c in added[:5]:
            print(f"  python pipeline/ingest.py --from-stored {c['id']}")
        if len(added) > 5:
            print(f"  ... and {len(added) - 5} more")
        print("\nReview data/cases.json, then commit + push.")


if __name__ == "__main__":
    main()
