#!/usr/bin/env python3
"""
One command from "judgment files in Cases/" to "live on the site, written up and
fact-checked" — a thin driver over the pieces that already exist, not a new pipeline.

    python pipeline/add_case.py --batch Cases/ --audit --push

For every judgment file in the folder whose citation is not already in the library:

  1. DISCOVER   the citation comes from the FILENAME ("2026WASCA0104.doc" or
                "Name v Name - [2026] WASCA 104.doc") and is then verified against the
                file's own text (add_text.py's rule). Skips ids already in
                data/cases.json, ids on data/blocklist.json, and "Suppressed" files
                (a header and one word — the court published no judgment).
  2. CLEAN+GATE clean_word.py's full integrity report. REFUSE (citation not in the
                text, a LexisNexis digest, Jade citator bleed) stops THAT FILE; the
                rest of the batch continues. WARNs are kept on the case as flags and
                shown in the summary — never dropped silently.
  3. ANALYSE    the same analyse() -> build_case() -> write_llm_file() path that
                ingest.py and the scheduled pipeline use: one Opus call per case,
                sequential (parallel calls race on cases.json). The court's own
                DELIVERED / Date of Judgment line beats the model's reading of the date.
  4. FIDELITY   the stored verbatim text must equal the cleaned source (whitespace-
                normalised) or the run stops. Nothing else is checked here because
                nothing else has been asserted about the verbatim text.
  5. AUDIT      (--audit) a second, independent model call fact-checks the entry
                against the judgment: citations, sections, names, dates, figures,
                disposition, the holding, the bench — the session-10 VERIFY brief,
                scripted. A problem is a HOLD, not a drop: the case stays in the
                library with relevance "" and needsReview set, so it renders as
                "Held for review" and never carries an ACTION/AWARENESS call nobody
                has verified. The report goes to Cases/audits/<date>/<id>.md.
                Clear a hold by correcting data/cases.json and the .md, then
                `--recheck <id>` (re-audits from the stored text and lifts the hold
                if it comes back clean).
  6. PUBLISH    (--push) git add data -> commit -> pull --rebase origin main -> push,
                in that order (HANDOFF §6). The scheduled bot pushes 3x/day, so the
                rebase is normal.

data/cases.json is written ONCE, at the end — a crash mid-batch still saves what
completed. Re-running is safe: everything already in the library is skipped, so the
command can be fired by a folder watcher. Nothing is downloaded: the files in Cases/
are the one human step (HANDOFF §4c — eCourts is behind a CAPTCHA, Jade/AustLII
forbid it), except pre-1998 High Court text, which the openly licensed corpus supplies:

    python pipeline/add_case.py --from-corpus "[1989] HCA 66" --case "S v The Queen" --audit

Other modes:
    --in FILE [--citation ..] [--case ..]   one file instead of a folder
    --reattach        a file whose id is already an analysed case replaces that case's
                      verbatim text and keeps the write-up (re-sourcing a judgment)
    --dry-run         discover + clean + gate only; no model call, nothing written
    --limit N         analyse at most N cases this run (the rest are reported, not lost)
    --recheck ID      re-audit a held case from its stored text; lift the hold if clean

Requires ANTHROPIC_API_KEY (pipeline/.env) and `pip install -r pipeline/requirements.txt`
unless --dry-run. Word conversion needs macOS textutil; PDFs need poppler's pdftotext.
"""
import argparse
import atexit
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update as P          # noqa: E402  (sibling module; reuse its functions wholesale)
import add_text as A        # noqa: E402  (filename citation, names, dates, the cleaner gate)

JUDGMENT_SUFFIXES = (".doc", ".docx", ".rtf", ".txt", ".pdf")
SUPPRESSED_NAME = re.compile(r"\bsuppressed\b", re.I)
# A suppressed eCourts export is the header template and, in place of the reasons,
# the single word "Suppressed". Nothing else is that short with that word on its own line.
SUPPRESSED_LINE = re.compile(r"(?mi)^\s*suppressed\.?\s*$")
LOCK = P.ROOT / "pipeline" / ".add_case.lock"

