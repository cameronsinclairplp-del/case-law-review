#!/usr/bin/env python3
"""
The fact-check — one home for it, used by BOTH routes into the library:

  * pipeline/add_case.py   (Cameron's drops, --audit)     — since session 11
  * pipeline/update.py     (the daily bot, every case)    — since 20/09/2026

Until 20/09 the bot published straight from the analysis call; only dropped cases
were audited. Now every entry that reaches cases.json has been checked against
the verbatim judgment by a second, independent model call, and one with problems
is HELD (relevance cleared, needsReview set, the grey "Held for review" badge)
rather than published with a call. Nothing here talks to the API except
audit_case(); everything else is plain data.

Moved here verbatim from add_case.py (AUDIT_SYSTEM, AUDIT_SCHEMA, ENTRY_FIELDS,
audit_case, render_audit_report, write_audit_report, hold, lift_hold) so the two
callers cannot drift. MODEL is read from the same environment variable update.py
uses; this module does not import update.py (update.py imports it).
"""
import json
import os
from pathlib import Path

MODEL = os.environ.get("ANALYSIS_MODEL", "claude-opus-4-8")

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


def audit_case(client, case, text):
    """Second, independent model call. Returns {"verdict", "problems", "unconfirmed"}."""
    entry = {k: case.get(k, "") for k in ENTRY_FIELDS}
    user = (f"ENTRY (JSON):\n{json.dumps(entry, ensure_ascii=False, indent=1)}\n\n"
            f"VERBATIM JUDGMENT ({case['citation']}):\n{text}")
    with client.messages.stream(
        model=MODEL,
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


def failed_audit_result(err):
    """When the audit call itself fails, the case is HELD, never published unchecked."""
    return {"verdict": "PROBLEMS", "unconfirmed": [],
            "problems": [{"field": "(audit)", "claim": "the fact-check could not run",
                          "why": f"the audit call failed: {str(err)[:200]}", "judgmentSays": ""}]}


def render_audit_report(case, result, when, model, by="pipeline/add_case.py --audit"):
    lines = [f"# {case['id']} — {case['caseName']} {case['citation']}", "",
             f"VERDICT: {result['verdict']}", ""]
    for p in result.get("problems") or []:
        lines.append(f"- **{p['field']}** — \"{p['claim']}\" — {p['why']} "
                     f"The judgment says: \"{p['judgmentSays']}\"")
    if result.get("unconfirmed"):
        lines += ["", "## Unconfirmed", *[f"- {u}" for u in result["unconfirmed"]]]
    cid = case["id"]
    lines += ["", f"_Fact-checked {when} by {by} ({model}) against "
                  f"data/files/{cid}/{cid}.md. A PROBLEMS verdict holds the case — relevance "
                  f"cleared, needsReview set, badge 'Held for review' — until the entry is "
                  f"corrected in data/cases.json and the .md and `add_case.py --recheck {cid}` "
                  f"comes back CLEAN._"]
    return "\n".join(lines) + "\n"


def write_audit_report(case, result, audit_dir, when, by="pipeline/add_case.py --audit"):
    audit_dir = Path(audit_dir)
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"{case['id']}.md"
    path.write_text(render_audit_report(case, result, when, MODEL, by=by), encoding="utf-8")
    return path


def hold(case, report_label, n_problems, when):
    """A problem is a hold, not a drop: the case stays, the call goes. `report_label` is
    where the report can be read (a repo-relative path, or a sentence)."""
    case["needsReview"] = {"heldOn": when, "call": case.get("relevance", ""),
                           "problems": n_problems, "report": str(report_label)}
    case["relevance"] = ""


def lift_hold(case):
    held = case.pop("needsReview", None) or {}
    if not case.get("relevance"):
        case["relevance"] = held.get("call") or "AWARENESS"
