#!/usr/bin/env python3
"""
Backfill verbatim full-text + LLM files for library cases the Open Australian Legal
Corpus carries (High Court / interstate). Western Australian cases are NOT in the
corpus and are skipped - use ingest.py for those.

Writes data/files/<id>/<id>.md (same shape as the pipeline's write_llm_file) and
sets each case's files.llm so the app's download button lights up. Idempotent:
skips cases that already have a full-text file. Never fabricates - a miss is reported.

  python pipeline/backfill.py                         # all eligible, missing-text cases
  python pipeline/backfill.py --ids hca-1993-63,hca-1978-22
  python pipeline/backfill.py --dry-run
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from corpus import fetch_judgment_meta, neutral_citation

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "data" / "cases.json"
FILES = ROOT / "data" / "files"


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", str(s or ""))


def yaml_str(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_md(case, row):
    text = row.get("text") or ""
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
        f"source: {yaml_str(str(row.get('source','')) + ' via Open Australian Legal Corpus (isaacus) - openly licensed')}",
        f"sourceUrl: {yaml_str(row.get('url',''))}",
        f"tags: [{', '.join(yaml_str(t) for t in tags)}]",
        "---", "",
        f"# {case.get('caseName','')} {case.get('citation','')}", "",
        "## One line", case.get("oneLine", ""), "",
        "## What happened", strip_tags(case.get("whatHappened", "")), "",
        "## What the Court held", strip_tags(case.get("whatHeld", "")), "",
        "## What it means for your casework", strip_tags(case.get("whatItMeans", "")), "",
        "## Verdict", strip_tags(case.get("verdict", "")), "",
        "---", "",
        f"## Full judgment (source text - {row.get('url','')} via Open Australian Legal Corpus)",
        "", text, "",
    ]
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", help="comma-separated case ids")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cases = json.loads(CASES.read_text(encoding="utf-8"))
    want = set(args.ids.split(",")) if args.ids else None
    report, changed = [], False

    for c in cases:
        cid = c["id"]
        if want and cid not in want:
            continue
        rel = f"data/files/{cid}/{cid}.md"
        if (c.get("files") or {}).get("llm") and (ROOT / rel).exists():
            report.append((cid, "skip - already has full text")); continue
        if not neutral_citation(c.get("citation", "")):
            report.append((cid, "skip - no neutral cite (WA/old -> ingest.py)")); continue
        row = fetch_judgment_meta(c.get("citation", ""))
        if not row or not (row.get("text") or ""):
            report.append((cid, "not in corpus (WA/gap -> ingest.py)")); continue
        report.append((cid, f"FOUND {len(row['text']):,} chars -> {rel}"))
        if args.dry_run:
            continue
        (FILES / cid).mkdir(parents=True, exist_ok=True)
        (ROOT / rel).write_text(build_md(c, row), encoding="utf-8")
        if not isinstance(c.get("files"), dict):
            c["files"] = {}
        c["files"]["llm"] = rel
        changed = True

    if changed and not args.dry_run:
        CASES.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print("\n=== backfill ===")
    for cid, msg in report:
        print(f"  {cid:24} {msg}")


if __name__ == "__main__":
    main()
