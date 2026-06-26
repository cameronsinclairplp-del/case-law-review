#!/usr/bin/env python3
"""
The Case-Law Review — daily update pipeline (runs in GitHub Actions, 3x/day).

Flow:
  1. Read BarNet Jade alert emails from Gmail via IMAP (rolling lookback window).
  2. Parse alerts into candidate cases; filter to scope; dedupe by id.
  3. Merge in any "pending" cases whose judgment wasn't published last time.
  4. Fetch each judgment's full text from AustLII.
  5. Analyse it with the Anthropic API (strict JSON, detective house style).
  6. Write data/files/<id>/<id>.md (full text + metadata) for the download button.
  7. Prepend the new case object(s) to data/cases.json (newest-first).
  8. Commit + push (Pages rebuilds). Email a summary if >=1 new case.

Design rules:
  - Only ever writes under data/. Never touches app code.
  - Idempotent: dedupe by id; re-running is safe; never duplicates a case.
  - DURABLE: cases whose judgment isn't out yet are persisted in data/state.json's
    "pending" queue and retried every run, independent of the email window, until
    they resolve or age out (then logged loudly — never silently dropped).
  - Fail loudly on infra errors (IMAP/API auth, unreadable data) -> non-zero exit.
  - A single bad case/message is logged and skipped, not fatal. cases.json is
    rewritten once, after all new cases are fully built (no half-writes).
"""

import datetime as dt
import email
import imaplib
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CASES_PATH = DATA / "cases.json"
STATE_PATH = DATA / "state.json"
FILES_DIR = DATA / "files"

APP_BASE = "https://cameronsinclairplp-del.github.io/case-law-review"
JADE_FROM = "editors@jade.io"
MODEL = os.environ.get("ANALYSIS_MODEL", "claude-opus-4-8")
MAX_JUDGMENT_CHARS = 500_000      # ~125k tokens; truncate longer judgments (flagged)
LOOKBACK_DAYS = 3                 # rolling IMAP window; dedupe-by-id makes overlap safe
PENDING_MAX_DAYS = 30             # give up on an unresolvable case after this (logged)
UA = ("Mozilla/5.0 (compatible; CaseLawReviewBot/1.0; "
      "+https://cameronsinclairplp-del.github.io/case-law-review/)")

# Courts in scope. "gated": only kept when a TOPIC keyword matches (noise control).
# HCA/WASCA/WASC = binding/WA primary. QCA/TASCCA/NTCCA/NTSC = persuasive Code
# jurisdictions (per the WA Detective's Canon, §30: other Code states are
# especially persuasive on Criminal Code questions; NT for Aboriginal-interview
# law, cf. Anunga). Persuasive courts are gated to criminal/investigation topics.
COURTS = {
    "HCA":    {"juris": "cth", "name": "High Court of Australia", "gated": False},
    "HCASJ":  {"juris": "cth", "name": "High Court of Australia (Single Justice)", "gated": False},
    "WASCA":  {"juris": "wa",  "name": "Supreme Court of WA — Court of Appeal", "gated": False},
    "WASC":   {"juris": "wa",  "name": "Supreme Court of Western Australia", "gated": False},
    "WADC":   {"juris": "wa",  "name": "District Court of Western Australia", "gated": True},
    "QCA":    {"juris": "qld", "name": "Supreme Court of Queensland — Court of Appeal", "gated": True},
    "TASCCA": {"juris": "tas", "name": "Supreme Court of Tasmania — Court of Criminal Appeal", "gated": True},
    "NTCCA":  {"juris": "nt",  "name": "Supreme Court of the NT — Court of Criminal Appeal", "gated": True},
    "NTSC":   {"juris": "nt",  "name": "Supreme Court of the Northern Territory", "gated": True},
}