AUDIT_SYSTEM = (
    "You are the adversarial fact-checker for a private case-law archive kept by a serving "
    "WA Police detective. A fabricated or mis-stated authority is a professional risk to him, "
    "not just an error. You are given a finished entry (JSON) and the verbatim judgment it was "
    "written from. Find anything in the entry that the judgment does not support.\n\n"
    "Check every one of these, one at a time:\n"
    "1. Citations and case names: every case name the entry cites must appear in the judgment, "
    "and a case merely listed in a 'Cases referred to' table but never reasoned about must not be "
    "presented as authority the Court applied.\n"
    "2. Section numbers and Act names: same number, subsection, Act, year and jurisdiction as "
    "the judgment.\n"
    "3. Names: every judge, party, witness, officer and expert — spelling and role (a judge below "
    "is not a judge on appeal).\n"
    "4. Dates: every date, and that it is the date of the thing the entry says it is.\n"
    "5. Numbers: sentence lengths, terms, parole eligibility, quantities, dollar figures, "
    "percentages, counts, ages.\n"
    "6. The disposition: did the appeal succeed or fail, on which grounds, and what orders were "
    "actually made.\n"
    "7. The holding: does the entry state the ratio the Court actually reasoned to, or a "
    "stronger, neater or more general proposition than the Court committed to? Flag any "
    "overstatement.\n"
    "8. Anything the Court expressly left open that the entry presents as decided.\n"
    "9. Court composition: is the bench correctly named, and is 'unanimous' / 'majority' right?\n"
    "10. Anything asserted that is simply not in the judgment at all.\n\n"
    "Do not comment on style, length, tone or word choice — only accuracy. Be specific and be "
    "honest: quote the entry's claim, say why it is wrong or unsupported, and quote what the "
    "judgment actually says (with the paragraph number if the judgment has one). If you could "
    "not confirm something either way, put it under unconfirmed rather than calling it a "
    "problem. Do not pad the list — a clean entry must come back CLEAN with an empty problems "
    "list. Return strict JSON only."
)

AUDIT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["CLEAN", "PROBLEMS"]},
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "field": {"type": "string"},
                    "claim": {"type": "string"},
                    "why": {"type": "string"},
                    "judgmentSays": {"type": "string"},
                },
                "required": ["field", "claim", "why", "judgmentSays"],
            },
        },
        "unconfirmed": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "problems", "unconfirmed"],
}

ENTRY_FIELDS = ("caseName", "citation", "court", "decided", "appealFrom", "outcome", "weight",
                "tags", "relevance", "oneLine", "whatHappened", "whatHeld", "whatItMeans",
                "verdict")


class FidelityError(RuntimeError):
    """The stored verbatim text is not the cleaned source. Stops the run."""


# ---------------------------------------------------------------------------
# 1. Discover
# ---------------------------------------------------------------------------
def discover(folder, existing, blocked, reattach=False):
    """(jobs, skipped) for every judgment file in `folder`.

    A job is {"kind": add|upgrade|reattach, "path", "citation", "id", "name"}.
    `skipped` is [(filename, why)] — every file that is not a job is named with its
    reason, because a silent skip is how a case goes missing."""
    by_id = {c["id"]: c for c in existing}
    jobs, skipped, seen = [], [], set()
    for f in sorted(Path(folder).expanduser().iterdir()):
        if f.name.startswith(("~$", ".")) or not f.is_file():
            continue                                   # Word lock files, dotfiles, dirs
        if f.suffix.lower() not in JUDGMENT_SUFFIXES:
            continue                                   # notes, lists, whatever else lives there
        cite = A.citation_from_filename(f)
        if not cite:
            skipped.append((f.name, 'no citation in the filename (expected like '
                                    '"2026WASCA0104.doc" or "Name v Name - [2026] WASCA 104.doc")'))
            continue
        cid = A.id_for_citation(cite)
        if SUPPRESSED_NAME.search(f.stem):
            skipped.append((f.name, f"{cid}: suppressed — the court published no judgment"))
            continue
        if cid in blocked:
            skipped.append((f.name, P.blocked_line(blocked[cid])))
            continue
        if cid in seen:
            skipped.append((f.name, f"{cid}: another file in this batch already carries this citation"))
            continue
        was = by_id.get(cid)
        if was and not was.get("textOnly"):
            if not reattach:
                skipped.append((f.name, f"{cid} is already in the library "
                                        f"(--reattach replaces its verbatim text and keeps the write-up)"))
                continue
            kind = "reattach"
        elif was:
            kind = "upgrade"                           # full-text-only entry: write it up from this file
        else:
            kind = "add"
        seen.add(cid)
        jobs.append({"kind": kind, "path": f, "citation": cite, "id": cid, "name": ""})
    return jobs, skipped


