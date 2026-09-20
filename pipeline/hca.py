#!/usr/bin/env python3
"""
High Court judgments straight from the Court — hcourt.gov.au.

Why this source and no other: the Court's Terms of Use say its material "may be
used and reproduced for commercial and non-commercial purposes without further
permission", on three conditions — attribution to the High Court of Australia,
no misleading context, and stating that a reproduction is a copy of the version
at the source URL (HANDOFF §4c, verified 10/09/2026). The `source` label this
module hands back carries the attribution and the file URL, and write_llm_file()
records it in the .md. AustLII and Jade forbid this; eCourts sits behind a
CAPTCHA. The Court is the one publisher that has said yes in writing.

What it does, for one medium-neutral citation ("[2026] HCA 29"):

  discover(known_ids)      page 0 of the listing (the newest 100 judgments): every
                           HCA judgment of the last two years whose id is not in
                           known_ids (library + blocklist + queue). Rows only — no
                           judgment page is read here. Since 20/09/2026 the daily bot
                           calls this beside the Jade alerts, which missed Potter
                           [2026] HCA 25 and HCZ [2026] HCA 24 altogether.
  lookup(citation, name)   the Court's judgment listing, newest first, 100 rows a
                           page, until the citation is found (or the listing's own
                           keyword search for the party name) -> the judgment page:
                           name, judgment date, case number, coram, catchwords, the
                           PDF and DOCX links. No file is downloaded. With url= (a
                           judgment page discover() found) the listing walk is skipped.
  fetch_text(meta)         downloads the PDF into a temp folder under a name that
                           carries the case name and citation, and runs it through
                           the SAME gate every other route uses (clean_word via
                           add_text.clean_and_report: citation must be in the text,
                           no citator bleed, no digest). Returns (text, warns, source).

The split matters: the daily bot can read the Court's own catchwords and decide
NOT to spend a download or a model call on an immigration appeal before a byte of
the judgment is fetched.

Politeness: one request at a time, a pause between them, a User-Agent that names
the project. robots.txt (stock Drupal) disallows none of these paths. Stdlib +
BeautifulSoup, which the pipeline already depends on.
"""
import datetime as dt
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from bs4 import BeautifulSoup

BASE = "https://www.hcourt.gov.au"
LISTING = BASE + "/cases-and-judgments/judgments/judgments-1998-current"
COURTS = {"HCA", "HCASJ"}          # the Court publishes both series on the same listing
# Plain and honest. NOT the crawler convention of a "+https://…" contact URL: the
# Court's edge (Akamai) drops any User-Agent that carries a URL at the connection
# (verified 20/09/2026: curl, Python-urllib and this string all get 200; the same
# string with "+https://…" appended gets a reset before any HTTP reply).
USER_AGENT = "case-law-review/1.0 (personal study archive)"
PAUSE_SECONDS = 1.5
MAX_PAGES = 4                     # 4 x 100 = the newest ~400 judgments, about three years
PER_PAGE = 100                    # the listing's own items_per_page options: 12 / 25 / 50 / 100
CITATION_RE = re.compile(r"\[(\d{4})\]\s*(HCASJ|HCA)\s*(\d+)")

_last_request = 0.0


def norm_citation(s):
    """'[2026]  HCA 29' / 'Citation: [2026] HCA 29' -> '[2026] HCA 29' (or None)."""
    m = CITATION_RE.search(s or "")
    return f"[{m.group(1)}] {m.group(2)} {int(m.group(3))}" if m else None


def _get(url, timeout=30, tries=3):
    """One polite GET (bytes). Retries on a transient failure, pauses between calls."""
    global _last_request
    last = None
    for attempt in range(tries):
        wait = PAUSE_SECONDS - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after {tries} tries: {last}")


