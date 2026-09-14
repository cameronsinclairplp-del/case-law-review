#!/usr/bin/env python3
"""Unit tests for pipeline/add_case.py — the one-command ingest driver.

No network, no Anthropic API, no textutil: the cleaner gate and the two model calls
are stubbed, and every write goes to a temp directory (FILES_DIR / CASES_PATH are
repointed for the duration of a test). What is pinned:

  * discovery skips ids already in the library, blocklisted ids, "Suppressed" files,
    files with no citation in the name, and duplicate citations in one batch —
    and names every skip with a reason;
  * --reattach turns an already-analysed id into a reattach job (verbatim replaced,
    write-up kept) instead of a skip;
  * a REFUSE from the cleaner stops that file only — nothing is written for it and
    the rest of the batch still lands;
  * a fact-check with problems is a HOLD: relevance cleared, needsReview set, the
    .md carries the held notice, the report is written — and the case is still saved;
  * cases.json is written exactly once per run, however many cases complete;
  * a fidelity mismatch stops the run;
  * --dry-run writes nothing;
  * --recheck lifts a hold when the re-audit is clean and keeps it when it is not.

Run directly (needs bs4 importable, because update.py imports it):

    python pipeline/test_add_case.py
"""
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import add_case as C   # noqa: E402
import add_text as A   # noqa: E402
import update as u     # noqa: E402

# A faithful slice of a WA eCourts export, padded past the cleaner's 800-char floor.
WA_TEXT = (
    "JURISDICTION\t:\tSUPREME COURT OF WESTERN AUSTRALIA\n"
    "\t\tTHE COURT OF APPEAL (WA)\n"
    "CITATION\t:\tTEST -v- THE STATE OF WESTERN AUSTRALIA [CITE]\n"
    "CORAM\t:\tMITCHELL JA\n"
    "HEARD\t:\t1 SEPTEMBER 2026\n"
    "DELIVERED\t:\t3 SEPTEMBER 2026\n"
    "FILE NO/S\t:\tCACR 1 of 2026\n"
    "BETWEEN\t:\tTEST\n\t\tAppellant\n\t\tAND\n\t\tTHE STATE OF WESTERN AUSTRALIA\n\t\tRespondent\n\n"
    "Catchwords:\nCriminal law - Appeal against sentence - Test fixture\n\n"
    "Counsel:\nAppellant : Mr A\nRespondent : Ms B\n\n"
    "MITCHELL JA:\n"
    + ("The appellant was sentenced on 1 May 2026 for one count of burglary. "
       "The sole ground is that the sentence was manifestly excessive. It was not. ") * 12
    + "\nI certify that the preceding paragraphs comprise the reasons for decision.\nAssociate\n"
)
SUPPRESSED_TEXT = WA_TEXT.split("Catchwords:")[0] + "\nSuppressed\n"

ANALYSIS = {
    "oneLine": "A <i>test</i> sentence appeal.", "whatHappened": "TEST was sentenced.",
    "whatHeld": "Appeal <b>dismissed</b>.", "whatItMeans": "Nothing changes for you.",
    "verdict": "AWARENESS — a fact-specific sentence appeal.", "outcome": "Appeal dismissed",
    "weight": "MITCHELL JA, single judge", "tags": ["sentencing", "burglary"],
    "relevance": "AWARENESS", "decided": "03/09/2026", "appealFrom": "District Court", "flags": [],
}
CLEAN = {"verdict": "CLEAN", "problems": [], "unconfirmed": []}
DIRTY = {"verdict": "PROBLEMS",
         "problems": [{"field": "whatHappened", "claim": "one count of burglary",
                       "why": "it was two", "judgmentSays": "two counts"}],
         "unconfirmed": ["the parole date"]}