def looks_suppressed(text):
    """The eCourts template with 'Suppressed' where the reasons should be."""
    return len(text) < 6000 and SUPPRESSED_LINE.search(text) is not None


def source_label(path):
    """Provenance as add_text.py records it: the path relative to the repo when the
    file lives under it (Cases/ is gitignored, so this is a name, not a link)."""
    try:
        return str(Path(path).resolve().relative_to(P.ROOT))
    except (ValueError, OSError):
        return str(path)


# ---------------------------------------------------------------------------
# 2–4. Clean, analyse, fidelity
# ---------------------------------------------------------------------------
def make_item(job, text):
    m = P.CITATION_RE.search(job["citation"])
    if not m:
        raise ValueError(f"no medium-neutral citation in {job['citation']!r}")
    name = (job.get("name") or A.name_from_watchlist(job["id"])
            or A.name_from_text(text, job["citation"])
            or (A.name_from_filename(job["path"], job["citation"]) if job.get("path") else ""))
    item = P._item_from_match(m, name or "", "", name or "", via="submission")
    if item["courtTag"] not in P.COURTS:
        raise ValueError(f"court {item['courtTag']} is not in COURTS — add it in update.py first")
    if not name:
        item["caseName"] = "(case name pending)"
        P.log(f"  NOTE {item['id']}: no case name could be read — pass --case, or fix it in "
              f"cases.json and the .md afterwards")
    return item


def analyse_job(client, item, text, warns, source):
    """One model call -> a case object written to data/files/<id>/<id>.md."""
    truncated = len(text) > P.MAX_JUDGMENT_CHARS
    if truncated:
        P.log(f"  NOTE {item['id']}: judgment truncated at {P.MAX_JUDGMENT_CHARS:,} chars")
        text = text[:P.MAX_JUDGMENT_CHARS]
    analysis = P.analyse(client, item, text, truncated)
    flags = [str(f) for f in (analysis.get("flags") or [])]
    flags += [f"cleaner: {w}" for w in warns]
    if truncated:
        flags.append(f"judgment truncated at {P.MAX_JUDGMENT_CHARS:,} characters for analysis")
    analysis["flags"] = flags
    case = P.build_case(item, analysis)
    header_date = A.decided_from_text(text)            # the court's own line, read verbatim
    if header_date:
        if case["decided"] != header_date:
            P.log(f"  date {item['id']}: header says {header_date}, model said "
                  f"{case['decided']!r} — using the header")
        case["decided"] = header_date
        case["date"] = P.dmy_to_iso(header_date) or case["date"]
    if flags:
        case["flags"] = flags
    P.write_llm_file(case, text, analysis, source=source)
    return case, text


def stored_text(cid):
    md = (P.FILES_DIR / cid / f"{cid}.md").read_text(encoding="utf-8")
    i = md.index("## Full judgment")
    return md[i:].split("\n", 1)[1].strip()


def stored_source(cid):
    md = (P.FILES_DIR / cid / f"{cid}.md").read_text(encoding="utf-8")
    m = re.search(r'^source: "((?:[^"\\]|\\.)*)"$', md, re.M)
    return m.group(1).replace('\\"', '"').replace("\\\\", "\\") if m else None