def _text(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el is not None else ""


def _after_label(s, label):
    """'Judgment date 12 August 2026' -> '12 August 2026'; 'Date: 09 Sep 2026' -> '09 Sep 2026'."""
    s = s.strip()
    if s.lower().startswith(label.lower()):
        s = s[len(label):]
    return s.strip(" : ")


def dmy(s):
    """'12 August 2026' or '09 Sep 2026' -> '12/08/2026'; '' when it cannot be read."""
    s = (s or "").strip()
    for fmt in ("%d %B %Y", "%d %b %Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return ""


# ---------------------------------------------------------------------------
# The listing: div.views-row > a.views-row-item-judgement[href] with the fields
# field--title / field--citation / field--legacy-before / field--hca-date-issued /
# field--hca-matter-number (verified 19/09/2026)
# ---------------------------------------------------------------------------
def parse_listing(html):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("a.views-row-item-judgement[href]"):
        cite = norm_citation(_text(a.select_one(".field--citation")))
        if not cite:
            continue
        out.append({
            "citation": cite,
            "name": _text(a.select_one(".field--title")),
            "url": urllib.parse.urljoin(BASE, a["href"]),
            "date": dmy(_after_label(_text(a.select_one(".field--hca-date-issued")), "Date")),
            "coram": _after_label(_text(a.select_one(".field--legacy-before")), "Before"),
            "caseNumber": _after_label(_text(a.select_one(".field--hca-matter-number")), "Case Number"),
        })
    return out


def listing_url(page=0, keywords=""):
    q = {"items_per_page": PER_PAGE}
    if page:
        q["page"] = page
    if keywords:
        q["keywords"] = keywords
    return LISTING + "?" + urllib.parse.urlencode(q)


# ---------------------------------------------------------------------------
# The judgment page: h1.page-title, span.citation, the field--name-field-hca-*
# blocks, and the files under /sites/default/files/ (verified 19/09/2026)
# ---------------------------------------------------------------------------
def parse_judgment_page(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    files = {}
    for a in soup.select('a[href*="/sites/default/files/"]'):
        href = urllib.parse.urljoin(BASE, a["href"])
        low = href.lower()
        if low.endswith(".pdf") and "pdf" not in files:
            files["pdf"] = href
        elif low.endswith(".docx") and "docx" not in files:
            files["docx"] = href
    catch = soup.select_one(".field--name-field-hca-catchwords")
    catchwords = _after_label(_text(catch), "Catchwords") if catch else ""
    justices = soup.select_one(".field--name-field-hca-justices")
    return {
        "name": _text(soup.select_one("h1.page-title") or soup.select_one("h1")),
        "citation": norm_citation(_text(soup.select_one("span.citation"))),
        "decided": dmy(_after_label(_text(soup.select_one(".field--name-field-hca-date-issued")), "Judgment date")),
        "caseNumber": _after_label(_text(soup.select_one(".field--name-field-hca-matter-number")), "Case number"),
        "coram": _after_label(_text(justices), "Before") if justices else "",
        "catchwords": catchwords,
        "pdf": files.get("pdf", ""),
        "docx": files.get("docx", ""),
        "url": page_url,
    }


def _first_party(name):
    """A word the listing's keyword search can find the case by: the first party's
    surname — 'Dale Haines by his litigation guardian …' -> 'Haines', 'EGH19 v Minister
    …' -> 'EGH19'. 'The King v Ko' -> '' (nothing distinctive enough to search on)."""
    stop = {"the", "king", "queen", "state", "western", "australia", "minister", "for", "and",
            "his", "her", "its", "commonwealth", "attorney", "general", "director", "public",
            "prosecutions", "commissioner", "police", "anor", "ors", "no"}
    parts = re.split(r"\s+v\.?\s+", name or "", maxsplit=1, flags=re.I)
    for p in parts:
        p = re.split(r"\s+by\s+(?:his|her|their|its)\s+", p, maxsplit=1, flags=re.I)[0]   # drop a guardian clause
        p = re.sub(r"\[.*?\]|\(.*?\)", " ", p)
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9'’-]{2,}", p) if w.lower() not in stop]
        if words:
            return words[-1]
    return ""


def _parts(cite):
    y, series, n = CITATION_RE.search(cite).groups()
    return int(y), series, int(n)


def case_id(cite):
    """'[2026] HCA 25' -> 'hca-2026-25' — the pipeline's own id shape."""
    y, series, n = _parts(cite)
    return f"{series.lower()}-{y}-{n}"


def discover(known_ids, get=None, min_year=None, pages=1, series="HCA"):
    """Rows from the newest listing page(s) for judgments the library does not have:
    [{citation, name, url, date, coram, caseNumber}], newest first. Only the HCA
    series (HCASJ single-justice matters stay with the alerts, which carry the
    vexatious-leave rule) and only years >= min_year (default: last year). One
    request per page; nothing else is fetched."""
    get = get or _get
    if min_year is None:
        min_year = dt.date.today().year - 1
    known = set(known_ids or ())
    out = []
    for page in range(pages):
        rows = parse_listing(get(listing_url(page)).decode("utf-8", "replace"))
        if not rows:
            break
        for r in rows:
            y, s, n = _parts(r["citation"])
            if s != series or y < min_year:
                continue
            cid = case_id(r["citation"])
            if cid in known:
                continue
            known.add(cid)
            out.append(dict(r))
    return out


def _past(rows, cite):
    """The listing is newest-first, so once a page shows an OLDER judgment of the
    same series (an earlier year, or the same year with a lower number) the one we
    want would already have gone by: stop walking."""
    y, series, n = _parts(cite)
    for r in rows:
        ry, rs, rn = _parts(r["citation"])
        if ry < y or (ry == y and rs == series and rn < n):
            return True
    return False


def lookup(citation, name="", max_pages=MAX_PAGES, get=None, url=""):
    """The judgment page's metadata for a citation, or None when the Court does not
    list it. Walks the newest pages first, then the listing's keyword search. With
    `url` (the judgment page discover() found) it reads that page directly."""
    get = get or _get
    cite = norm_citation(citation)
    if not cite:
        return None
    if url:
        meta = parse_judgment_page(get(url).decode("utf-8", "replace"), url)
        return meta if meta["citation"] == cite else None     # the page must say what we were told
    entry = None
    for page in range(max_pages):
        rows = parse_listing(get(listing_url(page)).decode("utf-8", "replace"))
        if not rows:
            break
        entry = next((r for r in rows if r["citation"] == cite), None)
        if entry:
            break
        if _past(rows, cite):
            break
    if entry is None and name:
        kw = _first_party(name)
        if kw:
            rows = parse_listing(get(listing_url(0, kw)).decode("utf-8", "replace"))
            entry = next((r for r in rows if r["citation"] == cite), None)
    if entry is None:
        return None
    meta = parse_judgment_page(get(entry["url"]).decode("utf-8", "replace"), entry["url"])
    if meta["citation"] != cite:                      # the page must say what the listing said
        return None
    meta["name"] = meta["name"] or entry["name"]
    meta["decided"] = meta["decided"] or entry["date"]
    meta["coram"] = meta["coram"] or entry["coram"]
    meta["caseNumber"] = meta["caseNumber"] or entry["caseNumber"]
    return meta


def source_label(meta):
    """The attribution the licence asks for, in the shape the .md records."""
    return f"High Court of Australia (copy of the version at {meta.get('pdf') or meta.get('url')})"


def fetch_text(meta, get=None, clean_and_report=None):
    """Download the PDF and run it through the shared gate. Returns (text, warns,
    source). Raises ValueError when the gate refuses (wrong file, citator bleed)."""
    get = get or _get
    if not meta.get("pdf"):
        raise ValueError(f"{meta.get('citation')}: the Court's page has no PDF link")
    if clean_and_report is None:
        import add_text as A                          # sibling module (pipeline/ on sys.path)
        clean_and_report = A.clean_and_report
    safe = re.sub(r"[^A-Za-z0-9 ,'()\[\]-]+", " ", meta.get("name") or "judgment").strip()[:80]
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / f"{safe} - {meta['citation']}.pdf"
        path.write_bytes(get(meta["pdf"]))
        text, warns = clean_and_report(str(path), meta["citation"])
    return text, warns, source_label(meta)


if __name__ == "__main__":                            # python pipeline/hca.py "[2026] HCA 29"
    import json
    import sys
    cite = sys.argv[1] if len(sys.argv) > 1 else "[2026] HCA 29"
    m = lookup(cite, sys.argv[2] if len(sys.argv) > 2 else "")
    print(json.dumps(m, indent=1, ensure_ascii=False))
    if m and "--text" in sys.argv:
        text, warns, source = fetch_text(m)
        print(f"{len(text):,} chars | warns: {warns} | {source}")
        print(text[:1200])
