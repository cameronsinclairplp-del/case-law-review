#!/usr/bin/env python3
"""
Attach a verbatim judgment text you already have (a WA judgment copied from
AustLII / eCourts / Lexis, etc.) to an EXISTING, already-curated case in
data/cases.json - WITHOUT re-analysing it. Writes data/files/<id>/<id>.md in the
same shape the pipeline uses and sets the case's files.llm, so the in-app
"Read the full judgment" reader and the download button light up.

Use this when the case already has good hand-written analysis you want to keep.
Use pipeline/ingest.py instead to analyse a brand-new case from scratch.

  python pipeline/attach_text.py --id wasca-2022-5 --file stefanski.txt \
      --source "https://www.austlii.edu.au/.../WASCA/2022/5.html"
  pbpaste | python pipeline/attach_text.py --id wasca-2017-141
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "data" / "cases.json"
FILES = ROOT / "data" / "files"


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", str(s or ""))


def yaml_str(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_md(case, text, source):
    tags = case.get("tags") or []
    parts = [
        "---",
        f"id: {case['id']}",
        f"caseName: {yaml_str(case.get('caseName',''))}",
        f"citation: {yaml_str(case.get('citation',''))}",
        f"court: {yaml_str(case.get('court',''))}",
        f"decided: {yaml_str(case.get('decided',''))}",
        f"relevance: {case.get('relevance','')}",
        f"austliiUrl: {yaml_str(case.get('austliiUrl',''))}",
        f"source: {yaml_str(source or 'supplied verbatim text')}",
        f"tags: [{', '.join(yaml_str(t) for t in tags)}]",
        "---", "",
        f"# {case.get('caseName','')} {case.get('citation','')}", "",
        "## One line", case.get("oneLine", ""), "",
        "## What happened", strip_tags(case.get("whatHappened", "")), "",
        "## What the Court held", strip_tags(case.get("whatHeld", "")), "",
        "## What it means for your casework", strip_tags(case.get("whatItMeans", "")), "",
        "## Verdict", strip_tags(case.get("verdict", "")), "",
        "---", "",
        f"## Full judgment (source text - {source or case.get('austliiUrl','')})",
        "", text.strip(), "",
    ]
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser(description="Attach verbatim text to a curated case.")
    ap.add_argument("--id", required=True, help="case id in cases.json, e.g. wasca-2022-5")
    ap.add_argument("--file", help="path to the judgment text; omit to read stdin")
    ap.add_argument("--source", default="", help="source URL/label for provenance")
    args = ap.parse_args()

    text = (Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()).strip()
    if len(text) < 500:
        sys.exit(f"text too short ({len(text)} chars) - paste/point to the full judgment")

    cases = json.loads(CASES.read_text(encoding="utf-8"))
    case = next((c for c in cases if c.get("id") == args.id), None)
    if not case:
        sys.exit(f"id {args.id!r} not found in cases.json")

    rel = f"data/files/{args.id}/{args.id}.md"
    (FILES / args.id).mkdir(parents=True, exist_ok=True)
    (ROOT / rel).write_text(build_md(case, text, args.source), encoding="utf-8")
    if not isinstance(case.get("files"), dict):
        case["files"] = {}
    case["files"]["llm"] = rel
    CASES.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"attached {len(text):,} chars to {args.id} ({case.get('caseName','')}) -> {rel}; files.llm set")


if __name__ == "__main__":
    main()
