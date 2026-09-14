# Folder watcher — "drop a judgment in `Cases/`, everything else happens"

`run_add_case.sh` wraps the one command

    python pipeline/add_case.py --batch Cases/ --audit --push

so that a launchd agent can fire it every time `Cases/` changes. It waits for the
folder to go quiet (a download in progress is not a judgment yet), makes a venv on
first use, loads `pipeline/.env`, and logs to `add_case.log` here (gitignored).

Install once (the plist carries this Mac's absolute repo path — edit it if the repo moves):

```bash
cp pipeline/watch/com.case-law-review.add-case.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.case-law-review.add-case.plist
```

Check / remove:

```bash
launchctl print gui/$(id -u)/com.case-law-review.add-case | head
launchctl bootout gui/$(id -u)/com.case-law-review.add-case
```

Try it without launchd first — the script is safe to run by hand:

```bash
zsh pipeline/watch/run_add_case.sh && tail -40 pipeline/watch/add_case.log
```

What you get per file: **ADDED** (written up, fact-check clean, live on push), **HELD**
(written up but the fact-check found problems — it is live with a grey "Held for review"
badge and no Action/Awareness call; the report is in `Cases/audits/<date>/<id>.md`),
**REFUSED** (wrong citation, LexisNexis digest, Jade citator view — fix the file),
**SUPPRESSED** (the court published no judgment), or **SKIPPED** (already in the library).
Nothing is ever dropped silently, and `Cases/` itself never reaches the public repo.

The alternative that needs no install at all is the email route: forward the judgment
to the Gmail address with the subject `INGEST [2026] WASCA 111 Mehrabi v The State of WA`
and the scheduled pipeline ingests it on its next run (HANDOFF §7.3).