def verify_fidelity(case, text):
    """Stored verbatim == cleaned source, whitespace-normalised (HANDOFF §4.5)."""
    n = lambda s: re.sub(r"\s+", " ", s).strip()      # noqa: E731
    if n(stored_text(case["id"])) != n(text):
        raise FidelityError(f"{case['id']}: the stored judgment text differs from the cleaned "
                            f"source — stopping before anything is published")


def reattach(job, text, case, source):
    """Replace an analysed case's verbatim text, keep its write-up (§7.2(b) re-sourcing)."""
    before = len(stored_text(case["id"]))
    P.write_llm_file(case, text, {"flags": case.get("flags") or []}, source=source)
    verify_fidelity(case, text)
    return case, before, len(text.strip())


# ---------------------------------------------------------------------------
# 5. Audit
# ---------------------------------------------------------------------------
def audit_case(client, case, text):
    """Second, independent model call. Returns {"verdict", "problems", "unconfirmed"}."""
    entry = {k: case.get(k, "") for k in ENTRY_FIELDS}
    user = (f"ENTRY (JSON):\n{json.dumps(entry, ensure_ascii=False, indent=1)}\n\n"
            f"VERBATIM JUDGMENT ({case['citation']}):\n{text}")
    with client.messages.stream(
        model=P.MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high",
                       "format": {"type": "json_schema", "schema": AUDIT_SCHEMA}},
        system=AUDIT_SYSTEM,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError("audit refused by safety classifier")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("audit truncated — hit max_tokens cap")
    out = next((b.text for b in msg.content if b.type == "text"), None)
    if not out:
        raise RuntimeError("no text block in audit response")
    result = json.loads(out)
    if result["verdict"] == "CLEAN" and result["problems"]:
        result["verdict"] = "PROBLEMS"                 # a listed problem is a problem
    return result


def render_audit_report(case, result, when, model):
    lines = [f"# {case['id']} — {case['caseName']} {case['citation']}", "",
             f"VERDICT: {result['verdict']}", ""]
    for p in result.get("problems") or []:
        lines.append(f"- **{p['field']}** — \"{p['claim']}\" — {p['why']} "
                     f"The judgment says: \"{p['judgmentSays']}\"")
    if result.get("unconfirmed"):
        lines += ["", "## Unconfirmed", *[f"- {u}" for u in result["unconfirmed"]]]
    cid = case["id"]
    lines += ["", f"_Fact-checked {when} by pipeline/add_case.py --audit ({model}) against "
                  f"data/files/{cid}/{cid}.md. A PROBLEMS verdict holds the case — relevance "
                  f"cleared, needsReview set, badge 'Held for review' — until the entry is "
                  f"corrected in data/cases.json and the .md and `add_case.py --recheck {cid}` "
                  f"comes back CLEAN._"]
    return "\n".join(lines) + "\n"


def write_audit_report(case, result, audit_dir, when):
    audit_dir = Path(audit_dir)
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"{case['id']}.md"
    path.write_text(render_audit_report(case, result, when, P.MODEL), encoding="utf-8")
    return path


def hold(case, report_path, n_problems, when):
    """A problem is a hold, not a drop: the case stays, the call goes."""
    case["needsReview"] = {"heldOn": when, "call": case.get("relevance", ""),
                           "problems": n_problems, "report": source_label(report_path)}
    case["relevance"] = ""


def lift_hold(case):
    held = case.pop("needsReview", None) or {}
    if not case.get("relevance"):
        case["relevance"] = held.get("call") or "AWARENESS"


