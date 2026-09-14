# The Case-Law Review

A personal, ever-growing web archive of WA-relevant **criminal case law**, written for a
WA Police detective (detective training).

Each entry is a judgment plus plain-English analysis written for a detective: what happened,
what the court held, what it means for casework, and an **ACTION vs AWARENESS** verdict.
New cases are appended **daily** by an automated pipeline — see [The data contract](#the-data-contract).

**Live site:** `https://cameronsinclairplp-del.github.io/case-law-review/`

---

## How it works

A static site. No backend, no build step, no login. The browser fetches
[`data/cases.json`](data/cases.json) and renders it.

```
case-law-review/
├── index.html              # shell: top bar, #app root, footer
├── data/
│   └── cases.json          # THE DATA — an array, newest-first. The pipeline edits ONLY this.
├── assets/
│   ├── css/
│   │   ├── fonts.css       # self-hosted @font-face (Fraunces + Inter)
│   │   └── styles.css      # the design system
│   ├── js/
│   │   └── app.js          # routing, rendering, filtering, sanitising
│   ├── fonts/              # self-hosted woff2 (no font CDN)
│   └── favicon.svg
├── .nojekyll               # serve files verbatim on GitHub Pages
└── README.md
```

### Data / view separation (the whole point)
The app renders whatever is in `cases.json`. **The daily pipeline must only ever edit
`data/cases.json`** — never the HTML, CSS, or JS. A data update can't break the app.

### Routing
Hash routing so deep links work on GitHub Pages:
- `#/` — the master list (filters are encoded in the hash, e.g. `#/?court=HCA&rel=ACTION&year=2026`)
- `#/case/:id` — the detail view for one case

### Resilience built in
- **Newest-first**, always re-sorted on load; never relies on file order.
- **Scales** to thousands of entries (rows render in batches as you scroll).
- **Safe rich text**: `whatHappened` / `whatHeld` / `whatItMeans` / `verdict` may contain inline
  `<b> <strong> <i> <em> <br>` — everything else (scripts, attributes, other tags) is stripped.
- **Safe links**: only `http(s)` and relative paths are accepted as link targets.
- Missing optional fields degrade gracefully (a field simply doesn't render).

---

## The data contract

`data/cases.json` is a JSON **array**, newest-first. The pipeline prepends one object per kept case.
Each object:

```json
{
  "id": "hca-2026-19",
  "date": "2026-06-17",
  "court": "High Court of Australia",
  "courtTag": "HCA",
  "caseName": "Cullen v The State of New South Wales",
  "citation": "[2026] HCA 19",
  "decided": "17/06/2026",
  "appealFrom": "NSW Court of Appeal",
  "outcome": "Appeal dismissed — State not liable",
  "weight": "Persuasive principle (HCA); statute is NSW",
  "tags": ["duty of care", "crowd control", "arrest", "use of force", "civil liability"],
  "relevance": "AWARENESS",
  "oneLine": "One-sentence summary shown on the list and detail header.",
  "whatHappened": "…(may contain <b>/<i>)…",
  "whatHeld": "…",
  "whatItMeans": "…",
  "verdict": "…",
  "austliiUrl": "https://www.austlii.edu.au/...",
  "jadeUrl": "",
  "sourceUrl": "",
  "sourceLabel": "",
  "files": { "judgment": "data/files/<id>/judgment.pdf", "llm": "data/files/<id>/<id>.md" }
}
```

| field | required | notes |
|---|---|---|
| `id` | yes | stable, unique; used in the URL (`#/case/:id`). If missing/duplicate, a fallback is generated, but the deep link won't be stable — always set it. |
| `date` | yes | ISO `YYYY-MM-DD`; drives sort order and the list date. |
| `courtTag` | yes | short tag (e.g. `HCA`, `WASCA`); becomes a court filter. |
| `caseName`, `citation`, `court` | yes | plain text. House style: UPPERCASE surnames. |
| `decided` | — | display date, house style `DD/MM/YYYY`. Falls back to `date`. |
| `relevance` | yes | `ACTION` or `AWARENESS` (case-insensitive). |
| `tags` | — | array of strings. |
| `oneLine` | yes | plain text, one sentence. |
| `whatHappened`, `whatHeld`, `whatItMeans`, `verdict` | — | plain text or inline `<b>/<i>` only. Italicise legislation. |
| `austliiUrl`, `jadeUrl` | — | full `http(s)` URLs. Empty string = hidden. |
| `sourceUrl`, `sourceLabel` | — | fallback source link when there's no AustLII/JADE page (e.g. an obscure/old report). `sourceLabel` is the button text (defaults to "Source"). |
| `files.judgment` | — | repo-relative path to the judgment PDF (`data/files/<id>/judgment.pdf`). Renders a **Download judgment (PDF)** button. Empty/absent = no button. |
| `files.llm` | — | repo-relative path to the full-text LLM-retrieval markdown (`data/files/<id>/<id>.md`). Renders a **Download LLM file (.md)** button. Empty/absent = no button. |

Download buttons use the HTML `download` attribute (save, not navigate) and only work for same-origin repo paths. The pipeline commits these files under `data/files/<id>/` and sets the `files` field; until then a case simply shows its source link(s).

**House style:** dates `DD/MM/YYYY` · UPPERCASE surnames · legislation *italicised* (use `<i>…</i>`).

After committing a change to `cases.json`, validate it is still valid JSON (e.g. `python3 -m json.tool data/cases.json`).

---

## Local preview

Because the app `fetch()`es `cases.json`, open it through a server, not `file://`:

```bash
cd case-law-review
python3 -m http.server 8000
# then open http://localhost:8000/
```

## Deploy (GitHub Pages)
Pages is served from `main` (root). Any push that updates `cases.json` publishes within a minute.

## Adding a judgment yourself — one command

WA judgments never arrive automatically (no free source carries them), so the human step is
to download the court's Word export — eCourts for WASC/WASCA, the High Court's own site for
HCA — and drop it in `Cases/` (gitignored: judgment files are copyright and never reach this
repo). Then:

```bash
python pipeline/add_case.py --batch Cases/ --audit --push
```

For every file whose citation is not already in the library it cleans the export
(`clean_word.py`, which refuses a wrong file, a LexisNexis digest or a Jade citator view),
writes the analysis with the same model call the pipeline uses, checks the stored verbatim
text against the source, **fact-checks the entry with a second, independent model call**, and
pushes. An entry that fails its fact-check is not dropped and not published as a call: it stays
in the library as **"Held for review"** (no Action/Awareness badge) with the report in
`Cases/audits/<date>/<id>.md`, until it is corrected and `--recheck <id>` comes back clean.
Re-running is safe — everything already in the library is skipped — so
[`pipeline/watch/`](pipeline/watch/README.md) can fire it from a launchd folder watcher.
Pre-1998 High Court cases need no file at all: `--from-corpus "[1989] HCA 66" --case "S v The Queen"`.

## The automatic pipeline
`pipeline/update.py`, run by [`.github/workflows/case-law-pipeline.yml`](.github/workflows/case-law-pipeline.yml) **three times a day** (03:00 / 12:00 / 18:00 AWST) and on demand via *Actions → Run workflow*. Each run:

1. reads BarNet Jade alert emails from Gmail (IMAP),
2. keeps in-scope matters — **HCA / WASCA / WASC** (binding/WA), **WADC** and the persuasive Code jurisdictions **QCA / TASCCA / NTCCA / NTSC** when an investigation/evidence topic matches — dedupes by `id`,
3. fetches the judgment's verbatim text from the openly licensed [Open Australian Legal Corpus](https://huggingface.co/datasets/isaacus/open-australian-legal-corpus) — High Court and interstate decisions only; it carries no WA judgments, so WASC/WASCA matters go on the watchlist email instead (nothing is scraped from AustLII, Jade or eCourts — their terms forbid it). A case whose text isn't available yet is held in `data/state.json`'s durable **`pending`** queue and retried every run until it resolves or ages out at 60 days,
4. writes the analysis with the Anthropic API (`claude-opus-4-8`, strict JSON, detective house style),
5. saves `data/files/<id>/<id>.md`, prepends the case to `cases.json`, commits, and emails a digest **only if there's something new** (no spam on quiet days).

Secrets (GitHub → Settings → Secrets → Actions): `MAIL_USERNAME`, `MAIL_PASSWORD` (Gmail app password), `ANTHROPIC_API_KEY`. The job needs `permissions: contents: write` (already set) — no PAT.

---

*Not legal advice — verify against the judgment before you rely on it. No case, citation or holding is invented.*