# Investigation / evidence topics a WA detective cares about (the Canon §8 watchlist
# plus the spec's keep-list). Used to gate the "gated" courts above.
# Leading \b only; most alternatives are word-PREFIXES (e.g. "intoxicat" must match
# "intoxication"), so a trailing \b would wrongly block them. Short/ambiguous terms
# carry their own \b (lie[sd]?, bail).
TOPIC_KEYWORDS = re.compile(
    r"\b(?:"
    r"evidence|admissib|exclud|improperly obtained|"
    r"confession|interview|interrogat|caution|voluntar|oppress|inducement|"
    r"right to silence|silence|"
    r"search|warrant|seiz|arrest|detain|reasonable suspicion|suspicion|"
    r"identif|"
    r"forensic|DNA|fingerprint|"
    r"listening device|surveillance|telecommunication|intercept|covert|controlled operation|assumed identity|"
    r"consciousness of guilt|lie[sd]?\b|fabricat|"
    r"homicide|murder|manslaughter|"
    r"sexual|indecent|rape|"
    r"family violence|domestic violence|restraining order|"
    r"drug|traffick|cultivat|"
    r"weapon|firearm|"
    r"complicity|accessor|aiding|abet|common purpose|joint enterprise|party to|"
    r"bail\b|"
    r"aborig|anunga|interpreter|prisoner.?s friend|"
    r"intoxicat|unsoundness|insanit|mental impairment|diminished"
    r")", re.I)

# Obvious out-of-scope topics to drop even from in-scope courts.
DROP_KEYWORDS = re.compile(
    r"\b(migration|deportation|visa|tax|taxation|patent|trademark|copyright|"
    r"bankruptc|insolvenc|workers.?compensation|family law|parenting order|"
    r"defamation|planning|strata|residential tenanc|industrial relations)\b", re.I)

SYSTEM_PROMPT = (
    "You are the case-law analyst for Cameron SINCLAIR, a First Class Constable and "
    "detective-in-training with WA Police in Karratha. From the judgment text provided, "
    "produce a briefing for a working detective. Return strict JSON only with keys: "
    "oneLine, whatHappened, whatHeld, whatItMeans, verdict, outcome, weight, tags, "
    "relevance, decided, appealFrom, flags.\n"
    "House style: dates DD/MM/YYYY; UPPERCASE surnames; legislation in italics (use "
    "<i>...</i>); plain, direct English; address him as \"you\" — whatItMeans is always "
    "\"what it means for your casework\". Use <b>...</b> for emphasis in whatHeld/whatItMeans.\n"
    "relevance = \"ACTION\" if it changes how you investigate, gather evidence, or charge; "
    "otherwise \"AWARENESS\". verdict is one line stating that call and why.\n"
    "Never invent a citation, section number, holding, party, or date. Use only what is in "
    "the judgment text and the supplied citation. If anything is uncertain (a section number, "
    "a date, the facts), put it in flags and keep the prose conservative — do not guess."
)

ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "oneLine": {"type": "string"},
        "whatHappened": {"type": "string"},
        "whatHeld": {"type": "string"},
        "whatItMeans": {"type": "string"},
        "verdict": {"type": "string"},
        "outcome": {"type": "string"},
        "weight": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "relevance": {"type": "string", "enum": ["ACTION", "AWARENESS"]},
        "decided": {"type": "string"},
        "appealFrom": {"type": "string"},
        "flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "oneLine", "whatHappened", "whatHeld", "whatItMeans", "verdict",
        "outcome", "weight", "tags", "relevance", "decided", "appealFrom", "flags",
    ],
}

CITATION_RE = re.compile(r"\[(\d{4})\]\s*([A-Za-z]{2,8})\s*(\d+)")
# party-pair extraction, anchored to end-of-string (the case name sits just before
# the citation). Plaintiff is lazy + bounded so a capitalised preamble can't be
# swallowed; defendant runs to the end.
_PLAINTIFF = r"[A-Z][\w'.()&-]*(?:\s+[\w'.()&-]+){0,5}?"
_DEFENDANT = r"[\w'.()&-]+(?:\s+[\w'.()&-]+){0,7}"
CASE_NAME_RE = re.compile(rf"({_PLAINTIFF}\s+v\.?\s+{_DEFENDANT})\s*$")


