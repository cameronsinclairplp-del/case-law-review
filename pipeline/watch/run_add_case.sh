#!/bin/zsh
# Runs pipeline/add_case.py --batch Cases/ --audit --push, the way a launchd
# WatchPaths agent (see com.case-law-review.add-case.plist) fires it whenever
# something lands in Cases/. Safe to run by hand too.
#
# What it does before the real work:
#   * waits until nothing in Cases/ has been modified for a minute — WatchPaths fires
#     on the first byte of a download, and half a Word file would fail the citation check;
#   * creates ~/.venvs/case-law-review on first use (bs4 + anthropic) so nothing
#     depends on the system python — OUTSIDE OneDrive on purpose: a venv inside the
#     synced folder gets evicted by Files On-Demand and `import anthropic` then
#     blocks for minutes while OneDrive rehydrates it one file at a time (20/09/2026);
#   * loads pipeline/.env (ANTHROPIC_API_KEY) into the environment;
#   * appends everything to pipeline/watch/add_case.log (gitignored).
# add_case.py itself refuses to run twice at once (pipeline/.add_case.lock), skips
# everything already in the library, and holds rather than publishes anything
# whose fact-check found problems.
set -u
ROOT="${0:A:h:h:h}"                       # …/case-law-review
cd "$ROOT" || exit 1
LOG="$ROOT/pipeline/watch/add_case.log"
SETTLE="${SETTLE:-20}"   # seconds between quiet-checks
exec >>"$LOG" 2>&1
echo "=== $(date '+%d/%m/%Y %H:%M:%S') watcher fired"

# settle: wait until nothing in Cases/ has been modified for a minute (max 15 min).
# /usr/bin/find -mmin is BSD find on macOS; a Homebrew bfs/gfind on PATH is not assumed.
waited=0
while true; do
  newest=$(/usr/bin/find "$ROOT/Cases" -maxdepth 1 -type f -mmin -1 2>/dev/null | head -1)
  [[ -z "$newest" ]] && break
  sleep "$SETTLE"; waited=$((waited + SETTLE))
  if (( waited >= 900 )); then echo "still changing after 15 min — giving up this round"; exit 0; fi
done

VENV="${VENV:-$HOME/.venvs/case-law-review}"   # never inside OneDrive (see above)
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "creating $VENV"
  mkdir -p "${VENV:h}" && python3 -m venv "$VENV" && "$VENV/bin/pip" -q install -r "$ROOT/pipeline/requirements.txt" || exit 1
fi
if [[ -f "$ROOT/pipeline/.env" ]]; then
  set -a; source "$ROOT/pipeline/.env"; set +a
fi
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"   # pdftotext (poppler) for PDFs

"$VENV/bin/python" "$ROOT/pipeline/add_case.py" --batch "$ROOT/Cases" --audit --push
echo "=== exit $? $(date '+%H:%M:%S')"