@contextlib.contextmanager
def sandbox(existing=None, clean=None, analyse=None, audit=None):
    """Temp FILES_DIR + CASES_PATH, stubbed cleaner/model calls, restored afterwards."""
    saved = (u.FILES_DIR, u.CASES_PATH, A.clean_and_report, u.analyse, u.get_client, C.audit_case)
    saves = []
    with tempfile.TemporaryDirectory() as d:
        u.FILES_DIR = Path(d) / "files"
        u.CASES_PATH = Path(d) / "cases.json"
        u.CASES_PATH.write_text(json.dumps(existing or []), encoding="utf-8")
        A.clean_and_report = clean or (lambda path, cite: (WA_TEXT.replace("[CITE]", cite),
                                                            ["no counsel / solicitors block found"]))
        u.analyse = analyse or (lambda client, item, text, truncated: json.loads(json.dumps(ANALYSIS)))
        u.get_client = lambda: object()
        C.audit_case = audit or (lambda client, case, text: json.loads(json.dumps(CLEAN)))
        real_save = u.save_cases
        u.save_cases = lambda cases: (saves.append(len(cases)), real_save(cases))
        try:
            yield Path(d), saves
        finally:
            u.save_cases = real_save
            (u.FILES_DIR, u.CASES_PATH, A.clean_and_report, u.analyse, u.get_client,
             C.audit_case) = saved


def touch(folder, *names):
    for n in names:
        (folder / n).write_text("placeholder — the cleaner is stubbed", encoding="utf-8")


def analysed(cid, cite, **extra):
    c = {"id": cid, "date": "2026-01-01", "court": "x", "courtTag": cid.split("-")[0].upper(),
         "caseName": "Known v Case", "citation": cite, "decided": "01/01/2026",
         "relevance": "ACTION", "oneLine": "x", "whatHappened": "x", "whatHeld": "x",
         "whatItMeans": "x", "verdict": "x", "tags": [], "austliiUrl": "https://example.invalid",
         "files": {"llm": f"data/files/{cid}/{cid}.md"}}
    c.update(extra)
    return c


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------
def test_discover_skips_known_blocked_suppressed_and_junk():
    with sandbox() as (d, _):
        folder = d / "Cases"; folder.mkdir()
        touch(folder, "Known v Case - [2026] WASCA 1.doc", "2026WASCA0002.doc",
              "Suppressed (no judgment published) - [2026] WASCA 3.docx",
              "Blocked v Case - [1998] HCA 11.txt", "random notes.docx", "DOWNLOAD-LIST.md",
              "~$2026WASCA0002.doc", "Dup v Case - [2026] WASCA 2.pdf")
        existing = [analysed("wasca-2026-1", "[2026] WASCA 1")]
        blocked = {"hca-1998-11": {"id": "hca-1998-11", "citation": "[1998] HCA 11",
                                   "caseName": "Oshlack", "reason": "civil"}}
        jobs, skipped = C.discover(folder, existing, blocked)
    assert [j["id"] for j in jobs] == ["wasca-2026-2"], jobs
    assert jobs[0]["kind"] == "add" and jobs[0]["citation"] == "[2026] WASCA 2"
    why = dict(skipped)
    assert "already in the library" in why["Known v Case - [2026] WASCA 1.doc"]
    assert "suppressed" in why["Suppressed (no judgment published) - [2026] WASCA 3.docx"]
    assert "BLOCKED hca-1998-11" in why["Blocked v Case - [1998] HCA 11.txt"]
    assert "no citation in the filename" in why["random notes.docx"]
    assert "already carries this citation" in why["Dup v Case - [2026] WASCA 2.pdf"]
    assert "DOWNLOAD-LIST.md" not in why and "~$2026WASCA0002.doc" not in why   # not judgment files