def log(msg):
    print(f"[pipeline] {msg}", flush=True)


def die(msg):
    print(f"[pipeline][FATAL] {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# State (durable: pending queue persists across runs via data/state.json)
# ---------------------------------------------------------------------------
def load_state():
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(s, dict):
            s = {}
    except FileNotFoundError:
        s = {}
    except Exception as e:
        log(f"state.json unreadable ({e}); starting fresh")
        s = {}
    s.setdefault("pending", [])
    if not isinstance(s["pending"], list):
        s["pending"] = []
    return s


def save_state(pending):
    # Stable key order + only the durable queue -> byte-identical when unchanged,
    # so quiet runs produce no commit (no empty-commit churn).
    STATE_PATH.write_text(
        json.dumps({
            "pending": pending,
            "note": ("Durable retry queue: cases seen in a Jade alert whose judgment "
                     "wasn't yet published. Retried every run until resolved or aged out "
                     f"(> {PENDING_MAX_DAYS} days). Written by pipeline/update.py."),
        }, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def imap_since_date():
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=LOOKBACK_DAYS)


# ---------------------------------------------------------------------------
# IMAP — read Jade alerts
# ---------------------------------------------------------------------------
def fetch_alert_html(user, password, since_dt):
    since_str = since_dt.strftime("%d-%b-%Y")
    bodies = []
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        M.login(user, password)
    except imaplib.IMAP4.error as e:
        die(f"IMAP login failed (check MAIL_USERNAME / MAIL_PASSWORD app password): {e}")
    except Exception as e:
        die(f"IMAP connection failed: {e}")
    try:
        M.select("INBOX")
        typ, data = M.search(None, f'(SINCE "{since_str}" FROM "{JADE_FROM}")')
        if typ != "OK":
            die(f"IMAP search failed: {typ}")
        raw = (data[0] if data else b"") or b""   # ('OK', [None]) safety
        ids = raw.split()
        log(f"IMAP: {len(ids)} message(s) from {JADE_FROM} since {since_str}")
        for mid in ids:
            try:
                typ, msgdata = M.fetch(mid, "(RFC822)")
                if typ != "OK" or not msgdata:
                    continue
                # imaplib can interleave untagged responses; find the first
                # (header, body) tuple whose payload is bytes.
                payload = next(
                    (p[1] for p in msgdata
                     if isinstance(p, tuple) and len(p) >= 2
                     and isinstance(p[1], (bytes, bytearray))),
                    None)
                if payload is None:
                    log(f"  skip {mid!r}: unexpected FETCH shape")
                    continue
                msg = email.message_from_bytes(payload)
                body = extract_body(msg)
                if body:
                    bodies.append(body)
            except Exception as e:
                log(f"  skip {mid!r}: fetch/parse error {e}")
                continue
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return bodies


def extract_body(msg):
    html = plain = None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/html", "text/plain"):
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            except Exception:
                continue
            if ctype == "text/html" and html is None:
                html = text
            elif ctype == "text/plain" and plain is None:
                plain = text
    else:
        try:
            payload = msg.get_payload(decode=True)
            text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace") if payload else ""
        except Exception:
            text = ""
        (html, plain) = (text, None) if msg.get_content_type() == "text/html" else (None, text)
    return html or plain or ""


# ---------------------------------------------------------------------------
# Parse alerts -> candidate items
# ---------------------------------------------------------------------------
def parse_alert(body):
    items = []
    if "<" in body and ">" in body:
        soup = BeautifulSoup(body, "html.parser")
        for a in soup.find_all("a", href=True):
            if "jade.io" not in a["href"]:
                continue
            text = " ".join(a.get_text(" ").split())
            m = CITATION_RE.search(text)
            blurb = text
            if not m and a.find_parent():
                blurb = " ".join(a.find_parent().get_text(" ").split())
                m = CITATION_RE.search(blurb)
            if m:
                items.append(_item_from_match(m, text or blurb, a["href"], blurb))
        _scan_text_for_citations(" ".join(soup.get_text(" ").split()), items)
    else:
        _scan_text_for_citations(" ".join(body.split()), items)

    seen, out = set(), []
    for it in items:
        if it and it["id"] not in seen:
            seen.add(it["id"])
            out.append(it)
    return out


def _scan_text_for_citations(text, items):
    for m in CITATION_RE.finditer(text):
        start = max(0, m.start() - 160)
        blurb = text[start:m.end() + 40]
        pre = text[start:m.start()]
        # take the fragment after the last structural/sentence boundary, capped,
        # then let clean_case_name find the actual "A v B" pair.
        pre = re.split(r"\s*[|·•—–]\s*|(?<=[.!?])\s+", pre)[-1]
        name = pre[-90:].strip(" .,-—|·•\t")
        items.append(_item_from_match(m, name, "", blurb))


def _item_from_match(m, name, href, blurb):
    year, code, num = m.group(1), m.group(2).upper(), m.group(3)
    return {
        "id": f"{code.lower()}-{year}-{num}",
        "citation": f"[{year}] {code} {num}",
        "courtTag": code,
        "year": year,
        "num": num,
        "caseName": clean_case_name(name, f"[{year}] {code} {num}"),
        "jadeUrl": href if _is_jade_url(href) else "",
        "blurb": blurb or "",
    }


def _is_jade_url(url):
    if not url:
        return False
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme in ("http", "https") and (host == "jade.io" or host.endswith(".jade.io"))


def clean_case_name(raw, citation):
    name = re.sub(r"\s+", " ", (raw or "").replace(citation, "")).strip(" .,-—|·•\t")
    m = CASE_NAME_RE.search(name)
    if m:
        return m.group(1).strip(" .,-—|·•")
    frag = re.split(r"\s*[|·•—–]\s*|(?<=[.!?])\s+", name)[-1].strip()
    return frag[-80:].strip() or "(case name pending)"


# ---------------------------------------------------------------------------
# Scope filter
# ---------------------------------------------------------------------------
def in_scope(item):
    tag = item["courtTag"]
    if tag not in COURTS:
        return False, f"court {tag} not in scope"
    blob = f"{item.get('caseName','')} {item.get('blurb','')}"
    if DROP_KEYWORDS.search(blob):
        return False, "out-of-scope topic"
    if COURTS[tag]["gated"] and not TOPIC_KEYWORDS.search(blob):
        return False, f"{tag}: no investigation/evidence topic keyword"
    return True, "in scope"


def austlii_url(item):
    juris = COURTS[item["courtTag"]]["juris"]
    return (f"https://www.austlii.edu.au/cgi-bin/viewdoc/au/cases/"
            f"{juris}/{item['courtTag']}/{item['year']}/{item['num']}.html")


# ---------------------------------------------------------------------------
# Fetch full judgment text
# ---------------------------------------------------------------------------
def fetch_judgment(url):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
    except Exception as e:
        log(f"  fetch error {url}: {e}")
        return None
    if r.status_code != 200:
        log(f"  fetch {url} -> HTTP {r.status_code}")
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "nav", "header", "footer", "form"]):
        t.decompose()
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    if len(text) < 800:
        log(f"  fetched text too short ({len(text)} chars) — treating as unresolved")
        return None
    return text


