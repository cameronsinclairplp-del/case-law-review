#!/usr/bin/env python3
"""
Clean an AustLII-format judgment (the markdown you get copying a WA judgment from
AustLII) into a tidy, readable article for the in-app reader:

  * strips markdown links, bold/italic, blockquotes, and the representation TABLES
  * drops the front-matter junk (Category, Representation, Case(s) referred to,
    Table of Contents, duplicate lower-court metadata, file numbers)
  * restructures into:  Header (court / coram / dates)  ->  CATCHWORDS  ->
    LEGISLATION CITED  ->  RESULT  ->  REASONS FOR JUDGMENT (the judges' reasons)

Pipe it into attach_text.py to wire a WA judgment into a curated case:

  python pipeline/clean_austlii.py --file gibson_raw.md \
    | python pipeline/attach_text.py --id wasca-2017-141 \
        --source "https://www.austlii.edu.au/.../WASCA/2017/141.html"

It is heuristic (judgment formats vary); always eyeball the output. Never invents
text - it only removes/relabels. Plain judgment text (e.g. High Court corpus text)
needs no cleaning and should not be run through this.
"""
import argparse
import re
import sys

# [label](url) -> label. Label may carry one level of nested brackets (a citation
# like "[2006] WASCA 31"); url may contain spaces + one level of parens (LawCite).
_URL = r"\((?:https?://|www\.)[^()]*(?:\([^()]*\)[^()]*)*\)"
LINK = re.compile(r"\[((?:[^\[\]]|\[[^\]]*\])*)\]" + _URL)
JUDGE_LINE = r"[A-Z][A-Za-z'’.\- ]*\s(?:P|JA|J|CJ|AJA)"


def strip_md(s):
    s = re.sub(r"\\(.)", r"\1", s)                  # unescape \] \[ \  etc.
    for _ in range(4):                              # [label](url) -> label
        s2 = LINK.sub(r"\1", s)
        if s2 == s:
            break
        s = s2
    s = re.sub(_URL, "", s)                          # leftover bare URL parentheticals
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"\*([^*\n]+)\*", r"\1", s)
    s = s.replace("*", "")
    s = re.sub(r"(?m)^(\s*>)+\s?", "", s)            # blockquote markers (> and > >)
    s = re.sub(r"(?m)^\s*\|.*$", "", s)             # markdown table rows
    s = re.sub(r"(?m)^\s*-{2,}\s*$", "", s)          # dash-only separator lines
    s = re.sub(r"\s*-{2,}\s*", " — ", s)             # inline runs of dashes -> em dash
    s = re.sub(r"(?m)^\s*Last Updated:.*$", "", s)
    return s


def titlecase_judges(s):
    def fix(tok):
        if re.match(r"^(P|JA|J|CJ|AJA|JJ|JJA)$", tok):
            return tok
        return tok[:1] + tok[1:].lower() if tok.isupper() else tok
    return " ".join(fix(t) for t in s.split())


def clean_austlii(raw):
    s = strip_md(raw)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()

    # body starts after the Table of Contents (front/body divider), at the first
    # numbered paragraph - backed up to a judge heading if one immediately precedes.
    # Paragraph numbers may be "1." (e.g. Stefanski) or "1 " (e.g. Gibson, no dot).
    toc = re.search(r"(?im)^\s*Table of Contents\s*$", s)
    search_from = toc.end() if toc else 0
    m = re.search(r"(?m)^\s*\d+(?:\.|\s)\s*\S", s[search_from:])
    if m:
        body_pos = search_from + m.start()
        jm = list(re.finditer(rf"(?m)^{JUDGE_LINE}\s*:?\s*$", s[:body_pos]))
        if jm and (body_pos - jm[-1].end()) < 120:
            body_pos = jm[-1].start()
    else:
        body_pos = len(s)
    front, body = s[:body_pos], s[body_pos:]

    def field(label):
        mm = re.search(rf"(?im)^{label}\s*:\s*(.+)$", front)
        return mm.group(1).strip() if mm else ""

    court = field("TITLE OF COURT")
    heard = field("HEARD")
    delivered = field("DELIVERED")

    coram = []
    for i, ln in enumerate(front.split("\n")):
        mm = re.match(r"(?i)^CORAM\s*:\s*(.+)$", ln.strip())
        if mm:
            coram.append(mm.group(1).strip())
            for ln2 in front.split("\n")[i + 1:]:
                t = ln2.strip()
                if not t:
                    continue
                if re.match(rf"^{JUDGE_LINE}$", t) and len(t) < 40:
                    coram.append(t)
                else:
                    break
            break
    coram_str = ", ".join(titlecase_judges(j) for j in coram)

    def grab(name):
        mm = re.search(rf"(?im)^{name}\s*:?\s*$", front) or re.search(rf"(?im)^{name}\s*:\s*\S", front)
        if not mm:
            return ""
        rest = re.sub(rf"(?i)^{name}\s*:?\s*", "", front[mm.start():], count=1)
        stop = re.search(r"(?im)^(Catchwords|Legislation|Result|Category|Representation|"
                         r"Counsel|Solicitors|Case\(s\)|TABLE OF CONTENTS)\b", rest)
        return rest[:stop.start()].strip() if stop else rest.strip()

    catch = grab("Catchwords")
    if not catch:
        for ln in front.split("\n"):
            t = ln.strip()
            if t.count(" - ") >= 3 and len(t) > 80:
                catch = t
                break
    legis = grab("Legislation")
    result = grab("Result")

    body = re.sub(rf"(?m)^({JUDGE_LINE})\s*:\s*$", r"\1", body).strip()

    def date_nice(d):
        return d.title() if d.isupper() else d

    parts = []
    if court:
        parts.append(court)
    if coram_str:
        parts.append("Coram: " + coram_str)
    dl = [x for x in [("Heard " + date_nice(heard)) if heard else "",
                      ("Delivered " + date_nice(delivered)) if delivered else ""] if x]
    if dl:
        parts.append("  ·  ".join(dl))
    parts.append("")
    if catch:
        parts += ["CATCHWORDS", "", catch.replace(" - ", " — "), ""]
    if legis:
        parts += ["LEGISLATION CITED", "", legis, ""]
    if result:
        parts += ["RESULT", "", result, ""]
    parts += ["REASONS FOR JUDGMENT", "", body, ""]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(parts)).strip()


def main():
    ap = argparse.ArgumentParser(description="Clean an AustLII-format judgment into a readable article.")
    ap.add_argument("--file", help="raw AustLII markdown; omit to read stdin")
    args = ap.parse_args()
    raw = open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read()
    sys.stdout.write(clean_austlii(raw) + "\n")


if __name__ == "__main__":
    main()