def test_discover_reattach_and_upgrade_kinds():
    with sandbox() as (d, _):
        folder = d / "Cases"; folder.mkdir()
        touch(folder, "Known v Case - [2026] WASCA 1.doc", "Text Only - [2026] WASCA 4.doc")
        existing = [analysed("wasca-2026-1", "[2026] WASCA 1"),
                    analysed("wasca-2026-4", "[2026] WASCA 4", relevance="", textOnly=True)]
        jobs, skipped = C.discover(folder, existing, {}, reattach=False)
        assert [(j["id"], j["kind"]) for j in jobs] == [("wasca-2026-4", "upgrade")]
        assert len(skipped) == 1
        jobs, skipped = C.discover(folder, existing, {}, reattach=True)
        assert [(j["id"], j["kind"]) for j in jobs] == [("wasca-2026-1", "reattach"),
                                                        ("wasca-2026-4", "upgrade")]
        assert skipped == []


def test_looks_suppressed():
    assert C.looks_suppressed(SUPPRESSED_TEXT)
    assert not C.looks_suppressed(WA_TEXT)
    assert not C.looks_suppressed("The order was suppressed by the judge.\n" * 40)   # prose, not a lone line


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
def test_refuse_stops_the_file_and_the_batch_continues():
    def clean(path, cite):
        if "Digest" in Path(path).name:
            raise ValueError("digest markers (CaseBase) — this is an editorial summary")
        return WA_TEXT.replace("[CITE]", cite), []
    with sandbox(clean=clean) as (d, saves):
        folder = d / "Cases"; folder.mkdir()
        touch(folder, "Digest v Case - [2026] WASCA 5.doc", "Good v Case - [2026] WASCA 6.doc")
        jobs, _ = C.discover(folder, [], {})
        completed, results = C.run(jobs, [], audit=False, audit_dir=d / "audits")
        status = {j["id"]: (s, why) for j, s, why in results}
        assert status["wasca-2026-5"][0] == "refused" and "digest" in status["wasca-2026-5"][1]
        assert status["wasca-2026-6"][0] == "added"
        assert [c["id"] for c in completed] == ["wasca-2026-6"]
        assert not (u.FILES_DIR / "wasca-2026-5").exists()          # nothing written for the refused file
        md = (u.FILES_DIR / "wasca-2026-6" / "wasca-2026-6.md").read_text(encoding="utf-8")
        assert "relevance: AWARENESS" in md and "[2026] WASCA 6" in md
        assert 'source: "' in md                                      # provenance carried
        assert completed[0]["decided"] == "03/09/2026"               # the court's DELIVERED line
        assert completed[0]["date"] == "2026-09-03"


def test_header_date_beats_the_model():
    wrong = dict(ANALYSIS, decided="30/09/2026")
    with sandbox(analyse=lambda *a: json.loads(json.dumps(wrong))) as (d, _):
        folder = d / "Cases"; folder.mkdir()
        touch(folder, "2026WASCA0007.doc")
        jobs, _ = C.discover(folder, [], {})
        completed, _ = C.run(jobs, [], audit_dir=d / "audits")
    assert completed[0]["decided"] == "03/09/2026" and completed[0]["date"] == "2026-09-03"


def test_cleaner_warnings_become_flags():
    with sandbox() as (d, _):
        folder = d / "Cases"; folder.mkdir()
        touch(folder, "2026WASCA0008.doc")
        jobs, _ = C.discover(folder, [], {})
        completed, _ = C.run(jobs, [], audit_dir=d / "audits")
        md = (u.FILES_DIR / "wasca-2026-8" / "wasca-2026-8.md").read_text(encoding="utf-8")
    assert completed[0]["flags"] == ["cleaner: no counsel / solicitors block found"]
    assert "## Flags (verify before relying)\n- cleaner: no counsel" in md