# ---------------------------------------------------------------------------
# Analyse via Anthropic API (strict JSON)
# ---------------------------------------------------------------------------
def get_client():
    import anthropic
    if not os.environ.get("ANTHROPIC_API_KEY"):
        die("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic()


def analyse(client, item, judgment_text, truncated):
    user = (
        f"Verified citation: {item['citation']}\n"
        f"Verified case name: {item['caseName']}\n"
        f"Verified court: {COURTS[item['courtTag']]['name']}\n"
        f"{'NOTE: the judgment text below was truncated for length.' if truncated else ''}\n\n"
        f"JUDGMENT TEXT:\n{judgment_text}"
    )
    with client.messages.stream(
        model=MODEL,
        max_tokens=32000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high",
                       "format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError("analysis refused by safety classifier")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("analysis truncated — hit max_tokens cap (increase max_tokens)")
    text = next((b.text for b in msg.content if b.type == "text"), None)
    if not text:
        raise RuntimeError("no text block in analysis response")
    return json.loads(text)


# ---------------------------------------------------------------------------
# Build case object + LLM file
# ---------------------------------------------------------------------------
def dmy_to_iso(decided):
    s = (decided or "").strip()
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})\b", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})\b", s)   # tolerate ISO too
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def build_case(item, a):
    decided = (a.get("decided") or "").strip()
    iso = dmy_to_iso(decided)
    if not iso:
        log(f"  note {item['id']}: decided '{decided}' not DD/MM/YYYY — using year-only date")
    date = iso or item["year"]
    return {
        "id": item["id"],
        "date": date,
        "court": COURTS[item["courtTag"]]["name"],
        "courtTag": item["courtTag"],
        "caseName": item["caseName"],
        "citation": item["citation"],
        "decided": decided or item["year"],
        "appealFrom": a.get("appealFrom", ""),
        "outcome": a.get("outcome", ""),
        "weight": a.get("weight", ""),
        "tags": a.get("tags", []) if isinstance(a.get("tags"), list) else [],
        "relevance": "ACTION" if str(a.get("relevance", "")).upper() == "ACTION" else "AWARENESS",
        "oneLine": a.get("oneLine", ""),
        "whatHappened": a.get("whatHappened", ""),
        "whatHeld": a.get("whatHeld", ""),
        "whatItMeans": a.get("whatItMeans", ""),
        "verdict": a.get("verdict", ""),
        "austliiUrl": austlii_url(item),
        "jadeUrl": item.get("jadeUrl", ""),
        "files": {},
    }