# ---------------------------------------------------------------------------
# 6. Publish
# ---------------------------------------------------------------------------
def publish(label):
    def git(*args):
        return subprocess.run(["git", *args], cwd=P.ROOT, check=True,
                              capture_output=True, text=True)
    git("add", "data")
    staged = subprocess.run(["git", "status", "--porcelain", "data"], cwd=P.ROOT,
                            capture_output=True, text=True).stdout.strip()
    if not staged:
        P.log("publish: no data changes to commit")
        return False
    git("commit", "-m", label)
    try:
        git("pull", "--rebase", "origin", "main")
    except subprocess.CalledProcessError as e:
        subprocess.run(["git", "rebase", "--abort"], cwd=P.ROOT)
        P.die(f"publish: rebase onto origin/main failed — the commit is LOCAL; resolve and push "
              f"by hand ({(e.stderr or '').strip()[-300:]})")
    try:
        git("push")
    except subprocess.CalledProcessError as e:
        P.die(f"publish: push failed — the commit is LOCAL; run `git push` by hand "
              f"({(e.stderr or '').strip()[-300:]})")
    P.log(f"published: {label}")
    return True


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def corpus_job(citation, name):
    from corpus import fetch_judgment_meta
    row = fetch_judgment_meta(citation, tries=8)     # the HF index is intermittently slow
    if not row or not (row.get("text") or "").strip():
        raise ValueError(f"{citation} is not in the Open Australian Legal Corpus "
                         f"(HCA to ~1998 only; zero WA) — supply the judgment as a file instead")
    text = row["text"].strip()
    cid = A.id_for_citation(citation)
    name = name or P.clean_case_name(str(row.get("citation") or ""), citation)
    if name == "(case name pending)":
        name = ""
    url = str(row.get("url") or "").strip()
    # The corpus mirrors the courts' own sites. Naming the originating court and the
    # URL is a condition of the High Court's licence (HANDOFF §4c), not decoration.
    origin = {"high_court_of_australia": "High Court of Australia"}.get(
        str(row.get("source") or ""), str(row.get("source") or "unknown source"))
    label = f"{origin} via the Open Australian Legal Corpus" + (f" — {url}" if url else "")
    return {"kind": "add", "path": None, "citation": citation, "id": cid, "name": name,
            "text": text, "warns": [], "source": label}


def run(jobs, existing, *, audit=False, dry_run=False, limit=None, audit_dir=None):
    """Execute jobs sequentially. Returns (completed_cases, results) where results is
    [(job, status, detail)] with status in add/upgrade -> added|held, reattach ->
    reattached, or refused|suppressed|ready|deferred|error."""
    by_id = {c["id"]: c for c in existing}
    when = dt.date.today().isoformat()
    client, n_model = None, 0
    completed, results = [], []
    for job in jobs:
        try:
            if "text" in job:                            # --from-corpus
                text, warns, source = job["text"], job["warns"], job["source"]
            else:
                text, warns = A.clean_and_report(job["path"], job["citation"])
                source = source_label(job["path"])
            text = text.strip()                          # what ingest.py / add_text.py store
        except ValueError as e:
            results.append((job, "refused", str(e)))
            continue
        if looks_suppressed(text):
            results.append((job, "suppressed", "the court published no judgment — a header and "
                                               "the word 'Suppressed'"))
            continue
        if dry_run:
            results.append((job, "ready", f"{len(text):,} chars"
                            + (f"; WARN: {' | '.join(warns)}" if warns else "")))
            continue
        try:
            if job["kind"] == "reattach":
                case, before, after = reattach(job, text, dict(by_id[job["id"]]), source)
                completed.append(case)
                results.append((job, "reattached", f"{before:,} -> {after:,} chars of verbatim "
                                                   f"text; write-up kept"))
                continue
            if limit is not None and n_model >= limit:
                results.append((job, "deferred", f"--limit {limit} reached; re-run to pick it up"))
                continue
            client = client or P.get_client()
            item = make_item(job, text)
            n_model += 1
            case, text = analyse_job(client, item, text, warns, source)
            verify_fidelity(case, text)
            status, detail = "added", f"[{case['relevance']}] {case['caseName']}"
            if audit:
                result = audit_case(client, case, text)
                report = write_audit_report(case, result, audit_dir, when)
                if result["verdict"] != "CLEAN":
                    hold(case, report, len(result["problems"]), when)
                    P.write_llm_file(case, text, {"flags": case.get("flags") or []}, source=source)
                    status = "held"
                    detail = (f"{len(result['problems'])} problem(s) — {source_label(report)} — "
                              f"held for review (was {case['needsReview']['call']})")
                else:
                    detail += f" — audit CLEAN ({source_label(report)})"
            completed.append(case)
            results.append((job, status, detail))
        except FidelityError:
            raise                                        # stop the whole run; nothing is saved past here
        except Exception as e:                           # one bad case must not stop the batch
            results.append((job, "error", f"{type(e).__name__}: {str(e)[:300]}"))
            P.log(f"  ERROR {job['id']}: {e}")
    return completed, results


