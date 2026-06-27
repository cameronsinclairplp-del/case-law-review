#!/usr/bin/env python3
"""
Legitimate full-text source for the pipeline: the Open Australian Legal Corpus
(isaacus, on Hugging Face) - 229k+ Australian legal documents, openly licensed
for reuse INCLUDING AI/LLM use, sourced from the courts' own free sites
(High Court eResources, etc.).

This REPLACES scraping AustLII / JADE, which those sites' terms forbid (AustLII's
usage policy explicitly bars scraping and AI/LLM use of its material).

COVERAGE (verified 27/06/2026):
  * High Court + interstate (NSW/QLD/etc.) decisions: present, full verbatim text.
  * Western Australian decisions: NONE. WASC/WASCA always return None here and
    fall through to the pipeline's watchlist + human-in-the-loop route (ingest.py).
  * A few High Court gaps exist (e.g. [2016] HCA 35) - reported as a miss, never faked.

Stdlib only - no API key, no extra dependency in the Action.
"""
import json
import re
import time
import urllib.parse
import urllib.request

ENDPOINT = "https://datasets-server.huggingface.co/filter"
DATASET = "isaacus/open-australian-legal-corpus"


def neutral_citation(citation):
    """'Edwards v The Queen [1993] HCA 63' or '[1993] HCA 63; (1993) 178 CLR 193'
    -> '[1993] HCA 63'. Returns None if there is no medium-neutral citation."""
    m = re.search(r"\[(\d{4})\]\s+([A-Z]+)\s+(\d+)", citation or "")
    return f"[{m.group(1)}] {m.group(2)} {m.group(3)}" if m else None


def _cite_token_match(pat, citation):
    """True only if the neutral citation `pat` (e.g. '[1993] HCA 63') appears in
    `citation` as a COMPLETE token - never as a prefix of a longer-numbered sibling.

    The ILIKE query is a substring match, so '%[1993] HCA 6%' also returns
    '[1993] HCA 63/64/65...'. Without this strict re-check the caller would accept
    the WRONG judgment's text and silently attach it to the right citation - a
    fabricated briefing that passes every other guard. The trailing (?!\\d) rejects
    a longer number; the literal 'COURT ' before the digits anchors the start."""
    if not citation:
        return False
    norm = re.sub(r"\s+", " ", citation).strip()
    return re.search(re.escape(pat) + r"(?!\d)", norm, re.IGNORECASE) is not None


def _is_wa(pat):
    """The corpus carries ZERO Western Australian decisions, so a WA court code
    (WASC/WASCA/WADC/...) skips the slow HF round-trip and returns a miss instantly.
    Every Australian WA court code starts with 'WA'; no non-WA code does."""
    parts = (pat or "").split()
    return len(parts) >= 2 and parts[1].upper().startswith("WA")


def fetch_judgment_text(citation, tries=4, timeout=30, min_chars=800):
    """Return verbatim judgment text from the corpus, or None.

    None means either (a) genuinely not in the corpus (e.g. every WA case, some
    HCA gaps) or (b) the endpoint was flaky after retries. Either way the caller
    treats it as 'not retrievable yet' and the case stays on the watchlist/queue
    - never fabricated.
    """
    pat = neutral_citation(citation)
    if not pat or _is_wa(pat):
        return None
    qs = urllib.parse.urlencode({
        "dataset": DATASET, "config": "corpus", "split": "corpus",
        # length 20: the substring ILIKE can return many same-prefix siblings;
        # fetch enough that the exact-token row (verified below) is in range.
        "where": f"\"citation\" ILIKE '%{pat}%'", "offset": 0, "length": 20,
    })
    url = f"{ENDPOINT}?{qs}"
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                d = json.load(r)
            if "rows" in d:  # endpoint answered cleanly
                for row in (x["row"] for x in d["rows"]):
                    if _cite_token_match(pat, row.get("citation")):
                        text = row.get("text") or ""
                        return text if len(text) >= min_chars else None
                return None  # answered, genuinely absent
        except Exception:
            pass
        time.sleep(2 * (i + 1))  # back off; the HF index is intermittently slow
    return None


def fetch_judgment_meta(citation, tries=4, timeout=30):
    """Like fetch_judgment_text but returns the whole corpus row (text + source url +
    corpus citation), or None. Used by backfill.py for provenance."""
    pat = neutral_citation(citation)
    if not pat or _is_wa(pat):
        return None
    qs = urllib.parse.urlencode({
        "dataset": DATASET, "config": "corpus", "split": "corpus",
        # length 20: the substring ILIKE can return many same-prefix siblings;
        # fetch enough that the exact-token row (verified below) is in range.
        "where": f"\"citation\" ILIKE '%{pat}%'", "offset": 0, "length": 20,
    })
    url = f"{ENDPOINT}?{qs}"
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                d = json.load(r)
            if "rows" in d:
                for row in (x["row"] for x in d["rows"]):
                    if _cite_token_match(pat, row.get("citation")):
                        return row
                return None
        except Exception:
            pass
        time.sleep(2 * (i + 1))
    return None


if __name__ == "__main__":  # quick manual check: python pipeline/corpus.py "[1993] HCA 63"
    import sys
    cit = sys.argv[1] if len(sys.argv) > 1 else "[1993] HCA 63"
    t = fetch_judgment_text(cit)
    print(f"{cit!r} -> {'None' if t is None else f'{len(t):,} chars'}")