def write_llm_file(case, judgment_text, analysis):
    cid = case["id"]
    out_dir = FILES_DIR / cid
    out_dir.mkdir(parents=True, exist_ok=True)
    flags = analysis.get("flags") or []
    parts = [
        "---",
        f"id: {cid}",
        f"caseName: {yaml_str(case['caseName'])}",
        f"citation: {yaml_str(case['citation'])}",
        f"court: {yaml_str(case['court'])}",
        f"decided: {yaml_str(case['decided'])}",
        f"relevance: {case['relevance']}",
        f"austliiUrl: {yaml_str(case['austliiUrl'])}",
        f"tags: [{', '.join(yaml_str(t) for t in case['tags'])}]",
        "---", "",
        f"# {case['caseName']} {case['citation']}", "",
        "## One line", case["oneLine"], "",
        "## What happened", strip_tags(case["whatHappened"]), "",
        "## What the Court held", strip_tags(case["whatHeld"]), "",
        "## What it means for your casework", strip_tags(case["whatItMeans"]), "",
        "## Verdict", strip_tags(case["verdict"]), "",
    ]
    if flags:
        parts += ["## Flags (verify before relying)", *[f"- {f}" for f in flags], ""]
    parts += ["---", "", "## Full judgment (source text)", "", judgment_text, ""]
    (out_dir / f"{cid}.md").write_text("\n".join(parts), encoding="utf-8")
    case["files"] = {"llm": f"data/files/{cid}/{cid}.md"}
    log(f"  wrote data/files/{cid}/{cid}.md")