def test_hold_clears_relevance_and_marks_needs_review():
    with sandbox(audit=lambda client, case, text: json.loads(json.dumps(DIRTY))) as (d, saves):
        folder = d / "Cases"; folder.mkdir()
        touch(folder, "2026WASCA0009.doc")
        jobs, _ = C.discover(folder, [], {})
        completed, results = C.run(jobs, [], audit=True, audit_dir=d / "audits")
        case = completed[0]
        md = (u.FILES_DIR / "wasca-2026-9" / "wasca-2026-9.md").read_text(encoding="utf-8")
        report = (d / "audits" / "wasca-2026-9.md").read_text(encoding="utf-8")
    assert results[0][1] == "held"
    assert case["relevance"] == ""                                    # no call nobody verified
    assert case["needsReview"]["call"] == "AWARENESS"                 # ...but the draft call is kept
    assert case["needsReview"]["problems"] == 1
    assert case["whatHeld"]                                           # the draft text stays for correction
    assert "relevance: \n" in md and "needsReview: true" in md
    assert "_Held for review" in md
    assert "VERDICT: PROBLEMS" in report and "one count of burglary" in report
    assert "## Unconfirmed\n- the parole date" in report


def test_clean_audit_keeps_the_call_and_writes_a_report():
    with sandbox() as (d, _):
        folder = d / "Cases"; folder.mkdir()
        touch(folder, "2026WASCA0010.doc")
        jobs, _ = C.discover(folder, [], {})
        completed, results = C.run(jobs, [], audit=True, audit_dir=d / "audits")
        report = (d / "audits" / "wasca-2026-10.md").read_text(encoding="utf-8")
    assert results[0][1] == "added" and "audit CLEAN" in results[0][2]
    assert completed[0]["relevance"] == "AWARENESS" and "needsReview" not in completed[0]
    assert report.startswith("# wasca-2026-10 — ") and "VERDICT: CLEAN" in report


def test_cases_json_written_once_per_run():
    with sandbox() as (d, saves):
        folder = d / "Cases"; folder.mkdir()
        touch(folder, "2026WASCA0011.doc", "2026WASCA0012.doc", "2026WASC0013.doc")
        existing = [analysed("wasca-2026-1", "[2026] WASCA 1")]
        jobs, _ = C.discover(folder, existing, {})
        completed, _ = C.run(jobs, existing, audit_dir=d / "audits")
        merged = C.merge_and_save(existing, completed)
        on_disk = json.loads(u.CASES_PATH.read_text(encoding="utf-8"))
    assert len(completed) == 3
    assert saves == [4], saves                                        # exactly one write, all four cases
    assert [c["id"] for c in on_disk] == [c["id"] for c in merged]
    assert {c["id"] for c in on_disk} == {"wasca-2026-1", "wasca-2026-11", "wasca-2026-12", "wasc-2026-13"}
    assert on_disk[-1]["id"] == "wasca-2026-1"                        # newest-first: the 2026-01-01 entry last


def test_limit_defers_without_losing():
    with sandbox() as (d, _):
        folder = d / "Cases"; folder.mkdir()
        touch(folder, "2026WASCA0014.doc", "2026WASCA0015.doc")
        jobs, _ = C.discover(folder, [], {})
        completed, results = C.run(jobs, [], limit=1, audit_dir=d / "audits")
    assert [s for _, s, _ in results] == ["added", "deferred"]
    assert len(completed) == 1


def test_dry_run_writes_nothing():
    with sandbox() as (d, saves):
        folder = d / "Cases"; folder.mkdir()
        touch(folder, "2026WASCA0016.doc")
        jobs, _ = C.discover(folder, [], {})
        completed, results = C.run(jobs, [], dry_run=True, audit_dir=d / "audits")
        assert not u.FILES_DIR.exists()
    assert completed == [] and results[0][1] == "ready" and "WARN" in results[0][2]
    assert saves == []


def test_suppressed_text_is_reported_not_added():
    with sandbox(clean=lambda p, c: (SUPPRESSED_TEXT.replace("[CITE]", c), [])) as (d, _):
        folder = d / "Cases"; folder.mkdir()
        touch(folder, "2026WASCA0017.doc")                              # nothing in the NAME says so
        jobs, _ = C.discover(folder, [], {})
        completed, results = C.run(jobs, [], audit_dir=d / "audits")
    assert completed == [] and results[0][1] == "suppressed"


