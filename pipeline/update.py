#!/usr/bin/env python3
"""
The Case-Law Review — daily update pipeline (runs in GitHub Actions, 3x/day).

Flow:
  1. Read BarNet Jade alert emails from Gmail via IMAP (rolling lookback window).
  2. Parse alerts into candidate cases; filter to scope; dedupe by id.
  3. Merge in any "pending" cases whose judgment wasn't published last time.
  4. Fetch each judgment's full text from the Open Australian Legal Corpus
     (openly licensed; HCA/interstate only - the corpus carries NO WA cases).
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

from bs4 import BeautifulSoup

# Sibling module (pipeline/ is sys.path[0] when run as `python pipeline/update.py`).
from corpus import fetch_judgment_text  # legitimate full-text source (Open Australian Legal Corpus)

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
SUBMISSION_LOOKBACK_DAYS = 35     # emailed judgments re-fetchable past PENDING_MAX_DAYS (no silent loss)
PENDING_MAX_DAYS = 30             # give up on an unresolvable case after this (logged)

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
    # "scope": False -> usable for hand-picked library cases (ingest/attach) but NOT
    # kept by the watchlist filter, so the national Jade alert doesn't flood the digest.
    "NSWSC":  {"juris": "nsw", "name": "Supreme Court of New South Wales", "gated": True, "scope": False},
    "NSWCCA": {"juris": "nsw", "name": "Supreme Court of NSW — Court of Criminal Appeal", "gated": True, "scope": False},
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

# WASC/WASCA come name-only in the Jade alert (no catchwords), so the topic gate
# can't fire on them and civil matters slip through (bank/authority disputes etc.).
# Drop the obvious CIVIL ones - but ONLY when no criminal signal is present, so a
# real criminal case is never dropped on a guess. WA criminal matters always name
# the State / Crown / police / DPP, or use a pseudonym.
WA_NAME_ONLY = {"WASC", "WASCA"}
# WA criminal matters name the State / Crown / police / DPP as a PARTY (positionally),
# or carry a pseudonym/coronial marker. "Western Australia" is required in a party
# position — "...of/for Western Australia" (the Sheriff, a Commission, a Minister)
# is a civil government body, not the prosecuting State, so a bare mention is NOT a
# criminal signal.
CRIMINAL_NAME = re.compile(
    r"\bthe state of western australia\b|\bstate of w\.?a\b|"
    r"(?<!of )(?<!for )\bwestern australia\s+v\b|"
    r"\bv\.?\s+(?:the state of\s+)?western australia\b|"
    r"\bthe (?:queen|king)\b|\bregina\b|\brex\b|\bcrown\b|"
    r"\bR\s+v\b|\bv\.?\s+the (?:queen|king)\b|\bpolice\b|"
    r"\bd\.?p\.?p\b|director of public prosecutions|commissioner of police|"
    r"\bex parte\b|prosecut|inquest|coronial|death of|\(a pseudonym\)", re.I)
CIVIL_NAME = re.compile(
    r"\bpty\.?\s*ltd|\bp/l\b|\bltd\b|\bplc\b|\binc\.?\b|\bllc\b|\blimited\b|\bbank\b|"
    r"westpac|\bnab\b|\banz\b|commonwealth bank|bankwest|insurance|assurance|"
    r"\bnominees\b|holdings|investments|\bauthority\b|\bcouncil\b|\bshire\b|"
    r"\bcity of\b|\btown of\b|body corporate|owners corporation|\bstrata\b|"
    r"liquidat|in liquidation|administrator|receiver|\btrustee\b|executor|"
    r"estate of|in the matter of|probate|superannuation|\bmortgage\b|"
    r"developments|constructions|enterprises|corporation|\bpartners\b|"
    # government / regulatory / disciplinary bodies and incorporated associations
    # (civil unless a criminal party signal above is also present)
    r"\bminister\b|\bsheriff\b|\btribunal\b|commissioner of (?:state revenue|taxation)|"
    r"legal profession|complaints committee|director of housing|"
    r"\bunion\b|\bassociation\b|\bco-?operative\b|\bsociety\b|\bclub\b|\bfund\b", re.I)

SYSTEM_PROMPT = (
    "You are the case-law analyst for a detective in training with WA Police. "
    "From the judgment text provided, "
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
# Trailing hearing-number suffix ("[No 2]" / "(No 3)") is part of the name.
_NO_SUFFIX = r"(?:\s+[\[(]\s*No\.?\s*\d+\s*[\])])?"
# "A v B" party pair anchored to the END of the fragment (the name sits just before
# the citation). clean_case_name tries the whole string and each point just after a
# structural/sentence boundary as a plaintiff start, keeping the RIGHTMOST match —
# so a leading "Court of Appeal." / "New decision:" preamble is stripped while a
# multi-word plaintiff (and an abbreviation like "State of W.A.") survives.
CASE_NAME_RE = re.compile(rf"{_PLAINTIFF}\s+v\.?\s+{_DEFENDANT}{_NO_SUFFIX}\s*$")
_NAME_BOUNDARY_RE = re.compile(r"[|·•—–:]\s*|[.!?]\s+")
# Parallel / report citations a name may carry before its medium-neutral citation,
# e.g. "(2020) 270 CLR 1" or "; [2019] HCA 12" — peeled off before matching
# (tolerating a trailing ; , . left by the wrapper).
_PARALLEL_CITE_RE = re.compile(
    r"\s*[;,]?\s*(?:\(\d{4}\)\s*\d+\s*[A-Z][A-Za-z]{1,8}\s*\d+"
    r"|\[\d{4}\]\s*[A-Z][A-Za-z]{1,7}\s*\d+)\s*[;,.]?\s*$")


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
    s.setdefault("processed", [])
    if not isinstance(s["processed"], list):
        s["processed"] = []
    return s


def save_state(pending, processed=None):
    # Stable key order + only the durable queue/log -> byte-identical when unchanged,
    # so quiet runs produce no commit (no empty-commit churn).
    STATE_PATH.write_text(
        json.dumps({
            "pending": pending,
            "processed": processed or [],
            "note": ("Durable retry queue: cases seen in a Jade alert whose judgment "
                     "wasn't yet published. Retried every run until resolved or aged out "
                     f"(> {PENDING_MAX_DAYS} days). 'processed' = Message-IDs of judgment "
                     "emails already ingested (see fetch_submissions). Written by "
                     "pipeline/update.py."),
        }, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def imap_since_date(days=LOOKBACK_DAYS):
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)


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
# Email-to-ingest — judgments you supply by emailing them to yourself
#
# The WA / Lexis path: WA judgments aren't in any free corpus, and Lexis is only
# reachable from a locked-down police machine. So you email the judgment to your
# own pipeline inbox and the next scheduled run analyses it into the app — no
# special software needed where you read Lexis/eCourts, just the ability to send
# an email. Format:
#     To/From: yourself (the MAIL_USERNAME account)
#     Subject: INGEST [2022] WASCA 5 Stefanski v Western Australia
#     Body:    <the full verbatim judgment text, pasted>
# The text is human-supplied (consistent with the no-fabricate rule — nothing is
# scraped). Each email is ingested once, tracked by Message-ID in state.json.
# ---------------------------------------------------------------------------
INGEST_SUBJECT_RE = re.compile(r"^\s*ingest\b[:\s-]*", re.I)


def submission_text(msg):
    """Verbatim judgment text from a submission email, preferring the plain-text
    part (cleanest for a pasted judgment) and falling back to stripped HTML."""
    plain = html = None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                t = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            except Exception:
                continue
            if ctype == "text/plain" and plain is None:
                plain = t
            elif ctype == "text/html" and html is None:
                html = t
    else:
        try:
            payload = msg.get_payload(decode=True)
            t = payload.decode(msg.get_content_charset() or "utf-8", errors="replace") if payload else ""
        except Exception:
            t = ""
        (html, plain) = (t, None) if msg.get_content_type() == "text/html" else (None, t)

    if plain and len(plain.strip()) >= 200:
        text = plain
    elif html:
        soup = BeautifulSoup(html, "html.parser")
        for x in soup(["script", "style"]):
            x.decompose()
        text = soup.get_text("\n")
    else:
        text = plain or ""
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def fetch_submissions(user, password, since_dt, processed):
    """Return work items for judgments you emailed to yourself (subject starts with
    INGEST and contains a medium-neutral citation; body is the verbatim text). Each
    item carries 'suppliedText' and '_msgid'. Only mail FROM yourself with the marker
    is read; already-ingested Message-IDs are skipped. Best-effort: never raises."""
    since_str = since_dt.strftime("%d-%b-%Y")
    seen = set(processed or [])
    token = os.environ.get("INGEST_TOKEN", "").strip()  # optional shared-secret gate
    out = []
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        M.login(user, password)
    except Exception as e:
        log(f"  submissions: IMAP login failed ({e})")
        return out
    try:
        M.select("INBOX")
        typ, data = M.search(None, f'(SINCE "{since_str}" FROM "{user}" SUBJECT "ingest")')
        if typ != "OK":
            return out
        ids = ((data[0] if data else b"") or b"").split()
        if ids:
            log(f"  submissions: {len(ids)} candidate email(s)")
        for mid in ids:
            try:
                typ, msgdata = M.fetch(mid, "(RFC822)")
                if typ != "OK" or not msgdata:
                    continue
                payload = next(
                    (p[1] for p in msgdata
                     if isinstance(p, tuple) and len(p) >= 2
                     and isinstance(p[1], (bytes, bytearray))),
                    None)
                if payload is None:
                    continue
                msg = email.message_from_bytes(payload)
                msgid = (msg.get("Message-ID") or "").strip()
                if not msgid or msgid in seen:
                    continue
                subject = " ".join((msg.get("Subject") or "").split())
                if not INGEST_SUBJECT_RE.match(subject):   # marker must LEAD the subject
                    continue
                if token and token not in subject:         # optional shared-secret gate
                    log("  submission skipped: INGEST_TOKEN not present in subject")
                    continue
                m = CITATION_RE.search(subject)
                if not m:
                    log(f"  submission skipped (no citation in subject): {subject!r}")
                    continue
                name = INGEST_SUBJECT_RE.sub("", subject)
                if token:
                    name = name.replace(token, "").strip()
                item = _item_from_match(m, name, "", name)
                if item["courtTag"] not in COURTS:
                    log(f"  submission skipped ({item['citation']}): court {item['courtTag']} not in COURTS")
                    continue
                text = submission_text(msg)
                if len(text) < 800:
                    log(f"  submission skipped ({item['citation']}): body too short ({len(text)} chars)")
                    continue
                # The verbatim judgment MUST contain its own medium-neutral citation.
                # Without this, a wrong paste / mistyped subject citation would attach a
                # real citation to the WRONG text (and, via replace-by-id, could clobber a
                # good existing case) - the exact mis-attribution the no-fabricate rule bars.
                cite_pat = re.compile(
                    rf"\[{item['year']}\]\s*{re.escape(item['courtTag'])}\s*{item['num']}(?!\d)", re.I)
                if not cite_pat.search(re.sub(r"\s+", " ", text)):
                    log(f"  submission skipped ({item['citation']}): citation not found in body — wrong paste?")
                    continue
                item["suppliedText"] = text
                item["_msgid"] = msgid
                out.append(item)
                log(f"  submission accepted: {item['id']} ({item['citation']}) — {len(text)} chars")
            except Exception as e:
                log(f"  submission fetch/parse error {mid!r}: {e}")
                continue
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return out


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
        # trust the alert's href only if it's genuinely a jade.io host; otherwise
        # build the canonical Jade summary URL from the citation ourselves (safe).
        "jadeUrl": href if _is_jade_url(href) else jade_summary_url(code, year, num),
        "blurb": blurb or "",
    }


def jade_summary_url(court_tag, year, num):
    return f"https://jade.io/summary/mnc/{year}/{court_tag}/{num}"


def _is_jade_url(url):
    if not url:
        return False
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme in ("http", "https") and (host == "jade.io" or host.endswith(".jade.io"))


def clean_case_name(raw, citation):
    name = re.sub(r"\s+", " ", (raw or "").replace(citation, "")).strip(" .,-—|·•;\t")
    # peel any trailing parallel/report citations (there may be more than one)
    prev = None
    while name and name != prev:
        prev = name
        name = _PARALLEL_CITE_RE.sub("", name).strip(" .,-—|·•;\t")
    # try the whole string and each point just after a structural/sentence boundary
    # as a plaintiff start; keep the rightmost "A v B" match (strips a leading
    # preamble without truncating a long plaintiff or an abbreviation).
    chosen = None
    for s in [0] + [b.end() for b in _NAME_BOUNDARY_RE.finditer(name)]:
        m = CASE_NAME_RE.match(name, s)
        if m:
            chosen = name[s:m.end()]
    if chosen is not None:
        return chosen.strip(" .,-—|·•")
    # no "A v B" pair (e.g. "Re X; Ex parte Y"): take the last structural fragment
    frag = re.split(r"\s*[|·•—–]\s*|(?<=[.!?])\s+", name)[-1].strip()
    return frag[-80:].strip() or "(case name pending)"


# ---------------------------------------------------------------------------
# Scope filter
# ---------------------------------------------------------------------------
def in_scope(item):
    tag = item["courtTag"]
    if tag not in COURTS:
        return False, f"court {tag} not in scope"
    if not COURTS[tag].get("scope", True):
        return False, f"{tag}: library-only court (not in watchlist scope)"
    blob = f"{item.get('caseName','')} {item.get('blurb','')}"
    if DROP_KEYWORDS.search(blob):
        return False, "out-of-scope topic"
    if COURTS[tag]["gated"] and not TOPIC_KEYWORDS.search(blob):
        return False, f"{tag}: no investigation/evidence topic keyword"
    # Name-only WA courts: drop a clear civil matter unless it carries a criminal
    # signal (conservative - ambiguous names are kept, never dropped on a guess).
    if tag in WA_NAME_ONLY and CIVIL_NAME.search(blob) and not CRIMINAL_NAME.search(blob):
        return False, f"{tag}: civil party, no criminal signal"
    return True, "in scope"


def austlii_url(item):
    juris = COURTS[item["courtTag"]]["juris"]
    return (f"https://www.austlii.edu.au/cgi-bin/viewdoc/au/cases/"
            f"{juris}/{item['courtTag']}/{item['year']}/{item['num']}.html")


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
def send_email(user, password, new_cases, stats=None):
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
    if stats:
        lines += ["", _health_text(stats)]
    html = (f"<div style='font-family:Inter,Arial,sans-serif;color:#1A1813'>"
            f"<p>{n} new {'case' if n == 1 else 'cases'} added to "
            f"<strong>The Case-Law Review</strong>:</p>"
            f"<ul style='list-style:none;padding-left:0'>{''.join(html_items)}</ul>"
            f"<p style='color:#7C7563;font-size:13px'>Full analysis + downloads in the app: "
            f"<a href='{esc(APP_BASE)}/'>{esc(APP_BASE)}/</a></p>"
            f"{_health_html(stats) if stats else ''}</div>")

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


def send_watchlist_email(user, password, items, stats=None):
    n = len(items)
    today = dt.datetime.now(dt.timezone.utc).strftime("%d/%m/%Y")
    subject = f"WA Case-Law Review — {n} new decision{'s' if n != 1 else ''} to read ({today})"
    intro = ("New in-scope decisions from your Jade alerts that the free corpus "
             "doesn't carry (all WA cases, plus the odd High Court gap). Full text and "
             "analysis are pending — these are the ones to be across; tap a link to read, "
             "or forward one to ingest it into the app:")
    lines = [intro, ""]
    html_items = []
    for it in items:
        au = austlii_url(it)
        jd = it.get("jadeUrl") or jade_summary_url(it["courtTag"], it["year"], it["num"])
        blurb = re.sub(r"\s+", " ", it.get("blurb", "")).strip()[:240]
        lines += [f"• {it['caseName']} {it['citation']} — {it['courtTag']}"]
        if blurb:
            lines.append(f"  {blurb}")
        lines += [f"  AustLII: {au}", f"  Jade: {jd}", ""]
        html_items.append(
            f"<li style='margin-bottom:14px'><strong>{esc(it['caseName'])} {esc(it['citation'])}</strong> "
            f"— {esc(it['courtTag'])}<br>"
            + (f"<span style='color:#46423A'>{esc(blurb)}</span><br>" if blurb else "")
            + f"<a href='{esc(au)}'>AustLII</a> · <a href='{esc(jd)}'>Jade</a></li>")
    lines += ["", "(The app library is unchanged until full analysis is available.)"]
    if stats:
        lines += ["", _health_text(stats)]
    html = (f"<div style='font-family:Inter,Arial,sans-serif;color:#1A1813'>"
            f"<p>{esc(intro)}</p><ul style='list-style:none;padding-left:0'>{''.join(html_items)}</ul>"
            f"<p style='color:#7C7563;font-size:13px'>The app library is unchanged until full "
            f"analysis is available: <a href='{esc(APP_BASE)}/'>{esc(APP_BASE)}/</a></p>"
            f"{_health_html(stats) if stats else ''}</div>")
    em = EmailMessage()
    em["Subject"] = subject
    em["From"] = f"Case-Law Review <{user}>"
    em["To"] = user
    em.set_content("\n".join(lines))
    em.add_alternative(html, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, password)
        s.send_message(em)
    log(f"watchlist email sent: {subject}")


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# Health signal — a one-line run summary on every email, plus a notice on an
# otherwise-silent run that hit errors or abandoned a case (so a quietly
# degrading pipeline can't look identical to a genuinely quiet day).
# ---------------------------------------------------------------------------
def _health_text(stats):
    return ("— run health: "
            f"{stats['alerts']} alert(s) · {stats['inScope']} in scope · "
            f"{stats['analysed']} analysed · {stats['watchlist']} to watchlist · "
            f"{stats['pending']} pending · {stats['errors']} error(s) · "
            f"{stats['gaveUp']} given up.")


def _health_html(stats):
    return ("<p style='color:#9a9488;font-size:12px;border-top:1px solid #e8e3d6;"
            "padding-top:8px;margin-top:16px'>Pipeline health — "
            f"{stats['alerts']} alert(s) · {stats['inScope']} in scope · "
            f"{stats['analysed']} analysed · {stats['watchlist']} to watchlist · "
            f"{stats['pending']} pending · <strong>{stats['errors']} error(s)</strong> · "
            f"{stats['gaveUp']} given up.</p>")


def send_health_email(user, password, stats, errors, gave_up):
    today = dt.datetime.now(dt.timezone.utc).strftime("%d/%m/%Y")
    subject = f"WA Case-Law Review — pipeline notice ({today})"
    lines = ["The pipeline ran but added nothing and sent no digest — worth a look:", ""]
    html_parts = ["<p>The pipeline ran but added nothing and sent no digest — worth a look:</p>"]
    if errors:
        lines.append(f"{len(errors)} processing error(s) this run:")
        html_parts.append(f"<p><strong>{len(errors)} processing error(s):</strong></p><ul>")
        for e in errors[:10]:
            lines.append(f"  • {e['id']} ({e['citation']}): {e['err']}")
            html_parts.append(f"<li>{esc(e['id'])} ({esc(e['citation'])}): {esc(e['err'])}</li>")
        lines.append("")
        html_parts.append("</ul>")
    if gave_up:
        lines.append(f"{len(gave_up)} case(s) given up after {PENDING_MAX_DAYS} days unresolved:")
        html_parts.append(f"<p><strong>{len(gave_up)} given up after {PENDING_MAX_DAYS} days:</strong></p><ul>")
        for g in gave_up[:10]:
            lines.append(f"  • {g.get('caseName','')} {g['citation']} ({g['courtTag']})")
            html_parts.append(f"<li>{esc(g.get('caseName',''))} {esc(g['citation'])} ({esc(g['courtTag'])})</li>")
        lines.append("")
        html_parts.append("</ul>")
    lines += ["", _health_text(stats)]
    html = ("<div style='font-family:Inter,Arial,sans-serif;color:#1A1813'>"
            + "".join(html_parts) + _health_html(stats) + "</div>")
    em = EmailMessage()
    em["Subject"] = subject
    em["From"] = f"Case-Law Review <{user}>"
    em["To"] = user
    em.set_content("\n".join(lines))
    em.add_alternative(html, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, password)
        s.send_message(em)
    log(f"health email sent: {subject}")


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
    processed = state.get("processed", [])
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
        it["notified"] = prev.get("notified", False)   # carry forward so we email each case ONCE
        work[it["id"]] = it
    for p in work.values():
        p.setdefault("firstSeen", now_iso())
    log(f"work items (new alerts + pending): {len(work)} (kept={len(kept)}, pending_in={len(pending)})")

    # Human-supplied judgments emailed in (the WA / Lexis path): they carry their own
    # verbatim text, bypass the corpus and the scope gate (you chose them), and win
    # over any alert/pending item of the same id. Non-fatal if the fetch fails.
    try:
        submissions = fetch_submissions(user, password, imap_since_date(SUBMISSION_LOOKBACK_DAYS), processed)
    except Exception as e:
        submissions = []
        log(f"submission fetch failed (non-fatal): {e}")
    for it in submissions:
        it.setdefault("firstSeen", now_iso())
        it["notified"] = True            # being analysed now, not watchlisted
        work[it["id"]] = it
    if submissions:
        log(f"email submissions accepted: {len(submissions)}")

    client = get_client() if work else None
    new_cases, unresolved, gave_up, errors, processed_now = [], [], [], [], []
    for it in work.values():
        try:
            # Full text comes ONLY from the openly-licensed Open Australian Legal
            # Corpus. AustLII/JADE scraping was removed - their terms forbid it
            # (AustLII's policy bars scraping AND AI/LLM use). The corpus carries
            # HCA/interstate judgments but NO WA cases, so WASC/WASCA fall through
            # to the watchlist + the human-in-the-loop ingest path (pipeline/ingest.py).
            # email-submitted judgments carry their own verbatim text; everything
            # else resolves from the corpus (HCA/interstate; WA always returns None).
            text = it.get("suppliedText") or fetch_judgment_text(it["citation"])
            if not text:
                age = _age_days(it.get("firstSeen"))
                if age > PENDING_MAX_DAYS:
                    gave_up.append(it)
                    log(f"  GAVE UP {it['id']} ({it['citation']}): unresolved after {age:.0f} days")
                else:
                    unresolved.append(it)
                    log(f"  pending {it['id']} ({it['citation']}): full text not retrievable yet ({age:.0f}d)")
                continue
            truncated = len(text) > MAX_JUDGMENT_CHARS
            if truncated:
                text = text[:MAX_JUDGMENT_CHARS]
            log(f"  analysing {it['id']} ({it['citation']}) — {len(text)} chars")
            analysis = analyse(client, it, text, truncated)
            case = build_case(it, analysis)
            write_llm_file(case, text, analysis)
            new_cases.append(case)
            if it.get("_msgid"):
                processed_now.append(it["_msgid"])
            log(f"  built {it['id']} [{case['relevance']}] {case['caseName']}")
        except Exception as e:
            errors.append({"id": it["id"], "citation": it["citation"], "err": str(e)[:200]})
            if it.get("_msgid"):
                # email submissions self-heal via re-fetch (long window) — don't queue a
                # textless pending copy that could later be wrongly "given up".
                log(f"  ERROR on {it['id']} ({it['citation']}): {e} — will retry from email")
            else:
                unresolved.append(it)   # transient error -> retry next run
                log(f"  ERROR on {it['id']} ({it['citation']}): {e} — kept pending")
            continue

    # Watchlist: surface newly-detected in-scope cases we couldn't full-text yet,
    # exactly once each (the "notified" flag prevents re-emailing on every run).
    to_notify = [w for w in unresolved if not w.get("notified")]

    if new_cases:
        # replace-by-id (an email submission can re-supply a case already in the
        # library), otherwise add — then sort newest-first. Guarantees no dup ids.
        by_id = {c["id"]: c for c in existing}
        for c in new_cases:
            by_id[c["id"]] = c
        merged = sorted(by_id.values(), key=lambda c: str(c.get("date", "")), reverse=True)
        save_cases(merged)
        log(f"cases.json: {len(existing)} -> {len(merged)}")

    for w in to_notify:
        w["notified"] = True
    processed = (processed + processed_now)[-300:]   # bound growth; IMAP lookback is short
    save_state([_pending_record(w) for w in unresolved], processed)

    label = (f"Pipeline: add {len(new_cases)} case(s)" if new_cases
             else "Pipeline: update watchlist/queue")
    pushed = commit_and_push(label)

    stats = {
        "alerts": len(bodies), "candidates": len(candidates), "inScope": len(kept),
        "analysed": len(new_cases), "watchlist": len(to_notify),
        "pending": len(unresolved), "errors": len(errors), "gaveUp": len(gave_up),
    }

    if new_cases:
        send_email(user, password, new_cases, stats)
    if to_notify:
        send_watchlist_email(user, password, to_notify, stats)
    if not new_cases and not to_notify:
        if errors or gave_up:
            try:
                send_health_email(user, password, stats, errors, gave_up)
                log("health notice sent (errors / gave-up on an otherwise quiet run)")
            except Exception as e:
                log(f"health email failed (non-fatal): {e}")
        else:
            log("nothing new — no email (no spam on quiet days)")

    log(f"RUN SUMMARY: alerts={len(bodies)} candidates={len(candidates)} new={len(new_cases)} "
        f"notified={len(to_notify)} pending={len(unresolved)} gave_up={len(gave_up)} "
        f"ingested={len(processed_now)} errors={len(errors)} pushed={pushed}")


def _pending_record(it):
    return {
        "id": it["id"], "citation": it["citation"], "courtTag": it["courtTag"],
        "year": it["year"], "num": it["num"], "caseName": it.get("caseName", ""),
        "jadeUrl": it.get("jadeUrl", ""), "blurb": it.get("blurb", ""),
        "firstSeen": it.get("firstSeen", now_iso()),
        "notified": bool(it.get("notified", False)),
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