def yaml_str(s):
    return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"') + '"'


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", str(s or ""))


# ---------------------------------------------------------------------------
# cases.json
# ---------------------------------------------------------------------------
def load_cases():
    try:
        data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        die("data/cases.json not found")
    if not isinstance(data, list):
        die("data/cases.json is not a JSON array")
    return data


def save_cases(cases):
    CASES_PATH.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")


# ---------------------------------------------------------------------------
# Commit + push (with rebase-on-conflict retry)
# ---------------------------------------------------------------------------
def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True)


def commit_and_push(label):
    git("config", "user.name", "case-law-bot")
    git("config", "user.email", "case-law-bot@users.noreply.github.com")
    git("add", "data")
    status = subprocess.run(["git", "status", "--porcelain", "data"],
                            cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if not status:
        log("no data changes to commit")
        return False
    git("commit", "-m", label)
    for attempt in range(3):
        try:
            git("push")
            log(f"committed + pushed: {label}")
            return True
        except subprocess.CalledProcessError:
            if attempt == 2:
                die("git push failed after retries (non-fast-forward / network?)")
            try:
                git("fetch", "origin", "main")
                git("rebase", "origin/main")
            except subprocess.CalledProcessError:
                subprocess.run(["git", "rebase", "--abort"], cwd=ROOT)
                die("git rebase onto origin/main failed (conflict outside data/?)")
    return False


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def send_email(user, password, new_cases):
    n = len(new_cases)
    today = dt.datetime.now(dt.timezone.utc).strftime("%d/%m/%Y")
    subject = f"WA Case-Law Review — {n} new {'case' if n == 1 else 'cases'} ({today})"
    lines = [f"{n} new {'case' if n == 1 else 'cases'} added to The Case-Law Review:", ""]
    html_items = []
    for c in new_cases:
        url = f"{APP_BASE}/#/case/{c['id']}"
        lines += [f"• {c['caseName']} {c['citation']} — {c['courtTag']} · {c['relevance']}",
                  f"  {c['oneLine']}", f"  {url}", ""]
        html_items.append(
            f"<li style='margin-bottom:14px'><strong>{esc(c['caseName'])} {esc(c['citation'])}</strong> — "
            f"{esc(c['courtTag'])} · {esc(c['relevance'])}<br>"
            f"<span style='color:#46423A'>{esc(c['oneLine'])}</span><br>"
            f"<a href='{esc(url)}'>{esc(url)}</a></li>")
    lines += ["Full analysis + downloads in the app."]
    html = (f"<div style='font-family:Inter,Arial,sans-serif;color:#1A1813'>"
            f"<p>{n} new {'case' if n == 1 else 'cases'} added to "
            f"<strong>The Case-Law Review</strong>:</p>"
            f"<ul style='list-style:none;padding-left:0'>{''.join(html_items)}</ul>"
            f"<p style='color:#7C7563;font-size:13px'>Full analysis + downloads in the app: "
            f"<a href='{esc(APP_BASE)}/'>{esc(APP_BASE)}/</a></p></div>")

    em = EmailMessage()
    em["Subject"] = subject
    em["From"] = f"Case-Law Review <{user}>"
    em["To"] = user
    em.set_content("\n".join(lines))
    em.add_alternative(html, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, password)
        s.send_message(em)
    log(f"email sent: {subject}")


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    user = os.environ.get("MAIL_USERNAME")
    password = os.environ.get("MAIL_PASSWORD")
    if not user or not password:
        die("MAIL_USERNAME / MAIL_PASSWORD not set")

    state = load_state()
    pending = state.get("pending", [])
    existing = load_cases()
    existing_ids = {c.get("id") for c in existing}

    bodies = fetch_alert_html(user, password, imap_since_date())
    log(f"alerts read: {len(bodies)}")

    # parse + dedupe across alerts
    candidates, seen = [], set()
    for body in bodies:
        for it in parse_alert(body):
            if it["id"] not in seen:
                seen.add(it["id"])
                candidates.append(it)
    log(f"candidates parsed: {len(candidates)}")

    # in-scope + new (not already in the library)
    kept = []
    for it in candidates:
        ok, why = in_scope(it)
        if not ok:
            log(f"  skip {it['id']} ({it['citation']}): {why}")
            continue
        if it["id"] in existing_ids:
            continue
        kept.append(it)

    # merge in the durable pending queue (fresh alert data wins on id collision)
    work = {}
    for p in pending:
        if p.get("id") and p["id"] not in existing_ids:
            work[p["id"]] = p
    for it in kept:
        prev = work.get(it["id"], {})
        it["firstSeen"] = prev.get("firstSeen", now_iso())
        work[it["id"]] = it
    for p in work.values():
        p.setdefault("firstSeen", now_iso())
    log(f"work items (new alerts + pending): {len(work)} (kept={len(kept)}, pending_in={len(pending)})")

    client = get_client() if work else None
    new_cases, still_pending, gave_up = [], [], []
    for it in work.values():
        try:
            text = fetch_judgment(austlii_url(it))
            if not text and it.get("jadeUrl"):
                text = fetch_judgment(it["jadeUrl"])
            if not text:
                age = _age_days(it.get("firstSeen"))
                if age > PENDING_MAX_DAYS:
                    gave_up.append(it)
                    log(f"  GAVE UP {it['id']} ({it['citation']}): unresolved after {age:.0f} days")
                else:
                    still_pending.append(_pending_record(it))
                    log(f"  pending {it['id']} ({it['citation']}): judgment not out yet ({age:.0f}d)")
                continue
            truncated = len(text) > MAX_JUDGMENT_CHARS
            if truncated:
                text = text[:MAX_JUDGMENT_CHARS]
            log(f"  analysing {it['id']} ({it['citation']}) — {len(text)} chars")
            analysis = analyse(client, it, text, truncated)
            case = build_case(it, analysis)
            write_llm_file(case, text, analysis)
            new_cases.append(case)
            log(f"  built {it['id']} [{case['relevance']}] {case['caseName']}")
        except Exception as e:
            # keep it pending so a transient analysis/fetch error retries next run
            still_pending.append(_pending_record(it))
            log(f"  ERROR on {it['id']} ({it['citation']}): {e} — kept pending")
            continue

    if new_cases:
        merged = new_cases + existing
        merged.sort(key=lambda c: str(c.get("date", "")), reverse=True)
        save_cases(merged)
        log(f"cases.json: {len(existing)} -> {len(merged)}")

    save_state(still_pending)

    label = (f"Pipeline: add {len(new_cases)} case(s)" if new_cases
             else "Pipeline: update pending queue")
    pushed = commit_and_push(label)

    if new_cases:
        send_email(user, password, new_cases)
    else:
        log("no new cases — no email (no spam on quiet days)")

    log(f"RUN SUMMARY: alerts={len(bodies)} candidates={len(candidates)} "
        f"new={len(new_cases)} pending={len(still_pending)} gave_up={len(gave_up)} pushed={pushed}")


def _pending_record(it):
    return {
        "id": it["id"], "citation": it["citation"], "courtTag": it["courtTag"],
        "year": it["year"], "num": it["num"], "caseName": it.get("caseName", ""),
        "jadeUrl": it.get("jadeUrl", ""), "blurb": it.get("blurb", ""),
        "firstSeen": it.get("firstSeen", now_iso()),
    }


def _age_days(first_seen_iso):
    try:
        first = dt.datetime.fromisoformat(first_seen_iso)
        if first.tzinfo is None:
            first = first.replace(tzinfo=dt.timezone.utc)
        return (dt.datetime.now(dt.timezone.utc) - first).total_seconds() / 86400.0
    except Exception:
        return 0.0


if __name__ == "__main__":
    main()