def test_fidelity_mismatch_stops_the_run():
    real = u.write_llm_file

    def corrupt(case, text, analysis, source=None):
        real(case, "SOMETHING ELSE ENTIRELY " * 50, analysis, source=source)
    with sandbox() as (d, _):
        u.write_llm_file = corrupt
        try:
            folder = d / "Cases"; folder.mkdir()
            touch(folder, "2026WASCA0018.doc")
            jobs, _ = C.discover(folder, [], {})
            try:
                C.run(jobs, [], audit_dir=d / "audits")
                assert False, "expected FidelityError"
            except C.FidelityError as e:
                assert "wasca-2026-18" in str(e)
        finally:
            u.write_llm_file = real


def test_reattach_replaces_text_and_keeps_the_writeup():
    with sandbox() as (d, _):
        folder = d / "Cases"; folder.mkdir()
        touch(folder, "Known v Case - [2026] WASCA 1.doc")
        case = analysed("wasca-2026-1", "[2026] WASCA 1", whatHeld="KEEP ME")
        u.write_llm_file(dict(case), "OLD TEXT " * 200, {})            # what is stored today
        jobs, _ = C.discover(folder, [case], {}, reattach=True)
        completed, results = C.run(jobs, [case], audit_dir=d / "audits")
        md = (u.FILES_DIR / "wasca-2026-1" / "wasca-2026-1.md").read_text(encoding="utf-8")
    assert results[0][1] == "reattached"
    assert completed[0]["whatHeld"] == "KEEP ME" and completed[0]["relevance"] == "ACTION"
    assert "OLD TEXT" not in md and "MITCHELL JA:" in md


def test_recheck_lifts_a_hold_only_when_clean():
    with sandbox(audit=lambda client, case, text: json.loads(json.dumps(DIRTY))) as (d, saves):
        held = analysed("wasca-2026-19", "[2026] WASCA 19", relevance="",
                        needsReview={"heldOn": "2026-09-14", "call": "ACTION", "problems": 1,
                                     "report": "x"})
        u.write_llm_file(dict(held), WA_TEXT.replace("[CITE]", "[2026] WASCA 19"), {},
                         source="Cases/x.doc")
        case, status = C.recheck("wasca-2026-19", [held], d / "audits")
        assert "still held" in status and case["relevance"] == "" and case["needsReview"]["problems"] == 1
        C.audit_case = lambda client, case, text: json.loads(json.dumps(CLEAN))
        case, status = C.recheck("wasca-2026-19", [held], d / "audits")
        md = (u.FILES_DIR / "wasca-2026-19" / "wasca-2026-19.md").read_text(encoding="utf-8")
    assert "hold lifted" in status
    assert case["relevance"] == "ACTION" and "needsReview" not in case
    assert "needsReview" not in md and "relevance: ACTION" in md and 'source: "Cases/x.doc"' in md


def test_render_audit_report_shapes():
    case = analysed("wasca-2026-20", "[2026] WASCA 20")
    clean = C.render_audit_report(case, CLEAN, "2026-09-14", "model-x")
    dirty = C.render_audit_report(case, DIRTY, "2026-09-14", "model-x")
    assert "VERDICT: CLEAN" in clean and "## Unconfirmed" not in clean
    assert "VERDICT: PROBLEMS" in dirty and '- **whatHappened** — "one count of burglary"' in dirty
    assert "--recheck wasca-2026-20" in dirty


# ---------------------------------------------------------------------------
# zero-dependency runner (same shape as test_update.py)
# ---------------------------------------------------------------------------
def _main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
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
    total = len(tests)
    print(f"\n{total - failed}/{total} passed" + ("" if not failed else f", {failed} FAILED"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
