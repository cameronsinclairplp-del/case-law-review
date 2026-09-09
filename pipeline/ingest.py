#!/usr/bin/env python3
"""
Ingest a judgment you supply by hand and write it into the app exactly as the
pipeline would. This is the route for WA judgments (WASC/WASCA), which no free
corpus carries: you open the case under your LICENSED access (LexisNexis / JADE /
eCourts), copy the verbatim text, and feed it in here. Nothing is scraped.

Reuses update.py's analysis -> build -> file-write -> cases.json path, so an
ingested case is identical in shape to a pipeline-produced one.

Requires:
  * ANTHROPIC_API_KEY in the environment
  * deps installed:  pip install -r pipeline/requirements.txt

Examples:
  python pipeline/ingest.py --citation "[2026] WASC 248" \
      --case "The State of Western Australia v RYAN" --file ryan.txt

  pbpaste | python pipeline/ingest.py --citation "[2026] WASCA 88" \
      --case "REYNOLDS v BYRAM [No 4]"
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update as P  # noqa: E402  (sibling module; reuse its functions wholesale)


def stored_judgment(cid):
    """(text, citation, caseName) read back out of an existing data/files/<id>/<id>.md.

    Lets a case added by add_text.py (full text, no analysis) be written up later
    WITHOUT the original Word export — the verbatim text is already in the repo, and
    re-reading it is safer than asking for the source file again months later and
    risking a different version of the judgment."""
    md_path = P.FILES_DIR / cid / f"{cid}.md"
    if not md_path.is_file():
        P.die(f"no stored judgment for {cid!r} (looked for {md_path})")
    md = md_path.read_text(encoding="utf-8")
    i = md.find("## Full judgment")
    if i < 0:
        P.die(f"{md_path} has no '## Full judgment' section")
    text = md[i:].split("\n", 1)[1].strip()
    case = next((c for c in P.load_cases() if c.get("id") == cid), None)
    if not case:
        P.die(f"{cid!r} is not in cases.json")
    return text, case.get("citation", ""), case.get("caseName", "")


def main():
    ap = argparse.ArgumentParser(description="Ingest a supplied judgment into The Case-Law Review.")
    ap.add_argument("--citation", help='medium-neutral, e.g. "[2026] WASC 248"')
    ap.add_argument("--case", default="", help='case name, e.g. "The State of WA v RYAN"')
    ap.add_argument("--file", help="path to the judgment text (.txt/.md); omit to read stdin")
    ap.add_argument("--from-stored", dest="from_stored", metavar="ID",
                    help="analyse a case already in the library from its stored verbatim "
                         "text, e.g. --from-stored wasca-2026-104 (upgrades a full-text-only "
                         "case added by add_text.py; no source file needed)")
    args = ap.parse_args()

    if args.from_stored:
        text, cite, name = stored_judgment(args.from_stored)
        args.citation = args.citation or cite
        args.case = args.case or name
        P.log(f"analysing {args.from_stored} from its stored verbatim text ({len(text):,} chars)")
    else:
        if not args.citation:
            P.die("--citation is required (or use --from-stored <id>)")
        text = (Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()).strip()
    if len(text) < 800:
        P.die(f"judgment text too short ({len(text)} chars) - paste/point to the full judgment")

    m = P.CITATION_RE.search(args.citation)
    if not m:
        P.die(f"no medium-neutral citation found in {args.citation!r} (expected like '[2026] WASC 248')")
    item = P._item_from_match(m, args.case, "", args.case)
    if item["courtTag"] not in P.COURTS:
        P.die(f"court '{item['courtTag']}' is not in COURTS - add it in update.py first if you want it in scope")

    truncated = len(text) > P.MAX_JUDGMENT_CHARS
    if truncated:
        text = text[:P.MAX_JUDGMENT_CHARS]

    P.log(f"ingesting {item['id']} ({item['citation']}) - {len(text)} chars; analysing with {P.MODEL}...")
    client = P.get_client()
    analysis = P.analyse(client, item, text, truncated)
    case = P.build_case(item, analysis)
    P.write_llm_file(case, text, analysis)

    cases = [c for c in P.load_cases() if c.get("id") != case["id"]]  # replace if already present
    cases.append(case)
    cases.sort(key=lambda c: str(c.get("date", "")), reverse=True)
    P.save_cases(cases)

    P.log(f"ingested {case['id']} [{case['relevance']}] {case['caseName']} -> cases.json ({len(cases)} cases)")
    print(f"\nDone. Review data/cases.json and data/files/{case['id']}/{case['id']}.md, then commit + push.")


if __name__ == "__main__":
    main()