def merge_and_save(existing, completed):
    by_id = {c["id"]: c for c in existing}
    for c in completed:
        by_id[c["id"]] = c
    merged = sorted(by_id.values(), key=lambda c: str(c.get("date", "")), reverse=True)
    P.save_cases(merged)                                 # written ONCE per run
    return merged


def recheck(cid, existing, audit_dir):
    by_id = {c["id"]: c for c in existing}
    case = by_id.get(cid)
    if not case:
        P.die(f"{cid} is not in cases.json")
    if not case.get("needsReview"):
        P.die(f"{cid} is not held (no needsReview) — nothing to recheck")
    text = stored_text(cid)
    when = dt.date.today().isoformat()
    client = P.get_client()
    result = audit_case(client, case, text)
    report = write_audit_report(case, result, audit_dir, when)
    if result["verdict"] == "CLEAN":
        lift_hold(case)
        status = f"CLEAN — hold lifted, relevance {case['relevance']}"
    else:
        case["needsReview"].update({"heldOn": when, "problems": len(result["problems"]),
                                    "report": source_label(report)})
        status = f"{len(result['problems'])} problem(s) remain — still held ({source_label(report)})"
    P.write_llm_file(case, text, {"flags": case.get("flags") or []}, source=stored_source(cid))
    verify_fidelity(case, text)
    return case, status


def acquire_lock():
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        P.die(f"another add_case.py run appears to be in progress ({LOCK}). "
              f"If it is not, delete that file and try again.")
    atexit.register(lambda: LOCK.unlink(missing_ok=True))


def summarise(results, skipped, before, after, dry_run):
    order = {"added": 0, "held": 1, "reattached": 2, "ready": 3, "deferred": 4, "error": 5,
             "refused": 6, "suppressed": 7}
    print()
    print("add_case.py — " + ("DRY RUN, nothing written" if dry_run else
                               f"{dt.datetime.now():%d/%m/%Y %H:%M}"))
    for job, status, detail in sorted(results, key=lambda r: order.get(r[1], 9)):
        who = job["id"] + (f"  {job['path'].name}" if job.get("path") else "")
        print(f"  {status.upper():<10} {who}\n             {detail}")
    known = [n for n, why in skipped if "is already in the library" in why]
    for name, why in skipped:
        if name in known:
            continue
        print(f"  {'SKIPPED':<10} {name}\n             {why}")
    if known:                                   # the normal case for a folder that is re-run
        print(f"  {'SKIPPED':<10} {len(known)} file(s) already in the library "
              f"(--reattach replaces a case's verbatim text and keeps its write-up)")
    if not dry_run:
        print(f"cases.json: {before} -> {after}" + ("" if before == after else " (written once)"))
    if any(s == "held" for _, s, _ in results):
        print("\nHeld cases render as 'Held for review' with no Action/Awareness call. To clear one:\n"
              "  1. read its report (path above), 2. correct data/cases.json AND data/files/<id>/<id>.md,\n"
              "  3. python pipeline/add_case.py --recheck <id>   (lifts the hold if the re-audit is CLEAN)")


def main():
    ap = argparse.ArgumentParser(description="Judgment files -> analysed, fact-checked, published.")
    ap.add_argument("--batch", help="a folder of judgment files (usually Cases/)")
    ap.add_argument("--in", dest="src", help="one judgment file")
    ap.add_argument("--citation", help='with --in: e.g. "[2026] WASCA 111" (default: the filename)')
    ap.add_argument("--case", default="", help="case name override (with --in / --from-corpus)")
    ap.add_argument("--from-corpus", dest="corpus", metavar="CITATION",
                    help='pre-1998 High Court text from the Open Australian Legal Corpus')
    ap.add_argument("--reattach", action="store_true",
                    help="a file whose id is already analysed replaces its verbatim text")
    ap.add_argument("--audit", action="store_true", help="fact-check every new entry; hold on problems")
    ap.add_argument("--push", action="store_true", help="commit data/ and push (after a rebase)")
    ap.add_argument("--dry-run", action="store_true", help="discover + clean + gate only")
    ap.add_argument("--limit", type=int, help="analyse at most N cases this run")
    ap.add_argument("--audit-dir", help="where audit reports go (default Cases/audits/<today>)")
    ap.add_argument("--recheck", metavar="ID", help="re-audit a held case; lift the hold if clean")
    args = ap.parse_args()

    audit_dir = Path(args.audit_dir) if args.audit_dir else \
        P.ROOT / "Cases" / "audits" / dt.date.today().isoformat()
    existing = P.load_cases()
    blocked = P.load_blocklist()

    if args.recheck:
        acquire_lock()
        case, status = recheck(args.recheck, existing, audit_dir)
        merge_and_save(existing, [case])
        print(f"\n{args.recheck}: {status}")
        if args.push:
            publish(f"Library: re-audit {args.recheck} ({'hold lifted' if not case.get('needsReview') else 'still held'})")
        return

    jobs, skipped = [], []
    if args.corpus:
        try:
            jobs.append(corpus_job(args.corpus, args.case))
        except ValueError as e:
            P.die(str(e))
        cid = jobs[0]["id"]
        if cid in {c["id"] for c in existing} and not args.reattach:
            P.die(f"{cid} is already in the library (--reattach replaces its text)")
        if cid in blocked:
            P.die(P.blocked_line(blocked[cid]))
        if cid in {c["id"] for c in existing}:
            jobs[0]["kind"] = "reattach"
    elif args.batch:
        d = Path(args.batch).expanduser()
        if not d.is_dir():
            P.die(f"--batch {d} is not a directory")
        jobs, skipped = discover(d, existing, blocked, reattach=args.reattach)
    elif args.src:
        f = Path(args.src).expanduser()
        if not f.is_file():
            P.die(f"--in {f} is not a file")
        cite = args.citation or A.citation_from_filename(f)
        if not cite:
            P.die("no --citation given and none found in the filename")
        cid = A.id_for_citation(cite)
        by_id = {c["id"]: c for c in existing}
        if cid in blocked:
            P.die(P.blocked_line(blocked[cid]))
        if cid in by_id and not by_id[cid].get("textOnly") and not args.reattach:
            P.die(f"{cid} is already in the library (--reattach replaces its verbatim text)")
        kind = "reattach" if (cid in by_id and not by_id[cid].get("textOnly")) else \
               ("upgrade" if cid in by_id else "add")
        jobs.append({"kind": kind, "path": f, "citation": cite, "id": cid, "name": args.case})
    else:
        P.die("give --batch <dir>, --in <file>, --from-corpus <citation> or --recheck <id>")

    if not jobs and not skipped:
        P.die("nothing to do — no judgment files found")
    if not args.dry_run:
        acquire_lock()

    completed = []
    try:
        completed, results = run(jobs, existing, audit=args.audit, dry_run=args.dry_run,
                                 limit=args.limit, audit_dir=audit_dir)
    finally:
        after = len(existing)
        if completed and not args.dry_run:
            after = len(merge_and_save(existing, completed))
            P.log(f"cases.json: {len(existing)} -> {after}")
    summarise(results, skipped, len(existing), after, args.dry_run)

    if args.push and completed:
        ids = ", ".join(c["id"] for c in completed)
        held = [c["id"] for c in completed if c.get("needsReview")]
        label = (f"Library: add {len(completed)} case(s) via add_case.py — {ids}"
                 + (f" (held for review: {', '.join(held)})" if held else ""))
        publish(label)
    elif args.push:
        print("nothing new to publish")


if __name__ == "__main__":
    main()
